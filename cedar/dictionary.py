"""Word definitions for the reader's long-press look-up.

The apps used to call dictionaryapi.dev straight from the device, which was
fine until it wasn't: that API is a free hobby service that stalls for tens
of seconds at a time, and a stall on the phone is a spinner the user stares
at. Look-ups now go through this module so the provider mix is a server-side
knob (admin Settings → ``dict_providers``) and every word is answered once,
then served from SQLite for everyone.

Providers (all free, no key):

* ``wiktionary``   — en.wiktionary.org REST ``page/definition``. Multilingual:
                     one page carries the word in every language it exists in,
                     so a Spanish document asks for the ``es`` section. Best
                     coverage; ~0.6 s; asks for a contact User-Agent.
* ``datamuse``     — api.datamuse.com ``md=dp``. English only, very fast, the
                     definitions are Wiktionary's own.
* ``dictionaryapi`` — api.dictionaryapi.dev. English only; keeps IPA phonetics
                     when the others have none. The slow one — last by default.

Strategy: the first provider in the list gets a head start (``dict_hedge_ms``);
if it hasn't answered by then the rest are fired in parallel and the first
non-empty answer wins. Each request has its own timeout (``dict_timeout_s``),
so the worst case is one timeout, not a chain of them. Misses are cached too
(for a day) — a word that no provider knows shouldn't cost three round trips
every time someone presses it.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from urllib.parse import quote

import httpx

from . import settings as appsettings
from .db import connect

log = logging.getLogger("cedar.dictionary")

DEFAULT_PROVIDERS = "wiktionary,datamuse,dictionaryapi"
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_HEDGE_MS = 700
HIT_TTL_S = 90 * 86400   # a definition doesn't go stale
MISS_TTL_S = 86400       # but a miss might just be a provider hiccup

_UA = "cedar-server/1.0 (+https://github.com/mlevinmail/cedar-server)"
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=3.0),
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    headers={"User-Agent": _UA, "Accept": "application/json"},
    follow_redirects=True,
)

# One meaning: part of speech, the definition, an optional example sentence.
Meaning = dict[str, str]
Entry = dict[str, Any]  # {"phonetic": str|None, "meanings": [Meaning]}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Wiktionary stubs that are editor notes, not definitions.
_JUNK_RE = re.compile(r"\{\{|rfdef|needs a definition|This term needs", re.I)
# "simple past and past participle of paper", "plural of whale",
# "third-person singular simple present indicative of run" — inflection stubs
# whose real definition lives on the base word.
_FORM_OF_RE = re.compile(
    r"^(?:(?:simple |past |present |third-person |first-person |second-person |singular |plural |"
    r"comparative |superlative |indicative |subjunctive |imperative |participle |form |tense |"
    r"alternative |obsolete |archaic |dated |nonstandard |misspelling |spelling |inflection |"
    r"and |of |the |a )*)"
    r"(?:participle|past|plural|form|spelling|inflection|present|singular|tense|comparative|superlative|misspelling)"
    r"[^:;.]*? of (?P<base>[A-Za-z\u00C0-\u024F'’-]{2,})\b", re.I)


def init_tables() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS definitions (
                word       TEXT NOT NULL,
                lang       TEXT NOT NULL,
                provider   TEXT,
                payload    TEXT,          -- JSON Entry, NULL = known miss
                fetched_at REAL NOT NULL,
                PRIMARY KEY (word, lang)
            );
            """
        )


# ------------------------------------------------------------------ helpers

def _clean(s: str) -> str:
    """Strip markup a provider leaves in a definition (Wiktionary links).
    Editor placeholders come back empty so callers skip them."""
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", s))).strip()
    return "" if _JUNK_RE.search(text) else text


def _form_of(entry: Entry) -> str | None:
    """The base word when *every* sense is an inflection stub ("past
    participle of paper") — the card should show the base word instead."""
    bases: list[str] = []
    for m in entry.get("meanings") or []:
        mo = _FORM_OF_RE.match(m.get("definition", ""))
        if not mo:
            return None
        bases.append(mo.group("base").lower())
    return bases[0] if bases else None


def normalize(word: str) -> str:
    """What the user pressed, trimmed to the word itself (no quotes, no
    trailing punctuation, no soft hyphens), lower-cased."""
    w = word.replace("­", "").strip()
    w = re.sub(r"^[^\w]+|[^\w]+$", "", w, flags=re.UNICODE)
    return w.lower()


def _candidates(word: str, lang: str) -> list[str]:
    """Spellings to try, most specific first. Inflections are cheap guesses:
    Wiktionary has "running" but datamuse only "run"."""
    out = [word]
    if lang == "en":
        if word.endswith("ies") and len(word) > 4:
            out.append(word[:-3] + "y")
        if word.endswith("es") and len(word) > 4:
            out.append(word[:-2])
        if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            out.append(word[:-1])
        if word.endswith("ing") and len(word) > 5:
            out.append(word[:-3])
            out.append(word[:-3] + "e")
        if word.endswith("ed") and len(word) > 4:
            out.append(word[:-2])
            out.append(word[:-1])
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


def _settings() -> tuple[list[str], float, int]:
    raw = appsettings.get("dict_providers") or DEFAULT_PROVIDERS
    names = [p.strip() for p in str(raw).split(",") if p.strip() in PROVIDERS]
    if not names:
        names = DEFAULT_PROVIDERS.split(",")
    try:
        timeout = float(appsettings.get("dict_timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    try:
        hedge = int(appsettings.get("dict_hedge_ms") or DEFAULT_HEDGE_MS)
    except (TypeError, ValueError):
        hedge = DEFAULT_HEDGE_MS
    # Bounded both ways: a knob set to an hour would let one look-up hold a
    # worker and a connection for that long.
    return names, min(30.0, max(0.5, timeout)), min(10_000, max(0, hedge))


# ---------------------------------------------------------------- providers

async def _wiktionary(word: str, lang: str, timeout: float) -> Entry | None:
    # The word is a path segment, whatever characters it carries.
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{quote(word, safe='')}"
    r = await _client.get(url, timeout=timeout, headers={"Accept-Language": lang})
    if r.status_code != 200:
        return None
    data = r.json()
    if not isinstance(data, dict):
        return None
    # The page carries every language the spelling exists in; prefer the
    # document's own, then English, then whatever there is.
    section = data.get(lang) or data.get("en") or next(iter(data.values()), None)
    if not section:
        return None
    meanings: list[Meaning] = []
    for block in section:
        pos = str(block.get("partOfSpeech") or "").lower()
        for d in block.get("definitions") or []:
            text = _clean(str(d.get("definition") or ""))
            if not text:
                continue
            m: Meaning = {"pos": pos, "definition": text}
            ex = (d.get("examples") or [None])[0]
            if ex:
                m["example"] = _clean(str(ex))
            meanings.append(m)
            break  # one per part of speech keeps the card short
        if len(meanings) >= 4:
            break
    return {"phonetic": None, "meanings": meanings} if meanings else None


_DATAMUSE_POS = {"n": "noun", "v": "verb", "adj": "adjective", "adv": "adverb",
                 "u": "", "prop": "proper noun"}


async def _datamuse(word: str, lang: str, timeout: float) -> Entry | None:
    if lang != "en":
        return None
    r = await _client.get("https://api.datamuse.com/words",
                          params={"sp": word, "md": "dp", "max": 1}, timeout=timeout)
    if r.status_code != 200:
        return None
    rows = r.json()
    if not rows or not isinstance(rows, list):
        return None
    top = rows[0]
    if str(top.get("word", "")).lower() != word:
        return None  # `sp` is a spelling *pattern*; a different word came back
    meanings: list[Meaning] = []
    seen_pos: set[str] = set()
    for d in top.get("defs") or []:
        pos, _, text = str(d).partition("\t")
        pos = _DATAMUSE_POS.get(pos, pos)
        text = _clean(text)
        if not text or pos in seen_pos:
            continue
        seen_pos.add(pos)
        meanings.append({"pos": pos, "definition": text})
        if len(meanings) >= 4:
            break
    return {"phonetic": None, "meanings": meanings} if meanings else None


async def _dictionaryapi(word: str, lang: str, timeout: float) -> Entry | None:
    if lang != "en":
        return None
    r = await _client.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word, safe='')}",
                          timeout=timeout)
    if r.status_code != 200:
        return None
    rows = r.json()
    if not rows or not isinstance(rows, list):
        return None
    entry = rows[0]
    phon = (entry.get("phonetic") or "").strip() or None
    if not phon:
        for ph in entry.get("phonetics") or []:
            t = (ph.get("text") or "").strip()
            if t:
                phon = t
                break
    meanings: list[Meaning] = []
    for m in entry.get("meanings") or []:
        pos = str(m.get("partOfSpeech") or "")
        for d in m.get("definitions") or []:
            text = _clean(str(d.get("definition") or ""))
            if not text:
                continue
            mm: Meaning = {"pos": pos, "definition": text}
            if d.get("example"):
                mm["example"] = _clean(str(d["example"]))
            meanings.append(mm)
            break
        if len(meanings) >= 4:
            break
    return {"phonetic": phon, "meanings": meanings} if meanings else None


Provider = Callable[[str, str, float], Awaitable[Entry | None]]
PROVIDERS: dict[str, Provider] = {
    "wiktionary": _wiktionary,
    "datamuse": _datamuse,
    "dictionaryapi": _dictionaryapi,
}


async def _ask(name: str, word: str, lang: str, timeout: float) -> tuple[str, Entry | None]:
    try:
        return name, await PROVIDERS[name](word, lang, timeout)
    except Exception as e:  # network, JSON, timeout — all just "no answer"
        log.info("dictionary %s(%r, %s) failed: %s", name, word, lang, e)
        return name, None


async def _race(word: str, lang: str) -> tuple[str, Entry | None]:
    """First provider gets a head start; then everyone races. First non-empty
    answer wins; the losers are cancelled."""
    names, timeout, hedge = _settings()
    tasks: list[asyncio.Task] = [asyncio.create_task(_ask(names[0], word, lang, timeout))]
    try:
        pending = set(tasks)
        deadline = hedge / 1000.0
        while pending:
            done, pending = await asyncio.wait(pending, timeout=deadline,
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                name, entry = t.result()
                if entry:
                    return name, entry
            if len(tasks) < len(names):
                # Head start over (timed out or the leader missed) — fire the rest.
                for n in names[1:]:
                    tasks.append(asyncio.create_task(_ask(n, word, lang, timeout)))
                pending |= set(tasks[1:])
                deadline = None
        return names[0], None
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


# -------------------------------------------------------------------- public

def _cached(word: str, lang: str) -> tuple[bool, str | None, Entry | None]:
    with connect() as c:
        row = c.execute("SELECT provider, payload, fetched_at FROM definitions "
                        "WHERE word=? AND lang=?", (word, lang)).fetchone()
    if row is None:
        return False, None, None
    age = time.time() - row["fetched_at"]
    if row["payload"] is None:
        return (age < MISS_TTL_S), row["provider"], None
    if age > HIT_TTL_S:
        return False, None, None
    try:
        return True, row["provider"], json.loads(row["payload"])
    except ValueError:
        return False, None, None


def _store(word: str, lang: str, provider: str | None, entry: Entry | None) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO definitions (word, lang, provider, payload, fetched_at) "
                  "VALUES (?, ?, ?, ?, ?)",
                  (word, lang, provider, json.dumps(entry) if entry else None, time.time()))


async def define(word: str, lang: str | None, _hop: int = 0) -> dict | None:
    """Look ``word`` up for a document in ``lang`` (None → English). Returns
    ``{word, lang, provider, phonetic, meanings, cached}`` or None. An
    inflection-only answer ("past participle of paper") is followed one hop
    to the base word, so the card for "papered" reads "paper"."""
    lang = (lang or "en").lower()[:2]
    base = normalize(word)
    if not base or len(base) > 64:
        return None
    for cand in _candidates(base, lang):
        hit, provider, entry = _cached(cand, lang)
        cached = True
        if not hit:
            provider, entry = await _race(cand, lang)
            _store(cand, lang, provider if entry else None, entry)
            cached = False
        if not entry:
            continue  # a miss (fresh or remembered) — try the next spelling
        stem = _form_of(entry)
        if stem and stem != cand and _hop == 0:
            followed = await define(stem, lang, _hop=1)
            if followed:
                followed["form_of"] = cand
                return followed
        return {"word": cand, "lang": lang, "provider": provider, "cached": cached, **entry}
    return None
