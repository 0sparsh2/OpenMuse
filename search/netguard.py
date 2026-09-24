"""
Safe fetching of public web pages (web.search / web.read / web.fetch).

Every request — and every redirect hop — must resolve to a public address:
no loopback, private ranges, link-local (cloud metadata), multicast,
reserved, or bare/.local/.internal names. No cookies, no credentials, size
and time caps. This is what stops a page or email that manipulates the
model from making OpenMuse call its own API (127.0.0.1:8765) or anything
else on the user's network.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (compatible; OpenMuse/1.0; +personal agent)"
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


class BlockedURL(ValueError):
    """The URL points somewhere the agent must never reach."""


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or getattr(addr, "is_site_local", False))


def check_url(url: str) -> str:
    """Raise BlockedURL unless `url` is http(s) on a public host; returns the host."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise BlockedURL("only http(s) links can be opened")
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("the link has no host")
    if p.username or p.password:
        raise BlockedURL("links with embedded credentials aren't opened")
    if "." not in host and not _looks_like_ip(host):
        raise BlockedURL("local network names aren't reachable from web tools")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise BlockedURL("local network names aren't reachable from web tools")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise BlockedURL(f"couldn't find {host}")
    ips = {i[4][0] for i in infos}
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise BlockedURL("that address is on a private or local network")
    return host


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def safe_get(url: str, *, timeout: float = 6.0, max_bytes: int = MAX_BYTES,
             accept: str = "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5"):
    """GET with every hop checked. Returns (final_url, status, content_type, body_bytes)."""
    session = requests.Session()
    session.trust_env = False            # never route through env proxies / netrc credentials
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        resp = session.get(current, timeout=timeout, allow_redirects=False, stream=True,
                           headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "en;q=0.9,*;q=0.5"})
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            resp.close()
            if not loc:
                raise BlockedURL("redirect without a destination")
            current = urljoin(current, loc)
            continue
        body = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                break
        ctype = resp.headers.get("Content-Type", "")
        status = resp.status_code
        resp.close()
        return current, status, ctype, bytes(body[:max_bytes])
    raise BlockedURL("too many redirects")
