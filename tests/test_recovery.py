from snag.info.formats import build_format_lists
from snag.info.models import PlaylistInfo, VideoInfo
from snag.jobs.models import ChoiceKind, FormatChoice, choice_for_row
from snag.jobs.recovery import closest_choice

M4A = {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5,
       "filesize": 1, "language": "en", "language_preference": 10}
DUB = {"format_id": "140-1", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5,
       "filesize": 1, "language": "de", "language_preference": -1}


def video(format_id, height, ext, vcodec, **extra):
    return {"format_id": format_id, "ext": ext, "protocol": "https", "vcodec": vcodec, "acodec": "none",
            "height": height, "filesize": 1, **extra}


AVC_1080 = video("299", 1080, "mp4", "avc1.64002A", tbr=4000)
AV1_1080 = video("399", 1080, "mp4", "av01.0.08M.08", tbr=2000)
VP9_1080 = video("303", 1080, "webm", "vp09.00.41.08", tbr=3000)
AVC_720 = video("298", 720, "mp4", "avc1.4D4020")


def info_with(*formats) -> VideoInfo:
    return VideoInfo(
        site="youtube", video_id="abc123", url="fake://format-gone", title="Fake Video", channel=None,
        duration=10, thumbnail=None, upload_date=None, formats=build_format_lists(list(formats), 10),
        subtitle_langs=(), auto_subtitle_langs=(), has_chapters=False,
    )


def gone(height=1080, ext="mp4", vcodec="avc1.640028", audio_id="140") -> FormatChoice:
    return FormatChoice(ChoiceKind.VIDEO, "gone", audio_id, height, ext, vcodec)


def test_keeps_the_codec_family_when_it_is_still_offered():
    # AV1 ranks higher, but the old choice was H.264, so the H.264 format is closest.
    choice = closest_choice(gone(), info_with(M4A, AV1_1080, AVC_1080, VP9_1080, AVC_720))
    assert choice == FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4", "avc1.64002A", "m4a", "mp4a.40.2", "en")


def test_same_height_and_container_when_the_codec_family_is_gone():
    choice = closest_choice(gone(), info_with(M4A, AV1_1080, VP9_1080))
    assert (choice.format_id, choice.ext, choice.vcodec) == ("399", "mp4", "av01.0.08M.08")


def test_same_height_and_container_when_the_codec_was_not_recorded():
    choice = closest_choice(gone(vcodec=None), info_with(M4A, AVC_1080, AV1_1080, VP9_1080))
    assert choice.format_id == "399"  # the best mp4 at 1080p


def test_same_height_in_another_container_as_a_last_resort():
    choice = closest_choice(gone(), info_with(M4A, VP9_1080, AVC_720))
    assert (choice.format_id, choice.height, choice.ext) == ("303", 1080, "webm")


def test_no_match_at_another_height():
    assert closest_choice(gone(), info_with(M4A, AVC_720)) is None


def test_keeps_a_chosen_audio_track_that_is_still_offered():
    choice = closest_choice(gone(audio_id="140-1"), info_with(M4A, DUB, AVC_1080))
    assert (choice.format_id, choice.audio_id) == ("299", "140-1")


def test_pairs_new_audio_when_the_chosen_track_is_gone():
    choice = closest_choice(gone(audio_id="251"), info_with(M4A, AVC_1080))
    assert (choice.format_id, choice.audio_id) == ("299", "140")


def test_a_combined_format_takes_no_separate_audio():
    combined = {**AVC_1080, "format_id": "37", "acodec": "mp4a.40.2"}
    choice = closest_choice(gone(), info_with(M4A, combined))
    assert (choice.format_id, choice.audio_id) == ("37", None)


def test_the_same_choice_again_is_no_match():
    old = FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4", "avc1.64002A", "m4a", "mp4a.40.2")
    assert closest_choice(old, info_with(M4A, AVC_1080)) is None


def test_only_specific_formats_are_matched():
    info = info_with(M4A, AVC_1080)
    assert closest_choice(FormatChoice(ChoiceKind.VIDEO, "gone", "140", None, "mp4"), info) is None
    for kind in (ChoiceKind.MP3, ChoiceKind.QUICK, ChoiceKind.BEST_AUDIO):  # yt-dlp picks those itself
        assert closest_choice(FormatChoice(kind, height=1080), info) is None


def test_a_playlist_is_no_match():
    playlist = PlaylistInfo(site="youtube", playlist_id="PL1", url="u", title="P", entries=())
    assert closest_choice(gone(), playlist) is None


# --- original audio formats ---

def audio(format_id, ext, acodec, abr, language="en", preference=10):
    return {"format_id": format_id, "ext": ext, "protocol": "https", "vcodec": "none", "acodec": acodec,
            "abr": abr, "filesize": 1, "language": language, "language_preference": preference}


OPUS_160 = audio("251", "webm", "opus", 160)
OPUS_70 = audio("250", "webm", "opus", 70)
AAC_128 = audio("140", "m4a", "mp4a.40.2", 128)
AAC_IN_MP4 = audio("139", "mp4", "mp4a.40.5", 48)
DUB_OPUS = audio("251-1", "webm", "opus", 200, language="de", preference=-1)
DUB_AAC = audio("140-1", "m4a", "mp4a.40.2", 200, language="de", preference=-1)
DESCRIPTIVE_OPUS = audio("251-2", "webm", "opus", 220, language="en-desc", preference=-10)


def gone_audio(ext="webm", codec="opus") -> FormatChoice:
    return FormatChoice(ChoiceKind.AUDIO, "gone", ext=ext, audio_ext=ext, audio_codec=codec)


def audio_row(info: VideoInfo, format_id: str):
    return next(r for r in info.formats.audio_tracks if r.format_id == format_id)


def test_audio_same_container_and_codec_in_the_original_language():
    info = info_with(DUB_OPUS, AAC_128, OPUS_70, OPUS_160)
    choice = closest_choice(gone_audio(), info)
    assert choice == choice_for_row(audio_row(info, "251"))  # the best of them, not the louder dub
    assert (choice.kind, choice.ext, choice.audio_ext, choice.audio_codec) == (ChoiceKind.AUDIO, "webm", "webm", "opus")


def test_audio_same_codec_in_another_container():
    info = info_with(OPUS_160, AAC_IN_MP4)
    choice = closest_choice(gone_audio(ext="m4a", codec="mp4a.40.2"), info)
    assert choice.format_id == "139"  # AAC, though in MP4 rather than M4A


def test_audio_best_original_language_track_when_the_codec_is_gone():
    info = info_with(DUB_OPUS, DESCRIPTIVE_OPUS, AAC_128)
    choice = closest_choice(gone_audio(), info)
    assert choice.format_id == "140"  # never a dub or described audio just to keep the codec


def test_audio_chosen_before_codecs_were_recorded_matches_by_container():
    old = FormatChoice(ChoiceKind.AUDIO, "gone", ext="webm")
    assert closest_choice(old, info_with(AAC_128, OPUS_70)).format_id == "250"
    assert closest_choice(FormatChoice(ChoiceKind.AUDIO, "gone"), info_with(OPUS_70, AAC_128)).format_id == "140"


def test_audio_without_language_information_uses_every_track():
    plain = [dict(f, language=None, language_preference=None) for f in (OPUS_70, AAC_128)]
    assert closest_choice(gone_audio(), info_with(*plain)).format_id == "250"


def test_audio_no_match_when_nothing_or_the_same_format_is_offered():
    assert closest_choice(gone_audio(), info_with(AVC_1080)) is None
    info = info_with(OPUS_160)
    assert closest_choice(choice_for_row(audio_row(info, "251")), info) is None


def test_a_replacement_video_format_records_its_audio_container_and_codec():
    choice = closest_choice(gone(), info_with(M4A, AVC_1080))
    assert (choice.audio_ext, choice.audio_codec) == ("m4a", "mp4a.40.2")
    kept = closest_choice(gone(audio_id="140-1"), info_with(M4A, DUB, AVC_1080))
    assert (kept.audio_id, kept.audio_ext, kept.audio_codec) == ("140-1", "m4a", "mp4a.40.2")
    assert (choice.audio_language, kept.audio_language) == ("en", "de")


# --- a deliberately chosen audio language ---

DUB_OPUS_LOW = audio("250-1", "webm", "opus", 70, language="de", preference=-1)
FRENCH_OPUS = audio("251-3", "webm", "opus", 210, language="fr", preference=-1)


def gone_dub(language="de", ext="webm", codec="opus") -> FormatChoice:
    return FormatChoice(ChoiceKind.AUDIO, "gone", ext=ext, audio_ext=ext, audio_codec=codec, audio_language=language)


def test_audio_keeps_a_chosen_language():
    info = info_with(OPUS_160, FRENCH_OPUS, DUB_AAC, DUB_OPUS_LOW)
    choice = closest_choice(gone_dub(), info)
    assert choice == choice_for_row(audio_row(info, "250-1"))  # the German Opus, not the original or French one
    assert choice.audio_language == "de"


def test_audio_language_outranks_the_codec():
    choice = closest_choice(gone_dub(), info_with(OPUS_160, DUB_AAC))
    assert (choice.format_id, choice.audio_language) == ("140-1", "de")


def test_audio_language_is_matched_whatever_its_case():
    assert closest_choice(gone_dub(language="DE"), info_with(OPUS_160, DUB_OPUS_LOW)).format_id == "250-1"


def test_audio_in_the_original_language_when_the_chosen_one_is_gone():
    choice = closest_choice(gone_dub(language="ja"), info_with(DUB_OPUS, OPUS_70, AAC_128))
    assert (choice.format_id, choice.audio_language) == ("250", "en")


def gone_video_with(language, audio_id="gone-audio") -> FormatChoice:
    return FormatChoice(ChoiceKind.VIDEO, "gone", audio_id, 1080, "mp4", "avc1.640028", "webm", "opus", language)


def test_video_pairs_audio_in_the_chosen_language_when_its_track_is_gone():
    info = info_with(OPUS_160, AAC_128, FRENCH_OPUS, DUB_AAC, DUB_OPUS_LOW, AVC_1080)
    choice = closest_choice(gone_video_with("de"), info)
    assert choice == choice_for_row(
        next(r for r in info.formats.video_all if r.format_id == "299"), audio_row(info, "250-1"),
    )  # the German track in the old audio's codec, not the original-language pairing
    assert (choice.audio_id, choice.audio_language) == ("250-1", "de")


def test_video_takes_the_automatic_pairing_when_it_is_in_the_chosen_language():
    choice = closest_choice(gone_video_with("en"), info_with(OPUS_160, AAC_128, DUB_OPUS, AVC_1080))
    assert (choice.audio_id, choice.audio_language) == ("140", "en")  # the pairing for an MP4, as before


def test_video_takes_the_automatic_pairing_when_the_chosen_language_is_gone():
    choice = closest_choice(gone_video_with("ja"), info_with(AAC_128, DUB_OPUS, AVC_1080))
    assert (choice.audio_id, choice.audio_language) == ("140", "en")


def test_video_without_a_recorded_language_takes_the_automatic_pairing():
    choice = closest_choice(gone_video_with(None), info_with(AAC_128, DUB_OPUS, AVC_1080))
    assert (choice.audio_id, choice.audio_language) == ("140", "en")


def test_a_combined_format_keeps_its_own_audio_whatever_the_chosen_language():
    combined = {**AVC_1080, "format_id": "37", "acodec": "mp4a.40.2", "language": "en"}
    choice = closest_choice(gone_video_with("de"), info_with(DUB_AAC, combined))
    assert (choice.format_id, choice.audio_id, choice.audio_language) == ("37", None, "en")
