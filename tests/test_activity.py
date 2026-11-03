"""The activity page's data: open streams, downloads and kept files, and removing them."""

from pathlib import Path
from unittest.mock import MagicMock

import hls_sessions as hs
import pytest
from fastapi.testclient import TestClient

from app import app as app_module
from app.locking import LockManager
from app.rapidbaydaemon import FileStatus, RapidBayDaemon

settings = app_module.settings

HASH = "a" * 40
OTHER = "b" * 40


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    output, downloads = tmp_path / "output", tmp_path / "downloads"
    output.mkdir()
    downloads.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(downloads))
    return output, downloads


def _daemon(in_progress: dict | None = None) -> RapidBayDaemon:
    daemon = RapidBayDaemon.__new__(RapidBayDaemon)
    daemon.sessions = MagicMock()
    daemon.sessions.snapshot.return_value = [{"id": "s1"}]
    daemon.torrent_client = MagicMock()
    daemon.torrent_client.locks = LockManager()
    daemon.torrent_client.torrents = {}
    daemon._notify_state_change = MagicMock()
    daemon._discard_torrent = MagicMock()
    daemon.downloads = MagicMock(return_value=in_progress or {})  # type: ignore[method-assign]
    return daemon


def test_the_activity_lists_downloads_in_progress_and_kept_files(folders):
    output, _ = folders
    (output / HASH).mkdir()
    (output / HASH / "movie.mkv").write_bytes(b"x" * 1234)
    (output / HASH / "movie.0_en.vtt").write_text("WEBVTT")
    daemon = _daemon({OTHER: {
        "show.mkv": {"status": FileStatus.DOWNLOADING, "progress": 0.4, "peers": 12, "download_rate": 5000},
        "show.nfo": {"status": FileStatus.DOWNLOADING, "progress": 0.1},
        "done.mkv": {"status": FileStatus.READY},
    }})

    result = daemon.activity()

    assert result["streams"] == [{"id": "s1"}]
    by_name = {d["filename"]: d for d in result["downloads"]}
    assert set(by_name) == {"show.mkv", "movie.mkv"}  # not the nfo, not the subtitle, the ready file once
    assert by_name["show.mkv"]["progress"] == 0.4 and by_name["show.mkv"]["peers"] == 12
    assert by_name["movie.mkv"]["status"] == FileStatus.READY and by_name["movie.mkv"]["size"] == 1234
    assert result["disk"]["free"] > 0


def test_removing_a_kept_file_deletes_it_and_its_subtitles_and_ends_its_stream(folders):
    output, downloads = folders
    (output / HASH).mkdir()
    for name in ("movie.mkv", "movie.0_en.vtt", "other.mkv", "other.0_en.vtt"):
        (output / HASH / name).write_bytes(b"x")
    daemon = _daemon()

    assert daemon.remove_file(HASH, "movie.mkv")

    assert sorted(p.name for p in (output / HASH).iterdir()) == ["other.0_en.vtt", "other.mkv"]
    daemon.sessions.close_key.assert_called_once_with(f"{HASH}/movie.mkv")
    daemon._discard_torrent.assert_not_called()  # another file of it is still kept


def test_removing_the_last_kept_file_removes_the_torrent_and_its_download(folders):
    output, downloads = folders
    (output / HASH).mkdir()
    (output / HASH / "movie.mkv").write_bytes(b"x")
    (downloads / HASH).mkdir()
    (downloads / HASH / "movie.mkv").write_bytes(b"x")
    daemon = _daemon()

    assert daemon.remove_file(HASH, "movie.mkv")

    assert not (output / HASH).exists() and not (downloads / HASH).exists()

    # while the torrent is still in the client, it is dropped through the daemon
    (output / OTHER).mkdir()
    (output / OTHER / "movie.mkv").write_bytes(b"x")
    handle = MagicMock()
    daemon.torrent_client.torrents = {OTHER: handle}
    assert daemon.remove_file(OTHER, "movie.mkv")
    daemon._discard_torrent.assert_called_once_with(OTHER, handle)


@pytest.mark.parametrize("hash_,name", [("nothex", "movie.mkv"), (HASH, "../movie.mkv"), (HASH, "sub/movie.mkv"), (HASH, ""), (HASH, "missing.mkv")])
def test_removal_refuses_anything_that_is_not_a_kept_file_here(folders, hash_, name):
    output, _ = folders
    (output / HASH).mkdir()
    (output / HASH / "movie.mkv").write_bytes(b"x")
    assert not _daemon().remove_file(hash_, name)
    assert (output / HASH / "movie.mkv").exists()


def test_a_stream_reports_how_far_it_has_encoded_and_how_fast(tmp_path):
    manager = hs.SessionManager(str(tmp_path))
    from tests.test_hls_sessions import _session

    session = _session(duration=600.0, directory=str(tmp_path))
    session.key = f"{HASH}/movie.mkv"
    session.requested = 2
    for i in range(0, 5):
        (tmp_path / hs.segment_name(0, i)).write_text("x")
    session.rate_samples = [(100.0, 0), (110.0, 3)]  # 3 segments of 6 s in 10 s of wall clock
    manager._sessions[session.id] = session

    [info] = manager.snapshot()

    assert info["hash"] == HASH and info["filename"] == "movie.mkv"
    assert info["position_seconds"] == 12 and info["encoded_seconds"] == 30
    assert info["speed"] == 1.8 and info["levels"] == [720, 480]
    assert info["paused"] is False and info["failed"] is False


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(app_module, "daemon", MagicMock(), raising=False)
    monkeypatch.setattr(app_module.settings, "PASSWORD", "secret")
    return TestClient(app_module.app)


def test_the_activity_routes_need_the_password(client):
    assert client.get("/api/activity").status_code == 404
    assert client.post("/api/activity/streams/abc/end").status_code == 404
    assert client.post(f"/api/activity/downloads/{HASH}/movie.mkv/remove").status_code == 404


def test_the_activity_routes_reach_the_daemon(client):
    headers = {"Authorization": "Bearer secret"}
    app_module.daemon.activity.return_value = {"streams": [], "downloads": [], "disk": {}}
    assert client.get("/api/activity", headers=headers).json() == {"streams": [], "downloads": [], "disk": {}}

    assert client.post("/api/activity/streams/abc/end", headers=headers).json() == {"ok": True}
    app_module.daemon.sessions.close.assert_called_once_with("abc")

    app_module.daemon.remove_file.return_value = True
    assert client.post(f"/api/activity/downloads/{HASH}/movie.mkv/remove", headers=headers).json() == {"ok": True}
    app_module.daemon.cancel_download.assert_not_called()

    app_module.daemon.remove_file.return_value = False  # not a kept file: it is a download in progress
    client.post(f"/api/activity/downloads/{HASH}/show.mkv/remove", headers=headers)
    app_module.daemon.cancel_download.assert_called_once_with(HASH, "show.mkv")


def test_a_tv_report_is_kept_and_read_back_with_the_password(client):
    plain = TestClient(app_module.app)
    app_module._tv_reports.clear()
    assert plain.post("/api/tv_report", json={"userAgent": "VIDAA", "keys": [{"keyCode": 13}]}).json() == {"ok": True}
    assert plain.get("/api/tv_report").status_code == 404  # reading needs the password
    [report] = client.get("/api/tv_report", headers={"Authorization": "Bearer secret"}).json()
    assert report["userAgent"] == "VIDAA" and report["received_at"]


def test_tv_reports_are_bounded_and_must_be_small_json_objects(client):
    app_module._tv_reports.clear()
    plain = TestClient(app_module.app)
    for i in range(app_module._TV_REPORT_LIMIT + 3):
        plain.post("/api/tv_report", json={"n": i})
    assert [r["n"] for r in app_module._tv_reports] == list(range(3, app_module._TV_REPORT_LIMIT + 3))
    assert plain.post("/api/tv_report", content=b"x" * (app_module._TV_REPORT_BYTES + 1)).status_code == 413
    assert plain.post("/api/tv_report", content=b"not json").status_code == 400
    assert plain.post("/api/tv_report", json=[1, 2]).status_code == 400
