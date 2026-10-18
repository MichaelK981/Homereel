"""Subtitles that sit beside the video, as Bazarr and friends leave them —
plus, failing that, whatever text track is muxed into the video itself.

Sidecar files are checked first and are all a Bazarr-managed show ever needs:
a directory listing is cheap next to the video that follows it. Some releases
never get a sidecar, though — a fansub MKV routinely ships its own ASS track
and nothing external to fetch — and leaving those permanently subtitle-less
was worse than the cost of an ffprobe/ffmpeg call, which only runs at all when
a video has no sidecar to begin with.

Everything is served as WebVTT because that is the only thing `<track>` accepts.
A sidecar SRT differs from it by a header and a comma; ffmpeg's own webvtt
muxer does the equivalent job for a muxed ASS/SRT stream. That conversion means
subtitles work identically in Direct Play and in transcode mode: the text
track rides alongside the picture either way, so it never has to be burned in.
"""
import json
import re
import subprocess
from pathlib import Path

SUBTITLE_EXTS = (".srt", ".vtt")

# Bitmap subtitle codecs (an image per cue) are excluded — turning those into
# WebVTT text would need OCR this app does not do.
MUXED_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Enough to label what a home library actually holds; anything else falls back
# to the code itself, which is more use than calling it "Unknown".
LANGUAGES = {
    "en": "English", "eng": "English", "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fre": "French", "de": "German", "ger": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ja": "Japanese",
    "jpn": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "af": "Afrikaans", "ar": "Arabic", "hi": "Hindi",
}

# Bazarr and friends append these after the language: ".en.cc.srt", ".en.sdh.srt",
# ".en.forced.srt". They change what the track *is*, so they belong in the label.
FLAGS = {"cc": "CC", "sdh": "SDH", "forced": "Forced", "hi": "HI"}

# A cue timestamp: SRT writes the fraction with a comma, WebVTT with a dot.
SRT_TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def _describe(suffix: str):
    """Turn the bit between the video's name and the extension into a label.

    "en" -> ("English", "en"); "en.cc" -> ("English (CC)", "en"); "" -> the file
    is just <video>.srt with nothing to go on.
    """
    parts = [p for p in suffix.split(".") if p]
    if not parts:
        return "Subtitles", ""
    code = parts[0].lower()
    language = LANGUAGES.get(code, code.upper())
    extras = [FLAGS[p.lower()] for p in parts[1:] if p.lower() in FLAGS]
    label = f"{language} ({', '.join(extras)})" if extras else language
    # "hi" is Hindi as a language and hearing-impaired as a flag; only the
    # trailing position can mean the latter.
    return label, (code if code in LANGUAGES else "")


def find_tracks(video_path):
    """Every usable subtitle for one video, in a stable order.

    Stable because the index is what the player asks for: sorting by filename
    (sidecars) or stream order (muxed) means the same track keeps the same
    number between requests. Muxed tracks are only even looked for once the
    sidecar pass comes back empty — never both, so the same index space is
    never shared between the two kinds.
    """
    video = Path(video_path)
    folder = video.parent
    tracks = []
    if folder.is_dir():
        stem = video.stem
        try:
            candidates = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            candidates = []
        for candidate in candidates:
            if candidate.suffix.lower() not in SUBTITLE_EXTS or not candidate.is_file():
                continue
            name = candidate.name
            if not name.startswith(stem):
                continue
            # What sits between the video's own name and the subtitle extension.
            middle = name[len(stem):-len(candidate.suffix)].strip(".")
            label, lang = _describe(middle)
            tracks.append({"index": len(tracks), "label": label, "lang": lang,
                           "kind": "sidecar", "path": str(candidate)})
    if not tracks:
        tracks = _find_muxed_tracks(video_path)
    return tracks


def _find_muxed_tracks(video_path):
    """Text-based subtitle streams inside the video itself, for a release
    that never got a sidecar. ffprobe only reads the container's own index —
    no decoding — so this is cheap next to the ffmpeg call a track's text
    actually costs once someone picks it.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index,codec_name:stream_tags=language,title",
             "-of", "json", str(video_path)],
            capture_output=True, timeout=10, check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    tracks = []
    for stream in streams:
        if (stream.get("codec_name") or "").lower() not in MUXED_TEXT_CODECS:
            continue
        codec = stream["codec_name"].lower()
        tags = stream.get("tags", {})
        lang_code = (tags.get("language") or "").lower()
        # A release routinely carries two English tracks for different jobs —
        # "Signs & Songs" versus "Dialogue" — so the stream's own title, when
        # there is one, beats collapsing both down to the same language name.
        label = tags.get("title") or LANGUAGES.get(lang_code, lang_code.upper() or "Subtitles")
        tracks.append({"index": len(tracks), "label": label,
                       "lang": lang_code if lang_code in LANGUAGES else "",
                       "kind": "muxed", "stream_index": stream["index"], "codec": codec})
    return tracks


def _srt_text_to_vtt(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().upper().startswith("WEBVTT"):
        return text
    return "WEBVTT\n\n" + SRT_TIME_RE.sub(r"\1.\2", text)


def to_vtt(path) -> str:
    """Read a sidecar and hand back WebVTT.

    SRT is decoded as UTF-8 with a lenient fallback: subtitle files in the wild
    are routinely cp1252 or latin-1, and one bad byte should cost a character,
    not the whole track.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return _srt_text_to_vtt(text)


# "H:MM:SS.CC" (ASS, centiseconds) -> WebVTT's "HH:MM:SS.mmm".
ASS_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")
# Everything between {}: override tags, including \k karaoke timing — dropped
# outright, since this app has no styled-cue renderer to spend any of it on.
ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def _ass_timestamp_to_vtt(ts: str) -> str:
    m = ASS_TIME_RE.match(ts.strip())
    if not m:
        return "00:00:00.000"
    h, mm, ss, cs = m.groups()
    return f"{int(h):02d}:{mm}:{ss}.{int(cs) * 10:03d}"


def _ass_to_vtt(text: str) -> str:
    """ASS/SSA -> WebVTT, one cue per Dialogue line.

    ffmpeg's own webvtt muxer re-times every \\k karaoke tag as a separate
    cue, which turns a normal anime sub — almost always karaoke-tagged even
    where nothing is actually sung — into one fragment per syllable instead
    of a line. Reading [Events] directly and keeping only a line's overall
    Start/End sidesteps that; override tags are stripped rather than mapped
    to WebVTT markup, for the same no-styled-renderer reason as their timing.
    """
    fields = None
    cues = []
    in_events = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events:
            continue
        lowered = stripped.lower()
        if lowered.startswith("format:"):
            fields = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
            continue
        if fields is None or not lowered.startswith("dialogue:"):
            continue
        parts = stripped.split(":", 1)[1].split(",", len(fields) - 1)
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts))
        cue_text = ASS_OVERRIDE_RE.sub("", row.get("text", ""))
        cue_text = cue_text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()
        if not cue_text:
            continue
        start = _ass_timestamp_to_vtt(row.get("start", ""))
        end = _ass_timestamp_to_vtt(row.get("end", ""))
        cues.append(f"{start} --> {end}\n{cue_text}")
    return "WEBVTT\n\n" + "\n\n".join(cues)


def muxed_to_vtt(video_path, stream_index: int, codec: str) -> str:
    """Demux one embedded subtitle stream and hand back WebVTT.

    ASS/SSA goes out raw and through _ass_to_vtt() rather than ffmpeg's own
    webvtt muxer — see its docstring for why. SRT and mov_text have no
    karaoke tag to trip over, so ffmpeg's straight conversion to SRT is fine,
    and the result goes through the same SRT->VTT step a sidecar file does.
    """
    out_format = "ass" if codec in ("ass", "ssa") else "srt"
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path),
             "-map", f"0:{stream_index}", "-f", out_format, "-"],
            capture_output=True, timeout=30, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise OSError(str(exc)) from exc
    text = result.stdout.decode("utf-8", errors="replace")
    return _ass_to_vtt(text) if out_format == "ass" else _srt_text_to_vtt(text)
