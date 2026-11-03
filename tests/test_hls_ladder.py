import hls_ladder
from hls_ladder import (
    SOFTWARE,
    VAAPI,
    Rendition,
    bitrate_for_height,
    plan_ladder,
    stream_inf_lines,
)

HEIGHTS = [1080, 720, 480, 360]


def test_ladder_for_a_1080p_source():
    ladder = plan_ladder(1920, 1080, HEIGHTS)
    assert [(r.width, r.height, r.video_kbps) for r in ladder] == [
        (1920, 1080, 5000), (1280, 720, 2800), (854, 480, 1400), (640, 360, 800),
    ]
    assert [r.index for r in ladder] == [0, 1, 2, 3]


def test_ladder_never_upscales():
    assert [r.height for r in plan_ladder(1280, 720, HEIGHTS)] == [720, 480, 360]
    # smaller than every configured height: one rendition at the source size
    ladder = plan_ladder(320, 240, HEIGHTS)
    assert [(r.width, r.height) for r in ladder] == [(320, 240)]
    assert ladder[0].video_kbps == 400


def test_odd_source_height_becomes_the_top_rung():
    ladder = plan_ladder(1920, 800, HEIGHTS)  # scope film
    assert [r.height for r in ladder] == [800, 720, 480, 360]
    assert ladder[0].width == 1920
    assert all(r.width % 2 == 0 and r.height % 2 == 0 for r in ladder)
    assert ladder[0].video_kbps > ladder[1].video_kbps


def test_4k_source_is_capped_at_the_largest_configured_height():
    assert [r.height for r in plan_ladder(3840, 2160, HEIGHTS)] == [1080, 720, 480, 360]


def test_bad_input_gives_no_ladder():
    assert plan_ladder(0, 0, HEIGHTS) == []
    assert plan_ladder(1920, 1080, []) == []


def test_bitrate_interpolates_and_has_a_floor():
    assert bitrate_for_height(720) == 2800
    assert 800 < bitrate_for_height(400) < 1400
    assert bitrate_for_height(60) == hls_ladder.MIN_VIDEO_KBPS


def test_bandwidth_is_peak_video_plus_audio():
    r = Rendition(0, 1280, 720, 2800)
    assert r.bandwidth == (int(2800 * 1.07) + 128) * 1000


def test_vaapi_args_upload_frames_and_scale_on_the_gpu():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    args = hls_ladder.build_session_args(ladder, 1080, VAAPI, "veryfast", 6, "/in.mkv", ["-c:a", "aac"], 0, "init")
    joined = " ".join(args)
    assert "-init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va" in joined
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith("[0:v:0]format=nv12,hwupload,split=2")
    assert "scale_vaapi=w=1280:h=720" in graph
    assert "-preset" not in args and "-sc_threshold" not in args  # x264-only options


def test_stream_inf_lines():
    ladder = plan_ladder(1280, 720, [720, 480])
    lines = stream_inf_lines(ladder, "movie", True, True)
    assert lines[0] == (
        '#EXT-X-STREAM-INF:BANDWIDTH=3124000,RESOLUTION=1280x720,CODECS="avc1.640029,mp4a.40.2",SUBTITLES="subs"'
    )
    assert lines[1] == "movie_v0.m3u8" and lines[3] == "movie_v1.m3u8"
    assert "mp4a" not in stream_inf_lines(ladder, "m", False, False)[0]
    assert "SUBTITLES" not in stream_inf_lines(ladder, "m", False, False)[0]


def test_a_chromecast_is_not_offered_renditions_wider_than_1080p():
    # A scope film: its "1080" level is 2376 wide, past what a Chromecast decodes.
    ladder = plan_ladder(2560, 1164, [1080, 720, 480])
    assert ladder[0].width > hls_ladder.CHROMECAST_MAX_WIDTH
    lines = stream_inf_lines(ladder, "playlist", True, False, max_width=hls_ladder.CHROMECAST_MAX_WIDTH)
    uris = [line for line in lines if not line.startswith("#")]
    assert uris == ["playlist_v1.m3u8", "playlist_v2.m3u8"]  # the playlists keep their numbers
    # Nothing narrow enough: the lowest one stays, so there is something to play.
    assert [line for line in stream_inf_lines(ladder, "playlist", True, False, max_width=100) if not line.startswith("#")] == ["playlist_v2.m3u8"]
    assert len(stream_inf_lines(ladder, "playlist", True, False)) == 6


def test_encoder_detection_prefers_software_unless_vaapi_really_works(monkeypatch, tmp_path):
    hls_ladder.reset_encoder_cache()
    device = tmp_path / "renderD128"
    device.write_text("")
    monkeypatch.setattr(hls_ladder, "_vaapi_works", lambda _d: False)
    assert hls_ladder.detect_encoder("auto", str(device)) == SOFTWARE  # driver present but no bitrate control
    hls_ladder.reset_encoder_cache()
    monkeypatch.setattr(hls_ladder, "_vaapi_works", lambda _d: True)
    assert hls_ladder.detect_encoder("auto", str(device)) == VAAPI
    assert hls_ladder.detect_encoder("auto", str(device)) == VAAPI  # cached
    assert hls_ladder.detect_encoder("software", str(device)) == SOFTWARE
    hls_ladder.reset_encoder_cache()
    assert hls_ladder.detect_encoder("auto", str(tmp_path / "missing")) == SOFTWARE
    hls_ladder.reset_encoder_cache()


def test_a_single_rendition_names_its_init_file_without_a_placeholder():
    # ffmpeg leaves %v in the init file name untouched when there is only one rendition.
    one = plan_ladder(320, 240, HEIGHTS)
    session = hls_ladder.build_session_args(one, 240, SOFTWARE, "veryfast", 6, "/in.mkv", None, 0, "init")
    assert session[session.index("-hls_fmp4_init_filename") + 1] == "init_v0.mp4"
    many = plan_ladder(1920, 1080, HEIGHTS)
    session = hls_ladder.build_session_args(many, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", None, 0, "init")
    assert session[session.index("-hls_fmp4_init_filename") + 1] == "init_v%v.mp4"


def test_session_args_restart_at_a_segment_on_the_same_timeline():
    ladder = plan_ladder(1920, 1080, HEIGHTS)
    args = build_session_args_for_test(ladder, start=7)
    assert args[args.index("-ss") + 1] == "42"
    assert "-output_ts_offset" not in args  # the muxer ignores it; the session places the segments
    assert args[args.index("-hls_segment_options") + 1] == "use_editlist=0"  # so every run's init is the same
    assert args[args.index("-start_number") + 1] == "7"
    assert args.index("-ss") < args.index("-i")  # input seeking, so it is fast
    assert "temp_file" in args[args.index("-hls_flags") + 1]  # a segment is never visible half-written
    assert args[args.index("-i") + 1] == "/in.mkv"
    fresh = build_session_args_for_test(ladder, start=0)
    assert "-ss" not in fresh


def build_session_args_for_test(ladder, start):
    return hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "aac"], start, "init")


def _tracks():
    return [hls_ladder.AudioTrack(0, "aac", "eng", ""), hls_ladder.AudioTrack(1, "ac3", "fra", "Version française"), hls_ladder.AudioTrack(2, "aac", "ita", "")]


def test_several_audio_tracks_become_separate_variants_after_the_video_ones():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    args = hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "aac"], 0, "init", _tracks())
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["[v0]", "[v1]", "0:a:0", "0:a:1", "0:a:2"]  # each track once, not once per video rendition
    assert args[args.index("-var_stream_map") + 1] == "v:0 v:1 a:0 a:1 a:2"
    assert args[args.index("-hls_fmp4_init_filename") + 1] == "init_v%v.mp4"


def test_one_audio_track_stays_muxed_into_every_video_variant():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    args = hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "aac"], 0, "init", _tracks()[:1])
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["[v0]", "[v1]", "0:a:0", "0:a:0"]
    assert args[args.index("-var_stream_map") + 1] == "v:0,a:0 v:1,a:1"


def test_audio_tracks_are_listed_in_the_master_playlist_and_tied_to_every_rendition():
    ladder = plan_ladder(1280, 720, [720, 480])
    names = {"eng": ("en", "English"), "fra": ("fr", "Version française"), "ita": ("it", "Italian")}
    lines = hls_ladder.audio_media_lines(_tracks(), len(ladder), "playlist", lambda t: names[t.language])
    assert lines[0] == '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="playlist_v2.m3u8"'
    assert 'NAME="Version française",LANGUAGE="fr",DEFAULT=NO' in lines[1] and lines[1].endswith('URI="playlist_v3.m3u8"')
    assert lines[2].endswith('URI="playlist_v4.m3u8"')
    streams = hls_ladder.stream_inf_lines(ladder, "playlist", True, False, audio_group=True)
    assert all('AUDIO="aud"' in line for line in streams[::2])
    assert 'AUDIO' not in hls_ladder.stream_inf_lines(ladder, "playlist", True, False)[0]


def _surround(layout="5.1", codec="aac", profile="LC", channels=6, index=0):
    return hls_ladder.AudioTrack(index, codec, "ita", "", channels, layout, profile)


def test_surround_dialogue_gets_full_weight_in_the_stereo_mix():
    mix = hls_ladder.downmix_filter(_surround(), 2)
    assert mix == "pan=stereo|FL=1*FC+0.707*FL+0.5*BL|FR=1*FC+0.707*FR+0.5*BR,alimiter=limit=0.97"
    assert "SL" in hls_ladder.downmix_filter(_surround("5.1(side)"), 2)
    seven = hls_ladder.downmix_filter(_surround("7.1", channels=8), 2)
    assert "0.25*BL" in seven and "0.25*SL" in seven  # a 7.1 mix splits the surround share between its pairs


def test_no_downmix_when_none_is_needed_or_possible():
    assert hls_ladder.downmix_filter(_surround("stereo", channels=2), 2) is None  # already stereo
    assert hls_ladder.downmix_filter(_surround(), 6) is None  # surround was asked for
    assert hls_ladder.downmix_filter(_surround("quad", channels=4), 2) is None  # no recipe: ffmpeg's default


def test_aac_is_only_copied_when_it_is_plain_aac_with_no_more_channels_than_wanted():
    stereo = _surround("stereo", channels=2)
    assert hls_ladder.can_copy_audio([stereo], 2)
    assert not hls_ladder.can_copy_audio([_surround()], 2)  # 5.1 is mixed down, not passed through
    assert hls_ladder.can_copy_audio([_surround()], 6)  # unless 5.1 was asked for
    assert not hls_ladder.can_copy_audio([_surround("stereo", profile="HE-AAC", channels=2)], 2)  # advertised as LC
    assert not hls_ladder.can_copy_audio([_surround("stereo", codec="ac3", profile="", channels=2)], 2)
    assert not hls_ladder.can_copy_audio([], 2)


def test_each_surround_track_is_mixed_down_on_its_own():
    ladder = plan_ladder(1280, 720, [720])
    tracks = [_surround(index=0), _surround("stereo", channels=2, index=1), _surround("5.1(side)", index=2)]
    args = hls_ladder.build_session_args(ladder, 720, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "aac", "-ac", "2"], 0, "init", tracks, 2)
    assert "FR=1*FC+0.707*FR+0.5*BR" in args[args.index("-filter:a:0") + 1]
    assert "-filter:a:1" not in args  # the stereo track is left alone
    assert "SL" in args[args.index("-filter:a:2") + 1]


def test_a_single_surround_track_is_mixed_down_in_every_video_variant():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    args = hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "aac", "-ac", "2"], 0, "init", [_surround()], 2)
    assert "-filter:a:0" in args and "-filter:a:1" in args


def test_copied_audio_gets_no_filter():
    ladder = plan_ladder(1280, 720, [720])
    args = hls_ladder.build_session_args(ladder, 720, SOFTWARE, "veryfast", 6, "/in.mkv", ["-c:a", "copy"], 0, "init", [_surround()], 2)
    assert "-filter:a:0" not in args


def test_the_downmix_recipes_differ_as_documented():
    dialogue = hls_ladder.downmix_filter(_surround(), 2, "dialogue")
    dave = hls_ladder.downmix_filter(_surround(), 2, "dave750")
    night = hls_ladder.downmix_filter(_surround(), 2, "nightmode")
    assert "0.5*LFE" in dave and "0.5*FC" in dave and "0.707*BL" in dave  # centre and bass split into both sides
    assert "LFE" not in dialogue  # the milder recipe leaves the bass channel out
    assert "0.3*FL" in night and "0.6*LFE" in night and "1*FC" in night  # centre up, everything else down


def test_the_default_downmix_leaves_the_mix_to_ffmpeg_but_still_honours_the_boost():
    assert hls_ladder.downmix_filter(_surround(), 2, "default") is None
    boosted = hls_ladder.downmix_filter(_surround(), 2, "default", 150)
    assert boosted == "volume=1.5,alimiter=limit=0.97"
    assert hls_ladder.downmix_filter(_surround(), 2, "nightmode", 200).count("volume=2") == 1


def test_a_boost_never_applies_to_a_track_that_is_not_downmixed():
    assert hls_ladder.downmix_filter(_surround("stereo", channels=2), 2, "nightmode", 300) is None
    assert hls_ladder.downmix_filter(_surround(), 6, "nightmode", 300) is None


def test_the_low_power_encoder_is_asked_for_only_when_it_is_the_one_that_works(monkeypatch):
    ladder = plan_ladder(1920, 1080, [1080])
    hls_ladder.reset_encoder_cache()
    assert "-low_power" not in hls_ladder.encode_args(ladder, VAAPI, "veryfast", 6, None)
    monkeypatch.setattr(hls_ladder, "_vaapi_low_power", True)
    args = hls_ladder.encode_args(ladder, VAAPI, "veryfast", 6, None)
    assert args[args.index("-low_power") + 1] == "1"
    assert "-low_power" not in hls_ladder.encode_args(ladder, SOFTWARE, "veryfast", 6, None)
    hls_ladder.reset_encoder_cache()


def test_detection_falls_back_to_the_low_power_encoder(monkeypatch):
    runs: list[list[str]] = []

    class Done:
        stderr = b""

        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(cmd, **_kw):
        runs.append(cmd)
        return Done(0 if "-low_power" in cmd else 1)  # only the low-power encoder works

    hls_ladder.reset_encoder_cache()
    monkeypatch.setattr(hls_ladder.subprocess, "run", fake_run)
    assert hls_ladder._vaapi_works("/dev/dri/renderD128")
    assert len(runs) == 2 and hls_ladder._vaapi_low_power
    hls_ladder.reset_encoder_cache()


def test_gpu_decoding_is_asked_for_only_when_it_works_and_is_wanted(monkeypatch, tmp_path):
    ladder = plan_ladder(1920, 1080, [1080, 720])
    off = hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", None, 0, "init")
    assert "-hwaccel" not in off
    on = hls_ladder.build_session_args(ladder, 1080, SOFTWARE, "veryfast", 6, "/in.mkv", None, 0, "init", hw_decode=True)
    assert on[on.index("-hwaccel") + 1] == "vaapi"
    assert on.index("-hwaccel") < on.index("-i")  # an input option
    assert "-hwaccel_output_format" not in on  # frames come back to memory for the software encoder

    device = tmp_path / "renderD128"
    device.write_text("")
    hls_ladder.reset_encoder_cache()
    monkeypatch.setattr(hls_ladder, "_hw_decode_works", lambda _d: True)
    assert hls_ladder.detect_hw_decode("auto", str(device))
    assert not hls_ladder.detect_hw_decode("off", str(device))
    hls_ladder.reset_encoder_cache()
    monkeypatch.setattr(hls_ladder, "_hw_decode_works", lambda _d: False)
    assert not hls_ladder.detect_hw_decode("auto", str(device))
    hls_ladder.reset_encoder_cache()
    assert not hls_ladder.detect_hw_decode("auto", str(tmp_path / "missing"))  # no GPU device at all
    hls_ladder.reset_encoder_cache()


# ---- HDR to SDR ---------------------------------------------------------------------------------


def _probe_json(monkeypatch, stream):
    import json

    class Result:
        returncode = 0
        stdout = json.dumps({"streams": [stream]}).encode()
        stderr = b""

    monkeypatch.setattr(hls_ladder.subprocess, "run", lambda *a, **k: Result())


def test_the_dynamic_range_of_a_source_is_read_from_its_transfer_function(monkeypatch):
    _probe_json(monkeypatch, {"color_transfer": "bt709"})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "sdr"
    _probe_json(monkeypatch, {})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "sdr"
    _probe_json(monkeypatch, {"color_transfer": "smpte2084", "color_primaries": "bt2020"})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "pq"
    _probe_json(monkeypatch, {"color_transfer": "arib-std-b67"})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "hlg"


def test_dolby_vision_without_an_hdr10_layer_is_told_apart(monkeypatch):
    _probe_json(monkeypatch, {"color_transfer": "smpte2084", "side_data_list": [{"dv_profile": 5}]})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "dovi"
    # profile 8 has an HDR10 base layer: tone-mapped like any HDR10
    _probe_json(monkeypatch, {"color_transfer": "smpte2084", "side_data_list": [{"dv_profile": 8}]})
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "pq"


def test_an_unreadable_probe_counts_as_sdr(monkeypatch):
    def fail(*a, **k):
        raise OSError("no ffprobe")

    monkeypatch.setattr(hls_ladder.subprocess, "run", fail)
    assert hls_ladder.probe_dynamic_range("/in.mkv") == "sdr"


def test_an_sdr_source_gets_no_tone_mapping():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    assert "tonemap" not in hls_ladder.filter_graph(ladder, 1080, SOFTWARE)
    assert "tonemap" not in hls_ladder.filter_graph(ladder, 1080, VAAPI)


def test_an_hdr_source_is_scaled_to_the_top_rendition_then_tone_mapped_once_then_split():
    ladder = plan_ladder(3840, 2160, [1080, 720])
    graph = hls_ladder.filter_graph(ladder, 2160, SOFTWARE, "pq")
    head, split_and_rest = graph.split(",split=", 1)
    assert head.startswith("[0:v:0]scale=1920:1080,zscale=t=linear")
    assert graph.count("tonemap=tonemap=hable") == 1
    assert head.endswith("format=yuv420p")
    # 1080 is the top: no second scale; 720 scales down after the tone mapping
    assert "[s0]null[v0]" in graph and "[s1]scale=1280:720[v1]" in graph


def test_hdr_tone_mapped_on_the_cpu_goes_up_to_the_gpu_afterwards_when_the_gpu_encodes():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    graph = hls_ladder.filter_graph(ladder, 1080, VAAPI, "hlg")
    assert "tonemap=tonemap=hable" in graph and "format=nv12,hwupload,split=2" in graph
    assert "[s1]scale_vaapi=w=1280:h=720[v1]" in graph


def test_hdr_can_be_tone_mapped_on_the_gpu():
    ladder = plan_ladder(1920, 1080, [1080, 720])
    graph = hls_ladder.filter_graph(ladder, 1080, VAAPI, "pq", gpu_tonemap=True)
    assert "format=p010le,hwupload,tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709,split=2" in graph
    assert "zscale" not in graph
    # the GPU does not do it for a software encoder
    assert "tonemap_vaapi" not in hls_ladder.filter_graph(ladder, 1080, SOFTWARE, "pq", gpu_tonemap=True)


def test_tone_mapped_output_is_tagged_as_sdr_bt709_and_other_output_is_left_alone():
    ladder = plan_ladder(1920, 1080, [1080])
    hdr = hls_ladder.encode_args(ladder, SOFTWARE, "veryfast", 6, None, tone_mapped=True)
    assert hdr[hdr.index("-color_primaries") + 1] == "bt709"
    assert hdr[hdr.index("-color_trc") + 1] == "bt709" and hdr[hdr.index("-colorspace") + 1] == "bt709"
    assert "-color_primaries" not in hls_ladder.encode_args(ladder, SOFTWARE, "veryfast", 6, None)


def test_gpu_tone_mapping_is_found_by_trying_it_and_the_result_is_logged(monkeypatch):
    class Done:
        returncode = 1
        stderr = b"Impossible to convert between the formats supported by the filter\n"

    hls_ladder.reset_encoder_cache()
    monkeypatch.setattr(hls_ladder.os.path, "exists", lambda p: True)
    monkeypatch.setattr(hls_ladder.subprocess, "run", lambda *a, **k: Done())
    assert hls_ladder.detect_vaapi_tonemap("/dev/dri/renderD128") is False
    assert hls_ladder.detection_log[-1][:2] == ("GPU tone mapping", False)
    hls_ladder.reset_encoder_cache()


def test_the_logged_reason_is_the_line_that_names_the_cause_not_ffmpegs_closing_line():
    stderr = (
        b"[Parsed_tonemap_vaapi_4 @ 0x1] Failed to create processing pipeline config: 12 (the requested VAProfile is not supported).\n"
        b"Error while filtering: Input/output error\n"
        b"[out#0/null @ 0x2] Nothing was written into output file, because at least one of its streams received no packets.\n"
    )
    assert "requested VAProfile is not supported" in hls_ladder.last_line(stderr)
    assert hls_ladder.last_line(b"one\ntwo\n") == "two"
    assert hls_ladder.last_line(b"") == ""
