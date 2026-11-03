"""Plays a real file through a real encode session, the way a player would: in order, with a jump ahead, and
back again. Every segment is checked for what a player needs: the right frame count, fragment times at the
segment's place in the file, audio and video starting together, and the whole thing decoding without error.

Needs ffmpeg and ffprobe; skipped without them.
"""

import json
import os
import shutil
import struct
import subprocess

import fmp4
import hls_sessions as hs
import pytest
import settings

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg and ffprobe are needed"
)

SEGMENT = 6
SOURCE_SECONDS = 45  # 8 segments, the last one 3 s long
FPS = 24


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("src") / "movie.mkv")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={FPS}:duration={SOURCE_SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={SOURCE_SECONDS}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", "-ac", "2", path,
        ],
        check=True,
    )
    return path


@pytest.fixture
def playback(source, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HLS_LADDER_HEIGHTS", [480])
    monkeypatch.setattr(settings, "HLS_ENCODER", "software")
    monkeypatch.setattr(settings, "HLS_HWDECODE", "off")
    monkeypatch.setattr(settings, "HLS_LIMIT_BY_SPEED", False)
    monkeypatch.setattr(settings, "HLS_X264_PRESET", "ultrafast")
    monkeypatch.setattr(settings, "HLS_SEGMENT_DURATION", SEGMENT)
    manager = hs.SessionManager(str(tmp_path / "sessions"))
    session = manager.create("hash/movie.mkv", source, 0.0)
    yield manager, session
    manager.close_all()


def _fetch(manager, session, name):
    path = manager.file_for(session, name)
    assert path is not None, f"{name} was never produced"
    with open(path, "rb") as f:
        return f.read()


def _segment_facts(init: bytes, data: bytes, tmp_path):
    """Frames, durations and start times of each track in one segment, read with ffprobe."""
    joined = tmp_path / "probe.mp4"
    joined.write_bytes(init + data)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,codec_type,pts_time,duration_time", "-of", "json", str(joined)],
        capture_output=True, check=True,
    ).stdout
    facts: dict[str, dict[str, float]] = {}
    for packet in json.loads(out)["packets"]:
        kind = packet["codec_type"]
        pts, duration = float(packet["pts_time"]), float(packet.get("duration_time") or 0)
        entry = facts.setdefault(kind, {"count": 0, "start": pts, "end": pts + duration})
        entry["count"] += 1
        entry["start"] = min(entry["start"], pts)
        entry["end"] = max(entry["end"], pts + duration)
    return facts


def _tfdt_seconds(init: bytes, data: bytes) -> dict[int, float]:
    timescales = fmp4.track_timescales(init)
    found: dict[int, float] = {}
    track = None
    for kind, payload, _end in fmp4._walk(data, 0, len(data)):  # pyright: ignore[reportPrivateUsage]
        if kind == b"tfhd":
            track = struct.unpack_from(">I", data, payload + 4)[0]
        elif kind == b"tfdt" and track in timescales and track not in found:
            field = ">Q" if data[payload] == 1 else ">I"
            found[track] = struct.unpack_from(field, data, payload + 4)[0] / timescales[track]
    return found


def _check_segment(manager, session, init, index, tmp_path):
    data = _fetch(manager, session, hs.segment_name(0, index))
    expected = min(SEGMENT, SOURCE_SECONDS - index * SEGMENT)
    facts = _segment_facts(init, data, tmp_path)
    assert set(facts) == {"video", "audio"}, f"segment {index} lacks a track: {facts}"
    # A whole segment, not one cut short when an encoder was stopped.
    assert abs(facts["video"]["count"] - expected * FPS) <= 2, f"segment {index}: {facts['video']['count']} frames"
    # Fragment times at the segment's place in the file, so a player puts it where it belongs.
    for track, seconds in _tfdt_seconds(init, data).items():
        assert abs(seconds - index * SEGMENT) < 0.1, f"segment {index} track {track} starts at {seconds}"
    # Picture and sound start together.
    assert abs(facts["video"]["start"] - facts["audio"]["start"]) < 0.1
    return data


def test_a_session_plays_in_order_after_a_jump_and_back(playback, tmp_path):
    manager, session = playback
    init = _fetch(manager, session, "init_v0.mp4")
    assert len(init) > 0

    checked: dict[int, bytes] = {}
    # In order from the start, then a seek ahead (the encoder restarts there), on, back (restarts again), and the end.
    for index in (0, 1, 2, 5, 6, 3, 4, 7):
        checked[index] = _check_segment(manager, session, init, index, tmp_path)

    # Every segment together decodes cleanly, with no gap between them.
    assert sorted(checked) == list(range(8))
    whole = tmp_path / "whole.mp4"
    whole.write_bytes(init + b"".join(checked[i] for i in range(8)))
    decode = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-i", str(whole), "-f", "null", "-"],
        capture_output=True,
    )
    assert decode.returncode == 0 and not decode.stderr.strip(), decode.stderr.decode()
    duration = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(whole)],
            capture_output=True, check=True,
        ).stdout
    )
    assert abs(duration - SOURCE_SECONDS) < 0.5


def test_stopping_the_encoder_midway_leaves_no_short_segment_behind(playback, tmp_path):
    manager, session = playback
    init = _fetch(manager, session, "init_v0.mp4")
    _fetch(manager, session, hs.segment_name(0, 0))
    # Jump away while the first encoder is still running ahead of the viewer, then come back to what it was on.
    _fetch(manager, session, hs.segment_name(0, 6))
    for index in range(0, 7):
        _check_segment(manager, session, init, index, tmp_path)
    assert all(os.path.getsize(os.path.join(session.directory, hs.segment_name(0, i))) > 0 for i in hs.produced_indexes(session.directory))
