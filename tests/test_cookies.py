import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from snag.cookies import CookieJar, to_netscape

DOMAIN_COOKIE = {"domain": ".youtube.com", "hostOnly": False, "httpOnly": True, "secure": True,
                 "path": "/", "expirationDate": 1800000000.5, "name": "SID", "value": "abc"}
HOST_COOKIE = {"domain": "www.youtube.com", "hostOnly": True, "httpOnly": False, "secure": False,
               "path": "/", "name": "PREF", "value": "f1=1"}


def _cookie(domain, name, value="v", host_only=None, **extra):
    cookie = {"domain": domain, "path": "/", "name": name, "value": value, **extra}
    if host_only is not None:
        cookie["hostOnly"] = host_only
    return cookie


def _names(text):
    if text is None:
        return set()
    return {line.split("\t")[5] for line in text.splitlines()[1:]}


def test_to_netscape():
    assert to_netscape([DOMAIN_COOKIE, HOST_COOKIE]).splitlines() == [
        "# Netscape HTTP Cookie File",
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tabc",
        "www.youtube.com\tFALSE\t/\tFALSE\t0\tPREF\tf1=1",
    ]


def test_include_subdomains_flag_follows_the_written_domain():
    lines = to_netscape([
        _cookie(".example.com", "a", host_only=True),   # inconsistent: the leading dot wins
        _cookie("example.com", "b", host_only=False),   # domain cookie without the dot gets one
        _cookie("example.com", "c"),                    # no hostOnly: decided by the dot
        _cookie(".example.com", "d"),
    ]).splitlines()[1:]
    assert [line.split("\t")[:2] for line in lines] == [
        [".example.com", "TRUE"], [".example.com", "TRUE"], ["example.com", "FALSE"], [".example.com", "TRUE"],
    ]


def test_generated_file_loads_in_yt_dlp(tmp_path):
    cookies = [
        DOMAIN_COOKIE,
        HOST_COOKIE,  # session cookie (no expiry)
        _cookie(".youtube.com", "BAD_FLAG", host_only=True, httpOnly=True, secure=True),
        _cookie("youtube.com", "NO_DOT", host_only=False, expirationDate=1900000000),
    ]
    path = tmp_path / "cookies.txt"
    path.write_text(to_netscape(cookies), encoding="utf-8")
    jar = YoutubeDLCookieJar(str(path))
    jar.load()
    loaded = {(c.domain, c.name): c for c in jar}
    assert set(loaded) == {
        (".youtube.com", "SID"), ("www.youtube.com", "PREF"), (".youtube.com", "BAD_FLAG"), (".youtube.com", "NO_DOT"),
    }
    assert loaded[(".youtube.com", "SID")].secure is True
    assert loaded[(".youtube.com", "SID")].expires == 1800000000
    assert loaded[("www.youtube.com", "PREF")].expires is None  # kept as a session cookie
    assert loaded[("www.youtube.com", "PREF")].domain_specified is False


def test_get_returns_cookies_that_domain_match_the_host():
    jar = CookieJar()
    jar.put("https://www.youtube.com/watch?v=1", [DOMAIN_COOKIE, HOST_COOKIE])
    assert _names(jar.get("https://www.youtube.com/watch?v=2")) == {"SID", "PREF"}
    assert _names(jar.get("https://music.youtube.com/watch?v=2")) == {"SID"}  # PREF is host-only
    assert _names(jar.get("https://youtube.com/")) == {"SID"}
    assert jar.get("https://notyoutube.com/") is None
    assert jar.get("https://youtube.com.evil.example/") is None


def test_get_writes_a_netscape_file():
    jar = CookieJar()
    jar.put("youtube.com", [DOMAIN_COOKIE])
    assert jar.get("https://www.youtube.com/") == to_netscape([DOMAIN_COOKIE])


def test_short_and_embed_youtube_links_use_youtube_cookies():
    jar = CookieJar()
    jar.put("https://www.youtube.com/", [DOMAIN_COOKIE, HOST_COOKIE])
    assert _names(jar.get("https://youtu.be/abc")) == {"SID", "PREF"}
    assert _names(jar.get("https://www.youtube-nocookie.com/embed/abc")) == {"SID", "PREF"}


def test_sites_are_kept_apart():
    jar = CookieJar()
    jar.put("https://vimeo.com/1", [_cookie(".vimeo.com", "vuid")])
    jar.put("https://www.youtube.com/", [DOMAIN_COOKIE])
    assert _names(jar.get("https://player.vimeo.com/video/1")) == {"vuid"}
    assert _names(jar.get("https://www.youtube.com/")) == {"SID"}


def test_put_replaces_the_cookies_for_that_host():
    jar = CookieJar()
    jar.put("https://www.youtube.com/", [DOMAIN_COOKIE, HOST_COOKIE, _cookie(".google.com", "NID")])
    # Signed out: the browser no longer has SID, and PREF changed.
    jar.put("https://www.youtube.com/", [_cookie("www.youtube.com", "PREF", "f1=2", host_only=True)])
    text = jar.get("https://www.youtube.com/")
    assert _names(text) == {"PREF"}
    assert "f1=2" in text
    assert _names(jar.get("https://www.google.com/")) == {"NID"}  # not sent to youtube, so not replaced


def test_put_for_a_sibling_host_keeps_other_host_only_cookies():
    jar = CookieJar()
    jar.put("https://www.youtube.com/", [DOMAIN_COOKIE, HOST_COOKIE])
    jar.put("https://music.youtube.com/", [_cookie(".youtube.com", "SID", "new", host_only=False),
                                           _cookie("music.youtube.com", "MPREF", host_only=True)])
    text = jar.get("https://www.youtube.com/")
    assert _names(text) == {"SID", "PREF"}
    assert "\tSID\tnew" in text
    assert _names(jar.get("https://music.youtube.com/")) == {"SID", "MPREF"}


def test_put_replaces_cookies_of_the_domains_it_sends():
    jar = CookieJar()
    jar.put("youtube.com", [_cookie("accounts.youtube.com", "ACC", "1", host_only=True)])
    jar.put("youtube.com", [_cookie("accounts.youtube.com", "ACC", "2", host_only=True)])
    text = jar.get("https://accounts.youtube.com/")
    assert text.count("\tACC\t") == 1 and "\tACC\t2" in text


@pytest.mark.parametrize("host", ["192.168.1.5", "[::1]"])
def test_ip_hosts_match_exactly(host):
    plain = host.strip("[]")
    jar = CookieJar()
    jar.put(f"http://{host}:8000/", [_cookie(plain, "s", host_only=True), _cookie(".1.5", "suffix", host_only=False)])
    assert _names(jar.get(f"http://{host}/v.mp4")) == {"s"}


def test_trailing_dots_and_case_are_ignored():
    jar = CookieJar()
    jar.put("https://WWW.YouTube.com./", [DOMAIN_COOKIE])
    assert _names(jar.get("https://www.youtube.com./watch")) == {"SID"}
    assert _names(jar.get("https://www.youtube.com/watch")) == {"SID"}


def test_updated_at_is_the_latest_put_touching_the_host():
    now = [10.0]
    jar = CookieJar(clock=lambda: now[0])
    assert jar.updated_at("youtube.com") is None
    jar.put("youtube.com", [DOMAIN_COOKIE])
    now[0] = 20.0
    jar.put("https://vimeo.com/", [_cookie(".vimeo.com", "vuid")])
    assert jar.updated_at("https://www.youtube.com/watch?v=1") == 10.0
    assert jar.updated_at("youtube.com") == 10.0
    assert jar.updated_at("https://youtu.be/x") == 10.0
    assert jar.updated_at("vimeo.com") == 20.0
    now[0] = 30.0
    jar.put("https://music.youtube.com/", [_cookie("music.youtube.com", "M", host_only=True)])
    assert jar.updated_at("https://music.youtube.com/") == 30.0
    assert jar.updated_at("https://www.youtube.com/") == 10.0
    assert jar.updated_at("https://example.com/") is None


@pytest.mark.parametrize("given, host", [
    ("https://youtu.be/abc", "www.youtube.com"),
    ("https://www.youtube-nocookie.com/embed/abc", "www.youtube.com"),
    ("https://Music.YouTube.com./watch?v=1", "music.youtube.com"),
    ("vimeo.com", "vimeo.com"),
    ("not a url", "not a url"),
])
def test_canonical_host_is_the_host_the_jar_files_cookies_under(given, host):
    from snag.cookies import canonical_host

    assert canonical_host(given) == host


def test_the_server_uses_the_public_host_function():
    import inspect

    from snag.server.routes import system

    assert "_host" not in inspect.getsource(system).replace("canonical_host", "")
