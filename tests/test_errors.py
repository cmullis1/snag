import pytest

from snag.errors import Action, ErrorInfo, ErrorKind, classify, http_forbidden, make_error, site_label

# Real wording from yt-dlp extractor/youtube/_video.py when YouTube rate-limits the session.
SESSION_RATE_LIMITED = (
    "ERROR: [youtube] x: Video unavailable. This content isn't available, try again later. "
    "The current session has been rate-limited by YouTube for up to an hour. It is recommended to use "
    "`-t sleep` to add a delay between video requests to avoid exceeding the rate limit. For more information, "
    "refer to  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#this-content-isnt-available-try-again-later"
)

# yt-dlp appends this to every YouTube reason that mentions signing in (extractor/common.py _login_hint).
COOKIES_HINT = (
    "Use --cookies-from-browser or --cookies for the authentication. "
    "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies"
)

@pytest.mark.parametrize(
    ("output", "kind"),
    [
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErrorKind.THROTTLED),
        (SESSION_RATE_LIMITED, ErrorKind.THROTTLED),
        (f"ERROR: [youtube] x: Sign in to confirm you're not a bot. {COOKIES_HINT}", ErrorKind.SIGN_IN),
        (f"ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users. {COOKIES_HINT}", ErrorKind.SIGN_IN),
        ("ERROR: [youtube] x: Join this channel to get access to members-only content like this video", ErrorKind.SIGN_IN),
        (f"ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video. {COOKIES_HINT}", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] x: The uploader has not made this video available in your country", ErrorKind.UNAVAILABLE),
        ("ERROR: Unsupported URL: https://example.com/", ErrorKind.UNSUPPORTED),
        ("ERROR: [youtube] x: Requested format is not available. Use --list-formats for a list of available formats", ErrorKind.FORMAT_GONE),
        ("ERROR: unable to write data: [Errno 28] No space left on device", ErrorKind.DISK_FULL),
        ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location", ErrorKind.FFMPEG_MISSING),
        ("ERROR: [youtube] x: nsig extraction failed: Some formats may be missing", ErrorKind.SITE_CHANGED),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ErrorKind.SITE_CHANGED),
        ("ERROR: [youtube] x: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", ErrorKind.NETWORK),
        ("ERROR: something odd happened", ErrorKind.UNKNOWN),
    ],
)
def test_classify_kinds(output, kind):
    assert classify(output, "youtube").kind is kind


def test_messages_name_the_site():
    assert classify("HTTP Error 403", "youtube").message == "YouTube changed something. An update usually fixes this."
    assert classify("Sign in to confirm your age", "vimeo").message == "Sign in to this site in Chrome or Edge, then retry"
    assert classify("Sign in to confirm your age", "youtube").message == "Sign in to YouTube in Chrome or Edge, then retry"


def test_outcome_flags():
    unavailable = classify("Video unavailable", "youtube")
    assert unavailable.can_retry is False and unavailable.keep_partial is False and unavailable.action is None
    network = classify("getaddrinfo failed", "youtube")
    assert network.can_retry is True and network.keep_partial is True and network.action is Action.RETRY


def test_details_keep_the_tail():
    output = "x" * 5000 + "Video unavailable"
    info = classify(output, "youtube")
    assert len(info.details) == 4000
    assert info.details.endswith("Video unavailable")


def test_make_error_custom_message():
    info = make_error(ErrorKind.FOLDER, message="Can't save to D:\\nope", details="denied")
    assert info.message == "Can't save to D:\\nope"
    assert info.action is Action.CHANGE_FOLDER


def test_round_trip_dict():
    info = classify("HTTP Error 429", "youtube")
    assert ErrorInfo.from_dict(info.to_dict()) == info


def test_site_label():
    assert site_label("youtube") == "YouTube"
    assert site_label("vimeo") == "This site"


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        (
            "WARNING: [youtube] x: nsig extraction failed: Some formats may be missing\n"
            "ERROR: [youtube] x: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
            ErrorKind.NETWORK,
        ),
        (
            "[download] Got error: HTTP Error 403: Forbidden. Retrying (1/10)...\n"
            "ERROR: unable to download video data: <urlopen error [Errno 11001] getaddrinfo failed>",
            ErrorKind.NETWORK,
        ),
        (
            "[download] Got error: Downloaded 2000000 bytes, expected 6000000 bytes. Retrying (10/10)...\n"
            "ERROR: Did not get any data blocks",
            ErrorKind.NETWORK,
        ),
        (
            "[download] Got error: 2000000 bytes read, 4000000 more expected. Retrying (3/3)...\n"
            "ERROR: [download] Got error: 2000000 bytes read, 4000000 more expected",
            ErrorKind.NETWORK,
        ),
        (
            "ERROR: [generic] Unable to read the stream: giving up after 10 retries",
            ErrorKind.NETWORK,
        ),
        (
            "WARNING: [youtube] HTTP Error 429 (retrying)\n"
            "ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader",
            ErrorKind.UNAVAILABLE,
        ),
        (
            "ERROR: [youtube] x: Video unavailable\n"
            "ERROR: unable to write data: [Errno 28] No space left on device",
            ErrorKind.DISK_FULL,
        ),
        (
            "ERROR: unable to write data: [Errno 28] No space left on device\n"
            "ERROR: something odd happened",
            ErrorKind.DISK_FULL,
        ),
        (
            "WARNING: [youtube] HTTP Error 429 (retrying)\n"
            "ERROR: something odd happened",
            ErrorKind.UNKNOWN,
        ),
        ("yt-dlp finished without producing a file", ErrorKind.UNKNOWN),
        ("HTTP Error 403", ErrorKind.SITE_CHANGED),
    ],
)
def test_classify_prefers_error_lines(output, kind):
    assert classify(output, "youtube").kind is kind


def test_classify_details_keep_full_output():
    output = "WARNING: [youtube] HTTP Error 429 (retrying)\nERROR: [youtube] x: Video unavailable"
    assert classify(output, "youtube").details == output


def test_from_dict_tolerates_missing_action():
    data = make_error(ErrorKind.UNAVAILABLE).to_dict()
    del data["action"]
    assert ErrorInfo.from_dict(data).action is None


def test_throttle_message_says_five_minutes_by_default():
    assert make_error(ErrorKind.THROTTLED, "youtube").message == "YouTube is slowing you down. Paused for 5 minutes."
    assert classify("HTTP Error 429", "youtube").message == "YouTube is slowing you down. Paused for 5 minutes."


@pytest.mark.parametrize("minutes, text", [(1, "1 minute"), (2, "2 minutes"), (30, "30 minutes"), (0, "1 minute")])
def test_throttle_message_names_the_pause(minutes, text):
    """A pause shorter than a minute still reads as "1 minute", never "0 minutes"."""
    expected = f"This site is slowing you down. Paused for {text}."
    assert make_error(ErrorKind.THROTTLED, "vimeo", throttle_minutes=minutes).message == expected
    assert classify("ERROR: HTTP Error 429: Too Many Requests", "vimeo", throttle_minutes=minutes).message == expected


def test_throttle_minutes_only_changes_the_throttle_message():
    assert make_error(ErrorKind.NETWORK, "youtube", throttle_minutes=9).message == "Lost connection"
    assert classify("getaddrinfo failed", "youtube", throttle_minutes=9).message == "Lost connection"


# --- yt-dlp's warning when it has no JavaScript runtime (Deno) ---

import inspect  # noqa: E402

from yt_dlp.extractor.youtube import _video  # noqa: E402

from snag.errors import JS_RUNTIME_MISSING_TEXT, missing_js_runtime  # noqa: E402

JS_RUNTIME_WARNING = (
    "WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default; to use "
    "another runtime add  --js-runtimes RUNTIME[:PATH]  to your command/config. YouTube extraction without a JS "
    "runtime has been deprecated, and some formats may be missing. See  https://github.com/yt-dlp/yt-dlp/wiki/EJS  "
    "for details on installing one"
)


def test_missing_js_runtime_recognises_yt_dlps_warning():
    assert missing_js_runtime(JS_RUNTIME_WARNING) is True
    assert missing_js_runtime(f"[info] abc: Downloading webpage\n{JS_RUNTIME_WARNING}\n") is True


@pytest.mark.parametrize("output", ["", "WARNING: [youtube] abc: Some formats may be missing", "ERROR: Video unavailable"])
def test_missing_js_runtime_ignores_other_output(output):
    assert missing_js_runtime(output) is False


def test_missing_js_runtime_text_is_yt_dlps():
    assert JS_RUNTIME_MISSING_TEXT in inspect.getsource(_video)


@pytest.mark.parametrize(
    ("output", "forbidden"),
    [
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", True),
        ("ERROR: unable to download video data: http error 403: forbidden", True),
        ("ERROR: [youtube] x: nsig extraction failed: Some formats may be missing", False),
        ("ERROR: [youtube] x: Unable to extract yt initial data", False),
    ],
)
def test_a_403_is_told_apart_from_other_site_changes(output, forbidden):
    """YouTube answers 403 when a download link it handed out has gone stale; a fresh run gets new ones."""
    error = classify(output, "youtube")
    assert error.kind is ErrorKind.SITE_CHANGED
    assert http_forbidden(error) is forbidden


def test_only_a_site_change_counts_as_a_stale_link_403():
    """A 403 in the details of another kind of error (a network failure that retried through one) isn't."""
    error = classify("[download] Got error: HTTP Error 403: Forbidden. Retrying (1/10)...\n"
                     "ERROR: unable to download video data: <urlopen error [Errno 11001] getaddrinfo failed>")
    assert error.kind is ErrorKind.NETWORK and not http_forbidden(error)
    assert not http_forbidden(None)


# --- the reason yt-dlp gave, for errors Snag has no rule for ---

from snag.errors import reason_text  # noqa: E402

# yt-dlp's bug_reports_message (yt_dlp/utils/_utils.py), appended to unexpected extractor errors.
REPORT_HINT = (
    "; please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate "
    "issue template. Confirm you are on the latest version using  yt-dlp -U"
)


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("ERROR: [youtube] OrMs0HnKU1I: This video requires payment to watch", "This video requires payment to watch."),
        ("ERROR: [youtube:tab] PL123: The playlist does not exist.", "The playlist does not exist."),
        ("ERROR: [generic] Something unusual happened here", "Something unusual happened here."),
        ("ERROR: something odd happened", "Something odd happened."),
        ("ERROR: [youtube] x: Is this a question?", "Is this a question?"),
        (f"ERROR: [youtube] x: This video is strange. {COOKIES_HINT}", "This video is strange."),
        (
            "ERROR: [youtube] x: This video is strange. Use --cookies, --cookies-from-browser, --username and "
            "--password, --netrc-cmd, or --netrc (youtube) to provide account credentials. See  "
            "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies",
            "This video is strange.",
        ),
        (
            "ERROR: [youtube] x: Odd thing. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-"
            "youtube-cookies  for tips on effectively exporting YouTube cookies",
            "Odd thing.",
        ),
        (f"ERROR: [vimeo] 123: An odd response came back{REPORT_HINT}", "An odd response came back."),
        (
            "ERROR: [vimeo] 123: An odd response came back please report this issue on  https://github.com/yt-dlp/yt-dlp/issues",
            "An odd response came back.",
        ),
        (
            "ERROR: [vimeo] 123: Strange data (caused by KeyError('x')); please report this issue on  https://x",
            "Strange data.",
        ),
        (
            "ERROR: [vimeo] 123: Strange data. Confirm you are on the latest version using  yt-dlp -U",
            "Strange data.",
        ),
        (
            "WARNING: [youtube] x: Some formats may be missing\n"
            "ERROR: [youtube] x: The first error\n"
            "ERROR: [youtube] x: The last error\n",
            "The last error.",
        ),
    ],
)
def test_reason_text_is_yt_dlps_reason_without_its_hints(output, reason):
    assert reason_text(output) == reason


@pytest.mark.parametrize(
    "output",
    ["", "yt-dlp finished without producing a file", "WARNING: [youtube] x: odd", "ERROR: ", f"ERROR: [youtube] x: {COOKIES_HINT}"],
)
def test_reason_text_is_none_without_a_reason(output):
    assert reason_text(output) is None


def test_reason_text_caps_a_long_reason_on_a_word_boundary():
    words = " ".join(f"Word{n}" for n in range(100))
    reason = reason_text(f"ERROR: [youtube] x: {words}")
    assert reason.endswith("…")
    assert len(reason) <= 201
    body = reason[:-1]
    assert words.startswith(body) and words[len(body)] == " "


def test_an_unrecognised_error_shows_yt_dlps_reason():
    error = classify("WARNING: [youtube] x: chatter\nERROR: [youtube] x: Something new from YouTube", "youtube")
    assert error.kind is ErrorKind.UNKNOWN
    assert error.message == "Something new from YouTube."
    assert error.action is Action.RETRY and error.can_retry is True


def test_an_unrecognised_error_without_a_reason_is_something_went_wrong():
    assert classify("yt-dlp finished without producing a file", "youtube").message == "Something went wrong"
    assert classify("ERROR: ", "youtube").message == "Something went wrong"


def test_a_recognised_error_keeps_its_own_message():
    assert classify("ERROR: [youtube] x: Video unavailable. Gone", "youtube").message == "This video isn't available"


# --- YouTube's common refusals, each with its own message ---

PAYMENT = "This video requires payment to watch, so Snag can't download it."
DRM = "This video is copy-protected (DRM), so it can't be downloaded."
NOT_STARTED = "This video hasn't started yet. Try again once it has aired."
MUSIC_PREMIUM = "This needs YouTube Music Premium. Sign in to YouTube in Chrome or Edge with a Premium account, then retry."
SIGN_IN = "Sign in to YouTube in Chrome or Edge, then retry"


@pytest.mark.parametrize(
    ("output", "kind", "message"),
    [
        ("ERROR: [youtube] x: This video requires payment to watch", ErrorKind.UNAVAILABLE, PAYMENT),
        ("ERROR: [youtube] x: Video unavailable. This video requires payment to watch", ErrorKind.UNAVAILABLE, PAYMENT),
        ("ERROR: [youtube] x: This video is DRM protected", ErrorKind.UNAVAILABLE, DRM),
        ("ERROR: [Netflix] x: This video is DRM protected", ErrorKind.UNAVAILABLE, DRM),
        ("ERROR: [youtube] x: Premieres in 3 hours", ErrorKind.NOT_STARTED, NOT_STARTED),
        ("ERROR: [youtube] x: Premiere will begin shortly", ErrorKind.NOT_STARTED, NOT_STARTED),
        ("ERROR: [youtube] x: This live event will begin in 2 days.", ErrorKind.NOT_STARTED, NOT_STARTED),
        ("ERROR: [youtube] x: This live event will begin in a few moments.", ErrorKind.NOT_STARTED, NOT_STARTED),
        ("ERROR: [youtube] x: Video unavailable. Premieres in 10 minutes", ErrorKind.NOT_STARTED, NOT_STARTED),
        ("ERROR: [youtube] x: This video is only available to Music Premium members", ErrorKind.SIGN_IN, MUSIC_PREMIUM),
        (
            f"ERROR: [youtube] x: Video unavailable. This video is only available to Music Premium members. {COOKIES_HINT}",
            ErrorKind.SIGN_IN, MUSIC_PREMIUM,
        ),
        (
            "ERROR: [youtube] x: Video unavailable. YouTube is requiring a captcha challenge before playback",
            ErrorKind.SIGN_IN, SIGN_IN,
        ),
    ],
)
def test_youtube_refusals_get_their_own_messages(output, kind, message):
    error = classify(output, "youtube")
    assert (error.kind, error.message) == (kind, message)


def test_payment_and_drm_can_not_be_retried():
    for output in ("ERROR: [youtube] x: This video requires payment to watch", "ERROR: [youtube] x: This video is DRM protected"):
        error = classify(output, "youtube")
        assert error.can_retry is False and error.action is None and error.keep_partial is False


def test_a_video_that_has_not_started_can_be_retried_later():
    error = classify("ERROR: [youtube] x: Premieres in 3 hours", "youtube")
    assert error.kind.value == "not_started"
    assert error.action is Action.RETRY and error.can_retry is True and error.keep_partial is False
    assert ErrorInfo.from_dict(error.to_dict()) == error


def test_drm_warnings_about_skipped_formats_are_not_a_drm_refusal():
    """yt-dlp warns when it skips some DRM formats; the video itself may still download."""
    output = (
        "WARNING: [youtube] x: Some tv client https formats have been skipped as they are DRM protected. \n"
        "yt-dlp finished without producing a file"
    )
    assert classify(output, "youtube").kind is ErrorKind.UNKNOWN
