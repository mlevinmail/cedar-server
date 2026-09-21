"""Word look-up for the reader's long-press definition card."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from .. import dictionary, translate

router = APIRouter(prefix="/api")

# How many languages one card may ask for. The device sends every language its
# owner reads; the cap keeps one press from fanning out without bound.
MAX_TARGETS = 4


def _targets(to: str | None) -> list[str]:
    """The languages asked for, in the order the reader prefers them."""
    out: list[str] = []
    for raw in (to or "").split(","):
        code = raw.strip().lower()[:2]
        if len(code) == 2 and code.isalpha() and code not in out:
            out.append(code)
        if len(out) >= MAX_TARGETS:
            break
    return out


@router.get("/define")
async def define(word: str = Query(..., min_length=1, max_length=80),
                 lang: str | None = Query(None, max_length=8),
                 to: str | None = Query(None, max_length=24)):
    """Define ``word`` as read in ``lang``; ``to`` is a comma-separated list of
    languages to also say it in ("uk,fr"), best first.

    Everything runs together rather than in sequence: the translations are bonus
    lines on a card the reader is already waiting for, and they must never be the
    reason it is slow. They are also the only part allowed to fail — a language
    that doesn't come back simply leaves its line off.
    """
    wanted = _targets(to)
    result, *found = await asyncio.gather(
        dictionary.define(word, lang),
        *(translate.lookup(word, lang, t) for t in wanted),
    )
    if result is None:
        raise HTTPException(404, "No definition found.")

    # The definition may have resolved to another spelling ("wolves" → "wolf");
    # the translation tables are on *that* entry, so the languages that missed
    # are worth one more look.
    resolved = str(result.get("word") or "")
    if resolved and resolved != dictionary.normalize(word):
        missing = [t for t, tr in zip(wanted, found) if tr is None]
        if missing:
            again = await asyncio.gather(
                *(translate.lookup(resolved, lang, t) for t in missing))
            filled = dict(zip(missing, again))
            found = [tr or filled.get(t) for t, tr in zip(wanted, found)]

    trs = [tr for tr in found if tr]
    if trs:
        result["translations"] = trs
        result["translation"] = trs[0]  # single-language shape older builds read
    return result
