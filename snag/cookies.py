"""Browser cookies (in the shape chrome.cookies returns) → the Netscape cookies.txt format yt-dlp reads.
Cookies live in memory only; a job writes them to a temporary file when it needs them.

Each cookie is stored under its own domain, and `get` picks the ones a browser would send to the URL's
host (RFC 6265 domain-matching): host-only cookies for that exact host, domain cookies for the domain
and its subdomains."""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

# Hosts whose pages yt-dlp fetches from another host; their cookies are that host's.
_ALIASES = {
    "youtu.be": "www.youtube.com",
    "youtube-nocookie.com": "www.youtube.com",
    "www.youtube-nocookie.com": "www.youtube.com",
}


def canonical_host(url_or_host: str) -> str:
    """The host a URL's (or a bare host's) cookies are filed under: lower-case, without a trailing
    dot, and with the aliases above mapped to the host whose cookies they use."""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    return _ALIASES.get(host, host)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _domain_matches(host: str, domain: str) -> bool:
    """RFC 6265 5.1.3: the host is the domain, or a subdomain of it (never for IP addresses)."""
    return host == domain or (host.endswith(f".{domain}") and not _is_ip(host))


def _netscape_domain(cookie: dict) -> str:
    """The domain as written to the file: a leading dot marks a cookie its subdomains also get."""
    domain = str(cookie["domain"]).lower().rstrip(".")
    host_only = cookie.get("hostOnly", not domain.startswith("."))
    if not host_only and not domain.startswith("."):
        domain = f".{domain}"
    return domain


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = _netscape_domain(cookie)
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        lines.append("\t".join([
            f"{prefix}{domain}",
            # Must agree with the leading dot, or yt-dlp rejects the whole file.
            "TRUE" if domain.startswith(".") else "FALSE",
            cookie.get("path", "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(int(cookie.get("expirationDate") or 0)),
            cookie["name"],
            cookie["value"],
        ]))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _Stored:
    domain: str  # without the leading dot
    host_only: bool
    cookie: dict

    @classmethod
    def of(cls, cookie: dict) -> _Stored:
        written = _netscape_domain(cookie)
        return cls(written.lstrip("."), not written.startswith("."), cookie)

    def sent_to(self, host: str) -> bool:
        return host == self.domain if self.host_only else _domain_matches(host, self.domain)


class CookieJar:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._cookies: list[_Stored] = []
        self._touched: dict[str, float] = {}  # host or cookie domain → time of the latest put naming it

    def put(self, url_or_host: str, cookies: list[dict]) -> None:
        """Replace what's stored for the host the browser sent cookies for: every stored cookie the
        browser would have sent to it, and every cookie of the domains in this batch."""
        host = canonical_host(url_or_host)
        incoming = [_Stored.of(cookie) for cookie in cookies]
        domains = {stored.domain for stored in incoming}
        self._cookies = [
            stored for stored in self._cookies
            if not stored.sent_to(host) and stored.domain not in domains
        ] + incoming
        now = self._clock()
        for name in {host, *domains}:
            self._touched[name] = now

    def get(self, url: str) -> str | None:
        """The Netscape file text of the cookies a browser would send to the URL's host, or None."""
        host = canonical_host(url)
        matching = [stored.cookie for stored in self._cookies if stored.sent_to(host)]
        return to_netscape(matching) if matching else None

    def updated_at(self, url_or_host: str) -> float | None:
        """When cookies were last put for this host or a domain it belongs to."""
        host = canonical_host(url_or_host)
        times = [at for name, at in self._touched.items() if _domain_matches(host, name)]
        return max(times, default=None)
