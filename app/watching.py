"""Who is playing something right now.

There is no heartbeat from the player, and there does not need to be: anyone
watching is by definition pulling media off the server continuously — byte
ranges in direct play, one segment every few seconds in transcode. Watching
those requests is a truer signal than anything a client could claim about
itself, and it catches any client at all, including one hitting /stream with
curl.

Deliberately in memory rather than a table. Each viewing produces a request
every few seconds, so this is written far too often to put in SQLite, and
"currently watching" is a fact about right now that *should* be forgotten when
the server restarts. `plays` remains the durable history.
"""
import os
import subprocess
import threading
import time

# How long a viewing survives without a request before it is considered over.
# It has to clear the largest gap a healthy player leaves: a paused direct play
# stops asking for bytes entirely once its buffer is full, and a transcode asks
# once per segment. Too short and pausing looks like leaving; too long and the
# list keeps ghosts.
STALE_AFTER = float(os.environ.get("WATCHING_TIMEOUT", "90"))

# Matches -hls_time in transcoder.start_transcode(): segment N covers the six
# seconds beginning at N * 6, which is how a segment request becomes a position.
SEGMENT_SECONDS = 6

_lock = threading.Lock()
_viewings = {}          # (username, item_id) -> viewing dict
_durations = {}         # item_id -> seconds, or None once a probe has failed
_probing = set()        # item_ids with a probe in flight


def _probe_duration(path):
    """The file's runtime in seconds, or None if ffprobe cannot say."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        seconds = float(result.stdout.strip())
        return seconds if seconds > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ensure_duration(item_id, path):
    """Learn the item's runtime in the background, once.

    Off the request thread on purpose: this runs under /stream, which is hit
    for every byte range, and a 30-second ffprobe timeout in that path would
    stall playback rather than merely delay a number on an admin screen.
    """
    with _lock:
        if item_id in _durations or item_id in _probing:
            return
        _probing.add(item_id)

    def work():
        seconds = _probe_duration(path)
        with _lock:
            _durations[item_id] = seconds
            _probing.discard(item_id)

    threading.Thread(target=work, daemon=True).start()


def note(username, item_id, item, *, mode, client=None,
         byte_start=None, file_size=None, segment=None, segment_base=0):
    """Record that this account just pulled some of this title.

    Position comes from whichever of the two transports asked. Direct play
    gives a byte offset, which is a fraction of the file and only approximately
    a fraction of the running time — a browser reads ahead of the picture, and
    a variable bitrate does not map evenly onto seconds. A transcode gives a
    segment number, which is exact. Neither is worth a heartbeat protocol to
    improve on for an at-a-glance admin view, so both are reported as
    approximate.
    """
    now = time.time()
    fraction = None
    if byte_start is not None and file_size:
        fraction = min(max(byte_start / file_size, 0.0), 1.0)

    _ensure_duration(item_id, item["path"])

    with _lock:
        key = (username, item_id)
        viewing = _viewings.get(key)
        if viewing is None:
            viewing = {"username": username, "item_id": item_id, "started": now}
            _viewings[key] = viewing
        viewing["last_seen"] = now
        viewing["mode"] = mode
        if client:
            viewing["client"] = client
        # A transcode's own clock is authoritative; a byte offset is a guess, so
        # it never overwrites one.
        if segment is not None:
            # A resumed transcode's segments count from where ffmpeg was told to
            # start, so the offset has to go back on to get a place in the film.
            viewing["position"] = segment_base + segment * SEGMENT_SECONDS
            viewing["fraction"] = None
        elif fraction is not None and "position" not in viewing:
            viewing["fraction"] = fraction


def _decorate(viewing, duration):
    """Fill in whichever of position/fraction the other one implies."""
    position = viewing.get("position")
    fraction = viewing.get("fraction")
    if duration:
        if position is None and fraction is not None:
            position = fraction * duration
        elif fraction is None and position is not None:
            fraction = min(position / duration, 1.0)
    return position, fraction


def active():
    """Every viewing still alive, newest activity first. Prunes as it goes."""
    now = time.time()
    with _lock:
        for key, viewing in list(_viewings.items()):
            if now - viewing["last_seen"] > STALE_AFTER:
                del _viewings[key]
        live = [dict(v) for v in _viewings.values()]
        durations = dict(_durations)

    out = []
    for viewing in live:
        duration = durations.get(viewing["item_id"])
        position, fraction = _decorate(viewing, duration)
        out.append({
            "username": viewing["username"],
            "item_id": viewing["item_id"],
            "mode": viewing["mode"],
            "client": viewing.get("client"),
            "position": position,
            "duration": duration,
            "percent": round(fraction * 100, 1) if fraction is not None else None,
            "watching_for": round(now - viewing["started"]),
            "idle_for": round(now - viewing["last_seen"]),
        })
    out.sort(key=lambda v: v["idle_for"])
    return out


def forget_item(item_id):
    """Drop everything known about one item — used when it leaves the index."""
    with _lock:
        for key in [k for k in _viewings if k[1] == item_id]:
            del _viewings[key]
        _durations.pop(item_id, None)
