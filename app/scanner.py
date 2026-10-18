import os
import re
import threading
from pathlib import Path

from .database import (bulk_writer, clear_missing, delete_paths, library_index,
                       list_libraries, upsert_item)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}

MOVIE_YEAR_RE = re.compile(r"^(.*?)[\.\s_\-\(]+(\d{4})\)?")
EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")

# Paths live on the `libraries` rows now (seeded from these same env vars on
# first run), so the scanner walks whatever Settings has mapped.

# Both scans write through one connection, and sqlite gives that connection's
# transaction the whole file. Two at once (the watcher tripping over a manual
# rescan) would spend 30s blocked and then raise "database is locked", so they
# queue here instead.
_SCAN_LOCK = threading.Lock()


def _clean_title(raw: str) -> str:
    raw = raw.replace(".", " ").replace("_", " ").strip(" -")
    return re.sub(r"\s+", " ", raw).strip()


def iter_video_files(base_path: Path):
    """Yield (path, stat) for every video file under base_path.

    scandir rather than rglob: rglob stats each entry to decide whether to
    recurse into it and we then stat the file a second time ourselves, so a
    walk of the pool costs roughly double the metadata reads it needs to. A
    DirEntry answers both questions from the one readdir.
    """
    stack = [base_path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue  # unreadable or vanished — one bad folder is not a failed scan
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif os.path.splitext(entry.name)[1].lower() in VIDEO_EXTS and entry.is_file():
                    yield Path(entry.path), entry.stat()
            except OSError:
                continue  # a file moved out from under the walk; the *arrs do that


def _movie_item(f: Path, stat, library_name: str) -> dict:
    """One film — a flat library, so the filename carries title and year."""
    name = f.stem
    m = MOVIE_YEAR_RE.match(name)
    return {
        "type": "movie",
        "library": library_name,
        "title": _clean_title(m.group(1)) if m else _clean_title(name),
        "year": m.group(2) if m else None,
        "show_title": None,
        "season": None,
        "episode": None,
        "path": str(f),
        "size_bytes": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


def _episode_item(f: Path, stat, library_name: str, base_path: Path) -> dict:
    """One episode of a Show/Season/Episode.ext library — the top-level folder
    under base_path is the show, the filename carries SxxExx."""
    try:
        show_title = _clean_title(f.relative_to(base_path).parts[0])
    except (ValueError, IndexError):
        show_title = "Unknown Show"

    m = EPISODE_RE.search(f.name)
    return {
        "type": "episode",
        "library": library_name,
        "title": _clean_title(f.stem),
        "year": None,
        "show_title": show_title,
        "season": int(m.group(1)) if m else None,
        "episode": int(m.group(2)) if m else None,
        "path": str(f),
        "size_bytes": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


def build_item(library, base_path: Path, f: Path, stat) -> dict:
    """The items row for one file, parsed the way its library's layout implies.
    'show' covers both Series and Anime — same convention, different path."""
    if library["kind"] == "movie":
        return _movie_item(f, stat, library["name"])
    return _episode_item(f, stat, library["name"], base_path)


def scan_movies(base_path: Path, library_name: str, conn=None):
    """Scan a flat film library — one folder (or loose file) per title."""
    if not base_path.exists():
        return []
    found = []
    for f, stat in iter_video_files(base_path):
        upsert_item(_movie_item(f, stat, library_name), conn)
        found.append(str(f))
    return found


def scan_show_library(base_path: Path, library_name: str, conn=None):
    """Scan a folder of Show/Season/Episode.ext structure. Used for both
    Series and Anime — same layout convention, different top-level path."""
    if not base_path.exists():
        return []
    found = []
    for f, stat in iter_video_files(base_path):
        upsert_item(_episode_item(f, stat, library_name, base_path), conn)
        found.append(str(f))
    return found


def run_full_scan(only=None):
    """Index every mapped library, or just one when `only` names it.

    Every file is re-parsed and re-written whether or not it changed, which is
    what you want after a parser change (or a first run) and is wasted work the
    rest of the time — run_partial_scan() is the routine one.

    A partial scan must not call clear_missing() with only its own paths — that
    would delete every other library's rows as "missing".
    """
    found = {}
    # Whole scan is one transaction — see bulk_writer() for why.
    with _SCAN_LOCK, bulk_writer() as conn:
        libraries = [row for row in list_libraries(conn)
                     if only is None or row["name"] == only]
        for library in libraries:
            base = Path(library["path"])
            if library["kind"] == "movie":
                paths = scan_movies(base, library["name"], conn)
            else:
                paths = scan_show_library(base, library["name"], conn)
            found[library["name"]] = paths
        if only is None:
            clear_missing({path for paths in found.values() for path in paths}, conn)
    return {name: len(paths) for name, paths in found.items()}


def scan_library_changes(library, conn):
    """Reconcile one library's folder against what the index already holds, and
    write only the difference: files that appeared, files whose bytes changed,
    and rows whose file is gone.

    A file is "unchanged" when its path, mtime and size all match the row we
    already have, which is the cheap test the walk can make without opening
    anything. That misses an edit that preserves both — not a thing that
    happens to a finished media file — and it deliberately does *not* re-parse
    titles, so a rename or a parser change still needs the full rescan.
    """
    base = Path(library["path"])
    known = library_index(library["name"], conn)
    result = {"status": "ok", "added": 0, "updated": 0, "removed": 0,
              "unchanged": 0, "total": len(known)}

    # A mount that has gone away looks exactly like "every file was deleted".
    # Refuse to act on that: an unmounted volume must never empty the index.
    if not base.is_dir():
        return {**result, "status": "unavailable", "unchanged": len(known)}

    seen = set()
    for f, stat in iter_video_files(base):
        path = str(f)
        seen.add(path)
        # NULL mtime on rows written before that column existed never matches,
        # so those get rewritten once on the first partial scan and settle.
        if known.get(path) == (int(stat.st_mtime), stat.st_size):
            result["unchanged"] += 1
            continue
        upsert_item(build_item(library, base, f, stat), conn)
        result["added" if path not in known else "updated"] += 1

    if known and not seen:
        # The folder is there but empty while the index is not. Emptying a
        # library is legal, but a bind mount whose source is missing looks
        # identical, so make the destructive reading the one you ask for
        # explicitly with a full rescan.
        return {**result, "status": "empty", "unchanged": len(known)}

    missing = [path for path in known if path not in seen]
    delete_paths(missing, conn)
    result["removed"] = len(missing)
    result["total"] = len(seen)
    return result


def run_partial_scan(only=None):
    """Walk the libraries but touch the database only where something changed.

    This is the scan to run on a schedule. The walk itself is unavoidable —
    there is no filesystem event to subscribe to across a bind mount, and the
    *arr apps import files behind the app's back — but everything after it is
    proportional to the change, not to the library: a run that finds nothing
    new writes nothing at all, where a full scan rewrites every row every time.

    Returns {library: {status, added, updated, removed, unchanged, total}}.
    Statuses other than "ok" mean the library was left alone: "unavailable"
    (its path is not mounted) or "empty" (see scan_library_changes).
    """
    results = {}
    with _SCAN_LOCK, bulk_writer() as conn:
        for library in list_libraries(conn):
            if only is None or library["name"] == only:
                results[library["name"]] = scan_library_changes(library, conn)
    return results


def summarize(results) -> dict:
    """Fold a run_partial_scan() result into one set of totals."""
    totals = {key: sum(r[key] for r in results.values())
              for key in ("added", "updated", "removed", "unchanged")}
    totals["changed"] = totals["added"] + totals["updated"] + totals["removed"]
    totals["skipped"] = sorted(name for name, r in results.items() if r["status"] != "ok")
    return totals
