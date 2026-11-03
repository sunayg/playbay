"""Adaptive-bitrate HLS ladder: which renditions to encode and the ffmpeg command for them.

One ffmpeg process decodes the source once, splits it into several renditions
and writes one fMP4 media playlist per rendition. Every rendition cuts
segments at the same timestamps, so a player can switch between them at any
segment boundary.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence

# Video bitrate (kbit/s) for the usual heights; other heights are interpolated.
_BITRATE_BY_HEIGHT = {2160: 14000, 1440: 8000, 1080: 5000, 720: 2800, 480: 1400, 360: 800, 240: 400}
MIN_VIDEO_KBPS = 300
# The encoder may overshoot its target; the master playlist advertises the peak.
MAXRATE_FACTOR = 1.07
AUDIO_KBPS = 128

SOFTWARE = "libx264"
VAAPI = "h264_vaapi"
VAAPI_DEVICE = "/dev/dri/renderD128"
# High profile, level 4.1: covers every rung up to 1080p60 and is what CODECS advertises.
H264_CODEC_TAG = "avc1.640029"
AAC_CODEC_TAG = "mp4a.40.2"


@dataclass(frozen=True)
class Rendition:
    index: int
    width: int
    height: int
    video_kbps: int

    @property
    def maxrate_kbps(self) -> int:
        return int(self.video_kbps * MAXRATE_FACTOR)

    @property
    def bufsize_kbps(self) -> int:
        return self.video_kbps * 2

    @property
    def bandwidth(self) -> int:
        """Peak bits per second, video plus audio, as EXT-X-STREAM-INF wants it."""
        return (self.maxrate_kbps + AUDIO_KBPS) * 1000

    @property
    def playlist_suffix(self) -> str:
        return f"_v{self.index}.m3u8"


@dataclass(frozen=True)
class AudioTrack:
    """One audio stream of the source. `index` counts audio streams only (0 is the first)."""

    index: int
    codec: str
    language: str  # as tagged in the file, usually a 3-letter code ("" when untagged)
    title: str
    channels: int = 2
    layout: str = ""  # ffprobe's name for the channel layout, such as "5.1" or "5.1(side)"
    profile: str = ""  # "LC", "HE-AAC", ...

    @property
    def is_plain_aac(self) -> bool:
        """AAC-LC, which is what the playlist advertises and what every browser plays."""
        return self.codec == "aac" and self.profile in ("", "LC")


# How a surround track is mixed down to stereo. The weights are the gain of each channel group in the
# result. ffmpeg's own mix gives the centre channel, where the dialogue is, -3 dB and then lowers everything
# to avoid clipping, which leaves speech 6 to 8 dB quiet; these keep it up front. "dave750" and "nightmode"
# are the recipes Jellyfin offers (from a Super User answer); "dialogue" is a milder one.
DOWNMIX_CHOICES = (
    ("dialogue", "Dialogue (recommended)"),
    ("default", "Default (ffmpeg's own mix)"),
    ("dave750", "Dave750"),
    ("nightmode", "Night mode (strong dialogue)"),
)
_RECIPES = {
    #              centre, front, surround, lfe
    "dialogue": (1.0, 0.707, 0.5, 0.0),
    "dave750": (0.5, 0.707, 0.707, 0.5),
    "nightmode": (1.0, 0.30, 0.30, 0.60),
}
# The channels of each layout: (front pair, surround pairs). All have FC and LFE except stereo.
_LAYOUTS = {
    "5.1": (("FL", "FR"), [("BL", "BR")]),
    "5.1(side)": (("FL", "FR"), [("SL", "SR")]),
    "7.1": (("FL", "FR"), [("BL", "BR"), ("SL", "SR")]),
}


def downmix_filter(track: "AudioTrack", target_channels: int, algorithm: str = "dialogue", boost_percent: int = 100) -> str | None:
    """An audio filter that mixes a surround track down to stereo, or None when the track needs none.

    `algorithm` is one of DOWNMIX_CHOICES; "default" leaves the mix to ffmpeg. `boost_percent` raises or
    lowers the result (a limiter catches any peak the boost creates). Layouts with no recipe are left to
    ffmpeg's default mix.
    """
    if target_channels != 2 or track.channels <= 2:
        return None
    layout = _LAYOUTS.get(track.layout)
    boost = max(0.1, boost_percent / 100.0)
    chain: List[str] = []
    if layout is not None and algorithm in _RECIPES:
        centre, front, surround, lfe = _RECIPES[algorithm]
        (fl, fr), pairs = layout
        share = surround / len(pairs) if len(pairs) > 1 else surround  # 7.1 splits it between its two pairs
        terms = {"L": [("FC", centre), (fl, front), ("LFE", lfe)], "R": [("FC", centre), (fr, front), ("LFE", lfe)]}
        for left, right in pairs:
            terms["L"].append((left, share))
            terms["R"].append((right, share))
        sides = ["FL=" + "+".join(f"{w:g}*{c}" for c, w in terms["L"] if w > 0), "FR=" + "+".join(f"{w:g}*{c}" for c, w in terms["R"] if w > 0)]
        chain.append("pan=stereo|" + "|".join(sides))
    if boost != 1.0:
        chain.append(f"volume={boost:g}")
    if chain:
        chain.append("alimiter=limit=0.97")
    return ",".join(chain) or None


def can_copy_audio(tracks: Sequence["AudioTrack"], target_channels: int) -> bool:
    """Whether the audio can go into the stream as it is: AAC-LC, and no more channels than wanted."""
    return bool(tracks) and all(t.is_plain_aac and t.channels <= target_channels for t in tracks)


def probe_audio_tracks(filepath: str) -> List[AudioTrack] | None:
    """The audio streams of a file, or None when ffprobe could not read it."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name,profile,channels,channel_layout:stream_tags=language,title",
                "-of", "json", filepath,
            ],
            capture_output=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        streams: List[Dict[str, Any]] = json.loads(result.stdout or b"{}").get("streams", [])
    except Exception:
        return None
    tracks: List[AudioTrack] = []
    for position, stream in enumerate(streams):
        tags: Dict[str, Any] = stream.get("tags") or {}
        language = str(tags.get("language") or "")
        tracks.append(
            AudioTrack(
                position,
                str(stream.get("codec_name") or ""),
                "" if language == "und" else language,
                str(tags.get("title") or ""),
                int(stream.get("channels") or 2),
                str(stream.get("channel_layout") or ""),
                str(stream.get("profile") or ""),
            )
        )
    return tracks


def bitrate_for_height(height: int) -> int:
    """Video kbit/s for a height: the table value, or interpolated by pixel area between its neighbours."""
    if height in _BITRATE_BY_HEIGHT:
        return _BITRATE_BY_HEIGHT[height]
    heights = sorted(_BITRATE_BY_HEIGHT)
    if height < heights[0]:
        return max(MIN_VIDEO_KBPS, int(_BITRATE_BY_HEIGHT[heights[0]] * (height / heights[0]) ** 2))
    if height > heights[-1]:
        return _BITRATE_BY_HEIGHT[heights[-1]]
    lo = max(h for h in heights if h < height)
    hi = min(h for h in heights if h > height)
    share = (height**2 - lo**2) / (hi**2 - lo**2)
    return int(_BITRATE_BY_HEIGHT[lo] + (_BITRATE_BY_HEIGHT[hi] - _BITRATE_BY_HEIGHT[lo]) * share)


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def plan_ladder(src_width: int, src_height: int, heights: Sequence[int]) -> List[Rendition]:
    """Renditions to encode, highest first. Never upscales: a source smaller than the
    configured heights gets a single rendition at its own size."""
    wanted = sorted({h for h in heights if h > 0}, reverse=True)
    if src_width <= 0 or src_height <= 0 or not wanted:
        return []
    top = min(src_height, wanted[0])
    rungs = [top] + [h for h in wanted if h < top]
    ladder: List[Rendition] = []
    for index, height in enumerate(rungs):
        width = src_width if height == src_height else _even(src_width * height / src_height)
        ladder.append(Rendition(index, _even(width), _even(height), bitrate_for_height(height)))
    return ladder


def probe_video_size(filepath: str) -> tuple[int, int] | None:
    """Width and height of the first video stream, or None if ffprobe cannot tell."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", filepath],
            capture_output=True,
            timeout=20,
            check=False,
        )
        stream = json.loads(result.stdout or b"{}")["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except Exception:
        return None
    return (width, height) if width > 0 and height > 0 else None


def probe_dynamic_range(filepath: str) -> str:
    """"sdr", "pq" (HDR10, or Dolby Vision with an HDR10 base layer), "hlg", or "dovi" (Dolby Vision profile 5,
    whose colours only a Dolby-aware player can show: tone-mapped as HDR10 they come out green and purple)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=color_transfer,color_primaries:stream_side_data=dv_profile",
                "-of", "json", filepath,
            ],
            capture_output=True, timeout=20, check=False,
        )
        stream = json.loads(result.stdout or b"{}")["streams"][0]
    except Exception:
        return "sdr"
    side_data_list: List[Dict[str, Any]] = stream.get("side_data_list") or []
    for side_data in side_data_list:
        if int(side_data.get("dv_profile") or 0) == 5:
            return "dovi"
    transfer = str(stream.get("color_transfer") or "")
    if transfer == "smpte2084":
        return "pq"
    if transfer == "arib-std-b67":
        return "hlg"
    return "sdr"


_encoder_cache: dict[str, str] = {}

# What the detection tried and how each try went, for the diagnostics view: (what, worked, detail).
detection_log: list[tuple[str, bool, str]] = []


def last_line(output: bytes | str | None) -> str:
    """The line of a command's error output that says what went wrong, for the log: the first one that names a
    cause (ffmpeg ends with generic lines such as "Nothing was written into output file"), else the last."""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else (output or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if any(word in line for word in ("not supported", "Failed", "Cannot", "Impossible", "No such", "denied", "Unknown")):
            return line[:300]
    return lines[-1][:300] if lines else ""


def detect_encoder(preference: str, device: str = VAAPI_DEVICE) -> str:
    """Pick the H.264 encoder. `preference` is "auto", "software" or "vaapi".

    Hardware is used only if a real test encode with a bitrate cap succeeds: many
    Intel iGPUs expose VAAPI but only support constant-QP, which cannot honour the
    ladder's bitrates. The result is cached for the life of the process.
    """
    if preference == "software":
        return SOFTWARE
    if preference in _encoder_cache:
        return _encoder_cache[preference]
    chosen = SOFTWARE
    if not os.path.exists(device):
        detection_log.append(("VAAPI device", False, f"{device} does not exist: no GPU is mapped into the container"))
    elif _vaapi_works(device):
        chosen = VAAPI
    _encoder_cache[preference] = chosen
    return chosen


def fall_back_to_software() -> None:
    """The hardware encoder was chosen but sessions keep dying on it: use software from now on."""
    for preference in ("auto", "vaapi"):
        _encoder_cache[preference] = SOFTWARE


# Many Intel iGPUs (Kaby Lake and later) only offer the low-power H.264 encoder, which h264_vaapi
# uses only when asked with -low_power 1. Set by _vaapi_works when that is the one that works.
_vaapi_low_power = False


def vaapi_low_power() -> bool:
    """Whether the VAAPI encoder that works here is the low-power one."""
    return _vaapi_low_power


def reset_encoder_cache() -> None:
    global _vaapi_low_power
    _encoder_cache.clear()
    _hw_decode_cache.clear()
    _tonemap_cache.clear()
    detection_log.clear()
    _vaapi_low_power = False


def _vaapi_works(device: str) -> bool:
    """Whether a test encode with a bitrate cap succeeds, with the normal encoder or else the low-power one."""
    global _vaapi_low_power
    for low_power in (False, True):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=1",
            "-vf", "format=nv12,hwupload",
            "-c:v", VAAPI, *(["-low_power", "1"] if low_power else []),
            "-b:v", "2M", "-maxrate", "2M", "-bufsize", "4M",
            "-f", "null", "-",
        ]
        label = "VAAPI encode, low-power mode" if low_power else "VAAPI encode"
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
            ok, detail = result.returncode == 0, last_line(result.stderr)
        except Exception as e:
            ok, detail = False, str(e)
        detection_log.append((label, ok, detail))
        if ok:
            _vaapi_low_power = low_power
            return True
    return False


_tonemap_cache: dict[str, bool] = {}


def detect_vaapi_tonemap(device: str = VAAPI_DEVICE) -> bool:
    """Whether the GPU can tone-map HDR to SDR (`tonemap_vaapi`), found by doing it to a test pattern."""
    if device not in _tonemap_cache:
        _tonemap_cache[device] = os.path.exists(device) and _vaapi_tonemap_works(device)
    return _tonemap_cache[device]


def _vaapi_tonemap_works(device: str) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=1",
        "-vf",
        "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,"
        + "format=p010le,hwupload,tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709,hwdownload,format=nv12",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
        ok, detail = result.returncode == 0, last_line(result.stderr)
    except Exception as e:
        ok, detail = False, str(e)
    detection_log.append(("GPU tone mapping", ok, detail))
    return ok


_hw_decode_cache: dict[str, bool] = {}


def detect_hw_decode(preference: str, device: str = VAAPI_DEVICE) -> bool:
    """Whether the source can be decoded on the GPU. Independent of the encoder: a chip that cannot
    honour the ladder's bitrates in hardware can still decode, which takes the heaviest load (a 10-bit
    HEVC file) off the CPU. `preference` is "auto" or "off". Cached for the life of the process."""
    if preference == "off":
        return False
    if preference not in _hw_decode_cache:
        _hw_decode_cache[preference] = os.path.exists(device) and _hw_decode_works(device)
    return _hw_decode_cache[preference]


def _hw_decode_works(device: str) -> bool:
    """Decode a one-second clip with the output kept on the GPU: if the GPU cannot, ffmpeg fails (rather than
    quietly decoding in software, as it does when the frames may come back to memory)."""
    try:
        clip = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
             "-i", "testsrc2=size=1280x720:rate=24:duration=1", "-c:v", "libx264", "-f", "mpegts", "pipe:1"],
            capture_output=True, timeout=20, check=False,
        ).stdout
        if not clip:
            detection_log.append(("GPU decode", False, "could not make the test clip"))
            return False
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-hwaccel", "vaapi", "-hwaccel_device", device,
             "-hwaccel_output_format", "vaapi", "-i", "pipe:0", "-f", "null", "-"],
            input=clip, capture_output=True, timeout=20, check=False,
        )
        detection_log.append(("GPU decode", result.returncode == 0, last_line(result.stderr)))
        return result.returncode == 0
    except Exception as e:
        detection_log.append(("GPU decode", False, str(e)))
        return False


# HDR to SDR on the CPU: linear light, BT.709 primaries, Hable tone curve, back to BT.709 video range.
CPU_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv"
)
GPU_TONEMAP = "tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709"


def filter_graph(ladder: Sequence[Rendition], src_height: int, encoder: str, hdr: str = "sdr", gpu_tonemap: bool = False) -> str:
    """The filter graph: the source, tone-mapped to SDR if it is HDR, then split into one scaled picture per
    rendition. An HDR source is scaled down to the top rendition first, so the expensive tone mapping sees
    the fewest pixels. `gpu_tonemap` (VAAPI encoder only) does it on the GPU."""
    count = len(ladder)
    top = ladder[0]
    resize = top.height != src_height
    if hdr != "sdr" and gpu_tonemap and encoder == VAAPI:
        scale_in = f"scale_vaapi=w={top.width}:h={top.height}:format=p010," if resize else ""
        head = f"[0:v:0]format=p010le,hwupload,{scale_in}{GPU_TONEMAP}"
        src_height = top.height  # already at the top rendition's size
    elif hdr != "sdr":
        scale_in = f"scale={top.width}:{top.height}," if resize else ""
        sdr = f"{scale_in}{CPU_TONEMAP}"
        head = f"[0:v:0]{sdr},format=nv12,hwupload" if encoder == VAAPI else f"[0:v:0]{sdr},format=yuv420p"
        src_height = top.height
    elif encoder == VAAPI:
        head = "[0:v:0]format=nv12,hwupload"
    else:
        head = "[0:v:0]format=yuv420p"
    scale = "scale_vaapi=w={w}:h={h}" if encoder == VAAPI else "scale={w}:{h}"
    labels = "".join(f"[s{r.index}]" for r in ladder)
    parts = [f"{head},split={count}{labels}" if count > 1 else f"{head}[s0]"]
    for r in ladder:
        if r.height == src_height:
            parts.append(f"[s{r.index}]null[v{r.index}]")
        else:
            parts.append(f"[s{r.index}]" + scale.format(w=r.width, h=r.height) + f"[v{r.index}]")
    return ";".join(parts)


def _variant_token(variant_count: int) -> str:
    """ffmpeg fills %v in the init file name only when there is more than one variant."""
    return "%v" if variant_count > 1 else "0"


def encode_args(
    ladder: Sequence[Rendition],
    encoder: str,
    preset: str,
    segment_seconds: int,
    audio_args: Sequence[str] | None,
    tone_mapped: bool = False,
) -> List[str]:
    """Codec, rate-control and keyframe options shared by every way of running the ladder."""
    args: List[str] = []
    args += ["-c:v", encoder, "-profile:v", "high"]
    if encoder == VAAPI and _vaapi_low_power:
        args += ["-low_power", "1"]
    if encoder == SOFTWARE:
        args += ["-preset", preset, "-level", "4.1", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]
    args += ["-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})"]
    for r in ladder:
        args += [
            f"-b:v:{r.index}", f"{r.video_kbps}k",
            f"-maxrate:v:{r.index}", f"{r.maxrate_kbps}k",
            f"-bufsize:v:{r.index}", f"{r.bufsize_kbps}k",
        ]
    if audio_args is not None:
        args += list(audio_args)
    if tone_mapped:
        # The pictures are SDR BT.709 now: say so, or the browser may still treat the stream as BT.2020 HDR.
        args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    args += ["-tag:v", "avc1"]
    return args


def audio_media_lines(tracks: Sequence[AudioTrack], first_variant: int, prefix: str, describe: "Callable[[AudioTrack], tuple[str, str]]") -> List[str]:
    """EXT-X-MEDIA lines for the audio tracks. Track k is variant `first_variant + k`.

    `describe` turns a track into (BCP-47 language code, display name).
    """
    lines: List[str] = []
    for track in tracks:
        code, name = describe(track)
        language = f'LANGUAGE="{code}",' if code else ""
        default = "YES" if track.index == 0 else "NO"
        uri = f"{prefix}_v{first_variant + track.index}.m3u8"
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="{name}",{language}DEFAULT={default},AUTOSELECT=YES,URI="{uri}"')
    return lines


def stream_inf_lines(
    ladder: Sequence[Rendition], prefix: str, has_audio: bool, subtitles_group: bool, audio_group: bool = False
) -> List[str]:
    """EXT-X-STREAM-INF entries (tag line plus URI line) for every rendition, best first."""
    lines: List[str] = []
    codecs = f"{H264_CODEC_TAG},{AAC_CODEC_TAG}" if has_audio else H264_CODEC_TAG
    for r in ladder:
        tag = f'#EXT-X-STREAM-INF:BANDWIDTH={r.bandwidth},RESOLUTION={r.width}x{r.height},CODECS="{codecs}"'
        if audio_group:
            tag += ',AUDIO="aud"'
        if subtitles_group:
            tag += ',SUBTITLES="subs"'
        lines += [tag, f"{prefix}{r.playlist_suffix}"]
    return lines


def build_session_args(
    ladder: Sequence[Rendition],
    src_height: int,
    encoder: str,
    preset: str,
    segment_seconds: int,
    source: str,
    audio_args: Sequence[str] | None,
    start_segment: int,
    init_prefix: str,
    audio_tracks: Sequence[AudioTrack] = (),
    audio_channels: int = 2,
    downmix: str = "dialogue",
    downmix_boost: int = 100,
    hw_decode: bool = False,
    hdr: str = "sdr",
    gpu_tonemap: bool = False,
) -> List[str]:
    """ffmpeg arguments for a seekable session: read `source` by path from `start_segment`.

    Output goes to the working directory as `seg_v{N}_{index:04d}.m4s` with the index continuing
    from the start point; the fragment times start at 0 in every run, and the session moves each
    segment to its place before sending it (see fmp4). With more than one audio track, each track is written as an audio-only
    variant after the video ones (so variant `len(ladder) + k` is track k) and the video variants
    carry no audio; with one, the audio is muxed into every video variant. `init_prefix` names the init files (`{init_prefix}_v{N}.mp4`); a
    restart writes its own beside the first run's, which the playlists keep pointing at.
    """
    start = start_segment * segment_seconds
    args: List[str] = ["ffmpeg", "-nostdin", "-v", "error"]
    if encoder == VAAPI:
        args += ["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va"]
    if hw_decode:
        # Frames come back to memory for the filters; a codec the GPU cannot decode falls back to software.
        args += ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE]
    if start > 0:
        args += ["-ss", str(start)]
    args += ["-i", source, "-sn", "-filter_complex", filter_graph(ladder, src_height, encoder, hdr, gpu_tonemap)]
    for r in ladder:
        args += ["-map", f"[v{r.index}]"]
    separate = audio_args is not None and len(audio_tracks) > 1
    if audio_args is not None:
        if separate:
            for track in audio_tracks:
                args += ["-map", f"0:a:{track.index}"]
        else:
            for _ in ladder:
                args += ["-map", "0:a:0"]
    args += encode_args(ladder, encoder, preset, segment_seconds, audio_args, hdr != "sdr")
    if audio_args is not None and "copy" not in audio_args:
        # Each output audio stream gets the downmix its own source track needs.
        outputs = list(audio_tracks) if separate else [audio_tracks[0] if audio_tracks else None] * len(ladder)
        for k, track in enumerate(outputs):
            mix = downmix_filter(track, audio_channels, downmix, downmix_boost) if track is not None else None
            if mix:
                args += [f"-filter:a:{k}", mix]
    if separate:
        parts = [f"v:{r.index}" for r in ladder] + [f"a:{k}" for k in range(len(audio_tracks))]
    else:
        parts = [f"v:{r.index},a:{r.index}" if audio_args is not None else f"v:{r.index}" for r in ladder]
    stream_map = " ".join(parts)
    args += [
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_time", str(segment_seconds),
        "-hls_list_size", "0",
        "-start_number", str(start_segment),
        "-hls_flags", "temp_file",
        # Without edit lists every run writes the same init file; the session puts each segment's fragment
        # times in place instead (the muxer starts them at 0 in every run, whatever -output_ts_offset says).
        "-hls_segment_options", "use_editlist=0",
        "-hls_fmp4_init_filename", f"{init_prefix}_v{_variant_token(len(parts))}.mp4",
        "-hls_segment_filename", "seg_v%v_%04d.m4s",
        "-var_stream_map", stream_map,
        "ff_v%v.m3u8",
    ]
    return args
