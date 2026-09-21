"""SSRF-safe URL fetching for the "import from link" feature.

The backend fetches arbitrary user-supplied URLs, so it must refuse to reach
anything that isn't a normal public web address — otherwise it becomes a proxy
into the private network (loopback services, the router, other LAN hosts, cloud
metadata, etc.). We validate the host before every request, including each
redirect hop.

We fetch with curl_cffi impersonating Chrome's TLS + HTTP/2 fingerprint on
every request. A meaningful slice of the web sits behind bot-management
(PerimeterX/HUMAN, Cloudflare, …) that fingerprints the *client* — its TLS and
HTTP/2 signature, not just its headers — and returns 403 to anything that
isn't a real browser. A plain Python HTTP client gets blocked there no matter
what User-Agent it sends, so we present a browser fingerprint up front. The
fetch helper is the one funnel for "import from link", so doing it here covers
articles, link-to-PDF, Project Gutenberg, and re-chunking alike.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx

from .config import MAX_FETCH_BYTES

MAX_REDIRECTS = 5

# Which browser curl_cffi mimics ("chrome" = latest version it supports).
_IMPERSONATE = "chrome"

# 3xx statuses we follow (when accompanied by a Location header).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


# The last label of a host that a URL parser would read as an IPv4 number.
_NUMERIC_LABEL = re.compile(r"^(?:0[xX][0-9a-fA-F]*|[0-9]+)$")


class UnsafeUrlError(Exception):
    """Raised when a URL targets a non-public / internal address."""


class FetchError(Exception):
    """The upstream site returned a non-success HTTP status."""


class TooLargeError(Exception):
    """The upstream body exceeded MAX_FETCH_BYTES."""


class FetchResult(NamedTuple):
    """A fetched page. Exposes the same surface the callers used on an
    ``httpx.Response``: ``.headers.get(...)``, ``.content`` (bytes), ``.text``."""

    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    text: str


def _check_ip(addr: str) -> None:
    """Raise UnsafeUrlError if `addr` (an IP literal) is not a public address."""
    ip = ipaddress.ip_address(addr)  # raises ValueError if not an IP literal
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:  # e.g. ::ffff:127.0.0.1
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # 169.254/16 + fe80::/10 (covers cloud metadata)
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified     # 0.0.0.0 / ::
        or getattr(ip, "is_site_local", False)  # fec0::/10: deprecated, yet is_global
        # Anything IANA doesn't mark globally reachable: 100.64/10 shared address
        # space (CGNAT, Tailscale tailnets, k8s pod CIDRs), 192.0.0/24, and any
        # future non-routable block. is_private is False for 100.64/10.
        or not ip.is_global
    ):
        raise UnsafeUrlError("That address points to a private network.")


def _validate_host(host: str) -> list[str]:
    """Raise UnsafeUrlError unless `host` is a public address. Returns the
    addresses a hostname resolved to (empty for an IP literal) so the fetch
    can be pinned to exactly those — see `_pin`."""
    if not host:
        raise UnsafeUrlError("The link has no host.")
    # A literal IP in the URL: check it directly.
    try:
        _check_ip(host)
        return []
    except ValueError:
        pass  # not an IP literal — it's a hostname, resolve it
    # A host whose last label is a number ("0177.0.0.1", "127.1", "0x7f.1",
    # "2130706433") is an IPv4 address in a legacy spelling, and the resolver
    # and curl do not agree on how to read one: macOS getaddrinfo reads
    # 0177.0.0.1 as decimal 177.0.0.1 (public, passes the check) while curl
    # reads it as octal 127.0.0.1 — and curl ignores the RESOLVE pin for a host
    # it takes for an IP. No real site is spelled this way; refuse them all.
    if _NUMERIC_LABEL.match(host.rstrip(".").rsplit(".", 1)[-1]):
        raise UnsafeUrlError("That address points to a private network.")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UnsafeUrlError("Could not resolve that host.")
    addrs: list[str] = []
    for info in infos:
        if info[4][0] not in addrs:
            addrs.append(info[4][0])
    if not addrs:
        raise UnsafeUrlError("Could not resolve that host.")
    for a in addrs:
        try:
            _check_ip(a)
        except ValueError:
            raise UnsafeUrlError("Could not resolve that host.")
    return addrs


def _pin(parsed, addrs: list[str]) -> list[str]:
    """A curl RESOLVE entry tying this hop's host:port to the addresses that
    were just validated. Without it curl resolves the name a second time for
    the actual connection, and a DNS answer that changes between the two
    lookups — on purpose: "DNS rebinding" — lands the request on a private
    address that passed as public. Pinned, the connection can only go where
    the check looked."""
    if not addrs:
        return []
    host = parsed.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")  # curl matches the punycode form
    except UnicodeError:
        pass
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    joined = ",".join(f"[{a}]" if ":" in a else a for a in addrs)
    return [f"{host}:{port}:{joined}"]


class _Hop(NamedTuple):
    """One raw HTTP response from a single (un-redirected) GET."""

    status: int
    headers: httpx.Headers
    content: bytes
    text: str


def _charset(headers: httpx.Headers, head: bytes) -> str:
    """The body's encoding: the Content-Type charset, else a <meta charset>
    declaration, else UTF-8. Streaming means we decode the bytes ourselves
    rather than letting the HTTP client do its own detection."""
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if not m:
        m = re.search(rb"""<meta[^>]+charset=["']?([\w\-]+)""", head[:4096], re.I)
        if m:
            return m.group(1).decode("ascii", "replace")
    return m.group(1) if m else "utf-8"


def _browser_get_sync(url: str, pin: list[str] | None = None) -> _Hop:
    """One browser-fingerprinted GET. Redirects are handled by the caller (so
    every hop is host-validated), hence ``allow_redirects=False``. ``pin`` is
    the RESOLVE entry from `_pin`: the only addresses curl may connect to.

    Streamed and capped at MAX_FETCH_BYTES: the body is attacker-chosen (any
    URL a user pastes), so reading it whole into memory unbounded is how one
    import takes the process down.
    """
    from curl_cffi import CurlOpt
    from curl_cffi import requests as creq

    too_large = TooLargeError(
        f"that page is larger than {MAX_FETCH_BYTES // (1024 * 1024)} MB.")
    options = {CurlOpt.RESOLVE: list(pin)} if pin else None
    with creq.Session(curl_options=options) as session:
        r = session.request("GET", url, impersonate=_IMPERSONATE,
                            allow_redirects=False, timeout=25, stream=True)
        try:
            headers = httpx.Headers(list(r.headers.items()))
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
                raise too_large  # save the download when the server is honest
            chunks, total = [], 0
            for chunk in r.iter_content():
                total += len(chunk)
                if total > MAX_FETCH_BYTES:
                    raise too_large  # chunked / lying Content-Length
                chunks.append(chunk)
            status = r.status_code
        finally:
            r.close()
    content = b"".join(chunks)
    try:
        text = content.decode(_charset(headers, content), errors="replace")
    except LookupError:
        text = content.decode("utf-8", errors="replace")  # site named a codec Python has no idea about
    return _Hop(status, headers, content, text)


def _fetch_hop(url: str) -> _Hop:
    """Validate, resolve, pin and GET one hop. Runs in a worker thread: name
    resolution blocks, and a slow resolver must not stall the event loop for
    every other request."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http and https links are supported.")
    addrs = _validate_host(parsed.hostname or "")
    return _browser_get_sync(url, _pin(parsed, addrs))


async def safe_fetch(url: str) -> tuple[str, FetchResult]:
    """Fetch a public web resource safely. Returns (final_url, result).

    Impersonates Chrome and follows redirects manually, re-validating the host
    at each hop. The body is fully read, so the caller can use ``result.text``
    (HTML), ``result.content`` (binary, e.g. a PDF) or ``result.headers`` after
    this returns.

    Raises UnsafeUrlError for internal/blocked targets, FetchError for an
    upstream non-success status, or curl_cffi errors for network failures.
    """
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    for _ in range(MAX_REDIRECTS + 1):
        # Validation, resolution and the (blocking) fetch, off the event loop.
        hop = await asyncio.to_thread(_fetch_hop, url)

        location = hop.headers.get("location")
        if hop.status in _REDIRECT_STATUSES and location:
            url = urljoin(url, location)  # re-validated at the top of the loop
            continue

        result = FetchResult(url, hop.status, hop.headers, hop.content, hop.text)
        if result.status_code >= 400:
            raise FetchError(f"the site returned HTTP {result.status_code}.")
        return url, result

    raise UnsafeUrlError("Too many redirects.")
