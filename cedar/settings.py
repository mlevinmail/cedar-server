"""Everything the owner can change, in one small key/value table.

Two kinds of thing live here, deliberately in the same place:

* **preferences** the app syncs across devices — the voice chosen per
  language, reading speed, theme (``GET/PUT /api/settings``);
* **server knobs** — the book store's curated/full switch, dictionary and
  translation tuning (``GET/PUT /api/settings/server``).

Values are JSON. Unknown keys are refused on write, so a typo can't silently
create a setting nothing reads.
"""
from __future__ import annotations

import json
from typing import Any

from . import lang
from .db import connect

# Server knobs, with their defaults. This is also the allow-list for
# PUT /api/settings/server.
KNOBS: dict[str, Any] = {
    # Book store: serve every Gutenberg book on disk (True) or only the curated
    # set the ingest marked copyright-verifiable and free of the explicit /
    # racist-ideology lists (False). Read per request by catalog.py.
    "catalog_full": False,
    # Word look-up (dictionary.py): provider order, per-request timeout, and
    # how long the first provider gets before the rest are raced.
    "dict_providers": "wiktionary,datamuse,dictionaryapi",
    "dict_timeout_s": 3.0,
    "dict_hedge_ms": 700,
    # Translations under the definition (translate.py): kill switch + timeout.
    "translate_enabled": True,
    "translate_timeout_s": 4.0,
}

# Preferences the app syncs. `voices` is {lang: voice}; `voice` is the flat
# "last picked" that older clients read; speed/theme are account-wide.
PREF_KEYS = ("voice", "voices", "speed", "theme")


def init_tables() -> None:
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def get(key: str, default: Any = None) -> Any:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return KNOBS.get(key, default)
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def put(key: str, value: Any) -> None:
    with connect() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))


# ------------------------------------------------------------------- server knobs

def knobs() -> dict[str, Any]:
    return {k: get(k) for k in KNOBS}


def update_knobs(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. Unknown keys raise KeyError; a None resets a knob
    to its default."""
    for k in patch:
        if k not in KNOBS:
            raise KeyError(k)
    for k, v in patch.items():
        put(k, None if v is None else _coerce(k, v))
    return knobs()


def _coerce(key: str, value: Any) -> Any:
    default = KNOBS[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


# -------------------------------------------------------------------- preferences

def prefs() -> dict[str, Any]:
    return {k: v for k in PREF_KEYS if (v := get(k)) is not None}


def update_prefs(patch: dict[str, Any]) -> dict[str, Any]:
    for k, v in patch.items():
        if k in PREF_KEYS:
            put(k, v)
    return prefs()


def chosen_voices() -> dict[str, str]:
    """The voice picked for each language — picks only, no defaults.

    A flat ``voice`` (older clients) counts as the pick for *that voice's*
    language and nothing wider: one Japanese voice, set once for Japanese,
    must never end up reading an English library.
    """
    voices = {k: v for k, v in (get("voices") or {}).items()
              if isinstance(k, str) and isinstance(v, str)}
    legacy = get("voice")
    if isinstance(legacy, str) and legacy:
        language = lang.language_of(legacy)
        if language and language not in voices:
            voices[language] = legacy
    return voices


def set_voice(voice: str, language: str | None = None) -> None:
    """Record ``voice`` as the voice for ``language`` (by default the language
    the voice itself speaks). Every document in that language follows.

    The flat ``voice`` key is written too for clients that predate per-language
    voices.
    """
    voices = dict(get("voices") or {})
    language = language or lang.language_of(voice)
    if language:
        voices[language] = voice
    put("voices", voices)
    put("voice", voice)


def reading_defaults() -> tuple[str, float]:
    """(main voice, speed) — the fallbacks a document uses when nothing more
    specific applies."""
    from .config import DEFAULT_SPEED, DEFAULT_VOICE
    return (get("voice") or DEFAULT_VOICE), float(get("speed") or DEFAULT_SPEED)
