"""Probing helpers and subtitle handling for downloaded video files.

Conversion itself lives in hls_sessions (encode on demand); nothing here converts a whole file.
"""

import contextlib
import math
import os
import re
import subprocess
import tempfile
import urllib.parse
from subprocess import Popen, TimeoutExpired
from typing import Any, Dict, List, Tuple

import log
from iso639 import languages as _iso639_languages
from langdetect import DetectorFactory, detect  # pyright: ignore[reportUnknownVariableType]
from langdetect.lang_detect_exception import LangDetectException
from pymediainfo import MediaInfo


def resolve_language(raw: str) -> Tuple[str, str]:
    """Resolve a raw language code (the suffix of a VTT filename) to a
    ``(BCP-47 code, human-readable name)`` pair for HLS ``EXT-X-MEDIA`` tags.

    Safari's native HLS player derives the subtitle menu label from the
    ``LANGUAGE`` attribute and renders anything it doesn't recognise — ``und``,
    a 3-letter ISO 639-2 code like ``eng``, or an unparseable tail — as
    "Unknown". It expects a BCP-47 tag, so map known codes down to their
    2-letter ISO 639-1 form and provide a readable ``NAME``.

    Returns ``("", "Unknown")`` when the language is undetermined.
    """
    code = (raw or "").strip().lower()
    if not code or code in ("und", "unknown", "unk"):
        return "", "Unknown"
    if code == "pb":  # OpenSubtitles' Brazilian Portuguese code is not ISO 639.
        code = "pt"
    # MediaInfo usually emits a 2/3-letter code, but can emit a full name
    # ("English"); try code lookups first, then a name lookup.
    for key, value in (
        ("part1", code),
        ("part2b", code),
        ("part2t", code),
        ("part3", code),
        ("name", code.title()),
    ):
        try:
            # iso639's Language object exposes .part1/.part2b/.name dynamically;
            # its type stub doesn't declare them, so treat it as Any.
            lang: Any = _iso639_languages.get(**{key: value})
        except KeyError:
            continue
        return (lang.part1 or lang.part2b or code), lang.name
    # Not a recognised code; pass it through unchanged so a valid tag the
    # parser simply didn't know still reaches the player.
    return code, code.upper()


DetectorFactory.seed = 0  # the same text always gets the same answer
UNTAGGED = "und"
_CUE_TEXT_LIMIT = 4000  # characters of dialogue read per track: plenty to tell languages apart
_MIN_TEXT = 60


def get_sub_tracks(filepath: str) -> List[Tuple[int, str]]:
    """(stream index, language as tagged in the file) of each text subtitle track; "und" when it has no tag."""
    media_info: Any = MediaInfo.parse(filepath)
    return [
        (int(t.streamorder), t.language or UNTAGGED)
        for t in [
            t for t in media_info.tracks if t.track_type == "Text" and "Picture" not in (t.codec_id_info or "")
        ]
        if t.streamorder
    ]


def extract_subtitles_as_vtt(filepath: str, output_dir: str) -> Any:
    basename: str = os.path.basename(filepath)
    filename_without_extension: str = os.path.splitext(basename)[0]
    sub_tracks: List[Tuple[int, str]] = get_sub_tracks(filepath)
    args: List[str] = ["ffmpeg", "-nostdin", "-v", "quiet", "-i", filepath]
    for (i, lang) in sub_tracks:
        args += [
            "-map", f"0:{i}",
            os.path.join(output_dir, f"{filename_without_extension}.{i}_{lang}.vtt"),
        ]
    return Popen(args)


def detect_vtt_language(path: str) -> str | None:
    """The language of a VTT file's dialogue as a 2-letter code, None when it cannot be told."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    dialogue = [
        re.sub(r"<[^>]+>|\{[^}]*\}", "", line).strip()
        for line in lines
        if line.strip() and "-->" not in line and not line.startswith("WEBVTT") and not line.strip().isdigit()
    ]
    text = " ".join(dialogue)[:_CUE_TEXT_LIMIT]
    if len(text) < _MIN_TEXT:
        return None
    try:
        return str(detect(text)).split("-")[0]
    except LangDetectException:
        return None


def name_untagged_subtitles(output_dir: str, filename_without_extension: str) -> None:
    """Extracted tracks the file did not tag with a language are named `.{i}_und.vtt`; read each one's dialogue
    and rename it for the language it turns out to be, so it is not listed as English (or Unknown)."""
    pattern = re.compile(rf"^{re.escape(filename_without_extension)}\.(\d+)_{UNTAGGED}\.vtt$")
    with contextlib.suppress(OSError):
        for name in sorted(os.listdir(output_dir)):
            match = pattern.match(name)
            if not match:
                continue
            path = os.path.join(output_dir, name)
            code = detect_vtt_language(path)
            if code:
                with contextlib.suppress(OSError):
                    os.replace(path, os.path.join(output_dir, f"{filename_without_extension}.{match.group(1)}_{code}.vtt"))


def audio_codecs(filepath: str) -> List[str] | None:
    """Codec names of the audio streams, or None when ffprobe could not read the file."""
    return _ffprobe_stream_codecs(filepath, "a")


def first_audio_format(filepath: str) -> str:
    """MediaInfo's name for the first audio track ("" when there is none or it cannot be read)."""
    return _detect_audio_codec(filepath)


def is_direct_playable(filepath: str) -> bool:
    """True when a browser can play the file as it is: an MP4 with H.264 video and AAC (or no) audio."""
    if os.path.splitext(filepath)[1].lower() != ".mp4":
        return False
    video = _ffprobe_stream_codecs(filepath, "v")
    audio = _ffprobe_stream_codecs(filepath, "a")
    if not video or audio is None:
        return False
    return all(c == "h264" for c in video) and all(c == "aac" for c in audio)


def _ffprobe_stream_codecs(filepath: str, stream_type: str) -> List[str] | None:
    """Returns ffprobe `codec_name` for every stream of the given type
    ("a" for audio, "v" for video). Returns `None` when ffprobe fails (timeout,
    missing binary, non-zero exit) so callers can distinguish probe failure
    from a file that genuinely has no streams of that type."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", stream_type,
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                filepath,
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (TimeoutExpired, OSError) as e:
        log.debug(f"ffprobe failed for {filepath} ({stream_type}): {e}")
        return None
    if result.returncode != 0:
        log.debug(
            f"ffprobe non-zero exit ({result.returncode}) for {filepath} ({stream_type}): {result.stderr.strip()}"
        )
        return None
    return [c.strip().lower() for c in result.stdout.splitlines() if c.strip()]


# Containers that support sequential reading from a pipe (no seeking needed)
PIPE_FRIENDLY_EXTENSIONS = {".mkv", ".avi", ".ts", ".mpg", ".mpeg"}


# ISO-BMFF containers (MP4) are only sequentially pipe-readable when their
# `moov` box precedes `mdat` ("faststart"). moov-at-end files can't be demuxed
# from a forward-only stream — ffmpeg would have to seek to EOF for the index,
# which a pipe can't do — so those fall back to download-then-convert.
PROBE_EXTENSIONS = {".mp4"}


def _mp4_head_layout(filepath: str, available_bytes: int) -> Dict[str, int | str] | None:
    """Classify an MP4's top-level layout from its downloaded prefix.

    Walks only the top-level box headers within the first `available_bytes`
    contiguous bytes (cheap header reads, never the multi-MB mdat payload), so
    it's safe to call mid-download. Returns:
      - {"mode": "direct"} when `moov` precedes `mdat` (faststart — pipe as-is)
      - {"mode": "remux", "mdat_start": int, "mdat_end": int} when `mdat`
        precedes `moov` (moov-at-end — needs the faststart remux)
      - None when the layout can't be determined yet or is malformed.
    """
    try:
        with open(filepath, "rb") as f:
            offset = 0
            # Bound the walk; real layouts reach moov/mdat within a handful of
            # top-level boxes (ftyp, optional free/wide, then mdat or moov).
            for _ in range(64):
                if offset + 8 > available_bytes:
                    return None
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    return None
                size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_len = 8
                if size == 1:
                    if offset + 16 > available_bytes:
                        return None
                    large = f.read(8)
                    if len(large) < 8:
                        return None
                    size = int.from_bytes(large, "big")
                    header_len = 16
                if box_type == b"moov":
                    return {"mode": "direct"}
                if box_type == b"mdat":
                    # mdat size 0 (extends to EOF) would put moov nowhere — and
                    # a corrupt size we can't trust to locate the tail.
                    if size < header_len:
                        return None
                    return {"mode": "remux", "mdat_start": offset, "mdat_end": offset + size}
                if size < header_len:
                    return None
                offset += size
    except OSError:
        return None
    return None


def is_pipe_streamable(filepath: str, available_bytes: int) -> bool:
    """Whether `filepath` can be HLS-streamed by feeding it sequentially into
    ffmpeg's stdin as-is. Always-friendly containers qualify by extension; MP4
    only when faststart (moov before mdat), probed against the downloaded prefix.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext in PIPE_FRIENDLY_EXTENSIONS:
        return True
    if ext in PROBE_EXTENSIONS:
        layout = _mp4_head_layout(filepath, available_bytes)
        return layout is not None and layout["mode"] == "direct"
    return False


def _detect_audio_codec(filepath: str) -> str:
    """Detect audio codec from file. Returns 'aac', 'ac3', etc."""
    try:
        media_info: Any = MediaInfo.parse(filepath)
        for t in media_info.tracks:
            if t.track_type == "Audio" and t.format:
                return t.format.lower()
    except Exception:
        pass
    return ""


def _atomic_write(path: str, content: str) -> None:
    # Use a unique tmp file so concurrent writers can't clobber each other's
    # in-flight tmp before the rename.
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # mkstemp defaults to 0600; nginx workers run as www-data and need
        # read access to the playlist files served from /play/.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def subtitle_media_lines(output_dir: str, vtt_filenames: List[str], uri_base: str = "") -> List[str]:
    """EXT-X-MEDIA lines for every VTT, writing each one's sub-playlist next to it in `output_dir`.

    `uri_base` is put in front of the sub-playlist URIs, for a master playlist that is served
    from somewhere other than `output_dir`.
    """
    # Use a generous fixed segment duration so hls.js never tries to fetch a
    # "next" subtitle segment; the VTT's own cue timestamps determine when each
    # cue is displayed, independent of EXTINF.
    duration = 86400.0
    target_duration = int(math.ceil(duration))

    # Resolve each VTT's language to a (BCP-47 code, display name) pair, then
    # assign unique NAMEs when a language has multiple tracks. hls.js's
    # subtitleTrackMatchesTextTrack matches by label+lang, so two manifest
    # tracks sharing both end up rendered concurrently when one is selected.
    resolved: List[Tuple[str, str]] = []
    for vtt in vtt_filenames:
        vtt_stem = os.path.splitext(vtt)[0]
        raw = vtt_stem.rsplit("_", 1)[-1] if "_" in vtt_stem else ""
        code, display = resolve_language(raw.replace('"', ""))
        resolved.append((code, display))
    name_totals: Dict[str, int] = {}
    for _code, display in resolved:
        name_totals[display] = name_totals.get(display, 0) + 1

    lines: List[str] = []
    name_seen: Dict[str, int] = {}
    for vtt, (lang_code, display) in zip(vtt_filenames, resolved, strict=False):
        name_seen[display] = name_seen.get(display, 0) + 1
        name = (
            f"{display} {name_seen[display]}"
            if name_totals[display] > 1
            else display
        )
        sub_playlist_filename = f"{vtt}.m3u8"
        sub_playlist_path = os.path.join(output_dir, sub_playlist_filename)
        # URI attributes in HLS manifests must be valid URIs — VTT filenames can
        # contain spaces, commas, and other chars that produce malformed manifests
        # if interpolated raw. Escape both the sub-playlist URI in the master and
        # the VTT URI inside the sub-playlist itself.
        sub_playlist_uri = uri_base + urllib.parse.quote(sub_playlist_filename)
        vtt_uri = urllib.parse.quote(vtt)
        _atomic_write(
            sub_playlist_path,
            (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-PLAYLIST-TYPE:VOD\n"
                f"#EXT-X-TARGETDURATION:{target_duration}\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
                f"#EXTINF:{duration:.3f},\n"
                f"{vtt_uri}\n"
                "#EXT-X-ENDLIST\n"
            ),
        )
        # Only emit LANGUAGE for a determined language; an empty LANGUAGE=""
        # is invalid and makes Safari fall back to "Unknown".
        language_attr = f'LANGUAGE="{lang_code}",' if lang_code else ""
        lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",'
            + f"{language_attr}DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,"
            + f'URI="{sub_playlist_uri}"'
        )
    return lines
