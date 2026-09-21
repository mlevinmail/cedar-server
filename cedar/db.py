"""SQLite persistence: the library of documents, their sentences, outline,
media, bookmarks, highlights and folders.

One owner, so nothing here is scoped by user. Schema changes are numbered
migrations applied in order on startup (``PRAGMA user_version`` tracks how far
a database has come), so upgrading the server is always just restarting it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from .config import DB_PATH


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------- schema

_MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE documents (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        filename        TEXT NOT NULL DEFAULT '',   -- stored PDF under uploads/, '' otherwise
        kind            TEXT NOT NULL DEFAULT 'pdf', -- pdf | epub | url | text | book
        source_url      TEXT,                        -- article URL, or "pg:<gid>" for a store book
        num_pages       INTEGER NOT NULL,
        num_sentences   INTEGER NOT NULL,
        pages_json      TEXT NOT NULL DEFAULT '[]',
        current_idx     INTEGER NOT NULL DEFAULT 0,
        content_rev     INTEGER NOT NULL DEFAULT 1,  -- bumped when the text changes (client cache key)
        folder_id       INTEGER,                     -- NULL = library root
        lang            TEXT,                        -- detected at import; NULL = couldn't place it
        voice_override  TEXT,                        -- a deliberate cross-language pin; NULL = follow the language
        generated_voice TEXT,                        -- voice a full offline copy was synthesized at
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    );
    CREATE INDEX documents_folder ON documents(folder_id);

    CREATE TABLE sentences (
        doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        idx     INTEGER NOT NULL,
        page    INTEGER NOT NULL,
        para    INTEGER NOT NULL,
        text    TEXT NOT NULL,
        heading INTEGER NOT NULL DEFAULT 0,  -- 0 = body; 1..3 = heading level
        PRIMARY KEY (doc_id, idx)
    );

    -- Inline images for article imports. Each anchors before a sentence; the
    -- bytes are cached under media/<doc_id>/.
    CREATE TABLE media (
        doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        ord        INTEGER NOT NULL,
        anchor_idx INTEGER NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'image',
        src_url    TEXT NOT NULL DEFAULT '',
        filename   TEXT,
        alt        TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (doc_id, ord)
    );

    CREATE TABLE toc (
        doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        ord          INTEGER NOT NULL,
        level        INTEGER NOT NULL,
        title        TEXT NOT NULL,
        page         INTEGER NOT NULL,
        sentence_idx INTEGER NOT NULL,
        PRIMARY KEY (doc_id, ord)
    );

    -- A marked spot (sentence) within a document; one per sentence index.
    CREATE TABLE bookmarks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE (doc_id, idx)
    );
    CREATE INDEX bookmarks_doc ON bookmarks(doc_id);

    -- A marked span of characters inside one sentence. The span is the
    -- identity, so marking the same word twice only restyles the row.
    CREATE TABLE highlights (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        cs         INTEGER NOT NULL,
        ce         INTEGER NOT NULL,
        text       TEXT NOT NULL DEFAULT '',
        color      TEXT NOT NULL DEFAULT 'yellow',
        created_at REAL NOT NULL,
        UNIQUE (doc_id, idx, cs, ce)
    );
    CREATE INDEX highlights_doc ON highlights(doc_id);

    -- Library folders: one flat level.
    CREATE TABLE folders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
]


def init_db() -> None:
    with connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        for n, script in enumerate(_MIGRATIONS, start=1):
            if n > version:
                c.executescript(script)
                c.execute(f"PRAGMA user_version = {n}")


def schema_version() -> int:
    with connect() as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


# ------------------------------------------------------------------ documents

def create_document(title, filename, num_pages, pages_index, chunks, toc,
                    kind="pdf", source_url=None, media=None, *,
                    voice_override: str | None = None,
                    lang: str | None = None, current_idx: int = 0) -> int:
    # current_idx > 0 opens a fresh document past stripped front matter (book
    # imports skip the printed table of contents); it's an ordinary resume
    # position, so the reader can still seek back to the very top.
    now = time.time()
    with connect() as c:
        cur = c.execute(
            """INSERT INTO documents
               (title, filename, num_pages, num_sentences, pages_json,
                voice_override, lang, current_idx, kind, source_url, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, filename, num_pages, len(chunks), json.dumps(pages_index),
             voice_override, lang,
             max(0, min(current_idx, max(0, len(chunks) - 1))),
             kind, source_url, now, now),
        )
        doc_id = cur.lastrowid
        c.executemany(
            "INSERT INTO sentences (doc_id, idx, page, para, text, heading) VALUES (?,?,?,?,?,?)",
            [(doc_id, ch.idx, ch.page, ch.para, ch.text, getattr(ch, "heading", 0)) for ch in chunks],
        )
        c.executemany(
            "INSERT INTO toc (doc_id, ord, level, title, page, sentence_idx) VALUES (?,?,?,?,?,?)",
            [(doc_id, i, t.level, t.title, t.page, t.sentence_idx) for i, t in enumerate(toc)],
        )
        if media:
            c.executemany(
                "INSERT INTO media (doc_id, ord, anchor_idx, kind, src_url, alt) VALUES (?,?,?,?,?,?)",
                [(doc_id, m.ord, m.anchor_idx, "image", m.src_url, m.alt) for m in media],
            )
    return doc_id


def list_documents() -> List[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT id, title, num_pages, num_sentences, current_idx, voice_override, lang,
                      kind, source_url, generated_voice, content_rev, folder_id,
                      created_at, updated_at,
                      EXISTS(SELECT 1 FROM bookmarks b WHERE b.doc_id = documents.id) AS bookmarked,
                      (SELECT m.ord FROM media m WHERE m.doc_id = documents.id
                         AND m.filename IS NOT NULL ORDER BY m.ord LIMIT 1) AS thumb_ord
               FROM documents ORDER BY updated_at DESC""",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = (r["current_idx"] / r["num_sentences"]) if r["num_sentences"] else 0.0
        out.append(d)
    return out


def document_exists(doc_id: int) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone() is not None


def get_document(doc_id: int) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        doc = dict(row)
        doc["pages"] = json.loads(doc.pop("pages_json") or "[]")
        toc = c.execute(
            "SELECT level, title, page, sentence_idx FROM toc WHERE doc_id=? ORDER BY ord",
            (doc_id,),
        ).fetchall()
        doc["toc"] = [dict(t) for t in toc]
        media = c.execute(
            """SELECT ord, anchor_idx, kind, alt FROM media
               WHERE doc_id=? AND filename IS NOT NULL ORDER BY ord""",
            (doc_id,),
        ).fetchall()
        doc["media"] = [dict(m) for m in media]
    return doc


def update_document(doc_id: int, *, title: str | None = None,
                    set_folder: bool = False, folder_id: int | None = None) -> bool:
    """Rename and/or move a document. ``set_folder`` distinguishes "move to the
    root" (folder_id None) from "leave the folder alone" (set_folder False)."""
    sets, args = [], []
    if title is not None:
        sets.append("title=?")
        args.append(title)
    if set_folder:
        sets.append("folder_id=?")
        args.append(folder_id)
    if not sets:
        return True
    with connect() as c:
        cur = c.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id=?", (*args, doc_id))
    return cur.rowcount > 0


def delete_document(doc_id: int) -> Optional[str]:
    """Delete a document; return its stored filename ('' if none) so the caller
    can remove the file. None = not found."""
    with connect() as c:
        row = c.execute("SELECT filename FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    return row["filename"]


def get_document_kind(doc_id: int) -> Optional[str]:
    with connect() as c:
        row = c.execute("SELECT kind FROM documents WHERE id=?", (doc_id,)).fetchone()
    return row["kind"] if row else None


def get_document_filename(doc_id: int) -> Optional[str]:
    """The stored PDF's filename, or None for anything that isn't a PDF."""
    with connect() as c:
        row = c.execute("SELECT filename, kind FROM documents WHERE id=?", (doc_id,)).fetchone()
    return row["filename"] if row and row["kind"] == "pdf" and row["filename"] else None


def set_document_lang(doc_id: int, lang: str | None) -> None:
    with connect() as c:
        c.execute("UPDATE documents SET lang=? WHERE id=?", (lang, doc_id))


def set_voice_override(doc_id: int, voice: str | None) -> None:
    """Pin one document to a voice, or (None) let its language decide again."""
    with connect() as c:
        c.execute("UPDATE documents SET voice_override=? WHERE id=?", (voice, doc_id))


def set_generated(doc_id: int, voice: str) -> None:
    with connect() as c:
        c.execute("UPDATE documents SET generated_voice=? WHERE id=?", (voice, doc_id))


def save_progress(doc_id: int, idx: int, voice: str | None = None) -> None:
    """Record the reading position, and — only for a document that already pins
    its own voice — the voice it's being read in. Everywhere else voice is a
    per-language preference, so a pick made inside the reader must not quietly
    re-pin the document."""
    with connect() as c:
        c.execute(
            """UPDATE documents SET current_idx=?, updated_at=?,
                      voice_override = CASE WHEN voice_override IS NULL THEN NULL
                                            ELSE COALESCE(?, voice_override) END
               WHERE id=?""",
            (idx, time.time(), voice, doc_id),
        )


def bump_content_rev(doc_id: int) -> None:
    """Sentences changed (rechunk): invalidate client-side text caches."""
    with connect() as c:
        c.execute("UPDATE documents SET content_rev = content_rev + 1 WHERE id=?", (doc_id,))


# ------------------------------------------------------------------ sentences

def get_sentences(doc_id: int, start: int, limit: int) -> List[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT idx, page, para, text, heading FROM sentences
               WHERE doc_id=? AND idx>=? ORDER BY idx LIMIT ?""",
            (doc_id, start, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_sentence_text(doc_id: int, idx: int) -> Optional[str]:
    with connect() as c:
        row = c.execute("SELECT text FROM sentences WHERE doc_id=? AND idx=?",
                        (doc_id, idx)).fetchone()
    return row["text"] if row else None


def get_sentence(doc_id: int, idx: int) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT idx, page, para, text FROM sentences WHERE doc_id=? AND idx=?",
                        (doc_id, idx)).fetchone()
    return dict(row) if row else None


def get_sentences_on_page(doc_id: int, page: int) -> List[tuple]:
    with connect() as c:
        rows = c.execute(
            "SELECT idx, text FROM sentences WHERE doc_id=? AND page=? ORDER BY idx",
            (doc_id, page),
        ).fetchall()
    return [(r["idx"], r["text"]) for r in rows]


# ---------------------------------------------------------------------- media

def list_media(doc_id: int, ready_only: bool = True) -> List[dict]:
    """Inline media for a document, in order. ``ready_only`` returns just the
    images whose bytes are already cached on disk."""
    q = "SELECT ord, anchor_idx, kind, alt, filename FROM media WHERE doc_id=?"
    if ready_only:
        q += " AND filename IS NOT NULL"
    q += " ORDER BY ord"
    with connect() as c:
        rows = c.execute(q, (doc_id,)).fetchall()
    return [dict(r) for r in rows]


def get_media(doc_id: int, ord: int) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT ord, src_url, filename FROM media WHERE doc_id=? AND ord=?",
                        (doc_id, ord)).fetchone()
    return dict(row) if row else None


def set_media_file(doc_id: int, ord: int, filename: str) -> None:
    with connect() as c:
        c.execute("UPDATE media SET filename=? WHERE doc_id=? AND ord=?", (filename, doc_id, ord))


# ------------------------------------------------------------------ bookmarks

def add_bookmark(doc_id: int, idx: int, note: str = "") -> Optional[dict]:
    """Mark a reading spot. Idempotent per (doc_id, idx). Returns the bookmark
    (with its sentence snippet + page), or None if the document doesn't exist."""
    with connect() as c:
        if not c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            return None
        c.execute(
            """INSERT INTO bookmarks (doc_id, idx, note, created_at) VALUES (?,?,?,?)
               ON CONFLICT(doc_id, idx) DO UPDATE SET note=excluded.note""",
            (doc_id, idx, note or "", time.time()),
        )
    return get_bookmark(doc_id, idx)


_BOOKMARK_SELECT = """SELECT b.idx, b.note, b.created_at, s.text AS snippet, s.page
                      FROM bookmarks b
                      LEFT JOIN sentences s ON s.doc_id = b.doc_id AND s.idx = b.idx"""


def get_bookmark(doc_id: int, idx: int) -> Optional[dict]:
    with connect() as c:
        row = c.execute(_BOOKMARK_SELECT + " WHERE b.doc_id=? AND b.idx=?",
                        (doc_id, idx)).fetchone()
    return dict(row) if row else None


def remove_bookmark(doc_id: int, idx: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM bookmarks WHERE doc_id=? AND idx=?", (doc_id, idx))
    return cur.rowcount > 0


def list_bookmarks(doc_id: int) -> List[dict]:
    with connect() as c:
        rows = c.execute(_BOOKMARK_SELECT + " WHERE b.doc_id=? ORDER BY b.idx",
                         (doc_id,)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- highlights

_HIGHLIGHT_SELECT = """SELECT h.id, h.idx, h.cs, h.ce, h.text, h.color, h.created_at,
                              COALESCE(s.page, 1) AS page
                       FROM highlights h
                       LEFT JOIN sentences s ON s.doc_id = h.doc_id AND s.idx = h.idx"""


def add_highlight(doc_id: int, idx: int, cs: int, ce: int, text: str = "",
                  color: str = "yellow") -> Optional[dict]:
    """Mark characters [cs, ce) of sentence [idx]. Idempotent per exact span —
    highlighting the same word again just re-colours it."""
    with connect() as c:
        if not c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            return None
        c.execute(
            """INSERT INTO highlights (doc_id, idx, cs, ce, text, color, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(doc_id, idx, cs, ce) DO UPDATE SET color=excluded.color""",
            (doc_id, idx, cs, ce, text or "", color or "yellow", time.time()),
        )
        row = c.execute(_HIGHLIGHT_SELECT + " WHERE h.doc_id=? AND h.idx=? AND h.cs=? AND h.ce=?",
                        (doc_id, idx, cs, ce)).fetchone()
    return dict(row) if row else None


def remove_highlight(doc_id: int, hid: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM highlights WHERE doc_id=? AND id=?", (doc_id, hid))
    return cur.rowcount > 0


def list_highlights(doc_id: int) -> List[dict]:
    with connect() as c:
        rows = c.execute(_HIGHLIGHT_SELECT + " WHERE h.doc_id=? ORDER BY h.idx, h.cs",
                         (doc_id,)).fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------- folders

def list_folders() -> List[dict]:
    """Library folders with live document counts, newest first."""
    with connect() as c:
        rows = c.execute(
            """SELECT f.id, f.name, f.created_at, f.updated_at,
                      (SELECT COUNT(*) FROM documents d WHERE d.folder_id = f.id) AS count
               FROM folders f ORDER BY f.created_at DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def create_folder(name: str) -> dict:
    now = time.time()
    with connect() as c:
        cur = c.execute("INSERT INTO folders (name, created_at, updated_at) VALUES (?,?,?)",
                        (name, now, now))
    return {"id": cur.lastrowid, "name": name, "count": 0, "created_at": now, "updated_at": now}


def folder_exists(folder_id: int) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone() is not None


def rename_folder(folder_id: int, name: str) -> bool:
    with connect() as c:
        cur = c.execute("UPDATE folders SET name=?, updated_at=? WHERE id=?",
                        (name, time.time(), folder_id))
    return cur.rowcount > 0


def delete_folder(folder_id: int) -> bool:
    """Remove a folder; its documents return to the library root, untouched."""
    with connect() as c:
        cur = c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        if cur.rowcount:
            c.execute("UPDATE documents SET folder_id=NULL WHERE folder_id=?", (folder_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------- stats

def counts() -> dict:
    with connect() as c:
        docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        folders = c.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    return {"documents": docs, "folders": folders}
