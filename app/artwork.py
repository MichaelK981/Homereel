"""Poster artwork, sourced from the Radarr/Sonarr instances next door.

Those services have already matched every file in these libraries and pulled
the cover art down to their own disks, so there is nothing to scrape and no
title/year guessing to redo here: they mount the libraries at the same paths
this app does, which makes the join exact. A local `poster.jpg` dropped next
to the media always wins, for anything the *arrs don't manage or get wrong.
"""

import hashlib
import os
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .database import bulk_writer, get_conn, prune_stills, upsert_poster

POSTER_DIR = Path(os.environ.get("POSTER_DIR", "/cache/posters"))

# Cast, for the "who is in this" detail popup. Radarr tracks it natively (its
# own /credit endpoint, added for its Cast UI tab) but Sonarr does not — v4.0.19
# 404s on the same route — so a show's cast comes from TMDB directly instead,
# keyed by the tmdbId Sonarr already reports per series. Free key, no quota
# trouble at this library's size.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY") or ""
TMDB_API = "https://api.themoviedb.org/3"
MAX_CAST = 12

# Checked, in order, in each title's own folder — an override that needs no re-sync.
LOCAL_POSTER_NAMES = ("poster.jpg", "poster.png", "poster.jpeg", "folder.jpg", "cover.jpg")

# The same idea for one episode: an image sitting beside the video and named
# after it. Nothing writes these but a person, so they always win.
LOCAL_STILL_SUFFIXES = (".jpg", ".png", ".jpeg", "-thumb.jpg", "-thumb.png")

# Episode stills come from TVDB's CDN one request at a time, and a first sync of
# a few thousand episodes is otherwise an hour of waiting on round trips.
STILL_WORKERS = 8

HTTP_TIMEOUT = 20

# Leading bytes of the formats a poster can legitimately be. Anything else means
# we were handed a web page, not an image — see _download().
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF")


def _path_map(env):
    """`<arr path>:<homereel path>` from the environment, e.g. `/movies:/animemovies`.

    The join below relies on the *arr and HomeReel seeing a title at the same
    path. That holds when both mount a folder at the same place, but not when
    an *arr mounts it somewhere else — a second Radarr for anime films mounts
    its folder at its own /movies, which HomeReel already uses for the main
    one. The map rewrites the *arr's prefix into HomeReel's before matching.
    """
    raw = (os.environ.get(env) or "").strip()
    if ":" not in raw:
        return None
    arr, ours = (p.strip().rstrip("/") for p in raw.split(":", 1))
    return (arr, ours) if arr.startswith("/") and ours.startswith("/") else None


def _source(name, kind, library, resource, url_env, key_env, map_env=None):
    return {
        "name": name,
        "kind": kind,
        "library": library,
        "resource": resource,          # the /api/v3 collection to list
        "url": (os.environ.get(url_env) or "").rstrip("/"),
        "key": os.environ.get(key_env) or "",
        "path_map": _path_map(map_env) if map_env else None,
    }


def _local_path(source, path):
    """An *arr's path for a title, as HomeReel sees it (see _path_map)."""
    mapping = source.get("path_map")
    if mapping:
        arr, ours = mapping
        if path == arr or path.startswith(arr + "/"):
            return ours + path[len(arr):]
    return path


# Every movie poster is keyed under "movies" whatever library the film is in
# (see poster_ref()), so a second Radarr joins the same catalogue — its paths
# are distinct folders, so the two never collide.
SOURCES = [
    _source("radarr", "movie", "movies", "movie", "RADARR_URL", "RADARR_API_KEY"),
    _source("radarr-anime", "movie", "movies", "movie",
            "RADARR_ANIME_URL", "RADARR_ANIME_API_KEY", "RADARR_ANIME_PATH_MAP"),
    _source("sonarr", "show", "series", "series", "SONARR_URL", "SONARR_API_KEY"),
    _source("sonarr-anime", "show", "anime", "series", "SONARR_ANIME_URL", "SONARR_ANIME_API_KEY"),
]


def _request(url, key=None, binary=False):
    """One GET. `key` is omitted for the public CDN the stills come from."""
    req = urllib.request.Request(url, headers={"X-Api-Key": key} if key else {})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    return (payload, content_type) if binary else json.loads(payload)


def poster_file(kind, library, ref):
    """Stable on-disk name for a title's cached poster."""
    digest = hashlib.sha1(f"{kind}|{library}|{ref}".encode()).hexdigest()
    return POSTER_DIR / f"{digest}.jpg"


def _movie_ref(path):
    """A movie is keyed by its own folder; one sitting loose in the library
    root has none, so it keys on the file itself."""
    folder = os.path.dirname(path)
    return folder if folder.count("/") > 1 else path


def poster_ref(item):
    """(kind, library, ref) for an item row — the key its poster is stored under."""
    if item["type"] == "movie":
        return "movie", "movies", _movie_ref(item["path"])
    return "show", item["library"], item["show_title"]


def _rating(record):
    """(value, votes) from an *arr record — Radarr nests ratings by source,
    Sonarr returns a single flat IMDb rating."""
    ratings = record.get("ratings") or {}
    if "imdb" in ratings or "tmdb" in ratings:
        best = ratings.get("imdb") or ratings.get("tmdb") or {}
        return best.get("value"), best.get("votes")
    return ratings.get("value"), ratings.get("votes")


def _radarr_cast(source, movie_id):
    """Top-billed cast for one film, from Radarr's own /credit endpoint."""
    try:
        records = _request(f"{source['url']}/api/v3/credit?movieId={movie_id}", source["key"])
    except (urllib.error.URLError, OSError, ValueError):
        return []
    cast = sorted(
        (r for r in records if r.get("type") == "cast"),
        key=lambda r: r.get("order") if r.get("order") is not None else 999,
    )
    out = []
    for r in cast[:MAX_CAST]:
        image = next(
            (i.get("remoteUrl") for i in r.get("images", []) if i.get("coverType") == "headshot"),
            None,
        )
        out.append({"name": r.get("personName"), "character": r.get("character"), "image": image})
    return out


def _tmdb_tv_cast(tmdb_id):
    """Top-billed cast for one show, straight from TMDB."""
    if not TMDB_API_KEY or not tmdb_id:
        return []
    try:
        data = _request(f"{TMDB_API}/tv/{tmdb_id}/credits?api_key={TMDB_API_KEY}")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    cast = sorted(data.get("cast") or [], key=lambda r: r.get("order", 999))
    out = []
    for r in cast[:MAX_CAST]:
        profile = r.get("profile_path")
        image = f"https://image.tmdb.org/t/p/w185{profile}" if profile else None
        out.append({"name": r.get("name"), "character": r.get("character"), "image": image})
    return out


def _local_poster(folder):
    for name in LOCAL_POSTER_NAMES:
        candidate = Path(folder) / name
        if candidate.is_file():
            return candidate
    return None


def still_ref(item):
    """(kind, library, ref) for one episode's banner.

    Keyed by the file path rather than by numbering: a show can be re-numbered
    or re-imported under a different scheme, and a path that no longer exists is
    a row we can safely prune.
    """
    return "episode", item["library"], item["path"]


def _local_still(video_path):
    """A person's own banner for one episode, sitting beside the video."""
    stem = os.path.splitext(video_path)[0]
    for suffix in LOCAL_STILL_SUFFIXES:
        candidate = Path(stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def _save_image(data, content_type, dest):
    """Write an image to the cache, refusing anything that is not one.

    Belt and braces: trust neither the status code nor the file extension — the
    *arrs' MediaCover routes answer a missing session with a 200 and a login
    page, which saves perfectly happily as a .jpg.
    """
    if not data.startswith(IMAGE_MAGIC) or not content_type.startswith("image/"):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def _download(source, record, dest):
    """Pull the poster from the *arr's own cache, falling back to TMDb's CDN.

    Note the URL: the `/MediaCover/...` path the API reports on each image is
    served behind the web UI's session auth and **302s to the login page**, so
    fetching it yields an HTML document that happily saves as a .jpg. The
    `/api/v3/mediacover/...` route serves the identical file for an API key.
    The -500 variant is the sensible size for a poster wall.
    """
    poster = next((i for i in record.get("images", []) if i.get("coverType") == "poster"), None)
    if poster is None:
        return None

    name = os.path.basename(urlparse(poster.get("url") or "").path) or "poster.jpg"
    base = f"{source['url']}/api/v3/mediacover/{record['id']}"
    attempts = [f"{base}/{name.replace('poster.jpg', 'poster-500.jpg')}", f"{base}/{name}"]
    if poster.get("remoteUrl"):
        attempts.append(poster["remoteUrl"])

    for attempt in attempts:
        try:
            data, content_type = _request(attempt, source["key"], binary=True)
        except (urllib.error.URLError, OSError):
            continue
        if not _save_image(data, content_type, dest):
            continue
        return attempt
    return None


def _show_folder(sample_path):
    """The show's own folder, from any file under it: /<library>/<show>/..."""
    parts = Path(sample_path).parts        # ('/', '<library>', '<show folder>', ...)
    return str(Path(*parts[:3])) if len(parts) > 2 else os.path.dirname(sample_path)


def _library_titles(conn):
    """Every distinct title in the index, as (kind, library, ref, folder).

    `ref` is what the API looks a poster up by — the folder for a movie, the
    show title for an episode (that is what the UI has in hand). `folder` is
    where a local override would live.
    """
    titles = []

    for (path,) in conn.execute("SELECT DISTINCT path FROM items WHERE type='movie'"):
        titles.append(("movie", "movies", _movie_ref(path), os.path.dirname(path)))

    rows = conn.execute(
        """SELECT library, show_title, MIN(path) FROM items
           WHERE type='episode' GROUP BY library, show_title"""
    )
    for library, show_title, sample_path in rows:
        titles.append(("show", library, show_title, _show_folder(sample_path)))

    return titles


def sync_artwork(force=False):
    """Match every title against the *arrs and cache its poster locally."""
    POSTER_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    titles = _library_titles(conn)
    conn.close()

    catalogue = {}       # (library, folder) -> (source, arr record)
    unreachable = []
    for source in SOURCES:
        if not source["url"] or not source["key"]:
            continue
        try:
            records = _request(f"{source['url']}/api/v3/{source['resource']}", source["key"])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            unreachable.append(f"{source['name']}: {exc}")
            continue
        for record in records:
            path = (record.get("path") or "").rstrip("/")
            if path:
                catalogue[(source["library"], _local_path(source, path))] = (source, record)

    stats = {"matched": 0, "local": 0, "downloaded": 0, "missing": 0, "unreachable": unreachable}
    resolved = []

    for kind, library, ref, folder in titles:
        dest = poster_file(kind, library, ref)

        local = _local_poster(folder)
        if local is not None:
            resolved.append({
                "kind": kind, "library": library, "ref": ref, "source": "local",
                "file": str(local), "tmdb_id": None, "tvdb_id": None, "imdb_id": None,
                "title": None, "overview": None, "runtime": None, "genres": None,
                "rating": None, "votes": None,
                "collection": None, "collection_tmdb_id": None, "cast": None,
            })
            stats["local"] += 1
            continue

        entry = catalogue.get((library, folder))
        if entry is None:
            stats["missing"] += 1
            continue
        source, record = entry
        rating, votes = _rating(record)
        # Radarr reports the TMDB collection a film belongs to; Sonarr has no
        # equivalent, so shows simply carry None here.
        collection = record.get("collection") or {}

        if force or not dest.is_file():
            if _download(source, record, dest):
                stats["downloaded"] += 1
            elif not dest.is_file():
                stats["missing"] += 1
                continue

        # Cast comes from wherever actually has it: Radarr's own /credit route
        # for a film, TMDB directly for a show (see _tmdb_tv_cast).
        if kind == "movie":
            cast = _radarr_cast(source, record["id"])
        else:
            cast = _tmdb_tv_cast(record.get("tmdbId"))

        resolved.append({
            "kind": kind, "library": library, "ref": ref, "source": source["name"],
            "file": str(dest),
            "title": None,          # a movie/show poster row: items.title is the name
            "tmdb_id": record.get("tmdbId"),
            "tvdb_id": record.get("tvdbId"),
            "imdb_id": record.get("imdbId"),
            "overview": record.get("overview"),
            "runtime": record.get("runtime"),
            "genres": json.dumps(record.get("genres") or []),
            "rating": rating,
            "votes": votes,
            "collection": collection.get("title"),
            "collection_tmdb_id": collection.get("tmdbId"),
            "cast": json.dumps(cast),
        })
        stats["matched"] += 1

    # One transaction for the lot, same reasoning as the library scan.
    with bulk_writer() as conn:
        for row in resolved:
            upsert_poster(row, conn)

    stats["titles"] = len(titles)

    # Posters are already written and committed above, so a stills pass that
    # cannot reach Sonarr leaves the poster wall intact and only reports itself
    # as failed — an episode banner is not worth losing a sync over.
    try:
        stats["stills"] = sync_stills(catalogue, force=force)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        stats["stills"] = {"error": str(exc)}
    return stats


# ---------- Episode stills ----------

def _episode_records(source, series_id):
    """Sonarr's episode list for one series, carrying what we need on it.

    includeImages is what puts the screenshot on each episode; includeEpisodeFile
    brings the imported file's path along, which is what makes the join back to
    our own index exact instead of a guess at numbering.
    """
    url = (f"{source['url']}/api/v3/episode?seriesId={series_id}"
           "&includeImages=true&includeEpisodeFile=true")
    return _request(url, source["key"])


def _still_url(record):
    """Where an episode's screenshot lives, or None if TVDB has none.

    Unlike a poster there is no local copy to ask the *arr for: Sonarr does not
    cache episode images, so the record only ever carries the remote URL.
    """
    image = next((i for i in record.get("images") or []
                  if i.get("coverType") == "screenshot"), None)
    return (image or {}).get("remoteUrl") or None


def _still_row(library, item, source, file, record):
    """A posters row for one episode. Overview and runtime come free with the
    same request, and the columns already exist, so they ride along."""
    record = record or {}
    return {
        "kind": "episode", "library": library, "ref": item["path"],
        "source": source, "file": file,
        "tmdb_id": None, "tvdb_id": record.get("tvdbId"), "imdb_id": None,
        # Sonarr's name for the episode. The scanner only has the release
        # filename to go on, which is how grids end up reading
        # "Show S01E01 1080p BluRay x265-RARBG".
        "title": (record.get("title") or "").strip() or None,
        "overview": record.get("overview"), "runtime": record.get("runtime"),
        "genres": None, "rating": None, "votes": None,
        "collection": None, "collection_tmdb_id": None, "cast": None,
    }


def _download_still(url, dest):
    try:
        data, content_type = _request(url, binary=True)
    except (urllib.error.URLError, OSError):
        return False
    return _save_image(data, content_type, dest)


def sync_stills(catalogue, force=False):
    """Cache one banner per episode across every show library.

    `catalogue` is the same (library, folder) -> (source, record) map the poster
    pass built, so the series is already identified; this only has to ask each
    matched series for its episodes. Inside a series an episode is matched on
    its file path, falling back to season/episode numbering for anything Sonarr
    has monitored but not filed where we scanned it.
    """
    conn = get_conn()
    try:
        episodes = conn.execute(
            """SELECT id, library, show_title, season, episode, path FROM items
               WHERE type='episode' ORDER BY library, show_title, season, episode"""
        ).fetchall()
    finally:
        conn.close()

    shows = {}
    for row in episodes:
        shows.setdefault((row["library"], row["show_title"]), []).append(row)

    stats = {"episodes": len(episodes), "local": 0, "cached": 0,
             "downloaded": 0, "missing": 0, "shows_unmatched": 0}
    resolved, pending = [], []

    for (library, show_title), rows in shows.items():
        entry = catalogue.get((library, _show_folder(rows[0]["path"])))
        if entry is None:
            stats["shows_unmatched"] += 1
            continue
        source, series = entry
        try:
            records = _episode_records(source, series["id"])
        except (urllib.error.URLError, OSError, ValueError):
            stats["shows_unmatched"] += 1
            continue

        by_path, by_number = {}, {}
        for record in records:
            by_number[(record.get("seasonNumber"), record.get("episodeNumber"))] = record
            path = ((record.get("episodeFile") or {}).get("path") or "").strip()
            if path:
                by_path[path] = record

        for row in rows:
            record = by_path.get(row["path"]) or by_number.get((row["season"], row["episode"]))
            dest = poster_file("episode", library, row["path"])

            local = _local_still(row["path"])
            if local is not None:
                resolved.append(_still_row(library, row, "local", str(local), record))
                stats["local"] += 1
                continue
            if dest.is_file() and not force:
                resolved.append(_still_row(library, row, source["name"], str(dest), record))
                stats["cached"] += 1
                continue
            url = _still_url(record) if record else None
            if url is None:
                stats["missing"] += 1
                continue
            pending.append((url, dest, library, row, source["name"], record))

    if pending:
        with ThreadPoolExecutor(max_workers=STILL_WORKERS) as pool:
            outcomes = list(pool.map(lambda job: _download_still(job[0], job[1]), pending))
        for (_, dest, library, row, source_name, record), ok in zip(pending, outcomes):
            if ok:
                resolved.append(_still_row(library, row, source_name, str(dest), record))
                stats["downloaded"] += 1
            else:
                stats["missing"] += 1

    with bulk_writer() as conn:
        for row in resolved:
            upsert_poster(row, conn)
        stale = prune_stills(conn)

    # A replaced release means a new path, so the old row's image is now
    # unreachable. Only ever unlink what we put in the cache ourselves.
    for path in stale:
        try:
            candidate = Path(path)
            if candidate.parent == POSTER_DIR:
                candidate.unlink(missing_ok=True)
        except OSError:
            pass
    stats["pruned"] = len(stale)
    return stats
