import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (StreamingResponse, FileResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import (DB_PATH, LIBRARY_KINDS, LIBRARY_NAME_RE, RESUME_FINISHED_PERCENT,
                       SKIP_INTRO_SECONDS, WATCHED_PERCENT, add_library, all_settings,
                       continue_watching, get_library, get_poster, get_conn, get_setting,
                       init_db, library_names, list_entries, list_libraries,
                       poster_counts, record_play, record_progress, recent_plays,
                       remove_continue_entry, remove_library, resume_position,
                       set_item_duration, set_item_media_info,
                       set_setting, toggle_list_entry, item_count, watched_item_ids, watched_shows)
from .scanner import run_full_scan, run_partial_scan, summarize
from . import artwork, auth, subtitles, transcoder, watching

app = FastAPI(title="HomeReel API", version="0.1.0")

CHUNK_SIZE = 1024 * 1024  # 1MB

# How often the background watcher re-checks the libraries for new or deleted
# files, in seconds; 0 turns it off and leaves scanning to the buttons. It is a
# folder walk, not a filesystem watch: inotify does not cross the bind mount
# into the container, and the *arr apps import files behind our back anyway.
WATCH_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))


def _downloads_url() -> str | None:
    """DOWNLOADS_URL, if it is a plain http(s) address. It ends up in an href on
    the sign-in page, so anything else (a javascript: URL, a typo) is dropped."""
    url = os.environ.get("DOWNLOADS_URL", "").strip()
    return url if url.startswith(("https://", "http://")) else None


# Where "Get the apps" points: a page hosting the Android and TV builds. Unset
# (the default) means the link is not shown anywhere.
DOWNLOADS_URL = _downloads_url()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _theme_ids() -> tuple:
    """The theme registry lives in static/themes.js — the front end needs all
    of it (names, swatches, effects) and the server only needs the ids, to
    refuse anything a profile or the household default can't render. Read from
    there rather than kept as a second list that could drift out of step."""
    try:
        source = (STATIC_DIR / "themes.js").read_text()
    except OSError:
        return ("default",)
    ids = tuple(re.findall(r'^\s*id: "([a-z0-9-]+)",\s*$', source, re.M))
    return ids if "default" in ids else ("default",) + ids


THEME_IDS = _theme_ids()

# Everything else — including /stream and /transcode, which are the actual
# media — requires a session. Only what the login page itself needs is open.
# The three sign-in endpoints are open because there is no session yet — the
# second and third are gated on the pending-login cookie instead, which only a
# correct password buys.
# /api/auth/logout is here too: it is what the sign-in page's Back button calls
# to throw away a half-finished login, which by definition has no session yet.
# The manifest is public because a browser fetches it without credentials
# unless the link says otherwise, and because installing to the home screen
# from the sign-in page has to work before anyone has signed in at all.
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/verify", "/api/auth/setup",
                "/api/auth/logout", "/api/public/stats", "/style.css", "/favicon.ico",
                "/manifest.webmanifest", "/api/auth/quickconnect/start",
                # The sign-in page draws in the last-used / household theme.
                "/themes.css", "/themes.js"}

# App icons and fonts. A prefix rather than one entry each, so adding a size
# (or a weight) later does not silently 303 to the login page. Fonts are here
# because the sign-in page's own @font-face rules need them before there is a
# session to gate on. Nothing in either prefix is more secret than a logo.
# /api/auth/quickconnect/poll/ is public for the same reason the sign-in
# endpoints are: the TV asking has no session yet — the token in the path *is*
# its credential here, the same way a pending-login cookie stands in for one
# during the password/2FA steps. /approve is deliberately not in either set —
# it is the phone's side, and the phone already has a real session by then.
PUBLIC_PREFIXES = ("/icons/", "/fonts/", "/api/auth/quickconnect/poll/")


def _sync_artwork_quietly():
    """Artwork sync talks to Radarr/Sonarr over the network, so it runs in the
    background — the library is browsable immediately, posters fill in."""
    try:
        artwork.sync_artwork()
    except Exception as exc:  # never take the server down over cover art
        print(f"artwork sync failed: {exc}", flush=True)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Gate every route on a valid session cookie.

    An API call gets a 401 it can act on; a page navigation gets bounced to the
    login form, so a bookmarked deep link still lands somewhere sensible.
    """
    path = request.url.path
    if (path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
            or request.method == "OPTIONS"):
        return await call_next(request)

    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    # An unenrolled account cannot hold a session — resetting someone's 2FA
    # deletes theirs — so this is a backstop rather than the enforcement point.
    if user is None or not user["totp_confirmed_at"]:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    request.state.user = user
    # Read fresh on every request rather than baked into the session, so
    # revoking a library takes effect immediately instead of at next login.
    request.state.libraries = set(auth.libraries_for(user))
    return await call_next(request)


def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:                      # unreachable via HTTP; guards direct calls
        raise HTTPException(401, "Authentication required")
    return user


def current_profile(request: Request):
    """The active profile — distinct from current_user(): that says which
    account is signed in, this says which of its profiles is doing the
    watching. Raises 401 with a `reason` the frontend tells apart from a dead
    session (see api() in app.js), so a signed-in account with no profile
    picked yet is sent back to the picker rather than to /login."""
    user = current_user(request)
    profile_id = request.cookies.get(auth.PROFILE_COOKIE)
    profile = auth.get_profile(user["username"], profile_id) if profile_id else None
    if profile is None:
        raise HTTPException(401, "No profile selected",
                            headers={"X-Auth-Reason": "profile"})
    return profile


def viewer_key(request: Request) -> str:
    """The string My List / watch history are keyed by — see the note above
    database.py's toggle_list_entry()."""
    return str(current_profile(request)["id"])


def require_admin(request: Request):
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def allowed_libraries(request: Request) -> set:
    """The libraries this request's user may see — the whole access check."""
    current_user(request)                 # 401 rather than a silent empty set
    return getattr(request.state, "libraries", set())


def require_library(request: Request, library: str):
    if library not in allowed_libraries(request):
        raise HTTPException(403, f"You do not have access to the {library.capitalize()} library")


def _library_of_kind(request: Request, name: str, kind: str):
    """Resolve a library by name, 404/400 on shape, 403 on access."""
    row = get_library(name)
    if row is None:
        raise HTTPException(404, f"No library called '{name}'")
    if row["kind"] != kind:
        raise HTTPException(400, f"'{name}' is a {row['kind']} library, not a {kind} one")
    require_library(request, name)
    return row


# X-Forwarded-For is set by the client unless something trusted overwrites it,
# so it is only believed when this says a proxy is in front. Without that, the
# one place the address is shown — the admin Live view — would report whatever
# the person being looked at decided to claim.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")


def _client_host(request: Request) -> str:
    """Which machine is asking: the socket, or the client the trusted proxy in
    front named. Its first entry is the original client."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _placeholders(values) -> str:
    """`IN (?,?,?)` for a list. Callers must handle the empty case themselves —
    SQLite rejects `IN ()`."""
    return ",".join("?" * len(values))


def accessible_item(request: Request, item_id: int):
    """One item row: 404 if it does not exist, 403 if its library is not yours.

    Every route that can reveal or play a file goes through here, so a hidden
    library cannot be reached by guessing an id.
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Item not found")
    require_library(request, row["library"])
    return row


def _watch_libraries():
    """Re-scan on a timer so media imported while the app is up shows up on its
    own. Only the difference is written, so a quiet pass costs one walk and no
    database writes at all — see scanner.run_partial_scan()."""
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            totals = summarize(run_partial_scan())
        except Exception as exc:  # a failed pass must not kill the watcher
            print(f"library watch scan failed: {exc}", flush=True)
            continue
        if totals["skipped"]:
            print(f"library watch: left alone {', '.join(totals['skipped'])} "
                  f"(path missing or empty)", flush=True)
        if totals["changed"]:
            print(f"library watch: +{totals['added']} new, {totals['updated']} changed, "
                  f"-{totals['removed']} gone", flush=True)
        # New files have no artwork yet; nothing else needs Radarr/Sonarr asked.
        if totals["added"]:
            _sync_artwork_quietly()


# The Home tab shipped invisible to anyone whose browser (or an intermediary
# in front of it — a CDN edge, a PWA's cache) held onto the previous app.js:
# RevalidatingStaticFiles' `no-cache` only forces *revalidation*, which
# nothing is obliged to honor, so a stale copy can sit there indefinitely
# without ever re-asking the server. A plain `/app.js` URL can't be told
# apart from the one already cached; a `/app.js?v=<hash>` one is a URL that
# was never fetched before, so there is nothing to have cached wrong.
def _cache_bust_static_html():
    # The sign-in and profile pages load the theme files too, so they need the
    # same treatment as index.html or a theme fix would never reach them.
    for page in ("index.html", "login.html", "profiles.html"):
        page_path = STATIC_DIR / page
        html = page_path.read_text()
        for asset in ("app.js", "style.css", "themes.css", "themes.js", "vendor/hls.min.js"):
            asset_path = STATIC_DIR / asset
            if not asset_path.exists():
                continue
            digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()[:10]
            html = re.sub(
                rf'(["\'])/{re.escape(asset)}(?:\?v=[0-9a-f]+)?\1',
                rf'\g<1>/{asset}?v={digest}\g<1>',
                html,
            )
        page_path.write_text(html)


@app.on_event("startup")
def startup():
    _cache_bust_static_html()
    init_db()
    # A cold start with an empty index has to walk and parse everything. Every
    # restart after that only needs what changed while the app was down.
    if item_count():
        run_partial_scan()
    else:
        run_full_scan()
    threading.Thread(target=transcoder.cleanup_loop, daemon=True).start()
    threading.Thread(target=_sync_artwork_quietly, daemon=True).start()
    if WATCH_INTERVAL > 0:
        threading.Thread(target=_watch_libraries, daemon=True).start()


# ---------- Auth ----------

class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class SecondFactor(BaseModel):
    # Six digits from the authenticator, or one of the backup codes.
    code: str


class PasswordCheck(BaseModel):
    password: str


class QuickConnectApprove(BaseModel):
    token: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    # None means every library, which is what account creation did before
    # access control existed.
    libraries: list[str] | None = None


class PlayProgress(BaseModel):
    item_id: int
    position: float                 # seconds into the title
    # What the player thinks the runtime is. Only a fallback: see /api/progress.
    duration: float | None = None


class LibraryAccess(BaseModel):
    libraries: list[str]


class NewLibrary(BaseModel):
    label: str
    path: str
    kind: str = "show"
    name: str = ""          # derived from the label when left blank


class NewProfile(BaseModel):
    name: str


class ProfileSelection(BaseModel):
    id: int


class SettingsUpdate(BaseModel):
    # None means "leave this one alone" — PUT is a partial update, so setting
    # only nav_mode does not require also knowing the current poster_style.
    nav_mode: str | None = None
    poster_style: str | None = None
    theme: str | None = None        # the household default theme


class ProfileTheme(BaseModel):
    # None (or omitted) clears the profile's own choice, so it follows the
    # household default again.
    theme: str | None = None


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/public/stats")
def public_stats():
    """The sign-in page's own stat line. Public and deliberately minimal —
    just enough to say this is a real, populated library, not an empty demo."""
    return {"items": item_count(), "libraries": len(library_names()),
            "downloads_url": DOWNLOADS_URL,
            # The household theme, for a browser that has never signed in here
            # and so has no last-used theme cached to draw the sign-in page in.
            "theme": _settings_payload()["theme"]}


@app.get("/profiles")
def profiles_page():
    """The picker between a real sign-in and the app. Requires a session (the
    middleware enforces that — this path is not in PUBLIC_PATHS) but not a
    profile, since picking one is the whole point of the page."""
    return FileResponse(STATIC_DIR / "profiles.html", headers={"Cache-Control": "no-cache"})


def _login_ip(request: Request) -> str:
    """The address the failed-login throttle counts against.

    The socket, never a header: this one is not for display, and a client that
    could choose its own key could sidestep the lockout by varying it.
    """
    return request.client.host if request.client else ""


def _set_session_cookie(response, token):
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,          # not reachable from JS, so XSS cannot lift it
        samesite="lax",
        path="/",
    )


def _set_profile_cookie(response, profile_id):
    response.set_cookie(
        auth.PROFILE_COOKIE,
        str(profile_id),
        max_age=auth.PROFILE_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _pending_login(request: Request):
    """The half-finished sign-in this request is carrying, or a 401."""
    pending = auth.pending_login(request.cookies.get(auth.PENDING_COOKIE))
    if pending is None:
        raise HTTPException(401, "That sign-in timed out — start again")
    return pending


def _finish_login(request: Request, username: str, extra: dict = None):
    """Both factors are done: swap the pending cookie for a real session."""
    auth.clear_pending(request.cookies.get(auth.PENDING_COOKIE))
    token = auth.start_session(username)
    user = auth.get_user(username)
    payload = {"username": user["username"], "role": user["role"]}
    payload.update(extra or {})
    response = JSONResponse(payload)
    response.delete_cookie(auth.PENDING_COOKIE, path="/")
    # A stale profile from a previous sign-in (this account or another one on
    # the same browser) must not carry over — every sign-in lands on the
    # picker.
    response.delete_cookie(auth.PROFILE_COOKIE, path="/")
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/login")
def login(credentials: Credentials, request: Request):
    """First factor only. Nothing is signed in when this returns.

    Two-factor is required of every account, so what comes back is which of the
    two second screens the browser should show: `totp` for someone who already
    has an authenticator, `enroll` for someone who has yet to set one up.
    """
    client_ip = _login_ip(request)
    locked = auth.is_locked_out(credentials.username, client_ip)
    if locked:
        raise HTTPException(429, f"Too many failed attempts — try again in {locked}s")

    user = auth.verify_credentials(credentials.username, credentials.password, client_ip)
    if user is None:
        raise HTTPException(401, "Incorrect username or password")

    purpose = "totp" if user["totp_confirmed_at"] else "enroll"
    token = auth.start_pending_login(user["username"], purpose)
    response = JSONResponse({"next": purpose, "username": user["username"]})
    response.set_cookie(
        auth.PENDING_COOKIE,
        token,
        max_age=auth.PENDING_MINUTES * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/verify")
def verify_login(body: SecondFactor, request: Request):
    """Second factor for an account that is already enrolled."""
    pending = _pending_login(request)
    if pending["purpose"] != "totp":
        raise HTTPException(400, "This account still has to set up an authenticator")

    client_ip = _login_ip(request)
    locked = auth.is_locked_out(pending["username"], client_ip)
    if locked:
        raise HTTPException(429, f"Too many failed attempts — try again in {locked}s")
    if not auth.verify_second_factor(pending["username"], body.code, client_ip):
        raise HTTPException(401, "That code is not right — check the app, or use a backup code")
    return _finish_login(request, pending["username"])


@app.get("/api/auth/setup")
def start_enrolment(request: Request):
    """The QR and secret for an account signing in before it has enrolled."""
    pending = _pending_login(request)
    if pending["purpose"] != "enroll":
        raise HTTPException(400, "This account already has an authenticator")
    return auth.begin_enrolment(pending["username"])


@app.post("/api/auth/setup")
def finish_enrolment(body: SecondFactor, request: Request):
    """Confirm the new authenticator and sign in, with the backup codes.

    The codes come back exactly once, here — only their hashes are kept.
    """
    pending = _pending_login(request)
    if pending["purpose"] != "enroll":
        raise HTTPException(400, "This account already has an authenticator")
    try:
        codes = auth.confirm_enrolment(pending["username"], body.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _finish_login(request, pending["username"], {"backup_codes": codes})


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(auth.SESSION_COOKIE))
    auth.clear_pending(request.cookies.get(auth.PENDING_COOKIE))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.PENDING_COOKIE, path="/")
    response.delete_cookie(auth.PROFILE_COOKIE, path="/")
    return response


# ---------- Quick Connect (TV sign-in via the phone app) ----------

@app.post("/api/auth/quickconnect/start")
def quick_connect_start():
    """A box with no keyboard calls this to get a QR (and a fallback code) to
    put on screen. No session yet, so this is public the same way the sign-in
    endpoints are — see PUBLIC_PATHS."""
    return auth.start_quick_connect()


@app.get("/api/auth/quickconnect/poll/{token}")
def quick_connect_poll(token: str):
    """What the TV asks on a timer while the QR is up. See PUBLIC_PREFIXES for
    why this needs no session either — the token in the path is the pairing's
    own credential, single-use the moment it comes back approved."""
    return auth.poll_quick_connect(token)


@app.post("/api/auth/quickconnect/approve")
def quick_connect_approve(body: QuickConnectApprove, request: Request):
    """The phone's side: it scanned the QR and already has a real session, so
    this is gated like any other authenticated route rather than added to
    PUBLIC_PATHS."""
    user = current_user(request)
    if not auth.approve_quick_connect(body.token, user["username"]):
        raise HTTPException(404, "That code has expired or was already used")
    return {"status": "ok"}


# ---------- Two-factor, from Settings ----------

@app.post("/api/auth/2fa/new")
def move_authenticator(body: PasswordCheck, request: Request):
    """Start moving to a new authenticator — a new phone, usually.

    Gated on the password again rather than the session alone: this is the one
    control that can hand the second factor to a different device, so a laptop
    left unlocked should not be enough to do it. The old authenticator keeps
    working until the new one is confirmed.
    """
    user = current_user(request)
    if auth.verify_credentials(user["username"], body.password) is None:
        raise HTTPException(403, "That password is not right")
    return auth.begin_enrolment(user["username"])


@app.post("/api/auth/2fa/confirm")
def confirm_authenticator(body: SecondFactor, request: Request):
    """Switch to the authenticator started by /api/auth/2fa/new."""
    user = current_user(request)
    try:
        codes = auth.confirm_enrolment(user["username"], body.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"backup_codes": codes}


@app.post("/api/auth/2fa/backup-codes")
def replace_backup_codes(body: PasswordCheck, request: Request):
    """Issue a fresh set, invalidating the old one. Shown once."""
    user = current_user(request)
    if auth.verify_credentials(user["username"], body.password) is None:
        raise HTTPException(403, "That password is not right")
    return {"backup_codes": auth.issue_backup_codes(user["username"])}


@app.get("/api/auth/me")
def whoami(request: Request):
    user = current_user(request)
    # Name/label/kind only: the on-disk path is a server detail, and /api/libraries
    # (admin-only) is where it belongs.
    def public(row):
        return {"name": row["name"], "label": row["label"], "kind": row["kind"]}

    allowed = allowed_libraries(request)
    payload = {
        "username": user["username"],
        "role": user["role"],
        # Ordered and labelled, so the sidebar can be built straight from this.
        "libraries": [public(row) for row in list_libraries() if row["name"] in allowed],
        "two_factor": {
            "enrolled": bool(user["totp_confirmed_at"]),
            "confirmed_at": user["totp_confirmed_at"],
            "backup_codes_left": auth.backup_codes_left(user["username"]),
        },
    }
    if user["role"] == "admin":
        # Only an admin needs the full list — it drives the access checkboxes,
        # and a viewer has no business knowing what they were not given.
        payload["all_libraries"] = [public(row) for row in list_libraries()]
    # None until a profile has been picked — /api/auth/me is also what the
    # profile picker itself calls right after login, before that has happened.
    profile_id = request.cookies.get(auth.PROFILE_COOKIE)
    profile = auth.get_profile(user["username"], profile_id) if profile_id else None
    payload["profile"] = dict(profile) if profile else None
    return payload


# ---------- Profiles ----------

@app.get("/api/profiles")
def get_profiles(request: Request):
    user = current_user(request)
    return auth.list_profiles(user["username"])


@app.post("/api/profiles")
def add_profile(body: NewProfile, request: Request):
    user = current_user(request)
    try:
        return auth.create_profile(user["username"], body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/profiles/{profile_id}")
def rename_profile(profile_id: int, body: NewProfile, request: Request):
    user = current_user(request)
    try:
        renamed = auth.rename_profile(user["username"], profile_id, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not renamed:
        raise HTTPException(404, "No such profile")
    return {"id": profile_id, "name": body.name.strip()}


@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: int, request: Request):
    user = current_user(request)
    try:
        removed = auth.delete_profile(user["username"], profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not removed:
        raise HTTPException(404, "No such profile")
    response = JSONResponse({"status": "ok"})
    # Deleting the profile you are currently signed in as must not leave a
    # cookie pointing at nothing — back to the picker rather than a 401 loop.
    if request.cookies.get(auth.PROFILE_COOKIE) == str(profile_id):
        response.delete_cookie(auth.PROFILE_COOKIE, path="/")
    return response


@app.post("/api/profiles/select")
def select_profile(body: ProfileSelection, request: Request):
    user = current_user(request)
    profile = auth.get_profile(user["username"], body.id)
    if profile is None:
        raise HTTPException(404, "No such profile")
    response = JSONResponse(dict(profile))
    _set_profile_cookie(response, profile["id"])
    return response


# ---------- Settings (nav_mode / poster_style, admin-editable) ----------

SETTINGS_DEFAULTS = {"nav_mode": "rail", "poster_style": "portrait", "theme": "default"}
SETTINGS_CHOICES = {"nav_mode": ("top", "rail"), "poster_style": ("still", "portrait"),
                    "theme": THEME_IDS}


def _settings_payload() -> dict:
    stored = all_settings()
    payload = {key: stored.get(key, default) for key, default in SETTINGS_DEFAULTS.items()}
    # A theme since removed from the registry falls back rather than leaving
    # every profile that follows the household default on a theme that no
    # longer draws anything.
    if payload["theme"] not in THEME_IDS:
        payload["theme"] = SETTINGS_DEFAULTS["theme"]
    # Not a stored setting — it comes from the environment — but the frontend
    # reads its configuration from this one call.
    payload["downloads_url"] = DOWNLOADS_URL
    return payload


@app.get("/api/settings")
def get_settings(request: Request):
    """Readable by anyone signed in — the frontend needs it to know how to
    render before it knows who is asking. Only PUT is admin-gated."""
    current_user(request)
    return _settings_payload()


@app.put("/api/settings")
def put_settings(body: SettingsUpdate, request: Request):
    require_admin(request)
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        if value not in SETTINGS_CHOICES[key]:
            raise HTTPException(400, f"{key} must be one of {list(SETTINGS_CHOICES[key])}")
        set_setting(key, value)
    return _settings_payload()


@app.put("/api/profiles/current/theme")
def put_profile_theme(body: ProfileTheme, request: Request):
    """The signed-in profile's own theme. Any profile may set its own — unlike
    /api/settings, this is a personal preference, not a household one."""
    user = current_user(request)
    profile = current_profile(request)
    if body.theme is not None and body.theme not in THEME_IDS:
        raise HTTPException(400, f"theme must be one of {list(THEME_IDS)} or null")
    auth.set_profile_theme(user["username"], profile["id"], body.theme)
    return {"theme": body.theme, "household": _settings_payload()["theme"]}


@app.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request):
    user = current_user(request)
    if auth.verify_credentials(user["username"], body.current_password) is None:
        raise HTTPException(403, "Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    auth.set_password(user["username"], body.new_password,
                      keep_token=request.cookies.get(auth.SESSION_COOKIE))
    return {"status": "ok"}


@app.get("/api/users")
def get_users(request: Request):
    require_admin(request)
    return auth.list_users()


@app.post("/api/users")
def add_user(body: NewUser, request: Request):
    require_admin(request)
    if auth.get_user(body.username):
        raise HTTPException(409, "That username already exists")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    try:
        return auth.create_user(body.username, body.password, body.role, body.libraries)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/users/{username}/libraries")
def set_user_access(username: str, body: LibraryAccess, request: Request):
    """Change which libraries an existing user can see. Takes effect on their
    very next request — no need to sign them out."""
    require_admin(request)
    user = auth.get_user(username)
    if not user:
        raise HTTPException(404, "No such user")
    if user["role"] == "admin":
        raise HTTPException(400, "Administrators always have access to every library")
    try:
        granted = auth.set_user_libraries(username, body.libraries)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"username": username, "libraries": granted}


@app.delete("/api/users/{username}/2fa")
def reset_user_two_factor(username: str, request: Request):
    """Un-enrol an account whose authenticator is gone.

    The way back in for a lost or wiped phone. It signs that account out
    everywhere and drops their backup codes; their next sign-in is the
    enrolment screen, since 2FA is not optional. Allowed on your own account
    too — that is the same path, just without needing another admin.
    """
    require_admin(request)
    if not auth.get_user(username):
        raise HTTPException(404, "No such user")
    auth.reset_two_factor(username)
    return {"status": "ok"}


@app.delete("/api/users/{username}")
def remove_user(username: str, request: Request):
    admin = require_admin(request)
    if username == admin["username"]:
        raise HTTPException(400, "You cannot delete the account you are signed in with")
    admins = [u for u in auth.list_users() if u["role"] == "admin"]
    if len(admins) <= 1 and any(u["username"] == username for u in admins):
        raise HTTPException(400, "Refusing to delete the last administrator")
    if not auth.delete_user(username):
        raise HTTPException(404, "No such user")
    return {"status": "ok"}


# ---------- Libraries (mapping new ones is admin-only) ----------

def _slug(text: str) -> str:
    """A label like "4K Films" becomes "4k-films"."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:31]


# Container OS directories, hidden from the browser so the root listing shows
# the media mounts rather than 20 rows of Debian.
SYSTEM_DIRS = {
    "bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "opt",
    "proc", "root", "run", "sbin", "srv", "sys", "tmp", "usr", "var",
}

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
EPISODE_HINT = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,2}")


def _guess_kind(folder: Path) -> str:
    """Shows or films? Look for an SxxEyy filename a couple of levels down.

    Deliberately shallow and capped: it runs while someone waits on a click, and
    the answer only pre-selects a dropdown they can override.
    """
    checked = 0

    def look(directory: Path, depth: int):
        nonlocal checked
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())[:12]
        except OSError:
            return None
        for child in children:
            if child.is_file():
                if child.suffix.lower() not in VIDEO_EXTS:
                    continue
                checked += 1
                if EPISODE_HINT.search(child.name):
                    return "show"
                if checked >= 40:
                    return "movie"
            elif depth and child.is_dir() and not child.name.startswith("."):
                found = look(child, depth - 1)
                if found:
                    return found
        return None

    return look(folder, 2) or "movie"


@app.get("/api/browse")
def browse(request: Request, path: str = "/"):
    """Folders the container can actually see, for the library picker.

    Browsing beats typing a path here: what is reachable is exactly what the
    compose mounted, and no amount of explaining beats showing it.
    """
    require_admin(request)
    folder = Path(path if path.startswith("/") else "/")
    try:
        folder = folder.resolve()
    except OSError:
        raise HTTPException(400, "Cannot read that path")
    if not folder.is_dir():
        raise HTTPException(404, "No such folder inside the container")
    # Hiding the container's own filesystem from the root listing is not the
    # same as refusing to walk it: without this, ?path=/etc still enumerates.
    # Nothing mountable lives under those, so the picker has no business there.
    if folder.parts[1:2] and folder.parts[1] in SYSTEM_DIRS:
        raise HTTPException(403, "That is the container's own filesystem, not media")

    mapped = {row["path"].rstrip("/") or "/": row["label"] for row in list_libraries()}
    entries = []
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise HTTPException(403, "That folder is not readable")

    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        if folder == Path("/") and child.name in SYSTEM_DIRS:
            continue
        # HomeReel's own cache mount (index, posters, transcode scratch) is not
        # a library and would only be a trap in the list.
        if child == DB_PATH.parent:
            continue
        entries.append({
            "name": child.name,
            "path": str(child),
            "mapped": mapped.get(str(child)),
        })
        if len(entries) >= 300:            # a media root can be enormous
            break

    return {
        "path": str(folder),
        "parent": None if folder == Path("/") else str(folder.parent),
        "entries": entries,
        # Only meaningful for a folder holding media; harmless otherwise.
        "suggested_kind": _guess_kind(folder) if folder != Path("/") else "show",
        "suggested_label": folder.name.replace("-", " ").replace("_", " ").title(),
        "mapped_as": mapped.get(str(folder)),
    }


@app.get("/api/libraries")
def get_libraries(request: Request):
    """Every mapped library with its item count. Admin-only: it exposes the
    server-side paths, which a viewer has no use for."""
    require_admin(request)
    conn = get_conn()
    try:
        counts = {
            row["library"]: row["c"]
            for row in conn.execute("SELECT library, COUNT(*) c FROM items GROUP BY library")
        }
        return [
            {**dict(row), "items": counts.get(row["name"], 0),
             "exists": Path(row["path"]).is_dir()}
            for row in list_libraries(conn)
        ]
    finally:
        conn.close()


@app.post("/api/libraries")
def create_library(body: NewLibrary, request: Request):
    """Map a folder as a new library and index it straight away.

    The path is checked *inside the container*, which is the whole subtlety
    here: HomeReel can only see what the compose mounts, so a folder that is
    obviously there on the host is invisible until it is bind-mounted.
    """
    require_admin(request)

    label = body.label.strip()
    if not label:
        raise HTTPException(400, "A name for the library is required")
    name = _slug(body.name or label)
    if not LIBRARY_NAME_RE.match(name):
        raise HTTPException(400, "Use letters, numbers, '-' or '_' for the library id")
    if body.kind not in LIBRARY_KINDS:
        raise HTTPException(400, f"kind must be one of {list(LIBRARY_KINDS)}")
    if get_library(name):
        raise HTTPException(409, f"A library called '{name}' already exists")

    path = body.path.strip().rstrip("/") or "/"
    if not path.startswith("/"):
        raise HTTPException(400, "Give the path as the container sees it, starting with /")
    folder = Path(path)
    if not folder.is_dir():
        raise HTTPException(
            400,
            f"'{path}' is not a folder inside the container. HomeReel only sees what the "
            f"compose file mounts — add a volume for it and restart, then map it here.",
        )
    for existing in list_libraries():
        if existing["path"].rstrip("/") == path:
            raise HTTPException(409, f"'{existing['label']}' is already mapped to that path")

    library = add_library(name, label, path, body.kind)
    # Index it now: an empty new tab looks broken, and this is a folder walk.
    found = run_full_scan(only=name)
    return {**library, "items": found.get(name, 0)}


@app.delete("/api/libraries/{name}")
def delete_library(name: str, request: Request):
    """Unmap a library. Its index rows, posters and grants go; **the files on
    disk are never touched**. The three built-ins cannot be removed."""
    require_admin(request)
    library = get_library(name)
    if library is None:
        raise HTTPException(404, f"No library called '{name}'")
    if library["builtin"]:
        raise HTTPException(400, f"'{library['label']}' is built in and cannot be unmapped")
    remove_library(name)
    return {"status": "ok"}


# ---------- API ----------

@app.get("/api/library")
def library_summary(request: Request):
    """Counts, restricted to the libraries the caller can see."""
    libraries = allowed_libraries(request)
    conn = get_conn()
    try:
        summary = {}
        if "movies" in libraries:
            summary["movies"] = conn.execute(
                "SELECT COUNT(*) c FROM items WHERE type='movie'"
            ).fetchone()["c"]
        for library in ("series", "anime"):
            if library not in libraries:
                continue
            row = conn.execute(
                """SELECT COUNT(*) episodes, COUNT(DISTINCT show_title) shows
                   FROM items WHERE library = ?""",
                (library,),
            ).fetchone()
            summary[library] = {"shows": row["shows"], "episodes": row["episodes"]}
        return summary
    finally:
        conn.close()


@app.get("/api/movies")
def list_movies(request: Request, library: str = "movies"):
    """Films in one movie library — there can be several now."""
    _library_of_kind(request, library, "movie")
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, title, year, size_bytes FROM items
           WHERE type='movie' AND library = ? ORDER BY title""",
        (library,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/shows")
def list_shows(request: Request, library: str = "series"):
    _library_of_kind(request, library, "show")
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT show_title FROM items WHERE type='episode' AND library = ? ORDER BY show_title",
        (library,),
    ).fetchall()
    conn.close()
    return [r["show_title"] for r in rows]


@app.get("/api/shows/{show_title}/episodes")
def list_episodes(show_title: str, request: Request, library: str = "series"):
    _library_of_kind(request, library, "show")
    conn = get_conn()
    # has_still says whether this episode has its own banner cached, so the grid
    # can render a plain title tile for the ones that do not rather than firing
    # an image request per episode and collecting 404s.
    rows = conn.execute(
        """SELECT i.id, COALESCE(p.title, i.title) AS title,
                  i.season, i.episode, i.size_bytes, i.duration_seconds,
                  p.overview, p.file IS NOT NULL AS has_still
           FROM items i
           LEFT JOIN posters p
                  ON p.kind = 'episode' AND p.library = i.library AND p.ref = i.path
           WHERE i.type='episode' AND i.library = ? AND i.show_title = ?
           ORDER BY i.season, i.episode""",
        (library, show_title),
    ).fetchall()
    conn.close()
    if not rows:
        raise HTTPException(404, "Show not found")
    return [dict(r) for r in rows]


# ---------- Browse: every library at once, filterable by genre ----------

@app.get("/api/discover")
def discover(request: Request, genre: str = None):
    """One grid over every accessible library, movies and shows together.

    The genre list always covers everything the account can see, regardless
    of which chip (if any) is currently picked — filtering only changes which
    cards come back, not which chips are on offer, or picking one would make
    the others disappear.
    """
    libraries = allowed_libraries(request)
    conn = get_conn()
    try:
        movie_posters = {
            r["ref"]: r for r in conn.execute("SELECT ref, genres FROM posters WHERE kind = 'movie'")
        }
        show_posters = {
            (r["library"], r["ref"]): r
            for r in conn.execute("SELECT library, ref, genres FROM posters WHERE kind = 'show'")
        }

        def genres_of(poster):
            return json.loads(poster["genres"]) if poster and poster["genres"] else []

        all_genres = set()
        cards = []
        for row in list_libraries(conn):
            library, kind = row["name"], row["kind"]
            if library not in libraries:
                continue
            if kind == "movie":
                for item in conn.execute(
                    """SELECT id, title, year, type, path FROM items
                       WHERE type = 'movie' AND library = ?""",
                    (library,),
                ):
                    ref = artwork.poster_ref(item)[2]
                    item_genres = genres_of(movie_posters.get(ref))
                    all_genres.update(item_genres)
                    if genre and genre not in item_genres:
                        continue
                    cards.append(_card("movie", library, item["title"], item["year"] or "", item["id"]))
            else:
                for show in conn.execute(
                    """SELECT show_title, COUNT(*) episodes FROM items
                       WHERE type = 'episode' AND library = ? GROUP BY show_title""",
                    (library,),
                ):
                    show_genres = genres_of(show_posters.get((library, show["show_title"])))
                    all_genres.update(show_genres)
                    if genre and genre not in show_genres:
                        continue
                    meta = f"{show['episodes']} episode{'s' if show['episodes'] != 1 else ''}"
                    cards.append(_card("show", library, show["show_title"], meta))
        cards.sort(key=lambda c: c["title"].lower())
        return {"genres": sorted(all_genres), "items": cards}
    finally:
        conn.close()


@app.get("/api/item/{item_id}")
def get_item(item_id: int, request: Request):
    return dict(accessible_item(request, item_id))


def _detail_response(title, poster, overview=None, runtime=None):
    """Shape shared by the movie/show/episode detail routes below.

    `poster` is a posters row (or None, for a title the artwork sync has never
    matched) — its own overview/runtime win when the caller did not already
    have something more specific to say (an episode's own summary, say)."""
    return {
        "title": title,
        "overview": overview if overview is not None else (poster["overview"] if poster else None),
        "runtime": runtime if runtime is not None else (poster["runtime"] if poster else None),
        "genres": json.loads(poster["genres"]) if poster and poster["genres"] else [],
        "rating": poster["rating"] if poster else None,
        "votes": poster["votes"] if poster else None,
        "collection": poster["collection"] if poster else None,
        "cast": json.loads(poster["cast"]) if poster and poster["cast"] else [],
    }


def _detail_facts(detail, item_row=None, episode_count=None):
    """The Details panel's fact table — only ever what is actually known.
    There is deliberately no Audio/Subtitles row: nothing in HomeReel tracks
    per-file track languages reliably enough to state as fact."""
    facts = []
    if detail["genres"]:
        facts.append({"k": "Genre", "v": ", ".join(detail["genres"])})
    if item_row is not None and item_row["type"] == "movie" and item_row["year"]:
        facts.append({"k": "Released", "v": str(item_row["year"])})
    if item_row is not None and detail["runtime"]:
        facts.append({"k": "Length", "v": _clock(detail["runtime"] * 60)})
    elif episode_count is not None:
        facts.append({"k": "Length", "v": f"{episode_count} episode{'s' if episode_count != 1 else ''}"})
    if detail["rating"] is not None:
        value = f"{detail['rating']:.1f} / 5"
        if detail["votes"]:
            value += f" · {detail['votes']:,} votes"
        facts.append({"k": "Rating", "v": value})
    if detail["collection"]:
        facts.append({"k": "Collection", "v": detail["collection"]})
    return facts


_VCODEC_LABELS = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "mpeg4": "MPEG-4"}
_ACODEC_LABELS = {"aac": "AAC", "ac3": "Dolby Digital", "eac3": "Dolby Digital+", "dts": "DTS",
                  "truehd": "Dolby TrueHD", "opus": "Opus", "mp3": "MP3", "flac": "FLAC"}
# No mainstream browser ships a native decoder for these — Direct Play still
# serves the file and the video track plays fine, but audio is just silent,
# with no error the player can react to (see /api/resume's audio_incompatible).
_BROWSER_INCOMPATIBLE_AUDIO = {"ac3", "eac3", "dts", "truehd"}
_CHANNEL_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def _format_chips(item_row):
    """The "Available in" panel — real facts about this one file (resolution,
    video codec, audio codec + channels), ffprobed lazily by the caller.
    Never invented: a file with an unrecognised codec just shows ffprobe's own
    name for it rather than nothing, but nothing here is guessed."""
    chips = []
    if item_row["resolution"]:
        chips.append(item_row["resolution"])
    if item_row["vcodec"]:
        chips.append(_VCODEC_LABELS.get(item_row["vcodec"], item_row["vcodec"].upper()))
    if item_row["acodec"]:
        label = _ACODEC_LABELS.get(item_row["acodec"], item_row["acodec"].upper())
        if item_row["channels"] in _CHANNEL_LABELS:
            label += " " + _CHANNEL_LABELS[item_row["channels"]]
        chips.append(label)
    return chips


def _similar_titles(conn, library, kind, exclude_title, genres, limit=4):
    """Up to `limit` other titles in this library sharing a genre — "More like
    this". Ranked by how many genres overlap, not by rating: the point here is
    kinship with the title being viewed, not a quality ranking like Top 10's.
    """
    if not genres:
        return []
    wanted = set(genres)
    candidates = []
    if kind == "movie":
        posters = {r["ref"]: r for r in conn.execute(
            "SELECT ref, genres FROM posters WHERE kind = 'movie'")}
        for item in conn.execute(
            "SELECT id, title, year, type, path FROM items WHERE type = 'movie' AND library = ?",
            (library,),
        ):
            if item["title"] == exclude_title:
                continue
            poster = posters.get(artwork.poster_ref(item)[2])
            item_genres = set(json.loads(poster["genres"])) if poster and poster["genres"] else set()
            overlap = len(wanted & item_genres)
            if overlap:
                candidates.append((overlap, _card("movie", library, item["title"], item["year"] or "", item["id"])))
    else:
        posters = {r["ref"]: r for r in conn.execute(
            "SELECT ref, genres FROM posters WHERE kind = 'show' AND library = ?", (library,))}
        for show in conn.execute(
            """SELECT show_title, COUNT(*) episodes FROM items
               WHERE type = 'episode' AND library = ? GROUP BY show_title""",
            (library,),
        ):
            if show["show_title"] == exclude_title:
                continue
            poster = posters.get(show["show_title"])
            item_genres = set(json.loads(poster["genres"])) if poster and poster["genres"] else set()
            overlap = len(wanted & item_genres)
            if overlap:
                meta = f"{show['episodes']} episode{'s' if show['episodes'] != 1 else ''}"
                candidates.append((overlap, _card("show", library, show["show_title"], meta)))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [card for _overlap, card in candidates[:limit]]


@app.get("/api/detail/item/{item_id}")
def detail_for_item(item_id: int, request: Request):
    """Description + cast + facts for one movie or episode, for its detail
    page. A movie's own posters row carries everything; an episode has no
    cast of its own (Sonarr does not track it per-episode, and neither does
    TMDB in a way worth a second fetch per episode) so it borrows its show's,
    keyed the same way the poster wall already looks the show up."""
    row = accessible_item(request, item_id)
    if row["resolution"] is None:
        info = transcoder.probe_media_info(Path(row["path"]))
        if info:
            set_item_media_info(row["id"], **info)
            row = accessible_item(request, item_id)   # re-read with the probed values

    conn = get_conn()
    try:
        if row["type"] == "movie":
            poster = get_poster(*artwork.poster_ref(row))
            detail = _detail_response(row["title"], poster)
            detail["type"] = "movie"
            detail["library"] = row["library"]
            detail["year"] = row["year"]
            detail["facts"] = _detail_facts(detail, item_row=row)
            detail["formats"] = _format_chips(row)
            detail["similar"] = _similar_titles(conn, row["library"], "movie", row["title"], detail["genres"])
            return detail

        show_poster = get_poster("show", row["library"], row["show_title"])
        episode_poster = get_poster(*artwork.still_ref(row))
        detail = _detail_response(
            row["title"],
            show_poster,
            overview=episode_poster["overview"] if episode_poster else None,
            runtime=episode_poster["runtime"] if episode_poster else None,
        )
        detail["type"] = "episode"
        detail["library"] = row["library"]
        # The My List button on an episode's page targets its show, not the
        # episode itself — nobody adds "S02E04" to a watchlist on its own.
        detail["show_title"] = row["show_title"]
        detail["facts"] = _detail_facts(detail, item_row=row)
        detail["formats"] = _format_chips(row)
        return detail
    finally:
        conn.close()


@app.get("/api/detail/show/{library}/{show_title}")
def detail_for_show(library: str, show_title: str, request: Request):
    _library_of_kind(request, library, "show")
    conn = get_conn()
    try:
        episode_count = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE type = 'episode' AND library = ? AND show_title = ?",
            (library, show_title),
        ).fetchone()["c"]
        detail = _detail_response(show_title, get_poster("show", library, show_title))
        detail["type"] = "show"
        detail["library"] = library
        detail["episode_count"] = episode_count
        detail["facts"] = _detail_facts(detail, episode_count=episode_count)
        # No "Available in" panel for a show — it is many files, not one, and
        # there is no single honest answer to "available in what".
        detail["similar"] = _similar_titles(conn, library, "show", show_title, detail["genres"])
        return detail
    finally:
        conn.close()


@app.get("/api/search")
def search(q: str, request: Request):
    """Movies and shows whose title, tagline (overview) or any genre contains
    the query — the same aggregated card shape /api/discover uses, so a show
    is one result here, not one per episode."""
    libraries = allowed_libraries(request)
    q = q.strip().lower()
    if not q or not libraries:
        return []
    conn = get_conn()
    try:
        movie_posters = {
            r["ref"]: r for r in conn.execute(
                "SELECT ref, overview, genres FROM posters WHERE kind = 'movie'"
            )
        }
        show_posters = {
            (r["library"], r["ref"]): r for r in conn.execute(
                "SELECT library, ref, overview, genres FROM posters WHERE kind = 'show'"
            )
        }

        def matches(title, poster):
            haystack = [title]
            if poster:
                if poster["overview"]:
                    haystack.append(poster["overview"])
                if poster["genres"]:
                    haystack.extend(json.loads(poster["genres"]))
            return any(q in h.lower() for h in haystack)

        results = []
        for row in list_libraries(conn):
            library, kind = row["name"], row["kind"]
            if library not in libraries:
                continue
            if kind == "movie":
                for item in conn.execute(
                    "SELECT id, title, year, type, path FROM items WHERE type = 'movie' AND library = ?",
                    (library,),
                ):
                    poster = movie_posters.get(artwork.poster_ref(item)[2])
                    if matches(item["title"], poster):
                        results.append(_card("movie", library, item["title"], item["year"] or "", item["id"]))
            else:
                for show in conn.execute(
                    """SELECT show_title, COUNT(*) episodes FROM items
                       WHERE type = 'episode' AND library = ? GROUP BY show_title""",
                    (library,),
                ):
                    poster = show_posters.get((library, show["show_title"]))
                    if matches(show["show_title"], poster):
                        meta = f"{show['episodes']} episode{'s' if show['episodes'] != 1 else ''}"
                        results.append(_card("show", library, show["show_title"], meta))
        results.sort(key=lambda c: c["title"].lower())
        return results
    finally:
        conn.close()


@app.post("/api/scan")
def trigger_scan(request: Request, library: str = None):
    """Re-index every mapped library, or just one. Admin-only: it walks the pool.

    This one re-parses and rewrites every file it finds — the scan to reach for
    after a rename, or when something in the index looks wrong. For the routine
    "did anything arrive?" pass, use /api/scan/changes.
    """
    require_admin(request)
    if library is not None and get_library(library) is None:
        raise HTTPException(404, f"No library called '{library}'")
    return {"status": "ok", "found": run_full_scan(only=library)}


@app.post("/api/scan/changes")
def trigger_partial_scan(request: Request, library: str = None):
    """Index only what changed since the last scan. Admin-only.

    The cheap one: it walks the same folders but compares each file's path,
    mtime and size against the index and writes only the difference, so a pass
    that finds nothing writes nothing. It deliberately does not re-parse titles
    — a rename still needs the full rescan.
    """
    require_admin(request)
    if library is not None and get_library(library) is None:
        raise HTTPException(404, f"No library called '{library}'")
    results = run_partial_scan(only=library)
    return {"status": "ok", "libraries": results, "totals": summarize(results)}


# ---------- Home: one hero + four shelves per top-level tab ----------
#
# There is no single combined Home any more — Movies, Series and Anime are
# each their own tab, and each gets this same treatment for its own library.
# See /api/tab/{library} below.

HOME_ROW_SIZE = 24
TOP_ROW_SIZE = 10
# How far back "recently added" looks when picking the hero — a title has to
# have shown up in this many most-recently-added before it can be featured,
# so the hero stays close to "new" even though what it actually optimizes for
# is rating.
HERO_RECENCY_POOL = 30


def _card(kind, library, title, meta, item_id=None):
    """One tile, in the shape every shelf and grid renders."""
    # quote() the title: show names contain spaces, and '#' or '&' in one would
    # otherwise truncate or break the URL once it lands in an <img src>.
    poster = (
        f"/api/poster/item/{item_id}" if kind == "movie"
        else f"/api/poster/show/{library}/{quote(title, safe='')}"
    )
    return {"kind": kind, "library": library, "title": title, "meta": meta, "id": item_id,
            "poster": poster}


def _new_releases_for(conn, library, kind, limit):
    """Most recently added titles in one library, by file mtime.

    Shows are grouped to one tile per title, so a freshly grabbed season does
    not bury the shelf under a dozen episodes.
    """
    if kind == "movie":
        return [
            _card("movie", library, row["title"], row["year"] or "", row["id"])
            for row in conn.execute(
                """SELECT id, title, year FROM items
                   WHERE type = 'movie' AND library = ? AND mtime IS NOT NULL
                   ORDER BY mtime DESC LIMIT ?""",
                (library, limit),
            )
        ]
    return [
        _card("show", library, row["show_title"],
              f"{row['episodes']} episode{'s' if row['episodes'] != 1 else ''}")
        for row in conn.execute(
            """SELECT show_title, MAX(mtime) newest, COUNT(*) episodes FROM items
               WHERE type = 'episode' AND library = ? AND mtime IS NOT NULL
               GROUP BY show_title ORDER BY newest DESC LIMIT ?""",
            (library, limit),
        )
    ]


def _rated_items(conn, library, kind):
    """(card, rating, genres, row, poster) for every title in one library that
    has a real Radarr/Sonarr/TMDB rating on file — the pool the shelves below,
    and the Home page's featured rotation, all draw from. Unrated titles are
    left out rather than sorted in as a false zero. `row` is the raw item (a
    movie) or show-group row and `poster` its posters-table record — kept
    alongside `card` so a hero can be built from a pick here without a second
    trip to the database for the same title."""
    entries = []
    if kind == "movie":
        posters = {
            r["ref"]: r for r in conn.execute(
                "SELECT ref, rating, genres FROM posters WHERE kind = 'movie'"
            )
        }
        for item in conn.execute(
            "SELECT id, title, year, type, path FROM items WHERE type = 'movie' AND library = ?",
            (library,),
        ):
            poster = posters.get(artwork.poster_ref(item)[2])
            if not poster or poster["rating"] is None:
                continue
            genres = json.loads(poster["genres"]) if poster["genres"] else []
            card = _card("movie", library, item["title"], item["year"] or "", item["id"])
            entries.append((card, poster["rating"], genres, item, poster))
    else:
        posters = {
            r["ref"]: r for r in conn.execute(
                "SELECT ref, rating, genres FROM posters WHERE kind = 'show' AND library = ?",
                (library,),
            )
        }
        for show in conn.execute(
            """SELECT show_title, COUNT(*) episodes FROM items
               WHERE type = 'episode' AND library = ? GROUP BY show_title""",
            (library,),
        ):
            poster = posters.get(show["show_title"])
            if not poster or poster["rating"] is None:
                continue
            genres = json.loads(poster["genres"]) if poster["genres"] else []
            meta = f"{show['episodes']} episode{'s' if show['episodes'] != 1 else ''}"
            card = _card("show", library, show["show_title"], meta)
            entries.append((card, poster["rating"], genres, show, poster))
    return entries


def _top_rated_for(conn, library, kind, limit):
    """The `limit` highest-rated titles in one library."""
    entries = sorted(_rated_items(conn, library, kind), key=lambda e: e[1], reverse=True)
    return [card for card, rating, genres, *_ in entries[:limit]]


def _recommended_for(conn, library, kind, limit, exclude_titles):
    """One highest-rated title per distinct genre this library has, skipping
    anything already on the Top 10 shelf — a genre-diverse row built from the
    same real ratings, not an editorial pick nobody made."""
    entries = [e for e in _rated_items(conn, library, kind) if e[0]["title"] not in exclude_titles]
    best_per_genre = {}
    for card, rating, genres, *_ in entries:
        for genre in genres:
            if genre not in best_per_genre or rating > best_per_genre[genre][1]:
                best_per_genre[genre] = (card, rating)
    picks = sorted(best_per_genre.values(), key=lambda pair: pair[1], reverse=True)
    seen, result = set(), []
    for card, rating in picks:
        if card["title"] in seen:
            continue
        seen.add(card["title"])
        result.append(card)
        if len(result) >= limit:
            break
    return result


def _first_episode_id(conn, library, show_title):
    row = conn.execute(
        """SELECT id FROM items WHERE type = 'episode' AND library = ? AND show_title = ?
           ORDER BY season IS NULL, season, episode IS NULL, episode LIMIT 1""",
        (library, show_title),
    ).fetchone()
    return row["id"] if row else None


def _hero_from(conn, kind, library, label, item, poster, tag="Featured"):
    """One hero-shaped entry from an already-fetched item (movie) or show-group
    row and its posters-table record — the object both _hero_for()'s single
    recency-weighted pick and _top_rated_heroes_for()'s rotation pool return,
    factored out so building it doesn't have to be repeated per caller.
    """
    if kind == "movie":
        detail = _detail_response(item["title"], poster)
        return {
            "kind": "movie", "id": item["id"], "library": library,
            "title": item["title"], "year": item["year"],
            "meta": _clock(detail["runtime"] * 60) if detail["runtime"] else None,
            "rating": detail["rating"], "synopsis": detail["overview"],
            "badge": f"{label} · {tag}",
            "poster": f"/api/poster/item/{item['id']}",
        }
    detail = _detail_response(item["show_title"], poster)
    return {
        "kind": "show", "id": _first_episode_id(conn, library, item["show_title"]),
        "library": library, "title": item["show_title"], "year": None,
        "meta": f"{item['episodes']} episode{'s' if item['episodes'] != 1 else ''}",
        "rating": detail["rating"], "synopsis": detail["overview"],
        "badge": f"{label} · {tag}",
        "poster": f"/api/poster/show/{library}/{quote(item['show_title'], safe='')}",
    }


def _hero_for(conn, library, kind, label):
    """The tab's featured title: the highest-rated among its most recently
    added, so the hero stays close to "new" without simply being "newest".
    Falls back to the single most recent title when nothing in the pool has a
    rating yet — a library that just started scanning still gets a hero.
    """
    if kind == "movie":
        recent = conn.execute(
            """SELECT id, title, year, type, path FROM items
               WHERE type = 'movie' AND library = ? AND mtime IS NOT NULL
               ORDER BY mtime DESC LIMIT ?""",
            (library, HERO_RECENCY_POOL),
        ).fetchall()
        best, best_poster = None, None
        for item in recent:
            poster = get_poster(*artwork.poster_ref(item))
            if poster and poster["rating"] is not None:
                if best_poster is None or poster["rating"] > best_poster["rating"]:
                    best, best_poster = item, poster
        if best is None and recent:
            best = recent[0]
            best_poster = get_poster(*artwork.poster_ref(best))
        if best is None:
            return None
        return _hero_from(conn, "movie", library, label, best, best_poster)

    recent = conn.execute(
        """SELECT show_title, MAX(mtime) newest, COUNT(*) episodes FROM items
           WHERE type = 'episode' AND library = ? AND mtime IS NOT NULL
           GROUP BY show_title ORDER BY newest DESC LIMIT ?""",
        (library, HERO_RECENCY_POOL),
    ).fetchall()
    best, best_poster = None, None
    for show in recent:
        poster = get_poster("show", library, show["show_title"])
        if poster and poster["rating"] is not None:
            if best_poster is None or poster["rating"] > best_poster["rating"]:
                best, best_poster = show, poster
    if best is None and recent:
        best = recent[0]
        best_poster = get_poster("show", library, best["show_title"])
    if best is None:
        return None
    return _hero_from(conn, "show", library, label, best, best_poster)


def _top_rated_heroes_for(conn, library, kind, label, limit):
    """The `limit` highest-rated titles in one library, as hero-shaped
    entries — the pool the Home page's featured rotation draws from, instead
    of _hero_for()'s single recency-weighted pick.

    Re-fetches each poster in full rather than reusing _rated_items()'s copy:
    that one only selects (ref, rating, genres) — enough for a Top 10/
    Recommended card — but _hero_from() needs the overview/runtime/cast a
    hero also shows, the same full row _hero_for() already fetches via
    get_poster().
    """
    entries = sorted(_rated_items(conn, library, kind), key=lambda e: e[1], reverse=True)[:limit]
    heroes = []
    for _, _, _, item, _narrow_poster in entries:
        poster = (get_poster(*artwork.poster_ref(item)) if kind == "movie"
                  else get_poster("show", library, item["show_title"]))
        heroes.append(_hero_from(conn, kind, library, label, item, poster, tag="Top Rated"))
    return heroes


@app.get("/api/tab/{library}")
def tab_home(library: str, request: Request):
    """Hero + four shelves for one top-level content tab."""
    row = get_library(library)
    if row is None:
        raise HTTPException(404, f"No library called '{library}'")
    require_library(request, library)
    kind = row["kind"]
    conn = get_conn()
    try:
        top10 = _top_rated_for(conn, library, kind, TOP_ROW_SIZE)
        exclude_titles = {card["title"] for card in top10}
        return {
            "hero": _hero_for(conn, library, kind, row["label"]),
            "continue": _continue_cards(conn, request, libraries={library}),
            "new": _new_releases_for(conn, library, kind, HOME_ROW_SIZE),
            "top10": top10,
            "recommended": _recommended_for(conn, library, kind, HOME_ROW_SIZE, exclude_titles),
        }
    finally:
        conn.close()


# How many of each Home draws into the featured rotation, and how long the
# frontend (see loadHome()/renderHomeHero() in app.js) shows each one before
# advancing — the rotation itself is client-side, against this one fetch.
HOME_HERO_COUNT = 5
HOME_HERO_ROTATE_MS = 15000


@app.get("/api/home")
def home_tab(request: Request):
    """Continue Watching + a rotating featured pool (top 5 rated movies, top
    5 rated series) + one Recently Added shelf per accessible library — the
    aggregate landing page across every library at once, the same shape
    /api/tab/{library} builds for a single one.
    """
    conn = get_conn()
    try:
        allowed = allowed_libraries(request)
        libraries = [row for row in list_libraries(conn) if row["name"] in allowed]
        heroes = []
        if "movies" in allowed:
            heroes += _top_rated_heroes_for(
                conn, "movies", "movie", get_library("movies")["label"], HOME_HERO_COUNT)
        if "series" in allowed:
            heroes += _top_rated_heroes_for(
                conn, "series", "show", get_library("series")["label"], HOME_HERO_COUNT)
        if not heroes:
            # Nothing rated yet on Movies/Series (or the account can see
            # neither) — fall back to whatever's freshest across every
            # accessible library rather than leaving the hero area blank.
            for row in libraries:
                candidate = _hero_for(conn, row["name"], row["kind"], row["label"])
                if candidate:
                    heroes.append(candidate)
        return {
            "heroes": heroes,
            "hero_rotate_ms": HOME_HERO_ROTATE_MS,
            "continue": _continue_cards(conn, request),
            "shelves": [
                {"library": row["name"], "label": row["label"],
                 "items": _new_releases_for(conn, row["name"], row["kind"], HOME_ROW_SIZE)}
                for row in libraries
            ],
        }
    finally:
        conn.close()


@app.get("/api/watched")
def watched(request: Request):
    """What this account has already watched, in the three identities the cards
    are keyed by: item ids for films and episodes, [library, show] pairs for a
    show whose every episode is watched, and the names of collections whose
    every film is watched.

    "Watched" means played to at least WATCHED_PERCENT of the runtime — a title
    that was started and abandoned is in the history but wears no check mark.

    One call decorates every grid. The alternative — a per-user `watched` field
    on each listing endpoint — would have to be recomputed on every listing and
    would still leave the front end unable to update a mark after playback
    without refetching the whole grid.
    """
    key = viewer_key(request)
    libraries = allowed_libraries(request)
    conn = get_conn()
    try:
        played = watched_item_ids(key, libraries, conn)
        shows = [[library, title]
                 for library, title in watched_shows(key, conn)
                 if library in libraries]
        collections = []
        if "movies" in libraries:
            collections = [
                name for name, entry in _collections(conn).items()
                if len(entry["movies"]) >= MIN_COLLECTION_SIZE
                and all(movie["id"] in played for movie in entry["movies"])
            ]
        return {"items": sorted(played), "shows": shows, "collections": collections,
                "threshold": WATCHED_PERCENT}
    finally:
        conn.close()


# ---------- My List ----------

class ListToggleRequest(BaseModel):
    kind: str            # 'movie' or 'show'
    library: str
    ref: str             # movie: item id as a string; show: its title


@app.post("/api/list/toggle")
def list_toggle(body: ListToggleRequest, request: Request):
    """Add or remove one title from My List — one button, one call, the
    server decides which direction since it is the one source of truth for
    what is already in there."""
    if body.kind not in ("movie", "show"):
        raise HTTPException(400, "kind must be 'movie' or 'show'")
    require_library(request, body.library)
    in_list = toggle_list_entry(viewer_key(request), body.kind, body.library, body.ref)
    return {"in_list": in_list}


@app.get("/api/list")
def get_list(request: Request):
    """This account's My List: the membership sets a card needs to know
    whether to offer "Add" or "Remove" (same shape as /api/watched, for the
    same reason), plus the full cards the My List tab itself renders.

    A row whose title no longer exists in the index (deleted, or a library
    that went away) is dropped rather than shown broken — the DELETE never
    happened because nothing tells this table when a scan removes a title.
    """
    libraries = allowed_libraries(request)
    conn = get_conn()
    try:
        rows = list_entries(viewer_key(request), libraries, conn)
        items, shows, cards = [], [], []
        for row in rows:
            if row["kind"] == "movie":
                item_row = conn.execute(
                    "SELECT id, title, year FROM items WHERE id = ? AND type = 'movie'",
                    (row["ref"],),
                ).fetchone()
                if item_row is None:
                    continue
                items.append(item_row["id"])
                cards.append(_card("movie", "movies", item_row["title"],
                                    item_row["year"] or "", item_row["id"]))
            else:
                count_row = conn.execute(
                    """SELECT COUNT(*) episodes FROM items
                       WHERE type = 'episode' AND library = ? AND show_title = ?""",
                    (row["library"], row["ref"]),
                ).fetchone()
                if not count_row["episodes"]:
                    continue
                shows.append([row["library"], row["ref"]])
                meta = f"{count_row['episodes']} episode{'s' if count_row['episodes'] != 1 else ''}"
                cards.append(_card("show", row["library"], row["ref"], meta))
        return {"items": items, "shows": shows, "cards": cards}
    finally:
        conn.close()


# ---------- Continue Watching ----------

def _clock(seconds) -> str:
    """A duration as "1h 24m" or "18m" — how long is left, not a timestamp."""
    minutes = int(round((seconds or 0) / 60))
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


def _continue_meta(entry) -> str:
    """The line under a Continue Watching tile: which episode, and how much of
    it is left. A next-up entry says so rather than claiming a position."""
    parts = []
    if entry["season"] is not None and entry["episode"] is not None:
        parts.append(f"S{entry['season']:02d}E{entry['episode']:02d}")
    elif entry["kind"] == "episode":
        parts.append(entry["title"])
    if not entry["resume"]:
        parts.append("Next up")
    elif entry["duration"]:
        parts.append(f"{_clock(entry['duration'] - entry['position'])} left")
    return " \u00b7 ".join(parts)


def _continue_cards(conn, request, libraries=None):
    """Continue Watching, in the shape the shelf renders.

    Episodes are shown under their show's poster rather than their own banner:
    the shelf is portrait tiles like every other one on Home, and what you are
    coming back to is the series, not the frame the banner was cut from.

    `libraries` narrows the shelf to one tab's library instead of everything
    the account can see — the per-tab Home endpoint passes a single-library
    set; /api/continue below leaves it at the default.
    """
    if libraries is None:
        libraries = allowed_libraries(request)
    entries = continue_watching(viewer_key(request), libraries, conn=conn)
    cards = []
    for entry in entries:
        title = entry["show_title"] or entry["title"]
        poster = (
            f"/api/poster/item/{entry['id']}" if entry["kind"] == "movie"
            else f"/api/poster/show/{entry['library']}/{quote(title, safe='')}"
        )
        cards.append({
            "kind": entry["kind"], "id": entry["id"], "library": entry["library"],
            "title": title, "meta": _continue_meta(entry), "poster": poster,
            "position": round(entry["position"], 3),
            "duration": entry["duration"],
            # The bar under the tile. A next-up episode sits at zero, which
            # reads as "not started" rather than as an empty tile.
            "percent": round(min(entry["progress"], 1.0) * 100, 1),
            "resume": entry["resume"],
        })
    return cards


@app.get("/api/continue")
def continue_shelf(request: Request):
    """Films part-watched, and each show's next episode. See continue_watching()."""
    conn = get_conn()
    try:
        return {"items": _continue_cards(conn, request)}
    finally:
        conn.close()


@app.delete("/api/continue/{item_id}")
def remove_from_continue(item_id: int, request: Request):
    """Drop one entry from Continue Watching.

    `item_id` is the card's own id — for a show that may be the next unwatched
    episode, which has no play row of its own; remove_continue_entry() re-derives
    the one actually responsible for the show's spot in the queue.
    """
    row = accessible_item(request, item_id)
    show_title = row["show_title"] if row["type"] == "episode" else None
    removed = remove_continue_entry(viewer_key(request), row["library"], show_title, item_id)
    if not removed:
        raise HTTPException(404, "Not in Continue Watching")
    return {"status": "ok"}


@app.post("/api/progress")
def report_progress(payload: PlayProgress, request: Request):
    """How far into a title the player has got, posted on a timer while it runs.

    The runtime it is measured against is decided here, not by the player: in
    transcode mode the browser only knows the slice of the stream ffmpeg has
    written so far, so a film ten minutes in would look finished. ffprobe the
    file once, cache it on the item, and use the player's number only if that
    fails.
    """
    row = accessible_item(request, payload.item_id)

    duration = row["duration_seconds"]
    if not duration or duration <= 0:
        duration = transcoder.probe_duration(Path(row["path"]))
        if duration:
            set_item_duration(row["id"], duration)
    if not duration or duration <= 0:
        duration = payload.duration          # unprobeable file: trust the player
    if not duration or duration <= 0:
        return {"progress": 0.0, "duration": None, "finished": False}

    progress = record_progress(
        viewer_key(request), payload.item_id, payload.position, duration
    )
    return {
        "progress": progress,
        # Handed back because this is where an unprobed title first gets a
        # runtime, and the player has no other way to learn it in transcode mode.
        "duration": duration,
        "finished": progress >= RESUME_FINISHED_PERCENT / 100,
    }


@app.get("/api/resume/{item_id}")
def resume_point(item_id: int, request: Request):
    """Where this account left off in one title, for the player to seek to.

    Asked once per open rather than carried on the listings: only the item
    actually being played needs it, and it has to be the live number — the same
    title may have been watched further on another device since the grid loaded.
    """
    row = accessible_item(request, item_id)
    # Same lazy-probe-once pattern as the detail page (detail_for_item): most
    # titles are already probed by the time this fires, since 1.37's pre-play
    # popup opens first for everything except a Continue Watching resume.
    if row["resolution"] is None:
        info = transcoder.probe_media_info(Path(row["path"]))
        if info:
            set_item_media_info(row["id"], **info)
            row = accessible_item(request, item_id)
    position = resume_position(viewer_key(request), item_id)
    return {"item_id": item_id, "position": position,
            "duration": row["duration_seconds"], "resume": position > 0,
            # Only an episode has an intro to skip; a movie gets null and the
            # player never shows the button.
            "skip_intro_seconds": SKIP_INTRO_SECONDS if row["type"] == "episode" else None,
            "audio_incompatible": row["acodec"] in _BROWSER_INCOMPATIBLE_AUDIO}


# ---------- Live: who is watching right now ----------

@app.get("/api/watching")
def watching_now(request: Request):
    """Everyone currently playing something, for the admin Live view.

    Built from the media requests themselves rather than anything the player
    reports — see app/watching.py. Titles are joined on here rather than held
    in the registry so a rename or a re-scan cannot leave it showing a stale
    name, and so the registry stays a pure record of who asked for what.
    """
    require_admin(request)
    viewings = watching.active()
    if not viewings:
        return {"watching": [], "stale_after": watching.STALE_AFTER}

    ids = [v["item_id"] for v in viewings]
    conn = get_conn()
    try:
        items = {
            row["id"]: row
            for row in conn.execute(
                f"""SELECT i.*, COALESCE(p.title, i.title) AS display_title
                    FROM items i
                    LEFT JOIN posters p
                           ON p.kind = 'episode' AND p.library = i.library
                          AND p.ref = i.path
                    WHERE i.id IN ({_placeholders(ids)})""", ids
            )
        }
    finally:
        conn.close()

    out = []
    for viewing in viewings:
        item = items.get(viewing["item_id"])
        if item is None:
            continue                      # unindexed mid-viewing; nothing to show
        if item["type"] == "episode":
            title = item["show_title"] or item["title"]
            subtitle = " · ".join(
                part for part in (
                    (f"S{item['season']:02d}E{item['episode']:02d}"
                     if item["season"] is not None and item["episode"] is not None else ""),
                    item["display_title"],
                ) if part
            )
        else:
            title = item["title"]
            subtitle = item["year"] or ""
        out.append({**viewing, "title": title, "subtitle": subtitle,
                    "library": item["library"],
                    "poster": f"/api/poster/item/{item['id']}"})
    return {"watching": out, "stale_after": watching.STALE_AFTER}


@app.get("/api/plays")
def my_plays(request: Request):
    """Your own play history.

    Nothing on the Home tab reads it any more — the shelves are purely "recently
    added" — but playback is still recorded, so the history stays available.
    """
    libraries = allowed_libraries(request)
    return [play for play in recent_plays(viewer_key(request)) if play["library"] in libraries]


# ---------- Collections (movies only, sourced from Radarr) ----------

# A "collection" of one is just the film — Radarr reports the whole TMDB set
# even when a single entry is on disk, and listing 120 of those would bury the
# 30 real ones. Drop this to 1 to show every set Radarr knows about.
MIN_COLLECTION_SIZE = 2


def _movie_refs(conn):
    """ref -> movie row, for joining poster rows back to a playable item.

    A poster's `ref` is the movie's folder (see artwork._movie_ref), which is
    what the collection rows are keyed by too.
    """
    refs = {}
    for row in conn.execute("SELECT id, type, title, year, path FROM items WHERE type='movie'"):
        refs.setdefault(artwork.poster_ref(row)[2], row)
    return refs


def _collections(conn):
    """Collection name -> the movies from it that are actually in the library."""
    refs = _movie_refs(conn)
    grouped = {}
    for row in conn.execute(
        """SELECT ref, collection, collection_tmdb_id FROM posters
           WHERE kind='movie' AND library='movies' AND collection IS NOT NULL
           ORDER BY collection"""
    ):
        item = refs.get(row["ref"])
        if item is None:                   # poster row for a since-deleted file
            continue
        entry = grouped.setdefault(
            row["collection"], {"tmdb_id": row["collection_tmdb_id"], "movies": []}
        )
        entry["movies"].append(item)
    for entry in grouped.values():
        # Chronological reads as the watch order for a franchise.
        entry["movies"].sort(key=lambda item: (item["year"] or "", item["title"]))
    return grouped


@app.get("/api/collections")
def list_collections(request: Request):
    """Movie collections with at least MIN_COLLECTION_SIZE films on disk."""
    require_library(request, "movies")
    conn = get_conn()
    try:
        grouped = _collections(conn)
    finally:
        conn.close()

    return [
        {
            "name": name,
            "tmdb_id": entry["tmdb_id"],
            "count": len(entry["movies"]),
            # The oldest film's artwork stands in for the set, the way a box set
            # is filed under its first entry.
            "poster": f"/api/poster/item/{entry['movies'][0]['id']}",
        }
        for name, entry in sorted(grouped.items())
        if len(entry["movies"]) >= MIN_COLLECTION_SIZE
    ]


@app.get("/api/collection")
def get_collection(name: str, request: Request):
    """The films in one collection. `name` is a query parameter rather than a
    path segment because TMDB collection titles contain '/' and '&'."""
    require_library(request, "movies")
    conn = get_conn()
    try:
        entry = _collections(conn).get(name)
    finally:
        conn.close()
    if entry is None:
        raise HTTPException(404, "No such collection")
    return [
        {"id": item["id"], "title": item["title"], "year": item["year"]}
        for item in entry["movies"]
    ]


# ---------- Poster artwork ----------

def _poster_response(kind: str, library: str, ref: str):
    row = get_poster(kind, library, ref)
    if not row:
        raise HTTPException(404, "No poster for this title")
    path = Path(row["file"])
    if not path.exists():
        raise HTTPException(404, "Poster file missing on disk")
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/poster/item/{item_id}")
def poster_for_item(item_id: int, request: Request):
    """Poster for one item — a movie's own, or its show's for an episode."""
    return _poster_response(*artwork.poster_ref(accessible_item(request, item_id)))


@app.get("/api/poster/show/{library}/{show_title}")
def poster_for_show(library: str, show_title: str, request: Request):
    _library_of_kind(request, library, "show")
    return _poster_response("show", library, show_title)


@app.get("/api/subtitles/{item_id}")
def list_subtitles(item_id: int, request: Request):
    """Subtitle tracks sitting beside this item's video file.

    Discovered per request rather than indexed: Bazarr drops files in long after
    a scan, and a directory listing is cheap next to the video that follows it.
    """
    row = accessible_item(request, item_id)
    return [{"index": t["index"], "label": t["label"], "lang": t["lang"],
             "url": f"/api/subtitles/{item_id}/{t['index']}"}
            for t in subtitles.find_tracks(row["path"])]


@app.get("/api/subtitles/{item_id}/{index}")
def get_subtitle(item_id: int, index: int, request: Request):
    """One track, converted to WebVTT — the only thing <track> will take.

    The index selects from the list the server just built, so no filename from
    the client ever reaches the filesystem.
    """
    row = accessible_item(request, item_id)
    tracks = subtitles.find_tracks(row["path"])
    if not 0 <= index < len(tracks):
        raise HTTPException(404, "No such subtitle track")
    track = tracks[index]
    try:
        if track["kind"] == "sidecar":
            body = subtitles.to_vtt(track["path"])
        else:
            body = subtitles.muxed_to_vtt(row["path"], track["stream_index"], track["codec"])
    except OSError:
        raise HTTPException(404, "Subtitle file could not be read")
    return PlainTextResponse(body, media_type="text/vtt",
                             headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/still/item/{item_id}")
def still_for_item(item_id: int, request: Request):
    """One episode's own banner. 404 when it has none — the grid asks only for
    the episodes /api/shows/{show}/episodes said were worth asking about."""
    row = accessible_item(request, item_id)
    if row["type"] != "episode":
        raise HTTPException(404, "Only episodes have banners")
    return _poster_response(*artwork.still_ref(row))


@app.post("/api/artwork/sync")
def artwork_sync(request: Request, force: bool = False):
    """Re-match every title against Radarr/Sonarr. force=true re-downloads
    images that are already cached. Admin-only, like the rescan it pairs with."""
    require_admin(request)
    return artwork.sync_artwork(force=force)


@app.get("/api/artwork/status")
def artwork_status():
    return poster_counts()


# ---------- Streaming ----------

RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


@app.get("/stream/{item_id}")
def stream(item_id: int, request: Request):
    row = accessible_item(request, item_id)
    user = current_user(request)
    record_play(viewer_key(request), item_id)

    file_path = Path(row["path"])
    if not file_path.exists():
        raise HTTPException(404, "File missing on disk")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    start = 0
    end = file_size - 1

    if range_header:
        match = RANGE_RE.match(range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1

    end = min(end, file_size - 1)
    length = end - start + 1

    # Every range request is a sign of life for the Live view, and where in the
    # file it asks for is the only position signal direct play offers.
    watching.note(user["username"], item_id, row, mode="direct",
                  client=_client_host(request), byte_start=start, file_size=file_size)

    def iterfile():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    ext = file_path.suffix.lower()
    media_type = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
    }.get(ext, "application/octet-stream")

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    status_code = 206 if range_header else 200
    return StreamingResponse(iterfile(), status_code=status_code, headers=headers, media_type=media_type)


# ---------- Transcoding (on-demand HLS, ffmpeg) ----------

def _get_item_path(item_id: int, request: Request) -> Path:
    path = Path(accessible_item(request, item_id)["path"])
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return path


# The start offset (and the audio track) ride in the path rather than a query
# string because the playlist ffmpeg writes names its segments relatively —
# "seg_00000.ts" — so the browser resolves them against the playlist's own URL
# and lands in the right session on its own. A query parameter would be
# dropped on the way (resolving a relative URL against a base drops the
# base's own query string, so a later segment fetch would lose it).
@app.get("/transcode/{item_id}/{quality}/{start}/{audio}/playlist.m3u8")
def transcode_playlist(item_id: int, quality: str, start: int, audio: int, request: Request):
    if quality not in transcoder.QUALITY_PRESETS:
        raise HTTPException(400, f"Unknown quality '{quality}'. Options: {list(transcoder.QUALITY_PRESETS)}")
    if start < 0:
        raise HTTPException(400, "Start offset cannot be negative")
    if audio < 0:
        raise HTTPException(400, "Invalid audio track")

    row = accessible_item(request, item_id)
    # A start past the end leaves ffmpeg with nothing to encode, and the request
    # would sit out wait_for_playlist()'s timeout before failing with a log tail
    # that explains nothing. Say what is wrong instead. Also what
    # build_vod_playlist() below needs to synthesize the whole session's
    # segment list up front — probe it directly if the library scan never
    # captured it, rather than falling back to ffmpeg's own live-style
    # playlist (see that function's docstring for why that's worth avoiding).
    runtime = row["duration_seconds"]
    source_path = Path(row["path"])
    if not runtime:
        runtime = transcoder.probe_duration(source_path)
    if runtime and start >= runtime:
        raise HTTPException(400, f"Start offset {start}s is past the end of this title")

    if not source_path.exists():
        raise HTTPException(404, "File missing on disk")
    user = current_user(request)
    record_play(viewer_key(request), item_id)
    watching.note(user["username"], item_id, row, mode=quality,
                  client=_client_host(request), segment=0, segment_base=start)
    work_dir = transcoder.start_transcode(item_id, quality, source_path, start, audio)

    if not transcoder.wait_for_playlist(work_dir):
        log_tail = transcoder.get_last_log_lines(work_dir)
        raise HTTPException(
            503,
            f"Transcode failed to start. ffmpeg log tail:\n{log_tail}"
            if log_tail else "Transcode did not start in time — try again",
        )

    return PlainTextResponse(
        transcoder.build_vod_playlist(runtime, start),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/transcode/{item_id}/{quality}/{start}/{audio}/{segment_name}")
def transcode_segment(item_id: int, quality: str, start: int, audio: int, segment_name: str,
                      request: Request):
    row = accessible_item(request, item_id)   # segments are the media itself
    if quality not in transcoder.QUALITY_PRESETS:
        raise HTTPException(400, "Unknown quality")
    if start < 0:
        raise HTTPException(400, "Invalid start offset")
    if audio < 0:
        raise HTTPException(400, "Invalid audio track")
    # Prevent path traversal — only allow the exact segment filenames ffmpeg produces
    if not re.fullmatch(r"seg_\d{5}\.ts", segment_name):
        raise HTTPException(400, "Invalid segment name")

    # Which segment was asked for *is* the playhead, to within six seconds — but
    # counted from where this session started, not from the top of the film.
    watching.note(current_user(request)["username"], item_id, row, mode=quality,
                  client=_client_host(request),
                  segment=int(segment_name[4:9]), segment_base=start)
    transcoder.touch(item_id, quality, start, audio)
    work_dir = transcoder._session_dir(item_id, quality, start, audio)
    segment_path = work_dir / segment_name
    if not segment_path.exists():
        # The synthesized playlist (build_vod_playlist() in transcoder.py)
        # lists every segment up front, before ffmpeg has necessarily written
        # it yet — a client that (correctly, now) believes this is VOD can
        # legitimately ask for segment N the instant it appears in the
        # playlist. At 10-13x realtime encode speed this wait is normally a
        # fraction of a second.
        transcoder.wait_for_segment(segment_path)
    if not segment_path.exists():
        raise HTTPException(404, "Segment not ready yet")

    # Without this, FileResponse falls back to its default ETag/Last-Modified
    # headers alone — no explicit Cache-Control — which a browser can and does
    # cache heuristically. seg_00000.ts (etc.) is the same URL across *any*
    # session that shares this item/quality/start/audio tuple, and "start" is
    # a rounded second that resuming near the same spot, or re-picking a
    # quality or audio track at a similar position, hits again often. Without
    # this header, a browser can silently keep serving an *old* session's
    # already-cached segment instead of fetching the new one — indistinguishable
    # from the switch never having happened at all. Same fix transcode_playlist
    # already has, for the same reason.
    return FileResponse(segment_path, media_type="video/mp2t", headers={"Cache-Control": "no-cache"})


@app.get("/api/audio/{item_id}")
def list_audio_tracks(item_id: int, request: Request):
    """Audio tracks embedded in this item's video file — same idea as
    /api/subtitles/{item_id}, but there is nothing to convert: the index a
    client picks here goes straight into the transcode URL's own path (see
    the comment above transcode_playlist) rather than a separate fetch."""
    row = accessible_item(request, item_id)
    return transcoder.audio_tracks(row["path"])


# ---------- Static web UI (mounted last so it doesn't shadow /api and /stream) ----------

class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles sends an ETag but no Cache-Control, so browsers fall back to
    *heuristic* caching and will happily serve a stale app.js for hours without
    ever asking — a UI change then looks like it simply didn't work. `no-cache`
    means "revalidate", not "don't cache": the ETag still turns almost every
    request into a cheap 304. Posters are served by their own route above and
    keep their long cache lifetime.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# The UI is a single page that moves between tabs with history.pushState
# (see activateTab()/tabFromUrl() in app.js) rather than a real route per tab
# — so a hard refresh, a bookmark, or a shared link for e.g. "/movies" is a
# path StaticFiles above has no file for, and would otherwise just 404. Any
# unmatched GET that isn't one of this app's own real (non-UI) routes gets
# index.html instead, so the client-side router gets a chance to read the URL
# and land on the right tab itself.
@app.exception_handler(404)
async def spa_fallback(request: Request, exc: HTTPException):
    if request.method == "GET" and not request.url.path.startswith(
        ("/api/", "/stream/", "/transcode/")
    ):
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Not found"}, status_code=404)


app.mount("/", RevalidatingStaticFiles(directory="static", html=True), name="static")
