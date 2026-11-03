"""What the encoder setup found on this machine, and how fast it is.

The report says which encoder and decoder were chosen and why the others were not, so a bug report from
unusual hardware can say what happened. The speed measurement lets a slow machine encode fewer renditions
at once instead of falling behind real time and stalling.
"""

import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Sequence

import hls_ladder
import settings

# A rung set must encode at least this many times faster than real time to be offered: the margin covers
# the work the benchmark leaves out (decoding the source, audio, the pipe that feeds it) and seeks.
TARGET_SPEED = 1.5
BENCH_SECONDS = 8
BENCH_FPS = 24
BENCH_WIDTH, BENCH_HEIGHT = 1280, 720
# Tone mapping HDR on the CPU costs about this many times the rest of the work (measured on a 1080p HEVC
# clip, one 720p rendition, veryfast x264: 3.5 s against 1.3 s).
HDR_CPU_COST = 3.0

_lock = threading.Lock()
_pixels_per_second: float | None = None  # encoded pixels per wall-clock second, once measured
_state = "idle"  # idle, running, done, failed
_error = ""


def _benchmark_args(encoder: str) -> List[str]:
    """ffmpeg arguments to encode a few seconds of noisy 720p video the way a session would, to nowhere."""
    source = ["-f", "lavfi", "-i", f"testsrc2=size={BENCH_WIDTH}x{BENCH_HEIGHT}:rate={BENCH_FPS}:duration={BENCH_SECONDS},noise=alls=20:allf=t"]
    rate = hls_ladder.bitrate_for_height(BENCH_HEIGHT)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if encoder == hls_ladder.VAAPI:
        args += ["-init_hw_device", f"vaapi=va:{hls_ladder.VAAPI_DEVICE}", "-filter_hw_device", "va"]
        args += source + ["-vf", "format=nv12,hwupload", "-c:v", hls_ladder.VAAPI]
        if hls_ladder.vaapi_low_power():
            args += ["-low_power", "1"]
    else:
        args += source + ["-c:v", hls_ladder.SOFTWARE, "-preset", settings.HLS_X264_PRESET, "-pix_fmt", "yuv420p"]
    return args + ["-b:v", f"{rate}k", "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k", "-f", "null", "-"]


def measure(encoder: str) -> float | None:
    """Encoded pixels per second with `encoder`, or None when the test encode fails."""
    global _error
    started = time.monotonic()
    try:
        result = subprocess.run(_benchmark_args(encoder), capture_output=True, timeout=120, check=False)
    except Exception as e:
        _error = str(e)
        return None
    elapsed = time.monotonic() - started
    if result.returncode != 0 or elapsed <= 0:
        _error = hls_ladder.last_line(result.stderr)
        return None
    return BENCH_SECONDS * BENCH_FPS * BENCH_WIDTH * BENCH_HEIGHT / elapsed


def start_measuring() -> None:
    """Measure the encoder in the background, once. Sessions plan without the result until it is in."""
    global _state
    with _lock:
        if _state != "idle":
            return
        _state = "running"

    def run() -> None:
        global _pixels_per_second, _state
        value = measure(hls_ladder.detect_encoder(settings.HLS_ENCODER))
        with _lock:
            _pixels_per_second = value
            _state = "done" if value else "failed"

    threading.Thread(target=run, daemon=True).start()


def reset() -> None:
    global _pixels_per_second, _state, _error
    with _lock:
        _pixels_per_second, _state, _error = None, "idle", ""


def estimated_speed(ladder: Sequence[hls_ladder.Rendition], hdr_on_cpu: bool = False) -> float | None:
    """How many times faster than real time this ladder should encode, None when the speed is not known yet.
    `hdr_on_cpu`: the source is HDR and is tone-mapped on the CPU, which makes everything slower."""
    throughput = _pixels_per_second
    if not throughput or not ladder:
        return None
    speed = throughput / (BENCH_FPS * sum(r.width * r.height for r in ladder))
    return speed / HDR_CPU_COST if hdr_on_cpu else speed


def fit_ladder(src_width: int, src_height: int, heights: Sequence[int], hdr_on_cpu: bool = False) -> List[hls_ladder.Rendition]:
    """The configured ladder, with the highest renditions dropped while this machine could not encode them
    all faster than real time. Never fewer than one rendition; the speed unknown means the ladder as asked."""
    wanted = list(heights)
    ladder = hls_ladder.plan_ladder(src_width, src_height, wanted)
    while len(ladder) > 1:
        speed = estimated_speed(ladder, hdr_on_cpu)
        if speed is None or speed >= TARGET_SPEED:
            break
        wanted = [h for h in wanted if h < ladder[0].height]
        ladder = hls_ladder.plan_ladder(src_width, src_height, wanted)
    return ladder


def _ffmpeg_line(*args: str) -> str:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, timeout=10, check=False).stdout
    except Exception:
        return ""
    return out.decode("utf-8", "replace")


def report() -> Dict[str, Any]:
    """Everything the diagnostics view shows."""
    start_measuring()
    device = hls_ladder.VAAPI_DEVICE
    encoder = hls_ladder.detect_encoder(settings.HLS_ENCODER, device)
    decode = hls_ladder.detect_hw_decode(settings.HLS_HWDECODE, device)
    version = _ffmpeg_line("-version").splitlines()[:1]
    hwaccels = [line.strip() for line in _ffmpeg_line("-hwaccels").splitlines()[1:] if line.strip()]
    speed = estimated_speed(hls_ladder.plan_ladder(1920, 1080, settings.HLS_LADDER_HEIGHTS))
    warnings: List[str] = []
    if encoder == hls_ladder.SOFTWARE and settings.HLS_ENCODER != "software":
        warnings.append("No hardware encoder works here, so video is encoded on the CPU.")
    if _state == "failed":
        warnings.append("The speed test failed, so the quality levels are not limited to what this machine can encode.")
    if speed is not None and speed < 1.0:
        warnings.append(
            "This machine encodes slower than real time even at the lowest quality level, so playback will stall. "
            + "A faster x264 preset (ultrafast), a hardware encoder or fewer quality levels would help."
        )
    return {
        "ffmpeg": version[0] if version else "not found",
        "hwaccels": hwaccels,
        "cpu_count": os.cpu_count() or 0,
        "gpu_device": {"path": device, "present": os.path.exists(device)},
        "encoder": {"setting": settings.HLS_ENCODER, "chosen": encoder, "low_power": hls_ladder.vaapi_low_power()},
        "decode": {"setting": settings.HLS_HWDECODE, "on_gpu": decode},
        "attempts": [{"what": w, "worked": ok, "detail": d} for (w, ok, d) in hls_ladder.detection_log],
        "speed": {
            "state": _state,
            "pixels_per_second": _pixels_per_second,
            "full_ladder_speed": round(speed, 2) if speed else None,
            "target": TARGET_SPEED,
            "error": _error,
        },
        "configured_heights": list(settings.HLS_LADDER_HEIGHTS),
        "warnings": warnings,
    }
