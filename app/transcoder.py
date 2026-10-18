import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

from .subtitles import LANGUAGES

TRANSCODE_DIR = Path(os.environ.get("TRANSCODE_DIR", "/cache/transcode"))

# "nvenc" uses the NVIDIA GPU encoder (h264_nvenc). "cpu" falls back to libx264.
# Set ENCODER=cpu in the environment if the GPU isn't available (e.g. testing
# without a container-toolkit setup, or a host with no NVIDIA card).
ENCODER = os.environ.get("ENCODER", "nvenc")

# Segment length ffmpeg is asked for, and — via -force_key_frames below — the
# length it actually produces. The two have to stay the same value.
HLS_SEGMENT_SECONDS = 6

QUALITY_PRESETS = {
    "1080p": {"height": 1080, "maxrate": "5000k", "bufsize": "10000k"},
    "720p": {"height": 720, "maxrate": "2800k", "bufsize": "5600k"},
    "480p": {"height": 480, "maxrate": "1400k", "bufsize": "2800k"},
}

# Codecs this host's NVDEC will actually decode in hardware — confirmed against
# the GTX 1050 Ti (Pascal). Deliberately narrow: Pascal has no AV1 decode at
# all (that needs Ampere+), and 10-bit VP9 is patchy before Volta, so anything
# outside this set keeps decoding on the CPU rather than risk ffmpeg refusing
# the file outright. h264/hevc covers effectively the whole library.
NVDEC_CODECS = {"h264", "hevc"}

_active = {}  # (item_id, quality, start) -> {process, dir, last_access}
_lock = threading.Lock()


# A transcode session is pinned to where ffmpeg was told to start, because that
# is where its HLS timeline's zero is. Resuming 40 minutes in cannot reuse a
# session that began at the top — it would have to encode 40 minutes of video
# nobody is going to watch before the player saw a frame.
def _session_dir(item_id, quality, start=0, audio=0):
    d = TRANSCODE_DIR / f"{item_id}_{quality}_{int(start)}_{int(audio)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_duration(source_path):
    """The file's runtime in seconds, or None if ffprobe cannot say.

    The container of a rip does not always carry a duration in its header, and a
    broken file has none at all — callers fall back to what the player reports
    rather than treating a missing answer as zero.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return duration if duration > 0 else None


def _video_codec(source_path):
    """The source's video codec_name, or None if ffprobe can't say — decides
    whether this file's decode can go on NVDEC (see NVDEC_CODECS)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
            capture_output=True, text=True, timeout=30,
        )
        codec = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return codec or None


def probe_media_info(source_path):
    """Resolution, video codec, and audio codec + channel count — real facts
    about the file for the detail page's "Available in" chips, not invented
    ones. Returns whatever ffprobe could read; a field it couldn't (or a
    stream that doesn't exist) is simply left out rather than guessed at.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,channels", "-of", "json", str(source_path)],
            capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info = {}
    if video and video.get("height"):
        info["resolution"] = _resolution_label(video["height"])
        if video.get("codec_name"):
            info["vcodec"] = video["codec_name"]
    if audio:
        if audio.get("codec_name"):
            info["acodec"] = audio["codec_name"]
        if audio.get("channels"):
            info["channels"] = audio["channels"]
    return info


# A rip's height is rarely the exact broadcast number (1078 rather than 1080
# is common), so the ladder catches anything close rather than requiring a
# perfect match.
_RESOLUTION_LADDER = [(2000, "4K"), (1300, "1440p"), (1000, "1080p"), (650, "720p"), (400, "480p")]


def _resolution_label(height):
    for threshold, label in _RESOLUTION_LADDER:
        if height >= threshold:
            return label
    return f"{height}p"


_CHANNEL_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def audio_tracks(source_path):
    """Every audio stream in a file, in ffmpeg's own `a:N` order.

    That order matters: it is exactly what -map 0:a:{index} in
    start_transcode() addresses, so the index returned here is the same
    number a client hands back to pick a track — no separate id space to keep
    in sync with ffmpeg's own.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=channels,codec_name:stream_tags=language,title",
             "-of", "json", str(source_path)],
            capture_output=True, timeout=10, check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    tracks = []
    for index, stream in enumerate(streams):
        tags = stream.get("tags", {})
        lang_code = (tags.get("language") or "").lower()
        label = tags.get("title") or LANGUAGES.get(lang_code, lang_code.upper() or "Audio")
        channels = stream.get("channels")
        if channels:
            label = f"{label} ({_CHANNEL_LABELS.get(channels, f'{channels}ch')})"
        tracks.append({
            "index": index, "label": label,
            "lang": lang_code if lang_code in LANGUAGES else "",
            "codec": stream.get("codec_name"), "channels": channels,
        })
    return tracks


def _video_encode_args(preset):
    """Build the ffmpeg args for the video codec, GPU or CPU."""
    if ENCODER == "nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",       # p1 (fastest/lowest quality) .. p7 (slowest/best quality)
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", "23",
            "-b:v", "0",
            "-maxrate", preset["maxrate"],
            "-bufsize", preset["bufsize"],
            # NVENC otherwise ignores the generic -force_key_frames list below —
            # confirmed by testing: without this, segments stayed at the
            # source's own ~10.4s GOP length despite -force_key_frames being
            # set. libx264 needs no equivalent; it honours -force_key_frames
            # natively.
            "-forced-idr", "1",
        ]
    # CPU fallback
    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-maxrate", preset["maxrate"],
        "-bufsize", preset["bufsize"],
    ]


def _scale_filter(preset, hw_decode):
    """The resize + pixel-format filter, GPU or CPU depending on how the
    source was decoded (see NVDEC_CODECS / hw_decode in start_transcode)."""
    if hw_decode:
        # Frames are already CUDA frames (-hwaccel_output_format cuda below);
        # scale_cuda resizes and converts them to nv12 without ever coming
        # back to system memory, which is what h264_nvenc wants as input.
        return f"scale_cuda=-2:{preset['height']}:format=nv12"
    # format=yuv420p is not cosmetic: h264_nvenc cannot encode 10-bit, and
    # scale alone preserves the source's pixel format, so a 10-bit source
    # (most anime releases are HEVC Main 10) reaches the encoder as
    # yuv420p10le and it refuses to open — reporting "No capable devices
    # found", which reads like a missing GPU and is not one.
    return f"scale=-2:{preset['height']},format=yuv420p"


def start_transcode(item_id, quality, source_path, start=0, audio=0):
    """Start (or reuse) a background ffmpeg process producing an HLS stream.

    `start` seeks the *source*, for resuming and for switching quality without
    going back to the beginning. The output timeline still starts at zero, so
    the player's currentTime is an offset into the session, not into the film —
    callers add `start` back on before reporting a position.

    `audio` is an index into the file's own audio streams (ffmpeg's `a:N`
    order — see audio_tracks()), for a release that carries more than one
    language or mix. Baked into the session key/dir the same way `start` is:
    a different pick is a different HLS timeline, not a variant of the same one.
    """
    key = (item_id, quality, int(start), int(audio))
    with _lock:
        existing = _active.get(key)
        if existing and existing["process"].poll() is None:
            existing["last_access"] = time.time()
            return existing["dir"]

        # Only one session for a given title is ever worth keeping alive.
        # Leaving a sibling running — an abandoned quality switch, or someone
        # re-clicking a quality button while ffmpeg was still starting up,
        # reading the normal few-second startup delay as nothing having
        # happened — does nothing but steal GPU throughput from the session
        # actually being watched: this GTX 1050 Ti's NVENC visibly drops from
        # ~13x realtime to ~2x once two or three sessions for the same item
        # run at once, which is what turns a slow start into one that never
        # catches up at all. cleanup_stale() would reap these eventually, but
        # "eventually" here is exactly the window that causes the problem.
        for other_key, other_session in list(_active.items()):
            if other_key != key and other_key[0] == item_id:
                _kill_session(other_key, other_session)

        work_dir = _session_dir(item_id, quality, start, audio)
        for f in work_dir.glob("*"):
            f.unlink()

        preset = QUALITY_PRESETS.get(quality)
        if preset is None:
            raise ValueError(f"Unknown quality preset: {quality}")

        playlist = work_dir / "playlist.m3u8"
        segment_pattern = str(work_dir / "seg_%05d.ts")
        log_path = work_dir / "ffmpeg.log"

        # -ss before -i is the fast seek: ffmpeg jumps the demuxer to the
        # nearest keyframe rather than decoding and discarding everything up to
        # the mark, which is the difference between instant and minutes.
        seek = ["-ss", f"{float(start):.3f}"] if start else []

        # Decode on the GPU too when it's safe to: without this, ffmpeg only
        # offloads the *encode* and the source is software-decoded on the CPU,
        # which for 1080p HEVC is often the heavier half of the work. Gated on
        # the source codec (NVDEC_CODECS) rather than turned on unconditionally
        # — forcing -hwaccel_output_format cuda on a codec this GPU can't
        # decode in hardware (AV1, mainly) fails the whole session outright
        # instead of falling back, so anything outside the known-good set
        # keeps the old CPU-decode path.
        hw_decode = ENCODER == "nvenc" and _video_codec(source_path) in NVDEC_CODECS
        hwaccel = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if hw_decode else []

        cmd = [
            "ffmpeg", "-y",
            *seek,
            *hwaccel,
            "-i", str(source_path),
            "-map", "0:v:0", "-map", f"0:a:{int(audio)}?",
            *_video_encode_args(preset),
            "-vf", _scale_filter(preset, hw_decode),
            # Without this, ffmpeg can only cut a segment at a keyframe the
            # *source* already has — most encodes carry one every ~250 frames
            # (~10s at 24fps), not every HLS_SEGMENT_SECONDS, so segments came
            # out ~10.4s instead of the requested 6s (and unevenly, since GOP
            # length varies by source). Bigger, uneven segments mean more can
            # go wrong per fetch and a smaller effective read-ahead buffer —
            # the intermittent stutter this was chasing. sc_threshold=0 stops
            # scene-cut detection adding its own unforced keyframes, which
            # would otherwise still leave segment length at the mercy of the
            # content. watching.py's Live-view position math already assumes
            # segment N covers exactly HLS_SEGMENT_SECONDS — this makes that
            # actually true instead of aspirational.
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})",
            "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(HLS_SEGMENT_SECONDS),
            "-hls_list_size", "0",       # keep all segments in the playlist (allows seeking backward)
            "-hls_flags", "append_list",
            # Without this, the playlist has no #EXT-X-PLAYLIST-TYPE tag until
            # ffmpeg finishes and appends #EXT-X-ENDLIST — so for the entire
            # encode, hls.js sees a plain growing playlist and treats it as a
            # *live* stream: it seeks near the live edge instead of segment 0,
            # and keeps jumping forward to "catch up" as new segments appear.
            # GPU decode+encode races at 10-13x realtime, so the playlist
            # balloons to 100+ segments within seconds of playback starting —
            # exactly when this bites, as "plays briefly then jumps/hangs."
            # "event" (not "vod") specifically: confirmed live that "vod" is
            # incompatible with -hls_flags append_list's incremental writes —
            # ffmpeg held the whole playlist.m3u8 back rather than writing it
            # early (still zero bytes on disk 145+ segments and 25s+ in),
            # which is a worse break than the bug this was meant to fix.
            # "event" is the HLS type actually meant for a still-growing,
            # eventually-complete (non-live) playlist — same #EXT-X-ENDLIST
            # on completion, but tells the client up front it isn't live.
            "-hls_playlist_type", "event",
            "-hls_segment_filename", segment_pattern,
            str(playlist),
        ]

        log_file = open(log_path, "wb")
        process = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
        _active[key] = {
            "process": process,
            "dir": work_dir,
            "last_access": time.time(),
            "log_file": log_file,
            "log_path": log_path,
        }
        return work_dir


def wait_for_playlist(work_dir, timeout=25):
    """Block until ffmpeg has produced a playlist with at least one segment,
    or return False early (with the process dead) if ffmpeg failed to start."""
    playlist = work_dir / "playlist.m3u8"
    start = time.time()
    while time.time() - start < timeout:
        if playlist.exists() and ".ts" in playlist.read_text(errors="ignore"):
            return True
        # If ffmpeg already exited without ever writing a usable playlist, stop waiting
        with _lock:
            session = next((s for s in _active.values() if s["dir"] == work_dir), None)
        if session and session["process"].poll() is not None:
            break
        time.sleep(0.5)
    return playlist.exists() and ".ts" in playlist.read_text(errors="ignore") if playlist.exists() else False


def build_vod_playlist(total_duration, start):
    """A synthesized, complete VOD playlist for the whole remaining runtime,
    every segment the encode will eventually produce listed with
    #EXT-X-ENDLIST present from the very first response.

    Needed because hls.js's "is this live" detection depends only on whether
    ENDLIST is present, never on -hls_playlist_type (confirmed against
    hls.js's own issue tracker, e.g. video-dev/hls.js#668) — so serving
    ffmpeg's own playlist file, which structurally can't carry ENDLIST until
    encoding actually finishes, means hls.js treats every session as a live
    stream for its entire runtime and keeps seeking toward the "live edge" as
    the GPU encode races ahead at 10-13x realtime. A segment a client asks
    for before ffmpeg has actually written it just waits — see
    wait_for_segment() below, used from transcode_segment() in main.py —
    rather than 404ing, since at that encode speed the wait is normally well
    under a second.

    `total_duration` is the *source* file's own runtime; `start` is where
    ffmpeg's -ss seeked it, so `total_duration - start` is how much this
    session will actually produce — matching the timeline transcode_playlist
    already reports 0 for regardless of `start` (transcoder.py's own
    docstring: "the output timeline still starts at zero").
    """
    remaining = (total_duration - start) if total_duration else None
    if not remaining or remaining <= 0:
        n_segments, last_duration = 1, HLS_SEGMENT_SECONDS
    else:
        n_segments = math.ceil(remaining / HLS_SEGMENT_SECONDS)
        last_duration = remaining - (n_segments - 1) * HLS_SEGMENT_SECONDS
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{HLS_SEGMENT_SECONDS}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-DISCONTINUITY",
    ]
    for i in range(n_segments):
        duration = last_duration if i == n_segments - 1 else HLS_SEGMENT_SECONDS
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"seg_{i:05d}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def wait_for_segment(segment_path, timeout=20):
    """Block until a segment ffmpeg hasn't written yet appears.

    Only reached for a segment build_vod_playlist() has already promised
    exists — the actual wait is normally a fraction of a second at 10-13x
    realtime encode speed. The timeout is a backstop for a genuinely dead
    encode, not the expected path.
    """
    start = time.time()
    while time.time() - start < timeout:
        if segment_path.exists():
            return True
        time.sleep(0.2)
    return segment_path.exists()


def get_last_log_lines(work_dir, n=15):
    """Grab the tail of ffmpeg's log for a session, useful for surfacing
    GPU/driver errors (e.g. NVENC init failures) back to the caller."""
    log_path = work_dir / "ffmpeg.log"
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def touch(item_id, quality, start=0, audio=0):
    with _lock:
        key = (item_id, quality, int(start), int(audio))
        if key in _active:
            _active[key]["last_access"] = time.time()


def _kill_session(key, session):
    """Stop one ffmpeg process and remove its scratch files. Caller already
    holds `_lock` — this never takes it itself, so cleanup_stale() and
    start_transcode() can both call it from inside their own `with _lock:`."""
    if session["process"].poll() is None:
        session["process"].terminate()
    session["log_file"].close()
    for f in session["dir"].glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        session["dir"].rmdir()
    except OSError:
        pass
    del _active[key]


def cleanup_stale(max_idle_seconds=300):
    """Kill and delete transcode sessions nobody has requested segments from recently."""
    now = time.time()
    with _lock:
        for key, session in list(_active.items()):
            if now - session["last_access"] > max_idle_seconds:
                _kill_session(key, session)


def cleanup_loop(interval=60, max_idle_seconds=300):
    while True:
        time.sleep(interval)
        cleanup_stale(max_idle_seconds)

