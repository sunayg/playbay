"""Subtitles looked up by file name while a file is still downloading, and replaced when it is done."""

import os
import shutil
import time
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock

import pytest

from app import app as app_module
from app.rapidbaydaemon import RapidBayDaemon

settings = app_module.settings
HASH = "a" * 40


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RapidBayDaemon:
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(settings, "SUBTITLE_LANGUAGES", ["en"])
    d = RapidBayDaemon.__new__(RapidBayDaemon)
    d._subtitle_gate = Lock()
    d._early_subtitles = set()
    return d


def test_a_file_that_is_downloading_is_looked_up_once_by_name_only(daemon, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.rapidbaydaemon.subtitles.download_all_subtitles", lambda path, **kw: calls.append({"path": path, **kw}))
    monkeypatch.setattr(RapidBayDaemon, "_emit_vtts_for_hls", staticmethod(lambda path, out: calls.append({"emit": out})))
    path = os.path.join(settings.DOWNLOAD_DIR, HASH, "Show.S01E01.mkv")

    daemon._start_early_subtitles(HASH, "Show.S01E01.mkv", path)
    daemon._start_early_subtitles(HASH, "Show.S01E01.mkv", path)  # every poll of the status asks again
    for _ in range(50):
        if len(calls) >= 2:
            break
        time.sleep(0.05)

    assert calls[0] == {"path": path, "early": True}
    assert calls[1] == {"emit": os.path.join(settings.OUTPUT_DIR, HASH)}
    assert len(calls) == 2
    assert os.path.isdir(os.path.join(settings.OUTPUT_DIR, HASH))


def test_no_lookup_without_subtitle_languages(daemon, monkeypatch):
    monkeypatch.setattr(settings, "SUBTITLE_LANGUAGES", [])
    started = MagicMock()
    monkeypatch.setattr(daemon, "_run_early_subtitles", started, raising=False)
    daemon._start_early_subtitles(HASH, "a.mkv", "/x/a.mkv")
    started.assert_not_called()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg converts the subtitles")
def test_subtitles_from_the_finished_file_replace_the_early_ones(tmp_path):
    downloads, output = tmp_path / "d", tmp_path / "o"
    downloads.mkdir()
    output.mkdir()
    video = downloads / "Show.mkv"
    srt, vtt = downloads / "Show.en.srt", output / "Show_en.vtt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    vtt.write_text("WEBVTT\n\nold\n")
    os.utime(vtt, (1, 1))  # older than the SRT, as a name-matched one fetched before the download ended

    RapidBayDaemon._emit_vtts_for_hls(str(video), str(output))

    assert "Hello" in vtt.read_text()
