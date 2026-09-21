"""Strings from the network, made safe to put in a log line."""
from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(s: str | None, limit: int = 200) -> str:
    """`s` with every control character (newlines included — a forged log line
    is the point of sending one) replaced by a space, cut to `limit`."""
    return _CONTROL.sub(" ", s or "")[:limit]
