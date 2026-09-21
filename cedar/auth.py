"""The owner key: one credential, one library, any number of devices.

There are no accounts. Whoever holds the key *is* the owner, so every device
that is given the same server address and key sees the same library, settings
and reading positions — that is the whole multi-device story.

The key comes from ``CEDAR_KEY`` when set. Otherwise it is generated once on
first start, kept in ``<data>/cedar.key`` (mode 0600) and printed to the
console, so a fresh ``docker compose up`` needs no configuration at all.

Clients send it as ``Authorization: Bearer <key>`` (or ``X-Cedar-Key``).
Comparison is constant-time. A peer that keeps presenting wrong keys is
throttled — but only wrong keys count and the throttle is checked *after* the
comparison, so a device holding the right key is never locked out, not even
when it shares an address with an attacker (everyone behind one NAT or one
reverse proxy looks like a single peer).
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets

from fastapi import Request

from . import ratelimit
from .config import CEDAR_KEY, KEY_PATH

log = logging.getLogger("cedar.auth")

# The generated key is announced on the console only. main._configure_logging
# gives this logger the console handlers and no file handler, so the key never
# lands in <data>/logs/cedar.log next to the database it protects.
banner_log = logging.getLogger("cedar.auth.banner")

_key: str = ""

# A configured key shorter than this is refused at startup: with the guess
# throttle below as the only other defence, the key's length is the security.
MIN_KEY_LENGTH = 16

# Wrong-key attempts per client address: 30 in 10 minutes, then 429 for the
# rest of the window. Only failures count.
_failures = ratelimit.SlidingWindow(limit=30, window_s=600)
THROTTLE_RETRY_S = 600


class WeakKeyError(RuntimeError):
    """A configured owner key that is too short or not plain ASCII."""


def _check_strength(key: str, source: str) -> None:
    if len(key) < MIN_KEY_LENGTH or not key.isascii() or not key.isprintable() or " " in key:
        raise WeakKeyError(
            f"{source}: the owner key must be at least {MIN_KEY_LENGTH} characters of plain "
            f"ASCII with no spaces (got {len(key)}). Generate one with: openssl rand -base64 24")


def load_key() -> str:
    """Resolve the owner key (env → file → freshly generated) and return it."""
    global _key
    if CEDAR_KEY:
        _check_strength(CEDAR_KEY, "CEDAR_KEY")
        _key = CEDAR_KEY
        log.info("owner key: from CEDAR_KEY")
        return _key
    try:
        existing = KEY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        _check_strength(existing, str(KEY_PATH))
        _key = existing
        log.info("owner key: from %s", KEY_PATH)
        return _key
    _key = secrets.token_urlsafe(24)
    KEY_PATH.write_text(_key + "\n", encoding="utf-8")
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    _banner(_key)
    return _key


def _banner(key: str) -> None:
    line = "=" * 64
    banner_log.warning("\n%s\n  A new owner key was generated. Enter it in the Cedar app\n"
                       "  (Use your own server) together with this server's address:\n\n"
                       "      %s\n\n  It is saved in %s — keep it private.\n%s",
                       line, key, KEY_PATH, line)


def key() -> str:
    return _key


def presented(request: Request) -> str:
    """The credential exactly as the client sent it, or ''."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-cedar-key", "").strip()


def valid(candidate: str) -> bool:
    """Constant-time comparison on bytes: `hmac.compare_digest` refuses
    non-ASCII *strings* with a TypeError, and a header is anything the peer
    chose to send — that must be a plain "no", not a 500."""
    if not _key or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), _key.encode("utf-8"))


def client_address(request: Request) -> str:
    return request.client.host if request.client else "?"


def authenticate(request: Request) -> str:
    """'ok' for the owner; otherwise 'throttled' (this peer has guessed too
    often — say nothing about the key) or 'bad' (wrong or missing, counted)."""
    if valid(presented(request)):
        return "ok"
    peer = client_address(request)
    if not _failures.check(peer):
        return "throttled"
    _failures.hit(peer)
    return "bad"
