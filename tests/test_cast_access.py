"""Cast links let a Chromecast fetch one torrent's files without the password."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app as app_module
from app import cast_access

HASH = "a" * 40


def test_token_is_valid_for_its_hash_until_it_expires() -> None:
    token = cast_access.make_token(HASH, now=1000)

    assert cast_access.verify_token(token, HASH, now=1000)
    assert cast_access.verify_token(token, HASH, now=1000 + cast_access.TOKEN_LIFETIME_SECONDS)
    assert not cast_access.verify_token(token, HASH, now=1000 + cast_access.TOKEN_LIFETIME_SECONDS + 1)


def test_token_does_not_open_another_hash() -> None:
    token = cast_access.make_token(HASH, now=1000)

    assert not cast_access.verify_token(token, "b" * 40, now=1000)


def test_tampered_or_malformed_tokens_are_refused() -> None:
    token = cast_access.make_token(HASH, now=1000)
    expiry, _, signature = token.partition(".")

    assert not cast_access.verify_token(f"{int(expiry) + 1000}.{signature}", HASH, now=1000)
    assert not cast_access.verify_token(f"{expiry}.{'0' * 64}", HASH, now=1000)
    assert not cast_access.verify_token("", HASH, now=1000)
    assert not cast_access.verify_token("nodot", HASH, now=1000)
    assert not cast_access.verify_token("abc.def", HASH, now=1000)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    output = tmp_path / "output"
    (output / HASH / "hls").mkdir(parents=True)
    (output / HASH / "movie.mp4").write_bytes(b"video")
    (output / HASH / "hls" / "segment0.ts").write_bytes(b"segment")
    monkeypatch.setattr(app_module.settings, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(app_module.settings, "PASSWORD", "secret")
    yield TestClient(app_module.app)


def _prefix(client: TestClient) -> str:
    response = client.get(f"/api/cast_link/{HASH}", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    return str(response.json()["prefix"])


def test_play_stays_closed_without_the_password(client: TestClient) -> None:
    assert client.get(f"/play/{HASH}/movie.mp4").status_code == 404


def test_cast_link_needs_the_password(client: TestClient) -> None:
    assert client.get(f"/api/cast_link/{HASH}").status_code == 404


def test_cast_link_serves_files_without_credentials(client: TestClient) -> None:
    prefix = _prefix(client)

    movie = client.get(f"{prefix}movie.mp4")
    segment = client.get(f"{prefix}hls/segment0.ts")

    assert movie.status_code == 200
    assert movie.content == b"video"
    assert movie.headers["access-control-allow-origin"] == "*"
    assert segment.content == b"segment"


def test_cast_link_refuses_other_torrents_and_bad_tokens(client: TestClient) -> None:
    prefix = _prefix(client)
    token = prefix.split("/")[2]

    assert client.get(f"/cast/{token}/{'b' * 40}/movie.mp4").status_code == 404
    altered = token[:-1] + ("1" if token[-1] == "0" else "0")
    assert client.get(f"/cast/{altered}/{HASH}/movie.mp4").status_code == 404
    assert client.get(f"{prefix}missing.mp4").status_code == 404


def test_cast_link_cannot_escape_the_output_directory(client: TestClient) -> None:
    prefix = _prefix(client)
    outside = Path(app_module.settings.OUTPUT_DIR).parent / "secret.txt"
    outside.write_text("nope")

    response = client.get(f"{prefix}..%2f..%2fsecret.txt")

    assert response.status_code == 404
    assert b"nope" not in response.content


class _FakeSessions:
    def __init__(self, session: object) -> None:
        self._session = session

    def get(self, session_id: str) -> object | None:
        return self._session if session_id == "sid1" else None


@pytest.fixture
def session_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from types import SimpleNamespace

    import hls_ladder
    import hls_sessions

    ladder = hls_ladder.plan_ladder(1280, 720, [720, 480])
    session = hls_sessions.Session(
        id="sid1", key=f"{HASH}/Movie.mkv", source="/in.mkv", directory="/nowhere", duration=12.0,
        segment_seconds=6, ladder=ladder, src_height=720, has_audio=True, audio_copy=False, encoder="libx264",
    )
    calls: list[bool] = []

    def master(session_id: str, magnet_hash: str, filename: str, subtitles: bool = True, for_cast: bool = False) -> str:
        calls.append(subtitles and not for_cast)
        return "#EXTM3U\nplaylist_v0.m3u8\n"

    monkeypatch.setattr(app_module, "daemon", SimpleNamespace(sessions=_FakeSessions(session), session_master_playlist=master), raising=False)
    client.subtitle_calls = calls  # type: ignore[attr-defined]
    return client


def test_a_chromecast_can_load_an_encode_session_with_the_signed_link(session_client: TestClient) -> None:
    token = _prefix(session_client).split("/")[2]
    base = f"/cast/{token}/{HASH}/session/sid1"

    master = session_client.get(f"{base}/master.m3u8")
    variant = session_client.get(f"{base}/playlist_v1.m3u8")

    assert master.status_code == 200
    assert master.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert master.headers["access-control-allow-origin"] == "*"
    assert session_client.subtitle_calls == [False]  # type: ignore[attr-defined]  # the sender brings the subtitles
    assert variant.status_code == 200 and variant.text.count("#EXTINF") == 2
    assert session_client.get(f"{base}/playlist_v9.m3u8").status_code == 404


def test_a_cast_session_link_is_refused_for_the_wrong_token_torrent_or_session(session_client: TestClient) -> None:
    token = _prefix(session_client).split("/")[2]
    other = "b" * 40

    assert session_client.get(f"/cast/{token}/{HASH}/session/nope/master.m3u8").status_code == 404
    assert session_client.get(f"/cast/{token}/{other}/session/sid1/master.m3u8").status_code == 404
    altered = token[:-1] + ("1" if token[-1] == "0" else "0")
    assert session_client.get(f"/cast/{altered}/{HASH}/session/sid1/master.m3u8").status_code == 404
    # A token for another torrent cannot open this torrent's session.
    other_token = cast_access.make_token(other)
    assert session_client.get(f"/cast/{other_token}/{other}/session/sid1/master.m3u8").status_code == 404


def test_cast_link_reports_the_configured_origin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.settings, "CAST_ORIGIN", "http://192.168.1.20:5050/")
    response = client.get(f"/api/cast_link/{HASH}", headers={"Authorization": "Bearer secret"})
    assert response.json()["origin"] == "http://192.168.1.20:5050"
    monkeypatch.setattr(app_module.settings, "CAST_ORIGIN", "")
    response = client.get(f"/api/cast_link/{HASH}", headers={"Authorization": "Bearer secret"})
    assert response.json()["origin"] == ""
