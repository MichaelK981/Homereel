"""Time-based one-time passwords (RFC 6238) and the enrolment QR.

Stdlib apart from the picture: HMAC-SHA1 over a 30-second counter is a handful
of lines, and it is what every authenticator app on a phone already speaks.
`segno` is here only to turn the otpauth:// URI into an SVG — hand-rolling a QR
encoder's Reed-Solomon is a great deal of code to get subtly wrong for a
drawing of a string we are holding anyway.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

import segno

DIGITS = 6
PERIOD = 30                  # seconds per code; every authenticator assumes 30
ISSUER = "HomeReel"

# How many steps either side of now are accepted. One covers a phone clock up
# to half a minute out, and a code typed just as it rolled over. More than that
# widens the guessing window for no real gain.
DRIFT_STEPS = 1


def new_secret() -> str:
    """A fresh base32 shared secret, in the form the QR and the apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def counter_now(at: float = None) -> int:
    return int((time.time() if at is None else at) // PERIOD)


def _code_for(secret: str, counter: int) -> str:
    # Padding is stripped from the stored secret because that is how it is
    # printed for manual entry; b32decode wants it back.
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % 10 ** DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, after: int = -1, at: float = None):
    """The counter `code` matches, or None if it does not.

    A counter comes back rather than a bool so the caller can store it and
    refuse the same digits twice: one code stays live for a whole step, which
    is long enough for someone reading over a shoulder to type it again.
    `after` is the last counter this account already spent.
    """
    if not secret:
        return None
    code = "".join(character for character in (code or "") if character.isdigit())
    if len(code) != DIGITS:
        return None
    now = counter_now(at)
    for counter in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
        if counter <= after:
            continue
        if secrets.compare_digest(_code_for(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, username: str) -> str:
    """The otpauth:// URI the QR encodes — also what a typed setup needs."""
    label = quote(f"{ISSUER}:{username}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


def qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG, ready to drop into the page.

    Error correction M rather than the default L: this gets photographed off a
    screen at an angle, and the damage tolerance costs a few modules.
    """
    # omitsize: without it segno emits fixed width/height attributes and no
    # viewBox, so the CSS resize in .qr-frame svg clips the artwork instead
    # of scaling it — the code renders with its bottom-right corner cropped
    # off and no authenticator app can read it.
    return segno.make(uri, error="m", micro=False).svg_inline(
        scale=5, border=2, dark="#0f0d15", light="#ffffff", omitsize=True)


def format_secret(secret: str) -> str:
    """Grouped in fours, for anyone typing it in instead of scanning."""
    return " ".join(secret[index:index + 4] for index in range(0, len(secret), 4))
