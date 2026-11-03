import os

import video_conversion


def _vtt(path, lines):
    cues = "\n\n".join(f"{i}\n00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\n{text}" for i, text in enumerate(lines))
    path.write_text("WEBVTT\n\n" + cues + "\n", encoding="utf-8")


ENGLISH = ["I told you we should have left before the storm came.", "Nobody is coming to help us now, are they?"] * 4
FRENCH = ["Je t'avais dit que nous aurions dû partir avant la tempête.", "Personne ne viendra nous aider maintenant, n'est-ce pas ?"] * 4
SPANISH = ["Te dije que deberíamos habernos ido antes de la tormenta.", "Ya nadie vendrá a ayudarnos, ¿verdad?"] * 4


def test_the_language_of_a_track_is_read_from_its_dialogue(tmp_path):
    for name, lines, code in (("e", ENGLISH, "en"), ("f", FRENCH, "fr"), ("s", SPANISH, "es")):
        _vtt(tmp_path / f"{name}.vtt", lines)
        assert video_conversion.detect_vtt_language(str(tmp_path / f"{name}.vtt")) == code


def test_a_track_with_hardly_any_text_has_no_language(tmp_path):
    _vtt(tmp_path / "t.vtt", ["Hi."])
    assert video_conversion.detect_vtt_language(str(tmp_path / "t.vtt")) is None
    assert video_conversion.detect_vtt_language(str(tmp_path / "missing.vtt")) is None


def test_untagged_tracks_are_renamed_for_their_language_and_tagged_ones_left_alone(tmp_path):
    _vtt(tmp_path / "Show.S01E01.2_und.vtt", FRENCH)
    _vtt(tmp_path / "Show.S01E01.3_und.vtt", ["Hi."])
    _vtt(tmp_path / "Show.S01E01.4_eng.vtt", FRENCH)
    video_conversion.name_untagged_subtitles(str(tmp_path), "Show.S01E01")
    assert sorted(os.listdir(tmp_path)) == ["Show.S01E01.2_fr.vtt", "Show.S01E01.3_und.vtt", "Show.S01E01.4_eng.vtt"]
