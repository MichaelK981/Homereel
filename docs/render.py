"""Renders docs/guide.txt for publishing. Not part of the running app.

guide.txt covers getting HomeReel running and nothing else — how the app behaves
once it is up belongs in the README. It stays plain text because that is what
survives: readable in the repo and in a terminal on a box where the app will not
start.

This module used to live at app/docs.py and back a Settings → Documentation tab.
That tab is gone (removed in the 1.24.5 rollback and not brought back), so the
renderer moved out of the `app` package with it — nothing here is imported at
runtime, and no route serves it. Its only caller is the publishing script,
/root/homereel-docs-publish.py, which turns the guide into the artifact page.
Keeping it beside the guide rather than inside the app means the shipped image
carries no module that would fail the moment something imported it.

An install can add its own final section by dropping a site-notes.txt beside the
database, which is where anything site-specific (addresses, paths, deploy steps)
belongs — the published guide stays generic.
"""

import html
import os
import re
from pathlib import Path

# Read the same way app/database.py reads it, rather than importing the app: the
# renderer is a build-time tool and must not need the package to be importable.
DB_PATH = Path(os.environ.get("DB_PATH", "/cache/library.db"))

GUIDE_PATH = Path(__file__).resolve().parent / "guide.txt"

# Beside the index, so it lives on the same volume that already survives a
# rebuild and is never baked into an image.
SITE_NOTES_PATH = DB_PATH.parent / "site-notes.txt"

RULE = re.compile(r"^-{10,}$")
BANNER = re.compile(r"^={10,}$")
UNDERLINE = re.compile(r"^-{3,}$")
SECTION = re.compile(r"^(\d{1,2})\.\s+(.+)$")
SUBSECTION = re.compile(r"^(\d{1,2}\.\d)\s+(.+)$")
BULLET = re.compile(r"^ {2}\* (.*)$")
ORDERED = re.compile(r"^ {2}(\d)\. (.*)$")
COLUMNS = re.compile(r"\S {3,}\S")

# The plain text shouts its headings. Sentence case reads better on screen, but
# only if the words that are not ordinary words survive it.
KEPT_WORDS = ("HomeReel", "Radarr", "Sonarr", "TVDB", "TMDB", "IMDb", "NVIDIA", "REST",
              "API", "HLS", "GPU", "CPU", "URL", "SQLite", "Docker", "Plex", "Jellyfin",
              "LXC", "ffmpeg", "NVENC", "TLS", "LAN")

_cache = {}


def _display_title(shouted):
    title = shouted[:1].upper() + shouted[1:].lower()
    for word in KEPT_WORDS:
        title = re.sub(rf"\b{re.escape(word)}\b", word, title, flags=re.IGNORECASE)
    return title


def _render(text):
    """Plain text in, (html, sections) out.

    The shape of the source is the shape of the output: a heading is fenced by
    full-width rules, a sub-heading is underlined, column-aligned or deeply
    indented material is preformatted, and everything else is prose.
    """
    lines = text.split("\n")
    out, sections = [], []
    para, pre, items, list_tag = [], [], [], None
    # The file opens with a banner and its own contents list. Both are there for
    # someone reading it in a terminal; on screen the panel header and the rail
    # already do that job, so everything before the first numbered section is
    # dropped — as is any other unnumbered section.
    skipping = True
    i, n = 0, len(lines)

    def flush_para():
        if para:
            out.append("<p>" + " ".join(html.escape(l) for l in para) + "</p>")
            para.clear()

    def flush_pre():
        while pre and not pre[-1].strip():
            pre.pop()
        if pre:
            # Dedent as a unit: per-line stripping would knock the aligned
            # tables (the environment and API references) out of column.
            margin = min((len(l) - len(l.lstrip()) for l in pre if l.strip()), default=0)
            body = "\n".join(html.escape(l[margin:]) for l in pre)
            out.append(f'<div class="doc-pre"><pre>{body}</pre></div>')
            pre.clear()

    def flush_list():
        nonlocal list_tag
        if items:
            # Ordered items carry the number they had in the source. A code
            # block between two steps closes the list, and without this the
            # step after it would start counting from one again.
            body = "".join(
                f"<li>{text}</li>" if value is None else f'<li value="{value}">{text}</li>'
                for value, text in items
            )
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
            list_tag = None

    def flush_all():
        flush_para()
        flush_pre()
        flush_list()

    while i < n:
        line = lines[i]

        if RULE.match(line) and i + 2 < n and RULE.match(lines[i + 2]):
            flush_all()
            heading = lines[i + 1].strip()
            match = SECTION.match(heading)
            skipping = match is None
            if skipping:
                i += 3
                continue
            number = match.group(1)
            title = _display_title(match.group(2))
            slug = "doc-" + (number or re.sub(r"[^a-z0-9]+", "-", title.lower()))
            sections.append({"number": number, "title": title, "slug": slug})
            out.append(
                f'<h2 id="{slug}"><span class="doc-no">{number}</span>'
                f"<span>{html.escape(title)}</span></h2>"
            )
            i += 3
            continue

        if BANNER.match(line) or skipping:
            i += 1
            continue

        if (i + 1 < n and UNDERLINE.match(lines[i + 1].strip())
                and line.strip() and not line.startswith(" ")):
            flush_all()
            heading = line.strip()
            match = SUBSECTION.match(heading)
            if match:
                out.append(f'<h3><span class="doc-sub-no">{match.group(1)}</span>'
                           f"{html.escape(match.group(2))}</h3>")
            else:
                out.append(f"<h3>{html.escape(heading)}</h3>")
            i += 2
            continue

        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0

        if not stripped:
            # A blank line inside a block is spacing, not the end of it.
            if pre and i + 1 < n and lines[i + 1].startswith("  "):
                pre.append("")
            else:
                flush_all()
            i += 1
            continue

        bullet, ordered = BULLET.match(line), ORDERED.match(line)
        if bullet or ordered:
            flush_para()
            flush_pre()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            if bullet:
                items.append((None, html.escape(bullet.group(1))))
            else:
                items.append((ordered.group(1), html.escape(ordered.group(2))))
            i += 1
            continue

        if items and indent >= 4:
            value, text = items[-1]
            items[-1] = (value, text + " " + html.escape(stripped))
            i += 1
            continue

        # A trailing colon means prose introducing a block, not the block.
        tree_head = (indent == 2 and not stripped.endswith(":")
                     and i + 1 < n and lines[i + 1].startswith("    "))
        if indent >= 4 or tree_head or (indent >= 2 and COLUMNS.search(line)):
            flush_para()
            flush_list()
            pre.append(line)
            i += 1
            continue

        if indent >= 2:
            flush_pre()
            flush_list()
            para.append(stripped)
            i += 1
            continue

        # An unindented line directly above indented prose is labelling it.
        if (not pre and not items and not para and i + 1 < n
                and lines[i + 1].startswith("  ") and not lines[i + 1].startswith("    ")
                and not COLUMNS.search(lines[i + 1])):
            flush_all()
            out.append(f"<h4>{html.escape(stripped)}</h4>")
            i += 1
            continue

        flush_pre()
        flush_list()
        para.append(stripped)
        i += 1

    flush_all()
    return "\n".join(out), sections


def source_text(site_notes=True):
    """The guide as plain text — what the published .txt copy is made of."""
    text = GUIDE_PATH.read_text(encoding="utf-8")
    if site_notes and SITE_NOTES_PATH.is_file():
        try:
            text = text.rstrip("\n") + "\n\n\n" + SITE_NOTES_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
    return text


def guide(site_notes=True):
    """Rendered guide, re-read only when a source file has actually changed."""
    stamps = []
    for path in (GUIDE_PATH, SITE_NOTES_PATH):
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(None)
    key = (site_notes, tuple(stamps))
    if _cache.get("key") != key:
        text = source_text(site_notes)
        rendered, sections = _render(text)
        _cache.clear()
        _cache.update(key=key, html=rendered, sections=sections, bytes=len(text.encode()),
                      lines=text.count("\n") + 1)
    return {
        "html": _cache["html"],
        "sections": _cache["sections"],
        "bytes": _cache["bytes"],
        "lines": _cache["lines"],
        "site_notes": site_notes and SITE_NOTES_PATH.is_file(),
    }
