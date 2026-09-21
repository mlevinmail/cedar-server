"""The book store: a server-side catalog of public-domain classics.

Backed by two artifacts that ``tools/gutenberg/gutenberg_ingest.py`` builds
(see that script for the inclusion rules — everything here is stripped down to
what the TTS can speak):

  * ``<data>/gutenberg/catalog.db``  — metadata + FTS5 search index
  * ``<data>/gutenberg/raw/{gid}/``  — mirrored plaintext + cover images

Curated vs. full: the ingest marks each row ``curated`` (copyright-verifiable
for Canada, not on the explicit or racist-ideology lists) and keeps the rest
with ``curated=0``. By default the store serves curated rows only; the setting
``catalog_full`` (``PUT /api/settings/server {"catalog_full": true}``) serves
every row that has text on disk instead — shelves, search, detail, cover and
add-to-library all follow it, so flipping it back hides those books again
(already-imported copies stay in the library). A catalog.db built before the
column existed is treated as all-curated.

The catalog is optional: when the files are absent (nobody downloaded the
corpus), every endpoint reports an empty store rather than erroring, and the
app simply hides its store tab.

Adding a book strips the Project Gutenberg header/footer/license — the texts
are public domain but the trademark is not, so what reaches the library
carries no PG branding — and feeds the clean text through the normal plain-text
chunker (which already detects PG's hard-wrapped paragraphs and chapters). The
chunker also drops the *printed* table of contents (the reader has its own
Contents picker) and reports where the body begins; that becomes the fresh
document's initial position, so a listener starts at the story — prefaces and
dedications included — instead of the title page, while staying free to seek
back to the very top.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

from . import db, lang, settings
from .chunker import chunk_plain_text
from .config import GUTENBERG_DIR

CATALOG_DB = GUTENBERG_DIR / "catalog.db"
RAW_DIR = GUTENBERG_DIR / "raw"

# Words-per-minute the estimate labels assume (Kokoro at 1.0x lands close).
_WPM = 155
# A PG plaintext file carries ~18 KB of header/license around the actual book,
# and English prose runs ~6 bytes per word — good enough for an "≈9 h" label.
_HEADER_BYTES, _BYTES_PER_WORD = 18_000, 6.0

_LANG_LABEL = {"en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
               "pt": "Portuguese", "hi": "Hindi"}

# Home-screen shelves, in display order. Genres must match GENRE_RULES tags in
# gutenberg_ingest.py.
_HOME_GENRES = [
    "Fiction", "Mystery & Crime", "Adventure", "Science Fiction", "Romance",
    "Children's", "Fantasy", "Horror & Gothic", "Philosophy", "Poetry",
    "History", "Biography", "Humor", "Drama", "Myths & Folklore",
    "Science & Nature", "Travel", "Short Stories",
]


def available() -> bool:
    return CATALOG_DB.exists()


def _connect() -> sqlite3.Connection:
    # Read-only: the catalog is built offline by the ingest tool; the app never
    # writes it. mode=ro also makes a half-copied file fail loudly, not corrupt.
    con = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def full_catalog() -> bool:
    """Serve every book on disk (True) or just the curated set (False)."""
    return bool(settings.get("catalog_full"))


def _visible(con: sqlite3.Connection, need_text: bool = True) -> str:
    """SQL condition for the books this server currently serves.
    Evaluated per request so the switch takes effect without a restart."""
    conds = ["has_text=1"] if need_text else []
    if not full_catalog() and any(
            r["name"] == "curated" for r in con.execute("PRAGMA table_info(books)")):
        conds.append("curated=1")
    return " AND ".join(conds) or "1=1"


def _est_minutes(text_bytes: int) -> int:
    words = max(text_bytes - _HEADER_BYTES, 0) / _BYTES_PER_WORD
    return int(words / _WPM)


def _card(r: sqlite3.Row) -> dict:
    """The compact book shape used by shelves and search results."""
    genres = [g for g in (r["genres"] or "").split(", ") if g]
    return {
        "gid": r["gid"],
        "title": r["title"],
        "authors": r["authors"],
        "language": r["language"],
        "language_label": _LANG_LABEL.get(r["language"], r["language"]),
        "downloads": r["downloads"],
        "genre": genres[0] if genres else None,
        "genres": genres,
        "minutes": _est_minutes(r["text_bytes"]),
        "has_cover": bool(r["has_cover"]),
    }


_CARD_COLS = "gid,title,authors,language,downloads,genres,text_bytes,has_cover"


def home() -> dict:
    """The store front page: featured row + top charts + one shelf per genre."""
    if not available():
        return {"available": False, "sections": []}
    with _connect() as con:
        vis = _visible(con)

        def shelf(sid: str, title: str, where: str, args: tuple, n: int) -> dict | None:
            rows = con.execute(
                f"SELECT {_CARD_COLS} FROM books WHERE {vis} AND {where} "
                f"ORDER BY downloads DESC LIMIT ?", (*args, n)).fetchall()
            return {"id": sid, "title": title, "books": [_card(r) for r in rows]} \
                if len(rows) >= 4 else None

        sections = [
            shelf("featured", "Featured", "has_cover=1", (), 10),
            shelf("top", "Top Books", "1=1", (), 15),
        ]
        for g in _HOME_GENRES:
            sections.append(shelf(
                f"genre:{g}", g,
                "has_cover=1 AND (', '||genres||', ') LIKE ?", (f"%, {g},%",), 12))
        sections.append(shelf("lang:fr", "En français", "language='fr'", (), 12))
        # The featured row is rendered as big cards with a blurb — attach one
        # there only, so the other shelves stay light on the wire.
        if sections[0]:
            for b in sections[0]["books"]:
                d = con.execute("SELECT description FROM books WHERE gid=?",
                                (b["gid"],)).fetchone()[0]
                b["description"] = (d[:220] + "…") if len(d) > 220 else d
        total = con.execute(f"SELECT COUNT(*) FROM books WHERE {vis}").fetchone()[0]
    return {"available": True, "total": total, "full": full_catalog(),
            "sections": [s for s in sections if s]}


def genres() -> list[dict]:
    """Genre chips for the browse screen, with book counts."""
    if not available():
        return []
    out = []
    with _connect() as con:
        vis = _visible(con)
        for g in _HOME_GENRES:
            n = con.execute(
                f"SELECT COUNT(*) FROM books WHERE {vis} AND (', '||genres||', ') LIKE ?",
                (f"%, {g},%",)).fetchone()[0]
            if n:
                out.append({"genre": g, "count": n})
    return out


def search(q: str = "", genre: str = "", language: str = "",
           page: int = 0, per: int = 40) -> dict:
    """Store search/browse. With a query: FTS over title/author/subjects, best
    sellers first among matches. Without: browse by genre/language, most
    downloaded first."""
    if not available():
        return {"books": [], "total": 0, "page": 0}
    page, per = max(0, page), min(max(per, 1), 60)
    where: list[str] = []
    args: list = []
    if genre:
        where.append("(', '||genres||', ') LIKE ?")
        args.append(f"%, {genre},%")
    if language:
        where.append("language=?")
        args.append(language)

    with _connect() as con:
        where.insert(0, _visible(con))
        tokens = re.findall(r"\w+", q or "", re.UNICODE)[:8]
        if tokens:
            # Sanitized prefix-match: each token quoted, so user input can't
            # inject FTS syntax. Rank pool by relevance, then order the pool by
            # popularity — "war" should surface War and Peace, not the most
            # bm25-exact obscurity.
            match = " ".join(f'"{t}"*' for t in tokens)
            pool = [r[0] for r in con.execute(
                "SELECT rowid FROM books_fts WHERE books_fts MATCH ? ORDER BY rank LIMIT 400",
                (match,)).fetchall()]
            if not pool:
                return {"books": [], "total": 0, "page": page}
            marks = ",".join("?" * len(pool))
            where.append(f"gid IN ({marks})")
            args.extend(pool)
        cond = " AND ".join(where)
        total = con.execute(f"SELECT COUNT(*) FROM books WHERE {cond}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT {_CARD_COLS} FROM books WHERE {cond} "
            f"ORDER BY downloads DESC LIMIT ? OFFSET ?",
            (*args, per, page * per)).fetchall()
    return {"books": [_card(r) for r in rows], "total": total, "page": page}


def book(gid: int) -> Optional[dict]:
    if not available():
        return None
    with _connect() as con:
        r = con.execute(
            f"SELECT * FROM books WHERE gid=? AND {_visible(con, need_text=False)}",
            (gid,)).fetchone()
    if not r:
        return None
    d = _card(r)
    d.update({
        "authors_full": r["authors_full"],
        "description": r["description"],
        "subjects": [s for s in (r["subjects"] or "").split("; ") if s][:12],
        "has_text": bool(r["has_text"]),
    })
    return d


def cover_path(gid: int) -> Optional[Path]:
    # Catalog membership first: the mirror on disk outlives a delisting (a book
    # held back for content keeps its files in raw/), and without this check
    # its cover would still be served to anyone guessing the gid.
    if not available():
        return None
    with _connect() as con:
        if not con.execute(
                f"SELECT 1 FROM books WHERE gid=? AND {_visible(con, need_text=False)}",
                (gid,)).fetchone():
            return None
    p = RAW_DIR / str(gid) / f"pg{gid}.cover.medium.jpg"
    return p if p.exists() else None


def _text_path(gid: int) -> Optional[Path]:
    p = RAW_DIR / str(gid) / f"pg{gid}.txt"
    return p if p.exists() else None


# --------------------------------------------------------------- PG stripping

# Header end / footer start markers, oldest formats included. The modern form is
# "*** START OF THE PROJECT GUTENBERG EBOOK TITLE ***".
_START_RES = [
    re.compile(r"^\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.I | re.M),
    re.compile(r"^\*END[* ]*THE SMALL PRINT!.*$", re.I | re.M),
]
_END_RES = [
    re.compile(r"^\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.I | re.M),
    re.compile(r"^End of (?:the )?Project Gutenberg.*$", re.I | re.M),
]
# Production credits that survive inside the body region.
_CREDITS_RE = re.compile(
    r"^(?:Produced by|E-text prepared by|Transcribed from|This etext was prepared by)"
    r"[^\n]*(?:\n[^\n]+)*", re.I)
# Stage directions for pictures we can't show — silently drop from the reading.
_ILLUSTRATION_RE = re.compile(r"\[Illustration[^\]]*\]")


def strip_gutenberg(text: str) -> str:
    """Cut the PG header/footer + license and de-brand the body text."""
    text = text.lstrip("﻿")
    start = 0
    for rx in _START_RES:
        m = rx.search(text)
        if m:
            start = m.end()
            break
    end = len(text)
    for rx in _END_RES:
        m = rx.search(text, start)
        if m:
            end = m.start()
            break
    body = text[start:end].strip("\n")
    body = _CREDITS_RE.sub("", body.lstrip("\n"), count=1)
    body = _ILLUSTRATION_RE.sub("", body)
    return body.strip()


# ------------------------------------------------------------ add to library

def existing_doc(gid: int) -> Optional[int]:
    """The already-imported copy of this book, if any (re-adds reopen it)."""
    with db.connect() as c:
        row = c.execute(
            "SELECT id FROM documents WHERE kind='book' AND source_url=?",
            (f"pg:{gid}",)).fetchone()
    return row["id"] if row else None


def add_to_library(gid: int) -> Optional[dict]:
    """Import a catalog book into the library as a kind='book' document.
    Returns the doc summary, or None when the book/text doesn't exist."""
    meta = book(gid)
    path = _text_path(gid)
    if not meta or not path:
        return None
    dup = existing_doc(gid)
    if dup:
        return {"id": dup, "title": meta["title"], "existing": True}

    raw = path.read_text(encoding="utf-8", errors="replace")
    body = strip_gutenberg(raw)
    if len(body) < 200:  # a stripped file that small is a bad mirror artifact
        return None
    result = chunk_plain_text(meta["title"], body, strip_printed_toc=True)
    if not result.chunks:
        return None
    doc_id = db.create_document(
        title=meta["title"], filename="", num_pages=1, pages_index=result.pages,
        chunks=result.chunks, toc=result.toc, kind="book", source_url=f"pg:{gid}",
        current_idx=result.start_idx,
        # A catalog book states its own language, so there's nothing to detect;
        # the voice that reads it is resolved from it like any other document.
        lang=meta["language"] if meta["language"] in lang.LANG_LABEL else None,
    )
    return {"id": doc_id, "title": meta["title"], "num_sentences": len(result.chunks),
            "existing": False}
