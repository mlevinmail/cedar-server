"""The library: import (PDF/EPUB/URL/text), reading, TTS, progress, bookmarks
and highlights.

Speed comes from the synced preferences and the reading voice is resolved per
language (see `_with_voice`); neither is stored on the document.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import db, lang, pdflayout, settings, tts
from ..chunker import chunk_article, chunk_plain_text, extract_epub, extract_pdf
from ..config import MAX_UPLOAD_BYTES, MEDIA_DIR, UPLOAD_DIR
from ..safefetch import TooLargeError, UnsafeUrlError, safe_fetch

router = APIRouter(prefix="/api")

log = logging.getLogger("cedar.import")

# Image content-types we cache for article imports -> stored file extension.
_IMG_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}


def _log_rejected(source: str, exc: HTTPException) -> None:
    """Record what an import was and why it bounced. Rejections are the
    interesting case, so only they are logged; a successful import is already
    visible as a row in `documents`."""
    log.info("import rejected: %s -> %s: %s", source, exc.status_code, exc.detail)


async def _cache_media(doc_id: int, media: list) -> None:
    """Fetch each article image (SSRF-safe) and cache it under MEDIA_DIR/<doc_id>.
    Best-effort and bounded: failures just leave that image unshown."""
    if not media:
        return
    dest = MEDIA_DIR / str(doc_id)
    dest.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(6)

    async def one(m) -> None:
        async with sem:
            try:
                _, resp = await safe_fetch(m.src_url)
            except Exception:
                return
            ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            ext = _IMG_EXT.get(ctype)
            data = resp.content or b""
            if not ext or not (2048 <= len(data) <= 12_000_000):
                return  # not a real image, a tracking pixel, or implausibly large
            try:
                (dest / f"{m.ord}.{ext}").write_bytes(data)
                db.set_media_file(doc_id, m.ord, f"{m.ord}.{ext}")
            except Exception:
                return

    try:
        await asyncio.wait_for(asyncio.gather(*(one(m) for m in media[:24])), timeout=25)
    except asyncio.TimeoutError:
        pass  # whatever finished is cached; the rest are simply skipped


def _title_from_url(url: str) -> str:
    """Derive a readable fallback title from a URL that points at a PDF."""
    stem = Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ").strip()
    return stem or (urlparse(url).hostname or "Untitled")


def _with_voice(docs: list[dict]) -> list[dict]:
    """Fill in each document's reading voice and speed.

    Speed is one preference for everything. Voice is per language (lang.py):
    the document says what language it is, the preferences say which voice
    reads that language, and the answer is worked out here on every read
    rather than stored — so changing your Spanish voice changes every Spanish
    document, including the ones imported months ago.

    `voice_locked` means a pick in the reader stays on this document instead
    of becoming a preference; that is true only for a deliberate
    cross-language override.
    """
    chosen = settings.chosen_voices()
    main, speed = settings.reading_defaults()
    for d in docs:
        override = d.pop("voice_override", None)
        d["voice"] = lang.resolve(d.get("lang"), chosen, main, override)
        d["speed"] = speed
        d["voice_locked"] = override is not None
    return docs


def _summary(doc_id: int, result, num_pages: int) -> dict:
    return {"id": doc_id, "title": result.title, "num_pages": num_pages,
            "num_sentences": len(result.chunks)}


def _ingest_pdf_bytes(data: bytes, fallback_title: str, *, source_url: str | None = None) -> dict:
    """Persist PDF bytes, extract text + layout, and create a document.

    Shared by the multipart upload route and the "the link is actually a PDF"
    branch of from_url, so a PDF imported either way gets the original-PDF view.
    """
    if not data:
        raise HTTPException(400, "Empty file.")

    stored = f"{uuid.uuid4().hex}.pdf"
    dest = UPLOAD_DIR / stored
    dest.write_bytes(data)

    try:
        result = extract_pdf(str(dest), fallback_title)
    except Exception as e:  # corrupt / unreadable PDF
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read PDF: {e}")

    if not result.chunks:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            400, "No selectable text found. This looks like a scanned PDF (needs OCR).")

    doc_id = db.create_document(
        title=result.title, filename=stored, num_pages=result.num_pages,
        pages_index=result.pages, chunks=result.chunks, toc=result.toc,
        source_url=source_url, lang=lang.detect_chunks(result.chunks),
    )
    return _summary(doc_id, result, result.num_pages)


def _ingest_epub_bytes(data: bytes, fallback_title: str) -> dict:
    """Parse EPUB bytes into a formatted document (headings + sentences + TOC).
    Nothing is stored on disk — an EPUB becomes plain reading text."""
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        result = extract_epub(io.BytesIO(data), fallback_title)
    except Exception as e:  # corrupt / not-an-epub
        raise HTTPException(400, f"Could not read EPUB: {e}")
    if not result.chunks:
        raise HTTPException(400, "No readable text found in that EPUB.")
    doc_id = db.create_document(
        title=result.title, filename="", num_pages=1, pages_index=result.pages,
        chunks=result.chunks, toc=result.toc, kind="epub",
        lang=lang.detect_chunks(result.chunks),
    )
    return _summary(doc_id, result, 1)


def _ingest_text_bytes(data: bytes, fallback_title: str, *, markdown: bool) -> dict:
    """A .txt/.md upload becomes a plain text document — same pipeline as
    pasted text; Markdown keeps its headings via the article chunker."""
    if not data:
        raise HTTPException(400, "Empty file.")
    text = data.decode("utf-8", errors="replace").lstrip("﻿")
    result = (chunk_article(fallback_title, text) if markdown
              else chunk_plain_text(fallback_title, text))
    if not result.chunks:
        raise HTTPException(400, "No readable text found in that file.")
    doc_id = db.create_document(
        title=result.title, filename="", num_pages=1, pages_index=result.pages,
        chunks=result.chunks, toc=result.toc, kind="text",
        lang=lang.detect_chunks(result.chunks),
    )
    return _summary(doc_id, result, 1)


def _exists_or_404(doc_id: int) -> None:
    if not db.document_exists(doc_id):
        raise HTTPException(404, "Document not found.")


# ------------------------------------------------------------------- library

@router.get("/documents")
def list_documents():
    return {"documents": _with_voice(db.list_documents())}


@router.post("/documents")
async def upload(file: UploadFile = File(...)):
    name = file.filename or "document"
    low = name.lower()
    # Bounded read: without a cap an oversized upload is pulled straight into RAM
    # (and, for PDFs, written to UPLOAD_DIR) with nothing to stop it.
    data = await file.read(MAX_UPLOAD_BYTES + 1)

    def _title(ext: str) -> str:
        return re.sub(ext + r"$", "", name, flags=re.I).replace("_", " ").strip() or "Untitled"

    try:
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"That file is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        if low.endswith(".epub"):
            return await run_in_threadpool(_ingest_epub_bytes, data, _title(r"\.epub"))
        if low.endswith(".pdf"):
            return await run_in_threadpool(_ingest_pdf_bytes, data, _title(r"\.pdf"))
        if low.endswith((".md", ".markdown")):
            return await run_in_threadpool(
                _ingest_text_bytes, data, _title(r"\.(md|markdown)"), markdown=True)
        if low.endswith(".txt"):
            return await run_in_threadpool(
                _ingest_text_bytes, data, _title(r"\.txt"), markdown=False)
        raise HTTPException(400, "Only PDF, EPUB, TXT, and Markdown files are supported.")
    except HTTPException as e:
        _log_rejected(f"file={name!r} bytes={len(data)}", e)
        raise


class UrlIn(BaseModel):
    url: str


@router.post("/documents/from_url")
async def from_url(body: UrlIn):
    try:
        return await _import_url(body)
    except HTTPException as e:
        _log_rejected(f"url={(body.url or '').strip()!r}", e)
        raise


async def _import_url(body: UrlIn) -> dict:
    import trafilatura

    # Make sure this is actually a web link before we try to fetch it.
    raw = (body.url or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or "." not in (parsed.netloc or ""):
        raise HTTPException(400, "That doesn't look like a web link. Use a URL like https://example.com/article.")

    try:
        url, resp = await safe_fetch(raw)
    except UnsafeUrlError as e:
        raise HTTPException(400, str(e))
    except TooLargeError as e:
        raise HTTPException(413, f"Could not import that link: {e}")
    except Exception as e:
        raise HTTPException(400, f"Could not fetch the page: {e}")

    # The link might point straight at a PDF rather than an HTML article — detect
    # that (by content-type or the %PDF- magic bytes) and run the real PDF
    # pipeline so the original-PDF view works just like an uploaded file.
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype == "application/pdf" or resp.content[:5] == b"%PDF-":
        return await run_in_threadpool(_ingest_pdf_bytes, resp.content,
                                       _title_from_url(url), source_url=url)

    # trafilatura parsing, chunking and the bulk INSERTs are all synchronous and
    # CPU-bound; run them off the event loop so one import can't stall the server.
    def _extract_and_store():
        html = resp.text

        title = url
        try:
            meta = trafilatura.extract_metadata(html)
            if meta and meta.title:
                title = meta.title.strip()
        except Exception:
            pass

        # Rich pass first: keep headings, paragraph structure, and inline images.
        md = trafilatura.extract(
            html, output_format="markdown", include_images=True, include_formatting=True,
            include_links=True, include_comments=False, include_tables=False,
            favor_recall=True, url=url, deduplicate=True,
        )
        result = chunk_article(title, md or "", base_url=url)
        if not result.chunks:
            # Fall back to a plain-text extraction if the structured pass came up empty.
            text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                       favor_recall=True, url=url)
            if not text or len(text.strip()) < 40:
                raise HTTPException(400, "Couldn't find readable article text on that page.")
            result = chunk_plain_text(title, text)
            if not result.chunks:
                raise HTTPException(400, "No readable text found on that page.")

        doc_id = db.create_document(
            title=result.title, filename="", num_pages=1, pages_index=result.pages,
            chunks=result.chunks, toc=result.toc, kind="url", source_url=url,
            media=result.media, lang=lang.detect_chunks(result.chunks),
        )
        return doc_id, result

    doc_id, result = await run_in_threadpool(_extract_and_store)
    await _cache_media(doc_id, result.media)
    return _summary(doc_id, result, 1)


class TextIn(BaseModel):
    text: str
    title: str | None = None


@router.post("/documents/from_text")
def from_text(body: TextIn):
    text = (body.text or "").strip()
    if len(text) < 2:
        err = HTTPException(400, "Please paste some text to import.")
        _log_rejected(f"text chars={len(text)}", err)
        raise err

    title = (body.title or "").strip()
    if not title:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        title = (first[:80] + "…") if len(first) > 80 else (first or "Pasted text")

    result = chunk_plain_text(title, text)
    if not result.chunks:
        err = HTTPException(400, "No readable text found.")
        _log_rejected(f"text chars={len(text)}", err)
        raise err

    doc_id = db.create_document(
        title=result.title, filename="", num_pages=1, pages_index=result.pages,
        chunks=result.chunks, toc=result.toc, kind="text",
        lang=lang.detect_chunks(result.chunks),
    )
    return _summary(doc_id, result, 1)


# ------------------------------------------------------------------- reading

@router.get("/documents/{doc_id}")
def get_document(doc_id: int):
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found.")
    return _with_voice([doc])[0]


@router.get("/documents/{doc_id}/sentences")
def get_sentences(doc_id: int, start: int = Query(0, ge=0),
                  limit: int = Query(200, ge=1, le=2000)):
    _exists_or_404(doc_id)
    return {"sentences": db.get_sentences(doc_id, start, limit)}


def _sentence_for_tts(doc_id: int, idx: int) -> str:
    """Existence + sentence lookup for /tts. A couple of blocking SQLite round
    trips, so it runs in a worker thread (see `synth`) rather than on the
    event loop, where it would serialize the client's prefetch."""
    _exists_or_404(doc_id)
    text = db.get_sentence_text(doc_id, idx)
    if text is None:
        raise HTTPException(404, "Sentence not found.")
    return text


@router.get("/documents/{doc_id}/tts/{idx}")
async def synth(doc_id: int, idx: int, voice: str = Query(...), speed: float = Query(1.0)):
    if not tts.valid_voice_id(voice):
        raise HTTPException(400, "Unknown voice.")
    text = await asyncio.to_thread(_sentence_for_tts, doc_id, idx)
    speed = max(0.5, min(2.0, speed))
    try:
        return await tts.synthesize(text, voice, speed)
    except Exception as e:
        raise HTTPException(502, f"TTS service error: {e}")


@router.get("/documents/{doc_id}/pdf")
def pdf(doc_id: int):
    _exists_or_404(doc_id)
    filename = db.get_document_filename(doc_id)
    if not filename:
        raise HTTPException(404, "No PDF for this document.")
    path = UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404, "PDF file missing.")
    return FileResponse(path, media_type="application/pdf")


@router.get("/documents/{doc_id}/thumb")
def thumb(doc_id: int):
    """A PDF's cover: its first page rendered to a small JPEG, once, cached in
    the doc's media dir (so it's cleaned up with the document). The library
    list uses it as the row thumbnail; non-PDF docs simply 404."""
    _exists_or_404(doc_id)
    filename = db.get_document_filename(doc_id)
    if not filename:
        raise HTTPException(404, "No cover for this document.")
    path = MEDIA_DIR / str(doc_id) / "cover.jpg"
    if not path.exists():
        src = UPLOAD_DIR / filename
        if not src.exists():
            raise HTTPException(404, "PDF file missing.")
        try:
            import fitz
            with fitz.open(str(src)) as pdf_:
                page = pdf_[0]
                zoom = 320 / max(page.rect.width, 1)  # ~320px wide, plenty for a list row
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                path.parent.mkdir(parents=True, exist_ok=True)
                pix.save(str(path), jpg_quality=82)
        except Exception:
            path.unlink(missing_ok=True)  # never cache a half-written cover
            raise HTTPException(404, "Could not render a cover for this PDF.")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/documents/{doc_id}/media/{ord}")
def media(doc_id: int, ord: int):
    _exists_or_404(doc_id)
    m = db.get_media(doc_id, ord)
    if not m or not m["filename"]:
        raise HTTPException(404, "Media not found.")
    path = MEDIA_DIR / str(doc_id) / m["filename"]
    if not path.exists():
        raise HTTPException(404, "Media file missing.")
    return FileResponse(path)


@router.get("/documents/{doc_id}/rects/{idx}")
def rects(doc_id: int, idx: int):
    _exists_or_404(doc_id)
    filename = db.get_document_filename(doc_id)
    sentence = db.get_sentence(doc_id, idx)
    if not sentence:
        raise HTTPException(404, "Sentence not found.")
    if not filename:
        return {"page": sentence["page"], "width": 0, "height": 0, "rotation": 0,
                "rects": [], "words": []}
    return pdflayout.sentence_rects(doc_id, filename, sentence["page"], sentence["text"])


@router.get("/documents/{doc_id}/at")
def at(doc_id: int, page: int = Query(...), x: float = Query(...), y: float = Query(...)):
    _exists_or_404(doc_id)
    filename = db.get_document_filename(doc_id)
    if not filename:
        raise HTTPException(404, "No PDF for this document.")
    sentences = db.get_sentences_on_page(doc_id, page)
    return {"idx": pdflayout.hit_at(doc_id, filename, page, sentences, x, y)}


# ------------------------------------------------- progress, voice, offline

class ProgressIn(BaseModel):
    idx: int
    # Voice and speed aren't document state — voice is a per-language
    # preference (POST /documents/{id}/voice) and speed is global. Both stay
    # accepted because clients send them: speed is ignored outright, and voice
    # only sticks on a doc that pins its own.
    voice: str | None = None
    speed: float | None = None


@router.post("/documents/{doc_id}/progress")
def progress(doc_id: int, body: ProgressIn):
    _exists_or_404(doc_id)
    voice = body.voice if tts.valid_voice_id(body.voice) else None
    db.save_progress(doc_id, max(0, body.idx), voice)
    return {"ok": True}


class VoiceIn(BaseModel):
    voice: str


@router.post("/documents/{doc_id}/voice")
def set_voice(doc_id: int, body: VoiceIn):
    """Choose the voice to read this document in.

    Picking a voice that speaks the document's own language is not really a
    fact about this document — it's you saying "this is how Spanish should
    sound". So it's stored as your voice for that language and every Spanish
    document follows, immediately and from now on. Only a cross-language pick
    (an English text in a French voice) can mean just this one document, and
    that is the only thing that pins.

    The response says which of the two happened, so the app can say so too.
    """
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found.")
    voice = (body.voice or "").strip()
    if not lang.language_of(voice) or len(voice) > 40:
        raise HTTPException(400, "Unknown voice.")
    language = doc.get("lang")
    label = lang.LANG_LABEL.get(language or "")
    if lang.speaks(voice, language):
        settings.set_voice(voice, language)
        db.set_voice_override(doc_id, None)
        return {"ok": True, "voice": voice, "scope": "language", "lang": language, "lang_label": label}
    db.set_voice_override(doc_id, voice)
    return {"ok": True, "voice": voice, "scope": "document", "lang": language, "lang_label": label}


class GeneratedIn(BaseModel):
    voice: str


@router.post("/documents/{doc_id}/generated")
def generated(doc_id: int, body: GeneratedIn):
    """The app marks a document as fully synthesized for offline listening, so
    another device knows the server's cache is warm at that voice."""
    _exists_or_404(doc_id)
    if not tts.valid_voice_id(body.voice):
        raise HTTPException(400, "Unknown voice.")
    db.set_generated(doc_id, body.voice)
    return {"ok": True}


# ------------------------------------------------------ bookmarks, highlights

class BookmarkIn(BaseModel):
    idx: int
    note: str | None = None


@router.get("/documents/{doc_id}/bookmarks")
def list_bookmarks(doc_id: int):
    _exists_or_404(doc_id)
    return {"bookmarks": db.list_bookmarks(doc_id)}


@router.post("/documents/{doc_id}/bookmarks")
def add_bookmark(doc_id: int, body: BookmarkIn):
    bm = db.add_bookmark(doc_id, max(0, body.idx), (body.note or "").strip())
    if bm is None:
        raise HTTPException(404, "Document not found.")
    return {"ok": True, "bookmark": bm}


@router.delete("/documents/{doc_id}/bookmarks/{idx}")
def remove_bookmark(doc_id: int, idx: int):
    if not db.remove_bookmark(doc_id, idx):
        raise HTTPException(404, "Bookmark not found.")
    return {"ok": True}


class HighlightIn(BaseModel):
    idx: int
    cs: int
    ce: int
    color: str | None = None


@router.get("/documents/{doc_id}/highlights")
def list_highlights(doc_id: int):
    _exists_or_404(doc_id)
    return {"highlights": db.list_highlights(doc_id)}


@router.post("/documents/{doc_id}/highlights")
def add_highlight(doc_id: int, body: HighlightIn):
    """Mark a span of one sentence. The client sends character offsets into the
    sentence text it is showing, so the span survives re-rendering at any font
    size — but not a rechunk, which is why they're clamped to the sentence."""
    sent = db.get_sentence(doc_id, max(0, body.idx))
    if sent is None:
        raise HTTPException(404, "Sentence not found.")
    n = len(sent["text"])
    cs = max(0, min(body.cs, n))
    ce = max(cs, min(body.ce, n))
    if ce <= cs:
        raise HTTPException(400, "Empty highlight.")
    hl = db.add_highlight(doc_id, body.idx, cs, ce, sent["text"][cs:ce],
                          (body.color or "yellow").strip()[:24])
    if hl is None:
        raise HTTPException(404, "Document not found.")
    return {"ok": True, "highlight": hl}


@router.delete("/documents/{doc_id}/highlights/{hid}")
def remove_highlight(doc_id: int, hid: int):
    if not db.remove_highlight(doc_id, hid):
        raise HTTPException(404, "Highlight not found.")
    return {"ok": True}


# -------------------------------------------------------- rename, move, delete

class DocUpdate(BaseModel):
    title: str | None = None
    folder_id: int | None = None  # explicit null moves the document to the root


@router.patch("/documents/{doc_id}")
def update(doc_id: int, body: DocUpdate):
    """Rename a document and/or move it between library folders. Only fields
    present in the request change; `"folder_id": null` means "back to the root"."""
    _exists_or_404(doc_id)
    sent = body.model_fields_set
    title = None
    if "title" in sent:
        title = " ".join((body.title or "").split())
        if not title:
            raise HTTPException(400, "Give the document a title.")
        title = title[:200]
    move = "folder_id" in sent
    if move and body.folder_id is not None and not db.folder_exists(body.folder_id):
        raise HTTPException(404, "Folder not found.")
    db.update_document(doc_id, title=title, set_folder=move, folder_id=body.folder_id)
    return {"ok": True}


@router.delete("/documents/{doc_id}")
def delete(doc_id: int):
    filename = db.delete_document(doc_id)
    if filename is None:
        raise HTTPException(404, "Document not found.")
    if filename:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    shutil.rmtree(MEDIA_DIR / str(doc_id), ignore_errors=True)  # cached article images + cover
    return {"ok": True}
