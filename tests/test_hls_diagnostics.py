import hls_diagnostics as hd
import hls_ladder
import pytest
import settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    hls_ladder.reset_encoder_cache()
    hd.reset()
    yield
    hls_ladder.reset_encoder_cache()
    hd.reset()


def _speed(monkeypatch, realtime_for_720p: float):
    """Pretend the machine encodes one 720p rendition `realtime_for_720p` times faster than real time."""
    monkeypatch.setattr(hd, "_pixels_per_second", realtime_for_720p * hd.BENCH_FPS * 1280 * 720)


def test_without_a_speed_measurement_the_ladder_is_as_configured():
    ladder = hd.fit_ladder(1920, 1080, [1080, 720, 480, 360])
    assert [r.height for r in ladder] == [1080, 720, 480, 360]


def test_a_fast_machine_keeps_every_rendition(monkeypatch):
    _speed(monkeypatch, 40)
    assert [r.height for r in hd.fit_ladder(1920, 1080, [1080, 720, 480])] == [1080, 720, 480]


def test_a_slow_machine_drops_the_highest_renditions_until_it_keeps_up(monkeypatch):
    _speed(monkeypatch, 3)  # 720p alone at 3x; with 1080p added it is far below 1.5x
    heights = [r.height for r in hd.fit_ladder(1920, 1080, [1080, 720, 480])]
    assert heights[0] < 1080 and heights[-1] == 480
    speed = hd.estimated_speed(hd.hls_ladder.plan_ladder(1920, 1080, heights))
    assert speed is not None and speed >= hd.TARGET_SPEED


def test_a_machine_too_slow_for_anything_still_gets_one_rendition(monkeypatch):
    _speed(monkeypatch, 0.2)
    ladder = hd.fit_ladder(1920, 1080, [1080, 720, 480])
    assert [r.height for r in ladder] == [480]


def test_a_source_smaller_than_the_configured_heights_is_not_upscaled(monkeypatch):
    _speed(monkeypatch, 40)
    assert [r.height for r in hd.fit_ladder(1280, 720, [1080, 720, 480])] == [720, 480]


def test_the_benchmark_encodes_with_the_encoder_in_use():
    software = hd._benchmark_args(hls_ladder.SOFTWARE)
    assert "libx264" in software and "hwupload" not in " ".join(software)
    vaapi = hd._benchmark_args(hls_ladder.VAAPI)
    assert hls_ladder.VAAPI in vaapi and "hwupload" in " ".join(vaapi)


def test_a_failed_benchmark_gives_no_speed(monkeypatch):
    class Result:
        returncode = 1
        stderr = b"Cannot load libcuda.so.1\n"

    monkeypatch.setattr(hd.subprocess, "run", lambda *a, **k: Result())
    assert hd.measure(hls_ladder.SOFTWARE) is None
    assert "libcuda" in hd._error


def test_the_report_says_what_was_tried_and_why_it_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(hls_ladder, "VAAPI_DEVICE", str(tmp_path / "missing"))
    monkeypatch.setattr(hd, "start_measuring", lambda: None)
    monkeypatch.setattr(hd, "_ffmpeg_line", lambda *a: "ffmpeg version 7.0\nlibavutil 59\n")
    monkeypatch.setattr(settings, "HLS_ENCODER", "auto")
    monkeypatch.setattr(settings, "HLS_HWDECODE", "auto")
    result = hd.report()
    assert result["ffmpeg"] == "ffmpeg version 7.0"
    assert result["encoder"]["chosen"] == hls_ladder.SOFTWARE
    assert result["gpu_device"]["present"] is False
    assert any("does not exist" in a["detail"] and not a["worked"] for a in result["attempts"])
    assert any("CPU" in w for w in result["warnings"])


def test_the_report_warns_when_even_the_lowest_level_is_too_slow(monkeypatch):
    monkeypatch.setattr(hd, "start_measuring", lambda: None)
    monkeypatch.setattr(hd, "_ffmpeg_line", lambda *a: "")
    monkeypatch.setattr(settings, "HLS_ENCODER", "software")
    monkeypatch.setattr(settings, "HLS_LADDER_HEIGHTS", [480])
    _speed(monkeypatch, 0.3)
    assert any("slower than real time" in w for w in hd.report()["warnings"])


def test_hdr_tone_mapped_on_the_cpu_leaves_room_for_fewer_renditions(monkeypatch):
    _speed(monkeypatch, 5)  # fine for SDR at 1080p+720p, not once tone mapping costs three times as much
    sdr = [r.height for r in hd.fit_ladder(1920, 1080, [1080, 720])]
    hdr = [r.height for r in hd.fit_ladder(1920, 1080, [1080, 720], hdr_on_cpu=True)]
    assert sdr == [1080, 720] and len(hdr) < len(sdr)
