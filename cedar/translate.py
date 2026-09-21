"""Word translations for the reader's long-press card.

A definition tells you what a word means; for someone reading in a second
language, the one-word answer in their own is often what they actually wanted.
This module supplies that line under the definition.

Source: **English Wiktionary's translation tables**. Every English entry
carries a table of the word in a hundred-odd languages, maintained by the same
project whose definitions already back ``dictionary.py`` — so this adds a
translation without adding a paid API, a key, or a third party who gets to see
what people are reading. Big entries move their table to a ``/translations``
subpage, which is why both pages are fetched.

Caveats worth knowing before trusting a blank result:

* Translation tables live on **English** entries. Asking for the French of a
  Spanish word in a Spanish document will usually find nothing, and that is the
  honest answer rather than a bug.
* The table is per *sense*, and only the first sense that carries the language
  is used. So a word with several meanings gets its primary one translated,
  which is right far more often than it is wrong — but it is a guess, not a
  reading of the sentence the word was in.
* Coverage thins out for rare words. A miss is cached for a day, a hit for
  three months, exactly like a definition.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from . import settings as appsettings
from .db import connect

log = logging.getLogger("cedar.translate")

DEFAULT_TIMEOUT_S = 4.0
HIT_TTL_S = 90 * 86400
MISS_TTL_S = 86400
MAX_TERMS = 3

_UA = "cedar-server/1.0 (+https://github.com/mlevinmail/cedar-server)"
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=3.0),
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    headers={"User-Agent": _UA, "Accept": "application/json"},
    follow_redirects=True,
)

_API = "https://en.wiktionary.org/w/api.php"

# Wiktionary marks up a translation as {{t|fr|…}}, and in a table as {{tt|…}};
# a trailing + means the target wiki has the page, and a -check/+check suffix
# means an editor flagged it as unverified. All of them are the same thing to
# a reader, so all of them are accepted.
def _term_re(lang: str) -> re.Pattern[str]:
    return re.compile(r"\{\{t{1,2}\+?(?:[-+]?check)?\|" + re.escape(lang) + r"\|([^}|]+)")


# Terms carrying wiki markup or a placeholder are editor scaffolding, not words.
_JUNK_RE = re.compile(r"[\[\]{}<>#=]|^\s*$")

# One sense's table: {{trans-top|a large stream of water}} … {{trans-bottom}}.
# The senses matter — an entry lists every meaning it has, and scanning the
# whole page for a language returns them jumbled together, which is how "river"
# came back as "коридор" (the corridor sense) and "wolf" as "пожирать" (to
# devour). Only the first table that carries the language is used, so the
# translation belongs to the word's primary sense.
_BLOCK_RE = re.compile(
    r"\{\{(?:check)?trans-top[^}]*\}\}(.*?)(?=\{\{trans-bottom|\{\{(?:check)?trans-top|\Z)",
    re.S,
)


def init_tables() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS translations (
                word       TEXT NOT NULL,
                src        TEXT NOT NULL,   -- the language the word was read in
                dst        TEXT NOT NULL,   -- the language asked for
                payload    TEXT,            -- JSON list of terms, NULL = a known miss
                fetched_at REAL NOT NULL,
                PRIMARY KEY (word, src, dst)
            );
            """
        )


def _timeout() -> float:
    try:
        return min(30.0, max(0.5, float(appsettings.get("translate_timeout_s") or DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def enabled() -> bool:
    v = appsettings.get("translate_enabled")
    return True if v is None else bool(v)


async def _wikitext(page: str, timeout: float) -> str | None:
    r = await _client.get(
        _API,
        params={"action": "parse", "page": page, "prop": "wikitext",
                "format": "json", "formatversion": "2"},
        timeout=timeout,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if not isinstance(data, dict) or "error" in data:
        return None
    text = data.get("parse", {}).get("wikitext")
    return text if isinstance(text, str) else None


def _scan(text: str, dst: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _term_re(dst).findall(text):
        term = raw.strip()
        if not term or _JUNK_RE.search(term) or term.lower() in seen:
            continue
        seen.add(term.lower())
        out.append(term)
        if len(out) >= MAX_TERMS:
            break
    return out


def _terms(wikitext: str, dst: str) -> list[str]:
    """The word in ``dst``, taken from the first sense that offers it."""
    for block in _BLOCK_RE.findall(wikitext):
        found = _scan(block, dst)
        if found:
            return found
    # No sense tables at all (a short entry, or a layout we don't know) — read
    # the page as one.
    return _scan(wikitext, dst)


async def _fetch(word: str, dst: str) -> list[str]:
    """The entry and its translations subpage, fetched together.

    A long entry (``book``, ``house``) moves its tables to ``word/translations``
    and leaves a {{trans-see}} pointer behind, so both pages have to be tried —
    and asking for them one after the other put the slowest words over the
    timeout, which is exactly the case the subpage exists for. The subpage wins
    when both answer: if it exists, it is where the real table lives.
    """
    timeout = _timeout()
    page, sub = await asyncio.gather(
        _wikitext(word, timeout),
        _wikitext(f"{word}/translations", timeout),
        return_exceptions=True,
    )
    for text in (sub, page):
        if isinstance(text, str) and text:
            found = _terms(text, dst)
            if found:
                return found
    if isinstance(page, BaseException) and isinstance(sub, BaseException):
        raise page  # both legs failed — a miss we must not cache
    return []


def _cached(word: str, src: str, dst: str) -> tuple[bool, list[str]]:
    with connect() as c:
        row = c.execute(
            "SELECT payload, fetched_at FROM translations WHERE word=? AND src=? AND dst=?",
            (word, src, dst),
        ).fetchone()
    if row is None:
        return False, []
    age = time.time() - row["fetched_at"]
    if row["payload"] is None:
        return (age < MISS_TTL_S), []
    if age > HIT_TTL_S:
        return False, []
    try:
        terms = json.loads(row["payload"])
        return True, [str(t) for t in terms] if isinstance(terms, list) else []
    except ValueError:
        return False, []


def _store(word: str, src: str, dst: str, terms: list[str]) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO translations (word, src, dst, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (word, src, dst, json.dumps(terms) if terms else None, time.time()),
        )


async def lookup(word: str, src: str | None, dst: str | None) -> dict | None:
    """``word`` (read in ``src``) said in ``dst``. Returns
    ``{"lang": dst, "terms": [...]}`` or None — None covers every uninteresting
    case (turned off, nothing asked for, same language, nothing found), because
    the card simply omits the line rather than explaining itself.

    Never raises: a translation is a bonus on a card that has to appear either
    way, so a provider having a bad day must cost nothing but the line.
    """
    if not dst or not enabled():
        return None
    src = (src or "en").lower()[:2]
    dst = dst.lower()[:2]
    if dst == src or not re.fullmatch(r"[a-z]{2}", dst):
        return None
    from .dictionary import normalize  # same trimming the definition side uses
    base = normalize(word)
    if not base or len(base) > 64:
        return None

    hit, terms = _cached(base, src, dst)
    if not hit:
        try:
            terms = await _fetch(base, dst)
        except Exception as e:  # network, JSON, timeout — all just "no line"
            log.info("translate(%r, %s→%s) failed: %s", base, src, dst, e)
            return None
        _store(base, src, dst, terms)
    return {"lang": dst, "terms": terms} if terms else None
