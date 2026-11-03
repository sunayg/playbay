import pytest
import settings
from settings_schema import FIELDS, ValidationError, coerce, to_wire

EXPECTED = {
    "JACKETT_HOST", "JACKETT_API_KEY", "PROWLARR_HOST", "PROWLARR_API_KEY",
    "MOVE_RESULTS_TO_BOTTOM_CONTAINING_STRINGS",
    "TORRENT_LISTENING_PORT", "DHT_ROUTERS", "MAX_TORRENT_AGE_HOURS", "TORRENT_DOWNLOAD_LIMIT",
    "TORRENT_UPLOAD_LIMIT", "SUPPORTED_EXTENSIONS", "MAX_OUTPUT_FILE_AGE",
    "HLS_LADDER_HEIGHTS", "HLS_LIMIT_BY_SPEED", "HLS_SEGMENT_DURATION", "HLS_START_SECONDS", "MAX_PARALLEL_HLS_STREAMS",
    "HLS_PREFETCH_BYTES",
    "AAC_BITRATE", "CAST_ORIGIN", "AAC_CHANNELS", "AUDIO_DOWNMIX", "AUDIO_DOWNMIX_BOOST", "VIDEO_EXTENSIONS",
    "SUBTITLE_LANGUAGES", "OPENSUBTITLES_USERNAME", "OPENSUBTITLES_PASSWORD", "TMDB_API_KEY",
    "EXCLUDE_TRACKERS_FROM_TRENDING", "EXCLUDE_CATEGORIES_FROM_TRENDING", "TRENDING_TITLE_BLOCKLIST",
    "TRENDING_BLOCK_NON_LATIN_TITLES",
    "AUTO_PLAY_NEXT_FILE", "PASSWORD",
}


def test_every_editable_setting_has_a_field():
    assert set(FIELDS) == EXPECTED
    assert len(FIELDS) == 34
    for key in FIELDS:
        assert hasattr(settings, key)
    assert {k for k, f in FIELDS.items() if f.restart_needed} == {"TORRENT_LISTENING_PORT", "DHT_ROUTERS"}
    assert {k for k, f in FIELDS.items() if f.secret} == {
        "JACKETT_API_KEY", "PROWLARR_API_KEY", "OPENSUBTITLES_PASSWORD", "TMDB_API_KEY", "PASSWORD",
    }


def test_host_must_be_http_url():
    f = FIELDS["PROWLARR_HOST"]
    for bad in ("ftp://x", "javascript:1", "http://", "nohost"):
        with pytest.raises(ValidationError):
            coerce(f, bad)
    assert coerce(f, "  http://prowlarr:9696/  ") == "http://prowlarr:9696"
    assert coerce(f, "") == ""


def test_aac_bitrate():
    f = FIELDS["AAC_BITRATE"]
    assert coerce(f, "256k") == "256k"
    assert coerce(f, "192") == "192"
    for bad in ("256k; rm", "-1k", "k"):
        with pytest.raises(ValidationError):
            coerce(f, bad)


def test_lists_are_cleaned():
    assert coerce(FIELDS["SUBTITLE_LANGUAGES"], [" en ", "", "de", "en"]) == ["en", "de"]
    with pytest.raises(ValidationError, match="x"):
        coerce(FIELDS["EXCLUDE_CATEGORIES_FROM_TRENDING"], ["5000", "x"])
    assert coerce(FIELDS["DHT_ROUTERS"], ["router.example.com:6881"]) == [("router.example.com", 6881)]
    for bad in ("nohostport", "h:99999"):
        with pytest.raises(ValidationError):
            coerce(FIELDS["DHT_ROUTERS"], [bad])
    assert to_wire(FIELDS["DHT_ROUTERS"], [("a", 1)]) == ["a:1"]


def test_blocklist_limits():
    f = FIELDS["TRENDING_TITLE_BLOCKLIST"]
    with pytest.raises(ValidationError, match=r"\("):
        coerce(f, ["("])
    with pytest.raises(ValidationError):
        coerce(f, ["a" * 201])
    with pytest.raises(ValidationError):
        coerce(f, [f"a{i}" for i in range(51)])
    assert coerce(f, [r"\bcam\b"]) == [r"\bcam\b"]


def test_ints_and_ranges():
    port = FIELDS["TORRENT_LISTENING_PORT"]
    assert coerce(port, None) is None
    assert coerce(port, 6881) == 6881
    for bad in (0, 65536, "x"):
        with pytest.raises(ValidationError):
            coerce(port, bad)
    limit = FIELDS["TORRENT_DOWNLOAD_LIMIT"]
    assert coerce(limit, -1) == -1
    with pytest.raises(ValidationError):
        coerce(limit, -2)
    ch = FIELDS["AAC_CHANNELS"]
    assert coerce(ch, 8) == 8
    with pytest.raises(ValidationError):
        coerce(ch, 9)
    flag = FIELDS["TRENDING_BLOCK_NON_LATIN_TITLES"]
    assert coerce(flag, True) is True
    for bad in ("yes", 1):
        with pytest.raises(ValidationError):
            coerce(flag, bad)


def test_errors_never_contain_secret():
    f = FIELDS["PASSWORD"]
    with pytest.raises(ValidationError) as exc:
        coerce(f, "bad value;")
    assert "bad value;" not in exc.value.message
    assert coerce(f, "") == ""
    assert coerce(f, "p4ss-w0rd_!") == "p4ss-w0rd_!"
    with pytest.raises(ValidationError):
        coerce(f, "pässword")


def test_ladder_heights_are_validated():
    f = FIELDS["HLS_LADDER_HEIGHTS"]
    assert coerce(f, ["1080", "720", "480"]) == [1080, 720, 480]
    for bad in ([], ["50"], ["9000"], ["tall"]):
        with pytest.raises(ValidationError):
            coerce(f, bad)
    assert to_wire(f, [1080, 720]) == ["1080", "720"]


def test_choice_settings_accept_only_their_listed_options():
    f = FIELDS["AUDIO_DOWNMIX"]
    assert coerce(f, "nightmode") == "nightmode"
    for bad in ("loud", "", None, 3):
        with pytest.raises(ValidationError):
            coerce(f, bad)
    boost = FIELDS["AUDIO_DOWNMIX_BOOST"]
    assert coerce(boost, 150) == 150
    for bad in (10, 400):
        with pytest.raises(ValidationError):
            coerce(boost, bad)
