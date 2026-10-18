import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# No dedicated /config volume in this deployment — persist the DB under the
# cache mount instead so it survives container restarts.
DB_PATH = Path(os.environ.get("DB_PATH", "/cache/library.db"))

# Libraries are rows now, not a constant — Settings can map new ones. These
# three are seeded on first run from the paths the scanner has always used, and
# are marked builtin so they cannot be removed out from under the mounts.
BUILTIN_LIBRARIES = (
    # name,     label,     env var,        default path, kind
    ("movies", "Movies", "MOVIES_PATH", "/movies", "movie"),
    ("series", "Series", "SERIES_PATH", "/series", "show"),
    ("anime", "Anime", "ANIME_PATH", "/anime", "show"),
)

# A movie library is a flat pile of films; a show library is Show/Season/Episode.
LIBRARY_KINDS = ("movie", "show")

# Slug rules: it lands in items.library, in grant rows and in URLs.
LIBRARY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,          -- 'movie' or 'episode'
            library TEXT NOT NULL DEFAULT 'movies',  -- 'movies', 'series', or 'anime'
            title TEXT NOT NULL,
            year TEXT,
            show_title TEXT,
            season INTEGER,
            episode INTEGER,
            path TEXT UNIQUE NOT NULL,
            size_bytes INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posters (
            kind TEXT NOT NULL,          -- 'movie' or 'show'
            library TEXT NOT NULL,       -- 'movies', 'series', or 'anime'
            ref TEXT NOT NULL,           -- movie: its folder; show: the show title
            source TEXT,                 -- 'radarr', 'sonarr', 'sonarr-anime' or 'local'
            file TEXT NOT NULL,          -- absolute path of the image on disk
            tmdb_id INTEGER,
            tvdb_id INTEGER,
            imdb_id TEXT,
            overview TEXT,
            runtime INTEGER,
            genres TEXT,                 -- JSON array
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (kind, library, ref)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,     -- pbkdf2_sha256$<iters>$<salt>$<b64>
            role TEXT NOT NULL DEFAULT 'viewer',   -- 'admin' or 'viewer'
            created_at TEXT DEFAULT (datetime('now')),
            password_changed_at TEXT DEFAULT (datetime('now')),
            -- Second factor. The confirmed secret is separate from the one a
            -- setup in progress is offering, so starting a new enrolment can
            -- never strand an account that already had a working one.
            totp_secret TEXT,
            totp_pending_secret TEXT,
            totp_confirmed_at TEXT,
            -- The last counter this account spent, so a code cannot be used
            -- twice inside the step it is live for.
            totp_last_counter INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)")
    # A TV's "Quick Connect": the box shows a QR/code, an already-signed-in
    # phone scans it and approves, and the box picks up a real session from
    # here on its next poll. `session_token` sits here in the clear rather
    # than hashed like everywhere else in this file — it is the one thing
    # this table exists to hand back to an unauthenticated caller, so there is
    # nothing to protect it *from* here that expiry and single-use pickup
    # (see poll_quick_connect()) do not already cover.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quick_connect (
            token TEXT PRIMARY KEY,       -- hash_token() of the value in the QR
            code TEXT NOT NULL,           -- short, shown beside the QR for a sanity check
            status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' or 'approved'
            session_token TEXT,           -- set once approved
            username TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quick_connect_expiry ON quick_connect(expires_at)")
    # One-time codes for the second factor, for when the phone with the
    # authenticator on it is lost, broken or elsewhere. Stored as a SHA-256 of
    # the code for the same reason session tokens are: the value is generated
    # here and has enough entropy that stretching would buy nothing.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_codes (
            username TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            used_at TEXT,
            PRIMARY KEY (username, code_hash),
            FOREIGN KEY (username) REFERENCES users(username)
        )
        """
    )
    # A login that has passed the password but not yet the second factor. It is
    # deliberately not a row in sessions: everything downstream treats a session
    # as fully signed in, and a half-authenticated one in the same table is the
    # kind of thing that gets mistaken for the real article later.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_logins (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            purpose TEXT NOT NULL,          -- 'totp' (enrolled) or 'enroll'
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
        """
    )
    # One row per episode now lives in posters, and the episode grid joins the
    # two by path — without this that join is a scan of the whole table per show.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posters_ref ON posters(kind, ref)")
    # Every partial scan pulls one library's rows to diff the folder against.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_library ON items(library)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            name TEXT PRIMARY KEY,           -- slug: items.library, grants, URLs
            label TEXT NOT NULL,             -- what the UI shows
            path TEXT NOT NULL,              -- as seen *inside* the container
            kind TEXT NOT NULL,              -- 'movie' or 'show'
            builtin INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    for position, (name, label, env_var, default, kind) in enumerate(BUILTIN_LIBRARIES):
        conn.execute(
            """INSERT INTO libraries (name, label, path, kind, builtin, position)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(name) DO NOTHING""",
            (name, label, os.environ.get(env_var, default), kind, position),
        )
    # Per-user library access: a row means "this user may see this library",
    # and no row means no access. That default is why the table's *first*
    # creation has to backfill — otherwise every account that predates this
    # feature would come back from the upgrade with an empty library.
    table_is_new = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_libraries'"
    ).fetchone() is None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_libraries (
            username TEXT NOT NULL,
            library TEXT NOT NULL,           -- libraries.name
            granted_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (username, library),
            FOREIGN KEY (username) REFERENCES users(username)
        )
        """
    )
    if table_is_new:
        conn.executemany(
            "INSERT OR IGNORE INTO user_libraries (username, library) VALUES (?, ?)",
            [
                (row["username"], library)
                for row in conn.execute("SELECT username FROM users").fetchall()
                for library in library_names(conn)
            ],
        )

    # Profiles: a household picks one after signing in, Netflix-style. The
    # account itself still owns auth, 2FA and admin rights — a profile is only
    # a viewing identity underneath it, which is why my_list/plays below key on
    # a profile's id rather than gaining a schema of their own; see the note
    # above toggle_list_entry().
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES users(username)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_username ON profiles(username)")
    # Every account needs at least one profile to sign into. One predating
    # profiles gets one named after itself, so a single-viewer household never
    # has to visit "Manage profiles" before it can watch anything.
    conn.executemany(
        "INSERT INTO profiles (username, name) VALUES (?, ?)",
        [
            (row["username"], row["username"])
            for row in conn.execute(
                "SELECT username FROM users WHERE username NOT IN "
                "(SELECT DISTINCT username FROM profiles)"
            ).fetchall()
        ],
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS my_list (
            -- Holds a profile's id (as text), not the account's username — see
            -- the note above toggle_list_entry().
            username TEXT NOT NULL,
            kind TEXT NOT NULL,          -- 'movie' or 'show'
            library TEXT NOT NULL,       -- 'movies', 'series', or 'anime'
            -- Same identity split as watched state: a movie is one item id, a
            -- show has no single row of its own so it is keyed by title
            -- instead. Stored as TEXT either way so one column covers both.
            ref TEXT NOT NULL,
            added_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (username, kind, library, ref)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Holds a profile's id (as text) — see the note above toggle_list_entry().
            username TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            played_at TEXT DEFAULT (datetime('now')),
            -- Furthest point reached in this play and the runtime it was
            -- measured against, both in seconds; progress is their ratio, kept
            -- as a column so "did this finish?" stays a plain filter.
            position_seconds REAL NOT NULL DEFAULT 0,
            duration_seconds REAL,
            progress REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user ON plays(username, item_id, played_at)")

    # One-time remap for anyone upgrading from before profiles existed: every
    # my_list/plays row was keyed by the account's own username, so each one
    # becomes that account's (now auto-created) default profile's key. Gated
    # on a settings flag rather than re-derived from the data, because a
    # profile can be renamed to look like a username and an account's own
    # username is otherwise indistinguishable from a profile id here.
    if conn.execute("SELECT 1 FROM settings WHERE key = 'profiles_migrated'").fetchone() is None:
        for table in ("my_list", "plays"):
            for row in conn.execute(f"SELECT DISTINCT username FROM {table}").fetchall():
                key = row["username"]
                profile = conn.execute(
                    "SELECT id FROM profiles WHERE username = ? ORDER BY id LIMIT 1", (key,)
                ).fetchone()
                if profile is None:
                    continue          # already a profile id, or an orphaned row
                conn.execute(
                    f"UPDATE {table} SET username = ? WHERE username = ?",
                    (str(profile["id"]), key),
                )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('profiles_migrated', '1')"
        )
    # Lightweight migration for anyone upgrading from the pre-library-column version
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "library" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN library TEXT NOT NULL DEFAULT 'movies'")
    # Runtime in seconds, ffprobed from the file on first playback and cached
    # here. The player's own idea of the duration cannot be trusted in transcode
    # mode — hls.js reports the length of the playlist ffmpeg has written so
    # far, which early in a film is a few minutes and would read as "finished".
    if "duration_seconds" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN duration_seconds REAL")

    # A profile's own visual theme (1.89) — an id from static/themes.js, or
    # NULL to follow the household default in settings('theme').
    profile_cols = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
    if "theme" not in profile_cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN theme TEXT")

    play_cols = {row["name"] for row in conn.execute("PRAGMA table_info(plays)")}
    if "progress" not in play_cols:
        conn.execute("ALTER TABLE plays ADD COLUMN position_seconds REAL NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE plays ADD COLUMN duration_seconds REAL")
        conn.execute("ALTER TABLE plays ADD COLUMN progress REAL NOT NULL DEFAULT 0")
    # added_at is useless for "recently added" — the whole library lands in one
    # scan and shares a timestamp. The file's own mtime is the real signal.
    if "mtime" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN mtime INTEGER")

    # Resolution/codec facts for the detail page's "Available in" chips —
    # ffprobed lazily (see set_item_media_info()) the first time a title's
    # detail is actually viewed, the same "ask on demand, cache forever"
    # pattern duration_seconds already uses above.
    if "resolution" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN resolution TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN vcodec TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN acodec TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN channels INTEGER")

    # Session tokens are stored hashed, so a readable database is not a set of
    # live cookies. Rows written before that still hold the raw token — which is
    # exactly what is needed to convert them, so the upgrade signs nobody out.
    # A stored hash is 64 hex characters and a raw token is 43, so the two are
    # told apart by shape without a schema change.
    for row in conn.execute("SELECT token FROM sessions").fetchall():
        token = row["token"]
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            continue
        conn.execute(
            "UPDATE sessions SET token = ? WHERE token = ?",
            (hashlib.sha256(token.encode()).hexdigest(), token),
        )

    # Two-factor authentication. Every account has to enrol, so the sessions
    # already in the table have to go with it: each one was issued against a
    # password alone, and leaving them valid would mean 2FA does not actually
    # start applying to anybody until their cookie aged out a month later.
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "totp_secret" not in user_cols:
        for column, decl in (("totp_secret", "TEXT"), ("totp_pending_secret", "TEXT"),
                             ("totp_confirmed_at", "TEXT"), ("totp_last_counter", "INTEGER")):
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {decl}")
        conn.execute("DELETE FROM sessions")
        print("2FA is on: everyone signs in again and enrols an authenticator app",
              flush=True)

    poster_cols = {row["name"] for row in conn.execute("PRAGMA table_info(posters)")}
    # collection/collection_tmdb_id come from Radarr and are movies-only; they
    # stay NULL until an artwork sync has run against a Radarr that knows the film.
    # Sonarr's own name for an episode. Kept beside the artwork rather than
    # written onto items.title, which the scanner parses from the filename and
    # would overwrite on the next full rescan.
    for column, decl in (("title", "TEXT"), ("rating", "REAL"), ("votes", "INTEGER"),
                         ("collection", "TEXT"), ("collection_tmdb_id", "INTEGER"),
                         # Cast is JSON: [{"name", "character", "image"}, ...], top-billed
                         # first. Movies get it from Radarr's own credit endpoint; shows
                         # from TMDB directly, since Sonarr does not track cast at all.
                         ("cast", "TEXT")):
        if column not in poster_cols:
            conn.execute(f"ALTER TABLE posters ADD COLUMN {column} {decl}")
    conn.commit()
    conn.close()


# ---------- libraries ----------

def list_libraries(conn=None):
    """Every mapped library, in sidebar order."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return conn.execute(
            "SELECT * FROM libraries ORDER BY position, name"
        ).fetchall()
    finally:
        if own_conn:
            conn.close()


def library_names(conn=None):
    return [row["name"] for row in list_libraries(conn)]


def get_library(name, conn=None):
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return conn.execute("SELECT * FROM libraries WHERE name = ?", (name,)).fetchone()
    finally:
        if own_conn:
            conn.close()


def add_library(name, label, path, kind):
    """Map a new library. Callers validate the path — see the API route."""
    conn = get_conn()
    try:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 p FROM libraries"
        ).fetchone()["p"]
        conn.execute(
            """INSERT INTO libraries (name, label, path, kind, builtin, position)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (name, label, path, kind, position),
        )
        conn.commit()
    finally:
        conn.close()
    return dict(get_library(name))


def remove_library(name):
    """Unmap a library and drop everything keyed to it. The files are untouched."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM items WHERE library = ?", (name,))
        conn.execute("DELETE FROM posters WHERE library = ?", (name,))
        conn.execute("DELETE FROM user_libraries WHERE library = ?", (name,))
        cur = conn.execute("DELETE FROM libraries WHERE name = ? AND builtin = 0", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- profiles & settings ----------

def default_profile_id(username: str, conn=None) -> int:
    """This account's first profile — where a pre-profiles account's history
    landed in the one-time migration, and what a brand-new account starts
    with. Creates one if somehow none exist (should not happen: init_db()
    seeds every account with at least one)."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM profiles WHERE username = ? ORDER BY id LIMIT 1", (username,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO profiles (username, name) VALUES (?, ?)", (username, username)
        )
        if own_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if own_conn:
            conn.close()


def get_setting(key: str, default=None):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def all_settings() -> dict:
    conn = get_conn()
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def bulk_writer():
    """One connection and one transaction for a whole scan.

    Committing per item costs an fsync per file, which on a large library is
    minutes of disk I/O for a job that is otherwise seconds of work. Pass the
    connection this yields to upsert_item()/clear_missing() and everything
    lands in a single commit at the end (or rolls back as a unit on error).
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def item_count(conn=None):
    """How many files are indexed, across every library."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    finally:
        if own_conn:
            conn.close()


def library_index(library, conn=None):
    """{path: (mtime, size_bytes)} for one library — the file signatures the
    last scan recorded. A partial scan diffs the folder against this to work out
    what is new, what was replaced and what is gone."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return {
            row["path"]: (row["mtime"], row["size_bytes"])
            for row in conn.execute(
                "SELECT path, mtime, size_bytes FROM items WHERE library = ?", (library,)
            )
        }
    finally:
        if own_conn:
            conn.close()


def delete_paths(paths, conn=None):
    """Drop the rows for these exact paths — a partial scan's removals, where
    clear_missing()'s "everything not in this set" would be wrong because the
    caller only walked one library."""
    paths = list(paths)
    if not paths:
        return 0
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.executemany("DELETE FROM items WHERE path = ?", [(p,) for p in paths])
        if own_conn:
            conn.commit()
        return len(paths)
    finally:
        if own_conn:
            conn.close()


def clear_missing(existing_paths, conn=None):
    """Remove DB entries whose file no longer exists on disk."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute("SELECT id, path FROM items").fetchall()
        to_delete = [r["id"] for r in rows if r["path"] not in existing_paths]
        if to_delete:
            conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in to_delete])
            if own_conn:
                conn.commit()
    finally:
        if own_conn:
            conn.close()


def upsert_item(item: dict, conn=None):
    """Insert or update one item. Pass a bulk_writer() connection to batch the
    write into the caller's transaction; without one it commits on its own."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            """
            INSERT INTO items (type, library, title, year, show_title, season, episode,
                               path, size_bytes, mtime)
            VALUES (:type, :library, :title, :year, :show_title, :season, :episode,
                    :path, :size_bytes, :mtime)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title, year=excluded.year, show_title=excluded.show_title,
                season=excluded.season, episode=excluded.episode, size_bytes=excluded.size_bytes,
                library=excluded.library, mtime=excluded.mtime
            """,
            item,
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def upsert_poster(row: dict, conn=None):
    """Record one title's artwork. Same conn= convention as upsert_item()."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            """
            INSERT INTO posters (kind, library, ref, source, file, tmdb_id, tvdb_id,
                                 imdb_id, title, overview, runtime, genres, rating,
                                 votes, collection, collection_tmdb_id, cast, updated_at)
            VALUES (:kind, :library, :ref, :source, :file, :tmdb_id, :tvdb_id,
                    :imdb_id, :title, :overview, :runtime, :genres, :rating,
                    :votes, :collection, :collection_tmdb_id, :cast, datetime('now'))
            ON CONFLICT(kind, library, ref) DO UPDATE SET
                source=excluded.source, file=excluded.file, tmdb_id=excluded.tmdb_id,
                tvdb_id=excluded.tvdb_id, imdb_id=excluded.imdb_id,
                title=excluded.title, overview=excluded.overview,
                runtime=excluded.runtime,
                genres=excluded.genres, rating=excluded.rating, votes=excluded.votes,
                collection=excluded.collection,
                collection_tmdb_id=excluded.collection_tmdb_id,
                cast=excluded.cast,
                updated_at=excluded.updated_at
            """,
            row,
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def prune_stills(conn=None):
    """Drop episode art whose file has left the index, returning the cached
    images that are now orphaned so the caller can unlink them.

    An episode's art is keyed by its file path, so a re-imported release leaves
    the old row behind pointing at art nothing can reach. Local overrides are
    left out of the return: those live beside the media and are not ours to
    delete.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        stale = [
            row["file"]
            for row in conn.execute(
                """SELECT file FROM posters
                   WHERE kind = 'episode' AND source != 'local'
                     AND ref NOT IN (SELECT path FROM items WHERE type = 'episode')"""
            )
        ]
        conn.execute(
            """DELETE FROM posters
               WHERE kind = 'episode'
                 AND ref NOT IN (SELECT path FROM items WHERE type = 'episode')"""
        )
        if own_conn:
            conn.commit()
        return stale
    finally:
        if own_conn:
            conn.close()


def get_poster(kind, library, ref):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM posters WHERE kind = ? AND library = ? AND ref = ?",
            (kind, library, ref),
        ).fetchone()
    finally:
        conn.close()


def poster_counts():
    conn = get_conn()
    try:
        by_source = {
            r["source"]: r["c"]
            for r in conn.execute("SELECT source, COUNT(*) c FROM posters GROUP BY source")
        }
        by_library = {
            r["library"]: r["c"]
            for r in conn.execute("SELECT library, COUNT(*) c FROM posters GROUP BY library")
        }
        return {"total": sum(by_source.values()), "by_source": by_source, "by_library": by_library}
    finally:
        conn.close()


# ---------- play history ----------

# A single viewing produces many requests — every seek re-hits /stream with a new
# Range, and HLS asks for a fresh playlist. Collapse anything inside this window
# into the one play it actually was.
PLAY_DEDUPE_MINUTES = 30

# A viewing has to have got somewhere before its position is worth keeping.
# Under this, playback was a mis-click or a look at the first shot, not a place
# in the film, and the title still starts from the beginning next time.
RESUME_MIN_SECONDS = float(os.environ.get("RESUME_MIN_SECONDS", "30"))

# Past this much of the runtime a title counts as finished, and the next play
# starts over rather than dropping you into the closing credits. It sits high on
# purpose: stopping at 90% often means you intend to come back for the last ten
# minutes, and a resume that skips them would be worse than one that never
# offered itself.
RESUME_FINISHED_PERCENT = float(os.environ.get("RESUME_FINISHED_PERCENT", "95"))
RESUME_FINISHED_FRACTION = RESUME_FINISHED_PERCENT / 100

# How much of a title has to be played before it earns a check mark. Separate
# from RESUME_FINISHED_PERCENT and deliberately lower: seeing a film is not the
# same question as having no more of it to watch. Between the two a title wears
# a mark, is off the Continue Watching shelf, and would still resume near its
# end if you opened it again.
WATCHED_PERCENT = float(os.environ.get("WATCHED_PERCENT", "85"))
WATCHED_FRACTION = WATCHED_PERCENT / 100

# There is no per-title intro detection (no audio fingerprinting, no manual
# markers) — this is a single fixed offset the player can jump to, only ever
# offered on an episode. A movie has no "intro" to skip in this sense.
SKIP_INTRO_SECONDS = float(os.environ.get("SKIP_INTRO_SECONDS", "85"))

# How many entries Continue Watching carries. It is a queue, not an archive —
# the tail of it is stuff you have stopped caring about.
CONTINUE_ROW_SIZE = 20


def set_item_duration(item_id: int, duration: float):
    """Cache an item's ffprobed runtime, in seconds."""
    conn = get_conn()
    try:
        conn.execute("UPDATE items SET duration_seconds = ? WHERE id = ?", (duration, item_id))
        conn.commit()
    finally:
        conn.close()


def set_item_media_info(item_id: int, resolution=None, vcodec=None, acodec=None, channels=None):
    """Cache an item's ffprobed resolution/codec facts — see probe_media_info()
    in transcoder.py. Any field left None (a stream ffprobe could not read)
    stays None rather than overwriting a previous real value with nothing."""
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE items SET
                 resolution = COALESCE(?, resolution),
                 vcodec = COALESCE(?, vcodec),
                 acodec = COALESCE(?, acodec),
                 channels = COALESCE(?, channels)
               WHERE id = ?""",
            (resolution, vcodec, acodec, channels, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def current_play(username: str, item_id: int, conn=None):
    """The play row this viewing belongs to — the newest one inside the dedupe
    window — or None if the viewing has not been logged yet."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return conn.execute(
            """SELECT * FROM plays
               WHERE username = ? AND item_id = ?
                 AND played_at > datetime('now', ?)
               ORDER BY played_at DESC, id DESC LIMIT 1""",
            (username, item_id, f"-{PLAY_DEDUPE_MINUTES} minutes"),
        ).fetchone()
    finally:
        if own_conn:
            conn.close()


def record_play(username: str, item_id: int):
    """Log that a user started this item. No-op inside the dedupe window.

    A fresh row starts at zero: hitting play is not watching, and how far the
    viewing gets is record_progress()'s to say.
    """
    conn = get_conn()
    try:
        if current_play(username, item_id, conn):
            return False
        conn.execute(
            "INSERT INTO plays (username, item_id) VALUES (?, ?)", (username, item_id)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def record_progress(username: str, item_id: int, position: float, duration: float):
    """Move a play's high-water mark, and return the fraction it now stands at.

    Only forwards: rewinding to catch a line of dialogue, or the player
    reporting 0 as it tears down, must not walk back a viewing that already got
    to the end.
    """
    if not duration or duration <= 0:
        return 0.0
    position = max(0.0, min(float(position), float(duration)))
    fraction = position / duration
    conn = get_conn()
    try:
        row = current_play(username, item_id, conn)
        if row is None:
            conn.execute(
                """INSERT INTO plays (username, item_id, position_seconds,
                                      duration_seconds, progress)
                   VALUES (?, ?, ?, ?, ?)""",
                (username, item_id, position, duration, fraction),
            )
        else:
            fraction = max(fraction, row["progress"] or 0.0)
            position = max(position, row["position_seconds"] or 0.0)
            conn.execute(
                """UPDATE plays SET position_seconds = ?, duration_seconds = ?, progress = ?
                   WHERE id = ?""",
                (position, duration, fraction, row["id"]),
            )
        conn.commit()
        return fraction
    finally:
        conn.close()


def latest_play(username: str, item_id: int, conn=None):
    """The newest play row for this item that actually says something.

    current_play() deliberately only looks inside the dedupe window — that
    window is what makes a re-request part of the same viewing. Resuming asks
    the opposite question: where did this account get to, whenever that was.

    Rows that say nothing are skipped rather than taken as the answer. A row
    still at zero and not finished is the placeholder record_play() writes the
    moment a stream is requested, before the player has reported anything; the
    newest row for a title is routinely one of those, and reading it literally
    would throw away the very place it was opened to return to.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        return conn.execute(
            """SELECT * FROM plays
               WHERE username = ? AND item_id = ?
                 AND (position_seconds >= ? OR progress >= ?)
               ORDER BY played_at DESC, id DESC LIMIT 1""",
            (username, item_id, RESUME_MIN_SECONDS, RESUME_FINISHED_FRACTION),
        ).fetchone()
    finally:
        if own_conn:
            conn.close()


def resume_position(username: str, item_id: int, conn=None) -> float:
    """Where playback should pick this title up, in seconds. 0 = from the top.

    A title whose last real viewing ran past RESUME_FINISHED_PERCENT starts
    over: it was finished, and dropping someone into the closing credits is not
    a resume.
    """
    row = latest_play(username, item_id, conn)
    if row is None or (row["progress"] or 0) >= RESUME_FINISHED_FRACTION:
        return 0.0
    return float(row["position_seconds"] or 0.0)


def _episodes_of(conn, library: str, show_title: str):
    """One show's episodes in running order.

    NULLs sort first in SQLite, so an episode the scanner could not read an
    SxxEyy out of would otherwise claim to be the first one — the IS NULL keys
    push those to the end where they belong.
    """
    return conn.execute(
        """SELECT i.id, COALESCE(p.title, i.title) AS title,
                  i.season, i.episode, i.path, i.duration_seconds
           FROM items i
           LEFT JOIN posters p
                  ON p.kind = 'episode' AND p.library = i.library AND p.ref = i.path
           WHERE i.type = 'episode' AND i.library = ? AND i.show_title = ?
           ORDER BY i.season IS NULL, i.season, i.episode IS NULL, i.episode, i.title""",
        (library, show_title),
    ).fetchall()


def _next_unwatched_episode(conn, username: str, library: str, show_title: str,
                            after_item_id: int):
    """The first episode after this one that has not been watched, or None once
    the show has nothing left to offer."""
    episodes = _episodes_of(conn, library, show_title)
    watched = {
        row["item_id"]
        for row in conn.execute(
            """SELECT DISTINCT p.item_id FROM plays p JOIN items i ON i.id = p.item_id
               WHERE p.username = ? AND p.progress >= ?
                 AND i.library = ? AND i.show_title = ?""",
            (username, WATCHED_FRACTION, library, show_title),
        )
    }
    seen_it = False
    for row in episodes:
        if seen_it and row["id"] not in watched:
            return row
        if row["id"] == after_item_id:
            seen_it = True
    return None


def _latest_play_per_item(conn, username: str):
    """The newest play row for every item this account has got somewhere in,
    with the item it belongs to, newest first.

    Rows that say nothing are ignored for the same reason as in latest_play():
    they are placeholders, and one of them would present a half-watched episode
    as untouched — which for a show means offering the *next* episode instead
    of the one you are in the middle of.
    """
    return conn.execute(
        """SELECT p.item_id, p.played_at, p.position_seconds, p.progress,
                  i.type, i.library, COALESCE(a.title, i.title) AS title,
                  i.show_title, i.season, i.episode,
                  i.path, i.duration_seconds AS item_duration,
                  p.duration_seconds AS play_duration
           FROM plays p
           JOIN items i ON i.id = p.item_id
           LEFT JOIN posters a
                  ON a.kind = 'episode' AND a.library = i.library AND a.ref = i.path
           WHERE p.username = ?
             AND (p.position_seconds >= ? OR p.progress >= ?)
             AND p.id = (SELECT q.id FROM plays q
                         WHERE q.username = p.username AND q.item_id = p.item_id
                           AND (q.position_seconds >= ? OR q.progress >= ?)
                         ORDER BY q.played_at DESC, q.id DESC LIMIT 1)
           ORDER BY p.played_at DESC, p.item_id DESC""",
        (username, RESUME_MIN_SECONDS, WATCHED_FRACTION,
         RESUME_MIN_SECONDS, WATCHED_FRACTION),
    ).fetchall()


def continue_watching(username: str, libraries=None, limit: int = CONTINUE_ROW_SIZE,
                      conn=None):
    """What this account has part-watched, most recently played first.

    One entry per film, and one per *show* rather than per episode — a series
    is a single place in a queue, not twelve. A show whose last episode was
    finished offers the next one at zero seconds instead of dropping off the
    shelf, which is what makes this a queue rather than a list of abandoned
    viewings. Finished films, and shows with nothing left, say nothing here.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        libraries = None if libraries is None else set(libraries)
        entries = []
        shows_seen = set()
        for row in _latest_play_per_item(conn, username):
            if libraries is not None and row["library"] not in libraries:
                continue
            position = row["position_seconds"] or 0.0
            unfinished = (row["progress"] or 0) < WATCHED_FRACTION
            duration = row["item_duration"] or row["play_duration"]

            if row["type"] != "episode" or row["show_title"] is None:
                if unfinished:
                    entries.append({
                        "kind": "movie", "id": row["item_id"], "library": row["library"],
                        "title": row["title"], "show_title": None,
                        "season": None, "episode": None, "path": row["path"],
                        "position": position, "duration": duration,
                        "progress": row["progress"] or 0.0, "resume": True,
                        "last_played": row["played_at"],
                    })
                continue

            # Shows are keyed by library *and* title: the same series can be
            # indexed in both Series and Anime, and they are separate entries.
            key = (row["library"], row["show_title"])
            if key in shows_seen:
                continue                      # an older episode of a show already placed
            shows_seen.add(key)

            if unfinished:
                episode, resume, at = row, True, position
                episode_id, progress = row["item_id"], row["progress"] or 0.0
            else:
                # Finished it — so the show's place in the queue is whatever
                # comes next, offered from the start.
                episode = _next_unwatched_episode(
                    conn, username, row["library"], row["show_title"], row["item_id"]
                )
                if episode is None:
                    continue                  # nothing left in this show
                resume, at, progress = False, 0.0, 0.0
                episode_id, duration = episode["id"], episode["duration_seconds"]

            entries.append({
                "kind": "episode", "id": episode_id, "library": row["library"],
                "title": episode["title"], "show_title": row["show_title"],
                "season": episode["season"], "episode": episode["episode"],
                "path": episode["path"], "position": at, "duration": duration,
                "progress": progress, "resume": resume,
                "last_played": row["played_at"],
            })
        return entries[:limit]
    finally:
        if own_conn:
            conn.close()


def remove_continue_entry(username: str, library: str, show_title, item_id: int, conn=None) -> bool:
    """Clear whichever play row is putting one entry on Continue Watching.

    A movie's own item_id is enough — delete every play of it and it drops off
    the shelf with nothing left to resume. A show is trickier: the id the shelf
    handed back may be the *next* unwatched episode, which has no play row of
    its own — the entry exists because of the show's most recently played
    episode, exactly the one _latest_play_per_item() would pick, so that is
    what has to go instead.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        target_id = item_id
        if show_title is not None:
            row = conn.execute(
                """SELECT p.item_id FROM plays p JOIN items i ON i.id = p.item_id
                   WHERE p.username = ? AND i.library = ? AND i.show_title = ?
                     AND (p.position_seconds >= ? OR p.progress >= ?)
                   ORDER BY p.played_at DESC, p.id DESC LIMIT 1""",
                (username, library, show_title, RESUME_MIN_SECONDS, WATCHED_FRACTION),
            ).fetchone()
            if row is None:
                return False
            target_id = row["item_id"]
        cur = conn.execute(
            "DELETE FROM plays WHERE username = ? AND item_id = ?", (username, target_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def watched_item_ids(username: str, libraries=None, conn=None) -> set:
    """Ids of every item this user has watched to WATCHED_PERCENT or beyond.

    A play that stopped short of the bar counts for nothing here — it is still
    in the history, it just does not put a mark on the card. Pass `libraries` to
    restrict the answer to what the account can still see: a revoked library
    must not leak its ids back.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        sql = ("SELECT DISTINCT p.item_id AS id FROM plays p "
               "JOIN items i ON i.id = p.item_id "
               "WHERE p.username = ? AND p.progress >= ?")
        params = [username, WATCHED_FRACTION]
        if libraries is not None:
            libraries = list(libraries)
            if not libraries:
                return set()
            sql += f" AND i.library IN ({','.join('?' * len(libraries))})"
            params += libraries
        return {row["id"] for row in conn.execute(sql, params)}
    finally:
        if own_conn:
            conn.close()


def watched_shows(username: str, conn=None):
    """(library, show_title) for every show this user has watched *all* of.

    A part-watched show gets nothing: the mark means finished, and a check on a
    show you are three episodes into would be a lie. Episodes count only once
    they clear WATCHED_PERCENT, so a season left running in the background does
    not tick the show off. Shows are counted from the index, so a newly grabbed
    episode silently un-marks its show, which is the behaviour you want.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            """SELECT i.library, i.show_title,
                      COUNT(*) AS total,
                      COUNT(p.item_id) AS played
               FROM items i
               LEFT JOIN (SELECT DISTINCT item_id FROM plays
                          WHERE username = ? AND progress >= ?) p
                      ON p.item_id = i.id
               WHERE i.type = 'episode' AND i.show_title IS NOT NULL
               GROUP BY i.library, i.show_title""",
            (username, WATCHED_FRACTION),
        ).fetchall()
        return [(r["library"], r["show_title"]) for r in rows if r["played"] == r["total"]]
    finally:
        if own_conn:
            conn.close()


# Every `username` parameter from here down (this function, list_entries(),
# continue_watching(), watched_item_ids(), watched_shows(), record_play(),
# record_progress(), resume_position(), current_play(), latest_play(),
# recent_plays()) actually receives a *profile's* id, stringified — not the
# account's username. Profiles sit on top of accounts (see the profiles table
# above); My List and watch history belong to the profile doing the watching,
# not the account it lives under. The column and parameter kept their old name
# because every one of these queries is a plain `WHERE username = ?` that does
# not care what the string means, and renaming it everywhere would touch far
# more lines than the one fact worth writing down here.
def toggle_list_entry(username: str, kind: str, library: str, ref: str) -> bool:
    """Flip one title's membership in My List. Returns the state it is now in.

    One call for both directions rather than separate add/remove endpoints —
    the button on every title only ever needs to know what to say next.
    """
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT 1 FROM my_list WHERE username = ? AND kind = ? AND library = ? AND ref = ?",
            (username, kind, library, ref),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM my_list WHERE username = ? AND kind = ? AND library = ? AND ref = ?",
                (username, kind, library, ref),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO my_list (username, kind, library, ref) VALUES (?, ?, ?, ?)",
            (username, kind, library, ref),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_entries(username: str, libraries=None, conn=None):
    """This account's My List rows, most recently added first.

    Restricted to `libraries` when given, the same way watched_item_ids() is —
    a library revoked after something was added to the list must not leak it
    back in the response.
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        sql = "SELECT kind, library, ref FROM my_list WHERE username = ?"
        params = [username]
        if libraries is not None:
            libraries = list(libraries)
            if not libraries:
                return []
            sql += f" AND library IN ({','.join('?' * len(libraries))})"
            params += libraries
        sql += " ORDER BY added_at DESC"
        return conn.execute(sql, params).fetchall()
    finally:
        if own_conn:
            conn.close()


def recent_plays(username: str, limit: int = 20):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.played_at, i.id, i.type, i.library, i.title, i.show_title,
                      i.season, i.episode
               FROM plays p JOIN items i ON i.id = p.item_id
               WHERE p.username = ?
               ORDER BY p.played_at DESC LIMIT ?""",
            (username, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

