"""One-off maintenance: re-chunk url/book documents with the current chunker.

Re-sources each url/book document (store books — source_url "pg:{gid}" — from the
local corpus, everything else over HTTP), re-chunks it with the current splitter,
and replaces its sentences in place, preserving the reader's position by character
offset. Book re-chunks also strip the printed table of contents and move anyone
still in the front matter up to where the body begins (chunker.start_idx).

PDFs and pasted text are left alone (their source isn't re-fetchable, and the PDF
path was never affected).

Usage (inside the container):
    python -m backend.rechunk            # dry run: report old->new chunk counts
    python -m backend.rechunk --apply    # back up the DB, then rewrite in place
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import time

import trafilatura

from .chunker import chunk_plain_text
from .config import DB_PATH
from .safefetch import UnsafeUrlError, safe_fetch

# Kept for pre-existing kind='book' documents (imported back when the app had a
# Gutenberg catalog — the feature was removed 2026-07-11, the documents remain).
_PG_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
_PG_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
_PG_PRODUCED = re.compile(r"^\s*Produced by .*?$", re.I | re.M)


def strip_gutenberg(text: str) -> str:
    """Drop the Project Gutenberg license header/footer, keeping the work itself."""
    s = _PG_START.search(text)
    if s:
        text = text[s.end():]
    e = _PG_END.search(text)
    if e:
        text = text[: e.start()]
    text = _PG_PRODUCED.sub("", text, count=1)
    return text.strip()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _backup_db() -> str:
    dest = f"{DB_PATH}.bak-{int(time.time())}"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)  # consistent snapshot incl. any WAL frames
    src.close()
    dst.close()
    return dest


_PG_LOCAL = re.compile(r"^pg:(\d+)$")


async def _refetch_text(kind: str, url: str) -> str:
    m = _PG_LOCAL.match(url or "")
    if kind == "book" and m:
        # A store book: its text lives in the local corpus, not behind a URL.
        from . import catalog
        path = catalog._text_path(int(m.group(1)))
        if not path:
            return ""
        return catalog.strip_gutenberg(path.read_text(encoding="utf-8", errors="replace"))
    _, resp = await safe_fetch(url)
    if kind == "book":
        return strip_gutenberg(resp.text)
    text = trafilatura.extract(
        resp.text, include_comments=False, include_tables=False, favor_recall=True, url=url
    )
    return text or ""


def _char_offset(texts: list[str], idx: int) -> int:
    """Approx chars of readable text up to (not including) sentence `idx`."""
    return sum(len(t) + 1 for t in texts[:idx])


def _map_progress(old_texts: list[str], old_idx: int, new_chunks: list) -> int:
    """Find the new chunk index covering the same character position as old_idx."""
    if old_idx <= 0:
        return 0
    target = _char_offset(old_texts, old_idx)
    acc = 0
    for i, ch in enumerate(new_chunks):
        if acc >= target:
            return i
        acc += len(ch.text) + 1
    return max(0, len(new_chunks) - 1)


async def _rechunk_doc(conn: sqlite3.Connection, row: sqlite3.Row, apply: bool) -> None:
    doc_id, kind, url, title = row["id"], row["kind"], row["source_url"], row["title"]
    old_texts = [r["text"] for r in conn.execute(
        "SELECT text FROM sentences WHERE doc_id=? ORDER BY idx", (doc_id,))]
    old_idx = row["current_idx"] or 0

    text = await _refetch_text(kind, url)
    if len(text) < 100:
        print(f"  [{doc_id}] {title!r}: refetch too short ({len(text)} chars) — skipped")
        return
    result = chunk_plain_text(title, text, strip_printed_toc=(kind == "book"))
    if not result.chunks:
        print(f"  [{doc_id}] {title!r}: produced no chunks — skipped")
        return
    new_idx = _map_progress(old_texts, old_idx, result.chunks)
    # Anyone who hadn't listened past the front matter starts at the body now.
    new_idx = max(new_idx, result.start_idx)

    verb = "rewrote" if apply else "would rewrite"
    print(f"  [{doc_id}] {title!r}: {len(old_texts)} -> {len(result.chunks)} chunks, "
          f"idx {old_idx} -> {new_idx}  ({verb})")

    if not apply:
        return
    now = time.time()
    conn.execute("DELETE FROM sentences WHERE doc_id=?", (doc_id,))
    conn.executemany(
        "INSERT INTO sentences (doc_id, idx, page, para, text, heading) VALUES (?,?,?,?,?,?)",
        [(doc_id, ch.idx, ch.page, ch.para, ch.text, getattr(ch, "heading", 0)) for ch in result.chunks])
    conn.execute("DELETE FROM toc WHERE doc_id=?", (doc_id,))
    conn.executemany(
        "INSERT INTO toc (doc_id, ord, level, title, page, sentence_idx) VALUES (?,?,?,?,?,?)",
        [(doc_id, i, t.level, t.title, t.page, t.sentence_idx) for i, t in enumerate(result.toc)])
    conn.execute(
        """UPDATE documents SET num_sentences=?, num_pages=?, pages_json=?, current_idx=?,
           updated_at=?, content_rev = content_rev + 1 WHERE id=?""",
        (len(result.chunks), result.num_pages, json.dumps(result.pages), new_idx, now, doc_id))
    conn.commit()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--kind", choices=["url", "book", "all"], default="all",
                    help="restrict to one document kind (news URLs can degrade on refetch)")
    args = ap.parse_args()

    if args.apply:
        print(f"Backed up DB -> {_backup_db()}")

    kinds = ("url", "book") if args.kind == "all" else (args.kind,)
    conn = _connect()
    rows = conn.execute(
        f"SELECT * FROM documents WHERE kind IN ({','.join('?' * len(kinds))}) "
        "AND source_url IS NOT NULL ORDER BY id",
        kinds,
    ).fetchall()
    print(f"{'Applying to' if args.apply else 'Dry run over'} {len(rows)} url/book documents:")
    for row in rows:
        try:
            await _rechunk_doc(conn, row, args.apply)
        except UnsafeUrlError as e:
            print(f"  [{row['id']}] {row['title']!r}: unsafe url — {e}")
        except Exception as e:  # noqa: BLE001 — report and continue per doc
            print(f"  [{row['id']}] {row['title']!r}: FAILED — {type(e).__name__}: {e}")
    conn.close()
    print("Done." + ("" if args.apply else "  (no changes written — re-run with --apply)"))


if __name__ == "__main__":
    asyncio.run(main())
