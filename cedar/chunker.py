"""PDF text extraction and sentence chunking.

Produces clean, naturally-reading chunks ("sentences") sized for fast TTS and
reliable word-level timestamps. Each chunk carries its page number so the reader
can virtualize by page and the outline can jump to the right place.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import List
from urllib.parse import unquote

import fitz  # PyMuPDF

from .config import CHUNK_MAX_CHARS, CHUNK_MIN_CHARS


@dataclass
class Chunk:
    idx: int          # global order across the document
    page: int         # 1-based page number
    para: int         # paragraph index within the page (for subtle spacing)
    text: str
    heading: int = 0  # 0 = body text; 1..3 = heading level (for article formatting)


@dataclass
class MediaItem:
    ord: int          # order within the document's media
    anchor_idx: int   # appears just before this sentence idx in the reading flow
    src_url: str      # absolute source URL (cached server-side after import)
    alt: str = ""     # alt text / caption


@dataclass
class TocItem:
    level: int
    title: str
    page: int
    sentence_idx: int = 0  # filled in after chunking (first chunk on/after page)


@dataclass
class ExtractResult:
    title: str
    num_pages: int
    chunks: List[Chunk] = field(default_factory=list)
    toc: List[TocItem] = field(default_factory=list)
    # page -> (start_idx, count) for fast windowed loading on the frontend
    pages: List[dict] = field(default_factory=list)
    media: List[MediaItem] = field(default_factory=list)
    # Where a fresh reader should open: first chunk after stripped front-matter
    # lists (see _strip_marked_list). 0 = the top, i.e. nothing was stripped.
    start_idx: int = 0


# Common abbreviations that should NOT trigger a sentence split.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc",
    "ltd", "co", "corp", "dept", "fig", "no", "vol", "pp", "eg", "ie",
    "al", "ca", "cf", "ph", "approx", "dept", "gen", "gov", "sen", "rep",
    "ave", "blvd", "rd", "mt", "ft", "i.e", "e.g",
}

# Hyphen at end of a line that splits a word: "infor-\nmation" -> "information".
_HYPHEN_BREAK = re.compile(r"(\w)[-‐]\n(\w)")
# Any remaining single newline inside a paragraph becomes a space.
_NEWLINES = re.compile(r"[ \t]*\n[ \t]*")

# A blank line (optionally holding whitespace) marks a real paragraph break.
_PARA_BREAK = re.compile(r"\n[ \t]*\n[ \t\n]*")
# A line that finishes a sentence (so the next line is a new sentence, not a wrap).
_LINE_ENDS_SENTENCE = re.compile(r'[.!?:;]["\'”’)\]]?\s*$')
_MULTISPACE = re.compile(r"[ \t ]{2,}")

# Sentence boundary: end punctuation, optional closing quote/bracket, whitespace,
# then something that looks like the start of a new sentence.
_SENT_SPLIT = re.compile(r'(?<=[.!?])(["\'”’)\]]?)\s+(?=[\"\'“‘(\[A-Z0-9])')


def _clean_paragraph(text: str) -> str:
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _NEWLINES.sub(" ", text)
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


def _looks_like_abbrev(sentence: str) -> bool:
    """True if `sentence` ends in a known abbreviation (so we shouldn't have split)."""
    m = re.search(r"(\b[\w.]+)\.$", sentence.strip())
    if not m:
        return False
    word = m.group(1).lower().rstrip(".")
    return word in _ABBREV or (len(word) == 1 and word.isalpha())  # single initial e.g. "A."


def _split_sentences(paragraph: str) -> List[str]:
    if not paragraph:
        return []
    # re.split with one capture group yields: text, sep, text, sep, ...
    # The captured group is the closing quote/bracket that belongs to the chunk
    # before the boundary, so stitch each chunk back together with its closer.
    tokens = _SENT_SPLIT.split(paragraph)
    pieces: List[str] = []
    i = 0
    while i < len(tokens):
        chunk = tokens[i]
        closer = tokens[i + 1] if i + 1 < len(tokens) else ""
        pieces.append((chunk + (closer or "")).strip())
        i += 2
    # Merge across false splits caused by abbreviations.
    merged: List[str] = []
    for p in pieces:
        if not p:
            continue
        if merged and _looks_like_abbrev(merged[-1]):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    # Keep multi-sentence quoted dialogue together ('"Stop. Listen," she
    # said.') so the voice doesn't take a full sentence pause mid-quote.
    # Capped at CHUNK_MAX_CHARS — a page-long quotation still splits.
    out: List[str] = []
    for p in merged:
        if out and _quote_open(out[-1]) and len(out[-1]) + len(p) < CHUNK_MAX_CHARS:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [o for o in out if o]


def _quote_open(text: str) -> bool:
    """True when `text` ends inside an unclosed double-quoted span. Curly
    quotes are counted as pairs when present; otherwise straight-quote parity.
    Single quotes are ignored entirely — apostrophes make them unreliable."""
    curly = text.count("“") - text.count("”")
    if curly != 0:
        return curly > 0
    if "“" in text or "”" in text:  # balanced curly usage — closed
        return False
    return text.count('"') % 2 == 1


def _soft_split_long(sentence: str, limit: int) -> List[str]:
    """Split an over-long sentence at clause boundaries, then by words if needed."""
    if len(sentence) <= limit:
        return [sentence]
    out: List[str] = []
    # Prefer splitting at ", " / "; " / ": " near the middle, greedily.
    remaining = sentence
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("; "), window.rfind(": "), window.rfind(", "))
        if cut < limit * 0.4:  # no good clause break; fall back to last space
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
            out.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
            continue
        out.append(remaining[: cut + 1].strip())
        remaining = remaining[cut + 1 :].strip()
    if remaining:
        out.append(remaining)
    return [o for o in out if o]


def _page_blocks(page: "fitz.Page") -> List[tuple]:
    """Text blocks with geometry for a page in reading order:
    (x0, y0, x1, y1, cleaned_text)."""
    blocks = page.get_text("blocks")  # (x0,y0,x1,y1, text, block_no, block_type)
    text_blocks = [b for b in blocks if (len(b) < 7 or b[6] == 0) and b[4].strip()]
    # Sort top-to-bottom, then left-to-right. Round y to group near-equal lines.
    text_blocks.sort(key=lambda b: (round(b[1] / 4), b[0]))
    out: List[tuple] = []
    for b in text_blocks:
        cleaned = _clean_paragraph(b[4])
        if cleaned:
            out.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]), cleaned))
    return out


# ------------------- PDF page furniture (headers/footers/page numbers) -------
#
# Academic and book PDFs repeat a running header/footer on every page and stamp
# a page number near the edge; read aloud they interrupt every page ("Journal
# of… 47 …"). A block is furniture when it sits in the top/bottom band of the
# page AND either (a) its digit-normalized text repeats at the same height on
# many pages, or (b) it is nothing but a page number. Body text can't trip (a):
# real prose never repeats identically at one height across 40% of pages.

_FURNITURE_BAND = 0.12  # fraction of page height considered top/bottom edge
_PAGENUM_RE = re.compile(
    r"^(?:page\s+)?(?:[0-9]{1,4}|[ivxlcdm]{1,7})(?:\s*(?:of|/|—|-)\s*[0-9]{1,4})?$",
    re.IGNORECASE,
)


def _band(block: tuple, page_height: float) -> str | None:
    if block[1] < page_height * _FURNITURE_BAND:
        return "top"
    if block[3] > page_height * (1 - _FURNITURE_BAND):
        return "bottom"
    return None


def _furniture_key(zone: str, block: tuple) -> tuple:
    return (zone, round(block[1] / 8), re.sub(r"\d+", "#", block[4].lower())[:80])


def _repeating_furniture(pages_blocks: List[List[tuple]],
                         page_heights: List[float]) -> set:
    """Keys of edge blocks whose text repeats at the same height across pages."""
    counts: dict = {}
    for blocks, height in zip(pages_blocks, page_heights):
        seen = set()
        for b in blocks:
            zone = _band(b, height)
            if not zone:
                continue
            key = _furniture_key(zone, b)
            if key not in seen:
                seen.add(key)
                counts[key] = counts.get(key, 0) + 1
    need = max(3, -(-len(pages_blocks) * 2 // 5))  # ceil(40% of pages), min 3
    return {k for k, c in counts.items() if c >= need}


# --------------------------- PDF cross-page stitching ------------------------

# A paragraph that ends a page mid-sentence ("…the results sug-" / "…and the")
# continues on the next page; emitting the halves separately reads with an
# awkward pause at every page break. Sentence-terminal test for the tail:
_SENT_TERMINAL = re.compile(r'[.!?]["\'”’)\]]*$')
# First sentence end inside the continuation head (decimal-safe via \s|$).
_HEAD_CUT = re.compile(r'[.!?]["\'”’)\]]*(?=\s|$)')


def _stitch_pages(pages_paras: List[List[str]]) -> None:
    """Join each page's trailing mid-sentence paragraph with the next page's
    continuation, in place. Only the continuation's first sentence moves; the
    rest stays on its own page so page navigation still lines up."""
    for pno in range(len(pages_paras) - 1):
        cur, nxt = pages_paras[pno], pages_paras[pno + 1]
        if not cur or not nxt:
            continue
        tail, head = cur[-1], nxt[0]
        hyphen = len(tail) > 1 and tail[-1] in "-‐" and tail[-2].isalnum()
        if not hyphen:
            if _SENT_TERMINAL.search(tail) or len(tail) < 40:
                continue
            # A head starting with a capital/digit usually IS a new sentence or
            # heading (uppercase continuations exist but joining one wrongly
            # glues two sentences — stay conservative).
            if not head[:1].isalpha() or not head[:1].islower():
                continue
        m = _HEAD_CUT.search(head)
        clause, rest = (head[:m.end()], head[m.end():].strip()) if m else (head, "")
        if hyphen and clause[:1].isalnum():
            cur[-1] = tail[:-1] + clause
        else:
            cur[-1] = tail + " " + clause
        if rest:
            nxt[0] = rest
        else:
            del nxt[0]


def _emit_sentences(para: str, page: int, para_no: int, idx: int, chunks: List[Chunk],
                    heading: int = 0) -> int:
    """Append sentence-sized chunks for one paragraph; return the next idx."""
    sentences = _split_sentences(para)
    # Merge tiny fragments forward, split long ones.
    normalized: List[str] = []
    for s in sentences:
        if normalized and len(normalized[-1]) < CHUNK_MIN_CHARS:
            normalized[-1] = (normalized[-1] + " " + s).strip()
        else:
            normalized.append(s)
    for s in normalized:
        for piece in _soft_split_long(s, CHUNK_MAX_CHARS):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(Chunk(idx=idx, page=page, para=para_no, text=piece, heading=heading))
            idx += 1
    return idx


def _emit_page_chunks(paragraphs: List[str], page: int, idx: int, chunks: List[Chunk]) -> int:
    """Append sentence-sized chunks for one page's paragraphs; return the next idx."""
    for para_no, para in enumerate(paragraphs):
        idx = _emit_sentences(para, page, para_no, idx, chunks)
    return idx


def _split_into_paragraphs(text: str) -> List[str]:
    """Split raw plain text into paragraph blocks, robust to hard-wrapping.

    Sources differ in how they mark paragraphs:
      * Project Gutenberg / plain .txt: lines are *hard-wrapped* at ~70 chars with
        a single newline, and paragraphs are separated by a blank line. Splitting on
        every newline (the old behaviour) shredded each wrapped line into its own
        "paragraph" -> the reader showed one line per row and TTS paused at every
        line break. The fix: split on blank lines only; the single newlines inside a
        block are wraps that `_clean_paragraph` later folds into spaces.
      * trafilatura / OCR: one paragraph per line, no blank lines and no wrapping.
        Here each newline *is* a paragraph break, so split on single newlines.

    We pick per document: if blank lines exist they define the paragraphs; otherwise
    we detect hard-wrapping (many short lines that don't end a sentence) and, if found,
    keep the whole block together so sentences read across line breaks.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    if _PARA_BREAK.search(text):
        blocks = _PARA_BREAK.split(text)
        return [b for b in blocks if b.strip()]

    # No blank lines: decide whether single newlines are wraps or paragraph breaks.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) > 2:
        short = sum(1 for ln in lines if len(ln.strip()) <= 80)
        ends_sentence = sum(1 for ln in lines if _LINE_ENDS_SENTENCE.search(ln))
        hard_wrapped = short / len(lines) > 0.8 and ends_sentence / len(lines) < 0.5
        if hard_wrapped:
            return [text]  # one block; _clean_paragraph folds the wraps into spaces
    return lines


# Chapter-heading heuristics for plain text (Project Gutenberg etc.). Explicit
# markers ("Chapter IV.", "PART TWO — …") are always headings; standalone
# numerals and short ALL-CAPS lines only count when the pattern repeats, so a
# lone shouty line in pasted text doesn't become a bogus chapter.
_CH_EXPLICIT = re.compile(
    r"^(chapter|part|book|canto|act|scene|stave|letter|prologue|epilogue|preface|"
    r"foreword|introduction|afterword|conclusion|appendix)\b[\s.:—–-]*"
    r"([ivxlcdm]+|\d{1,4}|[a-z]+)?\b.{0,70}$",
    re.IGNORECASE,
)
_CH_NUMERAL = re.compile(r"^(?:[IVXLCDM]{1,8}|\d{1,3})\.?$")
_CH_ALLCAPS = re.compile(r"^[^a-z]{2,60}$")
# "IV. The Descent" / "12. A New Home" — numeral + titled chapter on one line
# (common in Gutenberg fiction). Prone to matching numbered lists, so hits only
# count when their numbers form an ascending run (see _detect_chapters).
_CH_NUMTITLE = re.compile(r"^(?:[IVXLCDM]{1,8}|\d{1,3})\.\s+[\"'“‘(]?[A-Z].{0,60}$")


def _roman_or_int(s: str) -> int | None:
    s = s.strip().rstrip(".").upper()
    if s.isdigit():
        return int(s)
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    if not s or any(ch not in vals for ch in s):
        return None
    total = 0
    for j, ch in enumerate(s):
        v = vals[ch]
        total += -v if j + 1 < len(s) and vals[s[j + 1]] > v else v
    return total


def _detect_chapters(paras: List[str]) -> dict:
    """Map paragraph index -> heading level for paragraphs that look like
    chapter headings. Returns {} when the document doesn't look chaptered."""
    explicit, numeral, numtitle, allcaps = [], [], [], []
    for i, p in enumerate(paras):
        if len(p) > 90 or "\n" in p:
            continue
        if _CH_EXPLICIT.match(p):
            explicit.append(i)
        elif _CH_NUMERAL.match(p):
            numeral.append(i)
        elif _CH_NUMTITLE.match(p):
            v = _roman_or_int(p.split(".", 1)[0])
            if v is not None:
                numtitle.append((i, v))
        elif _CH_ALLCAPS.match(p) and sum(c.isalpha() for c in p) >= max(2, len(p) // 2):
            allcaps.append(i)

    out: dict = {i: 1 for i in explicit}
    if len(numeral) >= 2:
        out.update({i: 1 for i in numeral})
    # Numeral-plus-title lines ("IV. The Descent") are chapters only when their
    # numbers mostly ascend — a numbered list masquerades, a chapter run doesn't.
    seq_ok = False
    if len(numtitle) >= 3:
        vals = [v for _, v in numtitle]
        seq_ok = sum(1 for a, b in zip(vals, vals[1:]) if b >= a) >= 0.8 * (len(vals) - 1)
        if seq_ok:
            out.update({i: 1 for i, _ in numtitle})
    # A doc with confirmed chapter markers needs less evidence that its short
    # ALL-CAPS lines are titles (they usually follow "CHAPTER X." directly).
    chaptered = bool(explicit) or len(numeral) >= 2 or seq_ok
    if len(allcaps) >= (2 if chaptered else 3):
        out.update({i: 2 if chaptered else 1 for i in allcaps})
    # A single hit is noise, and a doc that's mostly "headings" isn't chaptered
    # (an OCR'd menu or a list would trip that); real books are mostly body text.
    # The absolute cap scales with size: the Complete Works of Shakespeare
    # legitimately carries thousands of act/scene headings.
    if len(out) < 2 or len(out) > 0.55 * len(paras) or len(out) > max(400, 0.08 * len(paras)):
        return {}
    return out


# ---------------- printed table-of-contents stripping (books' front matter) --
#
# Classic books print their table of contents (and illustration lists) right in
# the text; the reader builds its own Contents picker from detected headings, so
# a listener hears "Chapter 1. Chapter 2. …" read aloud for minutes (Moby Dick
# opens with 135 such lines) — and each printed entry also *matches* the heading
# heuristics above, duplicating every chapter in the picker. Removal is
# deliberately conservative so real front matter (prefaces, dedications,
# epigraphs, translator's notes) is never touched:
#   * only a block introduced by an explicit marker line ("CONTENTS", "TABLE OF
#     CONTENTS", "ILLUSTRATIONS", …) is considered — no marker, no change;
#   * only entry-shaped text after it is consumed, and consumption stops the
#     moment a consumed entry's text repeats — that repeat IS the first real
#     section heading of the body, so the body can never be eaten;
#   * ALL-CAPS lines (a would-be dedication) count as removable only when they
#     sit *between* entries, never as the trailing edge of the block.

_TOC_MARKER_CORE = (
    r"(?:(?:table of )?contents(?: and illustrations)?(?: of .{0,40})?"
    r"|(?:list of )?(?:illustrations|plates)"
    r"|page"  # a list's bare column header when the title line was lost
    r"|table des mati[eè]res|sommaire|[ií]ndice(?: de contenidos?)?|indice"
    r"|inhalt(?:sverzeichnis)?)"
)
_TOC_MARKER = re.compile(r"^" + _TOC_MARKER_CORE + r"[.:]?$", re.IGNORECASE)
# The same marker leading a glued block ("Contents Letter 1 Letter 2 …" /
# "INDICE Capitulo I. …" when the list wasn't blank-line-separated from its title).
_TOC_MARKER_PREFIX = re.compile(r"^" + _TOC_MARKER_CORE + r"[.:]?\s+", re.IGNORECASE)
# A word that starts a printed entry ("Chapter IV.", "Letter 1", "Chap 34" …).
_TOC_ENTRY_WORD = re.compile(
    r"\b(?:chapter|chap|letter|part|book|canto|act|scene|stave|volume|tome"
    r"|chapitre|lettre|partie|livre|acte|cap[ií]tulo|capitolo|libro|kapitel|brief)"
    r"\b[.\s]*(?=[ivxlcdm]+\b|\d)",
    re.IGNORECASE,
)
# Dot leaders with a page number ("The Spouter-Inn . . . . 12").
_TOC_LEADER = re.compile(r"(?:\.[ \t]?){3,}\s*\d{1,4}\b")
# A short line ending in a page reference ("Frontispiece iv", "Mr. Bennet 5").
# 4-digit numbers are excluded (years on title pages), and so are single-letter
# romans — "K. H. V." is a signature's initials, not a page v.
_TOC_PAGEREF = re.compile(r"^\S.{0,78}\s(?:[ivxlcdm]{2,8}|\d{1,3})\.?$", re.IGNORECASE)


def _glued_entry_count(para: str) -> int:
    """Entries inside one hard-wrap-folded block, if the block reads as a dense
    list ("Letter 1 Letter 2 … Chapter 24"); 0 when it reads as prose."""
    n = max(len(_TOC_ENTRY_WORD.findall(para)), len(_TOC_LEADER.findall(para)))
    return n if n >= 3 and len(para) / n <= 100 else 0


def _entry_strong(para: str) -> bool:
    """A printed TOC entry with unmistakable structure ("CHAPTER 1. Loomings.",
    "IV. The Descent", dot leaders) — trusted enough to keep consuming past a
    long synopsis line."""
    if len(para) > 90:
        return False
    return bool(
        _CH_EXPLICIT.match(para) or _CH_NUMTITLE.match(para)
        or _CH_NUMERAL.match(para) or _TOC_LEADER.search(para)
        or _TOC_ENTRY_WORD.search(para)
    )


def _entry_like(para: str) -> bool:
    """One printed TOC entry on its own line, including the weaker page-ref
    shape ("Frontispiece iv") that only counts inside a marked list."""
    return _entry_strong(para) or bool(len(para) <= 90 and _TOC_PAGEREF.match(para))


def _strip_marked_list(paras: List[str]) -> int | None:
    """Remove one explicitly marked printed contents/illustrations list near the
    front of ``paras`` (in place). Returns the index where the body now resumes,
    or None when no removable list was found."""
    for i in range(min(len(paras), 150)):
        p = paras[i].strip()
        entries = 0
        seen = ""  # normalized text consumed so far — repeats mark the body
        # An illustrations list's entries are bare captions ("The Torchlight
        # Procession") no structural pattern can match; behind its explicit
        # marker, any short line counts — until prose comes into sight.
        illus = bool(re.match(r"(?:list of )?(?:illustrations|plates)\b", p, re.IGNORECASE))
        if _TOC_MARKER.match(p):
            pass
        elif (m := _TOC_MARKER_PREFIX.match(p)) and _glued_entry_count(p[m.end():]):
            entries = _glued_entry_count(p[m.end():])
            seen = " ".join(p[m.end():].lower().split())
        elif _glued_entry_count(p) >= 8:
            # A really dense glued list is its own evidence — some books print
            # the contents with no heading line at all.
            entries = _glued_entry_count(p)
            seen = " ".join(p.lower().split())
        else:
            continue

        end, gap = i, 0
        for j in range(i + 1, min(i + 400, len(paras))):
            q = paras[j]
            norm = " ".join(q.lower().split())
            if _TOC_MARKER.match(q.strip()):
                # A follow-up list runs straight into this one ("CONTENTS" then
                # "LIST OF ILLUSTRATIONS."): absorb its marker and switch mode.
                illus = illus or bool(re.match(
                    r"(?:list of )?(?:illustrations|plates)\b", q.strip(), re.IGNORECASE))
                end, gap = j, 0
                seen += " " + norm
                continue
            if entries and len(norm) >= 6 and norm in seen:
                # A repeat of consumed text is the body starting (the first
                # section re-states its own entry) — unless the list merely
                # re-states earlier numbering mid-way (a two-part book's second
                # contents run): body means prose follows within a few lines.
                if any(len(paras[k]) > 120 for k in range(j + 1, min(j + 4, len(paras)))):
                    break
                end, gap = j, 0
                continue
            glued = _glued_entry_count(q)
            caption = (illus and len(q) <= 90 and not any(
                len(paras[k]) > 120 for k in range(j + 1, min(j + 4, len(paras)))))
            if glued or _entry_like(q) or caption:
                entries += glued or 1
                end, gap = j, 0
                seen += " " + norm
            else:
                # Not entry-shaped (an ALL-CAPS interlude, a chapter-synopsis
                # line): tolerate it inside the list — even a long synopsis,
                # as long as another entry follows — but never extend `end`
                # past it, so a trailing dedication stays in the book. Still
                # feed it to `seen`: its body repeat must stop consumption.
                nxt = paras[j + 1] if j + 1 < len(paras) else ""
                nxt_entry = bool(_entry_like(nxt) or _glued_entry_count(nxt))
                # A *long* gap (a chapter-synopsis paragraph) is only part of
                # the list when an unmistakable entry follows — a weak page-ref
                # lookalike ("K. H. V.") must not drag real prose into the list.
                nxt_strong = bool(_entry_strong(nxt) or _glued_entry_count(nxt))
                gap += 1
                if (gap > 2 and not nxt_entry) or (len(q) > 120 and not nxt_strong):
                    break
                seen += " " + norm
        if entries >= 3:
            # A consumed tail entry followed by prose is likely the body's
            # first heading that the printed list didn't mention (an unlisted
            # "PREFACE."): a real heading is followed by its text, a list entry
            # by more entries. Back off to the previous entry instead.
            nxt = paras[end + 1] if end + 1 < len(paras) else ""
            if (end > i and _entry_like(paras[end]) and not _glued_entry_count(paras[end])
                    and len(nxt) > 120):
                end -= 1
                while end > i and not (_entry_like(paras[end]) or _glued_entry_count(paras[end])):
                    end -= 1
            del paras[i:end + 1]
            return i
    return None


def chunk_plain_text(title: str, text: str, *,
                     strip_printed_toc: bool = False) -> ExtractResult:
    """Turn an extracted article (plain text) into a single-page document of sentence
    chunks. Detects hard-wrapped paragraphs (e.g. Project Gutenberg) so a sentence that
    spans several wrapped lines reads as one sentence, not one awkward pause per line.
    Chapter-looking paragraphs become heading chunks + a table of contents.

    ``strip_printed_toc`` (book imports) removes explicitly marked printed
    contents/illustration lists and reports where the body resumes via
    ``start_idx`` — used as the fresh document's initial reading position."""
    raw = _split_into_paragraphs(text)
    paras = [c for c in (_clean_paragraph(p) for p in raw) if c]
    start_para: int | None = None
    if strip_printed_toc:
        start_para = _strip_marked_list(paras)
        if start_para is not None:
            # Adjacent follow-up list (a "CONTENTS" then "ILLUSTRATIONS" pair):
            # strip it too, but only when it sits right where the first one was —
            # a removal further out must not move the start past real content.
            for _ in range(2):
                nxt = _strip_marked_list(paras)
                if nxt is None:
                    break
                if nxt <= start_para + 2:
                    start_para = nxt
            # Only move the opening position when what precedes the list is a
            # title page (a few short lines). More text than that is real front
            # matter — an introduction before the contents page — and the book
            # must open at the top so it's heard, not skipped.
            if sum(len(p) + 1 for p in paras[:start_para]) > 1000:
                start_para = None
    headings = _detect_chapters(paras)
    chunks: List[Chunk] = []
    toc: List[TocItem] = []
    idx = 0
    start_idx = 0
    for para_no, para in enumerate(paras):
        if start_para is not None and para_no == start_para:
            start_idx = idx
        level = headings.get(para_no, 0)
        if level:
            toc.append(TocItem(level=level, title=para[:120], page=1, sentence_idx=idx))
        idx = _emit_sentences(para, 1, para_no, idx, chunks, heading=level)
    # A start beyond the first third of the book means detection went wrong —
    # open at the top rather than risk skipping content.
    if start_idx >= len(chunks) or start_idx > 0.3 * len(chunks):
        start_idx = 0
    return ExtractResult(
        title=(title or "Untitled").strip() or "Untitled",
        num_pages=1,
        chunks=chunks,
        toc=toc,
        pages=[{"page": 1, "start": 0, "count": idx}],
        start_idx=start_idx,
    )


# --- Article (markdown) chunking: headings + paragraphs + inline images -------

_MD_IMG = re.compile(r"!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)[^)]*\)")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MD_EMPH = re.compile(r"(\*\*|__|\*|_|`)(.+?)\1", re.DOTALL)
_MD_BLOCK = re.compile(r"\n[ \t]*\n")
# Shortest body paragraph eligible for whole-document dedup. Anything shorter is
# kept even when repeated — short lines legitimately recur in real books.
_DEDUP_MIN_CHARS = 200


def _abs_url(base: str, url: str) -> str:
    url = (url or "").strip().strip("<>")
    if not url or url.startswith("data:"):
        return ""
    if "://" not in url:
        from urllib.parse import urljoin
        url = urljoin(base, url)
    return url if url.lower().startswith(("http://", "https://")) else ""


def _img_url_ok(url: str) -> bool:
    low = url.lower().split("?", 1)[0]
    if low.endswith((".svg", ".gif")):  # svg needs a renderer; gifs are usually decorative
        return False
    return True


def _strip_inline_md(text: str) -> str:
    """Markdown -> plain reading text: drop links to their label, strip emphasis."""
    text = _MD_LINK.sub(r"\1", text)
    for _ in range(3):  # nested emphasis, a couple of passes
        new = _MD_EMPH.sub(r"\2", text)
        if new == text:
            break
        text = new
    text = re.sub(r"^>\s?", "", text)            # blockquote marker
    text = re.sub(r"^[-*+]\s+", "", text)         # list bullet
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", text)  # unescape
    return text.strip()


def chunk_article(title: str, markdown: str, base_url: str = "") -> ExtractResult:
    """Turn a trafilatura *markdown* extraction into a formatted single-page
    document: heading-tagged chunks, body sentences, and inline images anchored
    to their place in the reading flow. Duplicate long blocks/images (a known
    quirk of markdown extraction) are dropped."""
    blocks = _MD_BLOCK.split((markdown or "").replace("\r\n", "\n"))
    chunks: List[Chunk] = []
    media: List[MediaItem] = []
    toc: List[TocItem] = []
    seen_text: set = set()
    seen_img: set = set()
    idx = 0
    para_no = 0

    for raw in blocks:
        block = raw.strip()
        if not block:
            continue
        # Pull any images out first — they anchor before the next emitted sentence.
        for alt, url in _MD_IMG.findall(block):
            u = _abs_url(base_url, url)
            if u and u not in seen_img and _img_url_ok(u):
                seen_img.add(u)
                media.append(MediaItem(ord=len(media), anchor_idx=idx, src_url=u, alt=alt.strip()))
        text = _MD_IMG.sub("", block).strip()
        if not text:
            continue

        head = _MD_HEAD.match(text)
        if head:
            level = min(3, len(head.group(1)))
            htext = _strip_inline_md(head.group(2))
            key = htext.lower()
            if not htext or key in seen_text:
                continue
            seen_text.add(key)
            chunks.append(Chunk(idx=idx, page=1, para=para_no, text=htext, heading=level))
            toc.append(TocItem(level=level, title=htext, page=1, sentence_idx=idx))
            idx += 1
            para_no += 1
            continue

        para = _clean_paragraph(_strip_inline_md(text))
        if not para:
            continue
        # Only long blocks are deduped: the extraction quirk repeats whole
        # paragraphs, whereas short repeated lines ('"Yes," he said.', a poem's
        # refrain) are real content — dropping those silently loses text.
        if len(para) >= _DEDUP_MIN_CHARS:
            key = para.lower()[:160]
            if key in seen_text:
                continue
            seen_text.add(key)
        idx = _emit_sentences(para, 1, para_no, idx, chunks)
        para_no += 1

    return ExtractResult(
        title=(title or "Untitled").strip() or "Untitled",
        num_pages=1,
        chunks=chunks,
        toc=toc,
        pages=[{"page": 1, "start": 0, "count": idx}],
        media=media,
    )


# --- EPUB extraction: spine order -> headings + paragraphs -> article pipeline -

def _epub_norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _epub_resolve(base: str, href: str) -> str:
    """A manifest href (relative to the OPF, possibly %-encoded, with a #anchor)
    resolved to a zip entry name."""
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base, href)) if base else href


def _xhtml_to_blocks(raw: bytes) -> str:
    """One EPUB chapter's (X)HTML -> markdown-ish text: '#'-prefixed headings and
    blank-line-separated paragraphs, ready for chunk_article. Falls back to the
    chapter's whole text when paragraphs aren't marked up with <p>."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(raw)
    except Exception:
        return ""
    for bad in doc.xpath("//script | //style"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    body = doc.find(".//body")
    root = body if body is not None else doc

    parts: List[str] = []
    captured = 0
    for el in root.iter("h1", "h2", "h3", "h4", "h5", "h6", "p"):
        tag = el.tag
        if not isinstance(tag, str):
            continue
        txt = _epub_norm_ws(el.text_content())
        if not txt:
            continue
        tag = tag.lower()
        if tag[0] == "h" and len(tag) == 2 and tag[1].isdigit():
            parts.append("#" * min(3, int(tag[1])) + " " + txt)
        else:
            parts.append(txt)
        captured += len(txt)

    body_text = _epub_norm_ws(root.text_content())
    if captured < len(body_text) * 0.5:
        # Paragraphs weren't in <p> tags (some publishers use <div>); keep the
        # whole chapter as one block so nothing is dropped.
        return body_text
    return "\n\n".join(parts)


# A zip member's *declared* size is attacker-controlled, so an EPUB is
# decompressed under a budget: a few hundred KB of deflate can otherwise expand
# to gigabytes ("zip bomb") and OOM the server. Real books are far below these:
# a whole book's text is a handful of MB, one chapter well under 1 MB.
_EPUB_ENTRY_MAX = 8 * 1024 * 1024    # per zip member
_EPUB_TOTAL_MAX = 24 * 1024 * 1024   # summed across the spine


def _epub_read(z, name: str, limit: int = _EPUB_ENTRY_MAX) -> bytes:
    """Read one zip member, decompressing at most ``limit`` bytes."""
    with z.open(name) as fh:
        data = fh.read(max(0, limit) + 1)
    if len(data) > limit:
        raise ValueError(f"EPUB entry is too large: {name}")
    return data


def extract_epub(source, fallback_title: str) -> ExtractResult:
    """Extract an EPUB (a path or a file-like object) into a formatted single-page
    document — heading-tagged chunks, body sentences, and a heading-derived table
    of contents — by reading the spine in order and reusing the article pipeline."""
    import zipfile

    from lxml import etree

    # The EPUB's XML is written by whoever made the file. No entity expansion,
    # no DTD, no network: an entity must not be able to read a file off the
    # server into the title, or expand into gigabytes.
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             huge_tree=False)

    with zipfile.ZipFile(source) as z:
        names = set(z.namelist())

        # 1) META-INF/container.xml -> the OPF package path.
        opf_path = ""
        if "META-INF/container.xml" in names:
            try:
                ctree = etree.fromstring(_epub_read(z, "META-INF/container.xml"), parser)
                roots = ctree.xpath("//*[local-name()='rootfile']/@full-path")
                opf_path = roots[0] if roots else ""
            except Exception:
                opf_path = ""
        if not opf_path:
            opf_path = next((n for n in names if n.lower().endswith(".opf")), "")
        if not opf_path or opf_path not in names:
            raise ValueError("No OPF package found — not a valid EPUB.")

        # 2) OPF -> title + spine reading order.
        opf = etree.fromstring(_epub_read(z, opf_path), parser)
        titles = opf.xpath("//*[local-name()='metadata']/*[local-name()='title']/text()")
        title = (titles[0].strip() if titles else "") or fallback_title

        manifest: dict = {}
        for it in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
            manifest[it.get("id")] = (it.get("href") or "", (it.get("media-type") or "").lower())

        base = posixpath.dirname(opf_path)
        spine_hrefs: List[str] = []
        for ref in opf.xpath("//*[local-name()='spine']/*[local-name()='itemref']"):
            href, mtype = manifest.get(ref.get("idref"), ("", ""))
            if href and ("html" in mtype or mtype == ""):
                spine_hrefs.append(href)
        # Fallback: no usable spine -> every (x)html item, in manifest order.
        if not spine_hrefs:
            spine_hrefs = [h for (h, m) in manifest.values() if h and "html" in m]

        md_parts: List[str] = []
        budget = _EPUB_TOTAL_MAX
        for href in spine_hrefs:
            entry = _epub_resolve(base, href)
            if entry not in names or budget <= 0:
                continue
            try:
                raw = _epub_read(z, entry, min(_EPUB_ENTRY_MAX, budget))
                budget -= len(raw)
                block = _xhtml_to_blocks(raw)
            except Exception:
                continue
            if block.strip():
                md_parts.append(block)

    result = chunk_article(title, "\n\n".join(md_parts))
    result.title = (title or "Untitled").strip() or "Untitled"
    return result


def extract_pdf(path: str, fallback_title: str) -> ExtractResult:
    doc = fitz.open(path)
    try:
        title = (doc.metadata or {}).get("title") or ""
        title = title.strip() or fallback_title
        num_pages = doc.page_count

        # Pass 1: blocks with geometry, so page furniture can be recognized by
        # its repetition across pages before anything is flattened to text.
        pages_blocks: List[List[tuple]] = []
        page_heights: List[float] = []
        for pno in range(num_pages):
            page = doc.load_page(pno)
            pages_blocks.append(_page_blocks(page))
            page_heights.append(float(page.rect.height))
        furniture = _repeating_furniture(pages_blocks, page_heights)

        # Pass 2: paragraphs per page minus furniture, stitched across breaks.
        pages_paras: List[List[str]] = []
        for blocks, height in zip(pages_blocks, page_heights):
            paras: List[str] = []
            for b in blocks:
                zone = _band(b, height)
                if zone and (_furniture_key(zone, b) in furniture
                             or _PAGENUM_RE.match(b[4].strip())):
                    continue
                paras.append(b[4])
            pages_paras.append(paras)
        _stitch_pages(pages_paras)

        chunks: List[Chunk] = []
        pages_index: List[dict] = []
        idx = 0
        for pno, paras in enumerate(pages_paras):
            start_idx = idx
            idx = _emit_page_chunks(paras, pno + 1, idx, chunks)
            pages_index.append({"page": pno + 1, "start": start_idx, "count": idx - start_idx})

        # Table of contents -> nearest chunk index for navigation.
        toc_items: List[TocItem] = []
        for level, ttl, pg in (doc.get_toc() or []):
            pg = max(1, pg)
            sent_idx = next((c.idx for c in chunks if c.page >= pg), 0)
            toc_items.append(TocItem(level=level, title=ttl.strip(), page=pg, sentence_idx=sent_idx))

        return ExtractResult(
            title=title,
            num_pages=num_pages,
            chunks=chunks,
            toc=toc_items,
            pages=pages_index,
        )
    finally:
        doc.close()
