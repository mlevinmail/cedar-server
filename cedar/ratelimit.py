"""Tiny in-memory sliding-window rate limiter.

The backend is a single process, so in-memory is authoritative; a restart
forgetting the windows is harmless. Keys are arbitrary strings ("ip:1.2.3.4",
"u:57") so one limiter can watch several dimensions at once.

Two usage patterns:
  * allow(key)        — count every attempt (signups).
  * check(key)+hit(key) — count only *failures* (promo-code guesses): the
    check gates up front, the hit is recorded after the attempt misses. Legit
    traffic (valid share-link lookups) then never contributes to the limit,
    while enumeration — which is almost all misses — locks out fast.
"""
from __future__ import annotations

import time
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_s: float, max_keys: int = 10_000):
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._log: dict[str, deque] = {}

    def _q(self, key: str, now: float) -> deque:
        q = self._log.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def check(self, key: str) -> bool:
        """True while `key` is under the limit. Records nothing."""
        return len(self._q(key, time.time())) < self.limit

    def hit(self, key: str) -> None:
        """Record one event against `key`."""
        now = time.time()
        self._q(key, now).append(now)
        if len(self._log) > self.max_keys:  # bound memory: drop idle keys
            for k in [k for k, v in self._log.items()
                      if not v or now - v[-1] > self.window_s]:
                self._log.pop(k, None)

    def allow(self, key: str) -> bool:
        """check + hit in one step, for events counted win or lose."""
        if not self.check(key):
            return False
        self.hit(key)
        return True

    def reset(self, key: str) -> None:
        """Forget `key`'s history. For failure counters that a success should
        clear — someone who finally remembers their password shouldn't keep
        carrying the misses that got them there."""
        self._log.pop(key, None)
