"""Auth: PBKDF2 password hashes, TOTP second factor, server-side sessions.

Deliberately stdlib-only — the image pins fastapi and uvicorn and nothing else,
and PBKDF2-HMAC-SHA256 at OWASP's iteration count does not need a dependency.
Sessions live in SQLite rather than in a signed cookie so that logging out (or
deleting a user) revokes access immediately instead of waiting for expiry. Only
a hash of each session token is stored, so a readable database is not a fistful
of live cookies — the same reason passwords are not kept either.

Signing in is two steps and so is the state behind it: a correct password buys
a short-lived row in `pending_logins`, and only a valid six-digit code (or a
backup code) turns that into a real session. Every account must enrol — there
is no per-user opt-out, because a media server that can be reached from outside
the house is exactly the thing a reused password gets found on.
"""

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import totp
from .database import get_conn, library_names

ITERATIONS = 600_000
SESSION_COOKIE = "mms_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

# The active profile, picked after a real sign-in. Session-length rather than
# its own expiry: switching profiles is meant to be as cheap as switching
# tabs, not something that re-prompts on its own schedule.
PROFILE_COOKIE = "mms_profile"
PROFILE_DAYS = SESSION_DAYS
# A tile row on the picker, not a subscription tier — kept small on purpose.
PROFILE_LIMIT = 5

# The cookie carrying a half-finished sign-in, and how long the second step may
# take. Long enough to find the phone and open the app, short enough that a
# password typed on a borrowed machine is not a standing invitation.
PENDING_COOKIE = "mms_pending"
PENDING_MINUTES = int(os.environ.get("PENDING_LOGIN_MINUTES", "10"))

BACKUP_CODE_COUNT = 10
# No 0/o/1/l/i — these get written on paper and typed back months later.
BACKUP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
BACKUP_CODE_LENGTH = 12          # ~59 bits, which is a hash worth storing plain

# Failed logins are throttled per username+IP. In-process and deliberately
# simple: this is a LAN app, the point is to make guessing tedious.
_FAILURES = {}
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 60


def hash_token(token: str) -> str:
    """What actually goes in the sessions table.

    A plain SHA-256, not PBKDF2: the input is 256 bits of `secrets` output, not
    a guessable human password, so there is nothing for an attacker to iterate
    through and no reason to make every request pay for stretching.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${base64.b64encode(derived).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return secrets.compare_digest(base64.b64encode(derived).decode(), expected)


# ---------- library access ----------

def normalise_libraries(libraries) -> list:
    """Validate a requested set of libraries and return it in a stable order.

    None means "everything" — that is what an unspecified grant has always
    meant here, and it keeps the API backwards-compatible with callers written
    before access control existed.
    """
    known = library_names()
    if libraries is None:
        return list(known)
    unknown = sorted(set(libraries) - set(known))
    if unknown:
        raise ValueError(f"unknown library: {', '.join(unknown)}")
    return [library for library in known if library in set(libraries)]


def set_user_libraries(username: str, libraries) -> list:
    """Replace a user's grants wholesale. Returns what they now have."""
    granted = normalise_libraries(libraries)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM user_libraries WHERE username = ?", (username,))
        conn.executemany(
            "INSERT INTO user_libraries (username, library) VALUES (?, ?)",
            [(username, library) for library in granted],
        )
        conn.commit()
    finally:
        conn.close()
    return granted


def user_libraries(username: str) -> list:
    """The libraries granted to one user, in sidebar order."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT library FROM user_libraries WHERE username = ?", (username,)
        ).fetchall()
    finally:
        conn.close()
    granted = {row["library"] for row in rows}
    return [library for library in library_names() if library in granted]


def libraries_for(user) -> list:
    """What a signed-in user may browse. Admins always get all of it."""
    if user is None:
        return []
    if user["role"] == "admin":
        return library_names()          # including any mapped after they signed in
    return user_libraries(user["username"])


# ---------- users ----------

def create_user(username: str, password: str, role: str = "viewer",
                libraries=None) -> dict:
    """Create an account with access to `libraries` (all of them if omitted).

    Admins are stored with every library: they can grant themselves any of it
    through the user API anyway, so a restricted admin would only be a
    misleading row in the table.
    """
    if role not in ("admin", "viewer"):
        raise ValueError("role must be 'admin' or 'viewer'")
    if not username or not password:
        raise ValueError("username and password are both required")
    granted = library_names() if role == "admin" else normalise_libraries(libraries)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hash_password(password), role),
        )
        conn.executemany(
            "INSERT INTO user_libraries (username, library) VALUES (?, ?)",
            [(username, library) for library in granted],
        )
        # init_db()'s default-profile seeding only runs at startup, so an
        # account created here (the app already running) needs its own — same
        # reasoning as user_libraries just above.
        conn.execute("INSERT INTO profiles (username, name) VALUES (?, ?)", (username, username))
        conn.commit()
    finally:
        conn.close()
    return {"username": username, "role": role, "libraries": granted}


def set_password(username: str, password: str, keep_token: str = None) -> bool:
    """Change an account's password and drop its other sessions.

    Revoking the rest is the point of the change as often as not: someone who
    thinks their password has leaked would otherwise leave every cookie cut from
    it valid for the remaining SESSION_DAYS. `keep_token` spares the session
    doing the changing, so nobody signs themselves out by tidying up.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = datetime('now') WHERE username = ?",
            (hash_password(password), username),
        )
        if cur.rowcount:
            if keep_token:
                conn.execute("DELETE FROM sessions WHERE username = ? AND token != ?",
                             (username, hash_token(keep_token)))
            else:
                conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            # A pending login was bought with the old password.
            conn.execute("DELETE FROM pending_logins WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user(username: str):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()


def list_users():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT username, role, created_at, password_changed_at, totp_confirmed_at "
            "FROM users ORDER BY username"
        ).fetchall()
        grants = {}
        for row in conn.execute("SELECT username, library FROM user_libraries"):
            grants.setdefault(row["username"], set()).add(row["library"])
        spare = {row["username"]: row["c"] for row in conn.execute(
            "SELECT username, COUNT(*) c FROM backup_codes WHERE used_at IS NULL "
            "GROUP BY username")}
    finally:
        conn.close()

    known = library_names()
    users = []
    for row in rows:
        user = dict(row)
        granted = set(known) if user["role"] == "admin" else grants.get(user["username"], set())
        user["libraries"] = [library for library in known if library in granted]
        user["two_factor"] = bool(user.pop("totp_confirmed_at"))
        user["backup_codes_left"] = spare.get(user["username"], 0)
        users.append(user)
    return users


def delete_user(username: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.execute("DELETE FROM user_libraries WHERE username = ?", (username,))
        conn.execute("DELETE FROM backup_codes WHERE username = ?", (username,))
        conn.execute("DELETE FROM pending_logins WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def user_count() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    finally:
        conn.close()


# ---------- sessions ----------

def _throttle_key(username, client_ip):
    return f"{username}|{client_ip}"


def is_locked_out(username, client_ip) -> int:
    """Seconds left on the lockout, 0 if not locked."""
    entry = _FAILURES.get(_throttle_key(username, client_ip))
    if not entry or entry["count"] < LOCKOUT_AFTER:
        return 0
    remaining = int(entry["until"] - time.time())
    return max(remaining, 0)


def _record_failure(username, client_ip):
    key = _throttle_key(username, client_ip)
    entry = _FAILURES.setdefault(key, {"count": 0, "until": 0})
    entry["count"] += 1
    if entry["count"] >= LOCKOUT_AFTER:
        entry["until"] = time.time() + LOCKOUT_SECONDS


def _clear_failures(username, client_ip):
    _FAILURES.pop(_throttle_key(username, client_ip), None)


def verify_credentials(username: str, password: str, client_ip: str = ""):
    """The user row if the password is right, otherwise None.

    This is only the first half of signing in — it deliberately does not issue
    anything. `start_pending_login` takes it from here.
    """
    user = get_user(username)
    # Hash even when the user does not exist, so a missing account and a wrong
    # password take the same time to answer.
    stored = user["password_hash"] if user else hash_password("_no_such_user_")
    if not verify_password(password, stored) or user is None:
        _record_failure(username, client_ip)
        return None
    return user


def start_session(username: str) -> str:
    """Issue a real session cookie value. Both factors are already done."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
        conn.execute(
            "INSERT INTO sessions (token, username, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), username, expires.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    return token


# ---------- half-finished logins ----------

def start_pending_login(username: str, purpose: str) -> str:
    """Token for a login that has passed the password and nothing else.

    `purpose` is 'totp' when the account already has an authenticator to answer
    with, or 'enroll' when it still has to set one up. It is stored rather than
    worked out again at the far end so that an account cannot be nudged down
    the enrolment path by anything the client says.
    """
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=PENDING_MINUTES)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM pending_logins WHERE expires_at < datetime('now')")
        conn.execute(
            "INSERT INTO pending_logins (token, username, purpose, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (hash_token(token), username, purpose, expires.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def pending_login(token: str):
    """The half-finished login a pending token names, or None."""
    if not token:
        return None
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT username, purpose FROM pending_logins "
            "WHERE token = ? AND expires_at > datetime('now')",
            (hash_token(token),),
        ).fetchone()
    finally:
        conn.close()


def clear_pending(token: str):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM pending_logins WHERE token = ?", (hash_token(token),))
        conn.commit()
    finally:
        conn.close()


# ---------- second factor ----------

def _new_backup_code() -> str:
    raw = "".join(secrets.choice(BACKUP_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def _normalise_backup_code(code: str) -> str:
    """Ignore case, spaces and the dashes we printed it with."""
    return "".join(c for c in (code or "").lower() if c in BACKUP_ALPHABET)


def issue_backup_codes(username: str) -> list:
    """Replace this account's codes with a fresh set, and return them.

    The only time the plain values exist — after this they are hashes, so a set
    that gets closed without being written down has to be reissued.
    """
    codes = [_new_backup_code() for _ in range(BACKUP_CODE_COUNT)]
    conn = get_conn()
    try:
        conn.execute("DELETE FROM backup_codes WHERE username = ?", (username,))
        conn.executemany(
            "INSERT INTO backup_codes (username, code_hash) VALUES (?, ?)",
            [(username, hash_token(_normalise_backup_code(code))) for code in codes],
        )
        conn.commit()
    finally:
        conn.close()
    return codes


def backup_codes_left(username: str) -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM backup_codes WHERE username = ? AND used_at IS NULL",
            (username,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _spend_backup_code(username: str, code: str) -> bool:
    """Burn a backup code if it is one of theirs and unused."""
    normalised = _normalise_backup_code(code)
    if len(normalised) != BACKUP_CODE_LENGTH:
        return False
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE backup_codes SET used_at = datetime('now') "
            "WHERE username = ? AND code_hash = ? AND used_at IS NULL",
            (username, hash_token(normalised)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_enrolled(username: str) -> bool:
    user = get_user(username)
    return bool(user and user["totp_confirmed_at"])


def begin_enrolment(username: str) -> dict:
    """Offer a new secret and the QR for it. Nothing is switched over yet.

    It lands in totp_pending_secret, so an account that already has a working
    authenticator keeps it until the new one has proved itself.
    """
    secret = totp.new_secret()
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET totp_pending_secret = ? WHERE username = ?",
                     (secret, username))
        conn.commit()
    finally:
        conn.close()
    uri = totp.provisioning_uri(secret, username)
    return {"secret": totp.format_secret(secret), "uri": uri, "qr": totp.qr_svg(uri)}


def confirm_enrolment(username: str, code: str) -> list:
    """Accept the pending secret once the app proves it has it.

    Returns the new backup codes. Raises ValueError when there is nothing to
    confirm or the code does not match — the caller turns that into a 400.
    """
    user = get_user(username)
    secret = user["totp_pending_secret"] if user else None
    if not secret:
        raise ValueError("Start the setup again — there is no code waiting to be confirmed")
    counter = totp.verify(secret, code)
    if counter is None:
        raise ValueError("That code is not right — check the app and try the current one")
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE users SET totp_secret = ?, totp_pending_secret = NULL,
                                totp_confirmed_at = datetime('now'), totp_last_counter = ?
               WHERE username = ?""",
            (secret, counter, username),
        )
        conn.commit()
    finally:
        conn.close()
    return issue_backup_codes(username)


def verify_second_factor(username: str, code: str, client_ip: str = "") -> bool:
    """Six digits from the app, or one of the backup codes. Either finishes it."""
    user = get_user(username)
    if user is None or not user["totp_confirmed_at"]:
        return False

    spent = user["totp_last_counter"]
    counter = totp.verify(user["totp_secret"], code, after=-1 if spent is None else spent)
    if counter is not None:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET totp_last_counter = ? WHERE username = ?",
                         (counter, username))
            conn.commit()
        finally:
            conn.close()
    elif not _spend_backup_code(username, code):
        _record_failure(username, client_ip)
        return False

    _clear_failures(username, client_ip)
    return True


def reset_two_factor(username: str) -> bool:
    """Un-enrol an account: the way back in for a lost phone.

    Their sessions and any half-finished login go too. The next sign-in gets
    the enrolment screen, because enrolling is not optional.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            """UPDATE users SET totp_secret = NULL, totp_pending_secret = NULL,
                                totp_confirmed_at = NULL, totp_last_counter = NULL
               WHERE username = ?""",
            (username,),
        )
        conn.execute("DELETE FROM backup_codes WHERE username = ?", (username,))
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.execute("DELETE FROM pending_logins WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def session_user(token: str):
    """The user a session token belongs to, or None if absent/expired.

    The cookie carries the raw token; the table holds its hash, so the lookup
    hashes on the way in. Nothing ever reverses the stored value.
    """
    if not token:
        return None
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT u.username, u.role, u.totp_confirmed_at FROM sessions s
               JOIN users u ON u.username = s.username
               WHERE s.token = ? AND s.expires_at > datetime('now')""",
            (hash_token(token),),
        ).fetchone()
    finally:
        conn.close()


# ---------- Quick Connect ----------
# A TV without a keyboard shows a QR (and a short code beside it, for a sanity
# check); an already-signed-in phone scans it and hits /approve. The TV keeps
# polling and picks up a real session the moment that happens — see
# poll_quick_connect(). No profile is carried over: the box lands on the same
# picker a fresh sign-in would, which is a D-pad grid rather than typing.

QUICK_CONNECT_MINUTES = int(os.environ.get("QUICK_CONNECT_MINUTES", "5"))
# No 0/O/1/I — read off a TV screen from across a room, not typed from a
# generated string, so ambiguous glyphs cost more here than in a backup code.
QUICK_CONNECT_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
QUICK_CONNECT_CODE_LENGTH = 6


def start_quick_connect() -> dict:
    """A fresh pairing: the token that goes in the QR, and the code shown
    beside it. Returns both in the clear — this is the one moment either value
    exists outside the (hashed) table."""
    token = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(QUICK_CONNECT_CODE_ALPHABET) for _ in range(QUICK_CONNECT_CODE_LENGTH))
    expires = datetime.now(timezone.utc) + timedelta(minutes=QUICK_CONNECT_MINUTES)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM quick_connect WHERE expires_at < datetime('now')")
        conn.execute(
            "INSERT INTO quick_connect (token, code, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), code, expires.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "code": code, "expires_in": QUICK_CONNECT_MINUTES * 60}


def approve_quick_connect(token: str, username: str) -> bool:
    """The phone's side: mint a real session for `username` and attach it to
    this pairing. False means the code was wrong, expired, or already used —
    the caller does not get to tell those apart."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM quick_connect WHERE token = ? AND status = 'pending' "
            "AND expires_at > datetime('now')",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return False
        session_token = start_session(username)
        conn.execute(
            "UPDATE quick_connect SET status = 'approved', session_token = ?, username = ? "
            "WHERE token = ?",
            (session_token, username, hash_token(token)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def poll_quick_connect(token: str) -> dict:
    """What the TV asks, over and over, while the QR is on screen.

    Single-use on approval: the session token is only ever handed back once,
    and the row is gone immediately after — a second poll (a retry, a stray
    duplicate request) must not find a still-live credential sitting there.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status, session_token, username FROM quick_connect "
            "WHERE token = ? AND expires_at > datetime('now')",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return {"status": "expired"}
        if row["status"] != "approved":
            return {"status": "pending"}
        conn.execute("DELETE FROM quick_connect WHERE token = ?", (hash_token(token),))
        conn.commit()
        return {"status": "approved", "session_token": row["session_token"], "username": row["username"]}
    finally:
        conn.close()


def logout(token: str):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (hash_token(token),))
        conn.commit()
    finally:
        conn.close()


# ---------- profiles ----------
# A profile is a viewing identity under one account, picked after that
# account's own password+2FA sign-in — it carries no auth of its own. See the
# profiles table in database.py and the note above database.py's
# toggle_list_entry() for how My List / watch history key off a profile's id.

def list_profiles(username: str) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at FROM profiles WHERE username = ? ORDER BY id",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_profile(username: str, profile_id: int):
    """One of this account's profiles, or None — also how a profile cookie is
    validated: a profile id alone proves nothing without the account it
    belongs to."""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT id, name, theme FROM profiles WHERE id = ? AND username = ?",
            (profile_id, username),
        ).fetchone()
    finally:
        conn.close()


def set_profile_theme(username: str, profile_id: int, theme) -> bool:
    """A profile's own theme id, or None to follow the household default.
    The caller validates the id against the registry (see THEME_IDS in
    main.py) — this only stores it."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE profiles SET theme = ? WHERE id = ? AND username = ?",
            (theme, profile_id, username),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def create_profile(username: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the profile a name")
    conn = get_conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM profiles WHERE username = ?", (username,)
        ).fetchone()["c"]
        if count >= PROFILE_LIMIT:
            raise ValueError(f"This account already has {PROFILE_LIMIT} profiles")
        cur = conn.execute(
            "INSERT INTO profiles (username, name) VALUES (?, ?)", (username, name)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name}
    finally:
        conn.close()


def rename_profile(username: str, profile_id: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the profile a name")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE profiles SET name = ? WHERE id = ? AND username = ?",
            (name, profile_id, username),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_profile(username: str, profile_id: int) -> bool:
    """Remove a profile. Its My List and watch history rows are left in place
    — orphaned rather than deleted, the same way a removed library's rows are
    handled elsewhere, so a profile re-created by mistake is not a total loss.
    Refuses to take an account's last profile: there has to be one to sign
    into."""
    conn = get_conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM profiles WHERE username = ?", (username,)
        ).fetchone()["c"]
        if count <= 1:
            raise ValueError("An account needs at least one profile")
        cur = conn.execute(
            "DELETE FROM profiles WHERE id = ? AND username = ?", (profile_id, username)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
