"""PDF layout for the original-view highlight:
  • per-sentence line rectangles (the sentence band)
  • per-word boxes with their char offsets (for the spoken-word sub-highlight)
  • point -> sentence lookup (for tap-to-seek)

Uses the page's actual word boxes (tight, per-line, hyphenation-aware). Computed
on demand from the stored PDF and cached.
"""
from __future__ import annotations

import re
import threading
import unicodedata

import fitz

from .config import UPLOAD_DIR

_lock = threading.Lock()
_cache: dict = {}        # (doc_id, page, text) -> {rects, words, ...}
_hit_cache: dict = {}    # (doc_id, page) -> [(x0,y0,x1,y1, idx)]
_WORD = re.compile(r"\w+", re.UNICODE)


def _norm(w: str) -> str:
    """Normalize a token for matching: decompose ligatures/accents (NFKD,
    e.g. 'ﬁ'->'fi', 'é'->'e'), lowercase, keep alphanumerics only."""
    w = unicodedata.normalize("NFKD", w)
    return "".join(ch for ch in w.lower() if ch.isalnum() and not unicodedata.combining(ch))


def _merge_lines(boxes):
    groups: dict = {}
    for b in boxes:
        key = (b[5], b[6])  # (block, line)
        g = groups.get(key)
        if g is None:
            groups[key] = [b[0], b[1], b[2], b[3]]
        else:
            g[0] = min(g[0], b[0]); g[1] = min(g[1], b[1])
            g[2] = max(g[2], b[2]); g[3] = max(g[3], b[3])
    rects = sorted(groups.values(), key=lambda r: (r[1], r[0]))
    return [[float(r[0]), float(r[1]), float(r[2]), float(r[3])] for r in rects[:24]]


def _match(words, sentence: str):
    """Align `sentence` to the page's word boxes.

    Returns (line_rects, word_boxes) where word_boxes is a list of
    {cs, ce, box} — char offsets into the sentence text + the word's box,
    stitching hyphenated / split words. Resyncs over the occasional
    unmatchable token instead of giving up, so the tail of a sentence still
    gets highlighted.

    When several places on the page begin with the same words (e.g. two
    sentences that share a prefix), candidates are ranked by how many tokens
    they actually *match to a box* and then by how *contiguous* the run is
    (fewest page words consumed). That keeps the highlight inside the sentence
    being read instead of leaking a word into a look-alike neighbour.
    """
    toks = [(m.start(), m.end(), _norm(m.group())) for m in _WORD.finditer(sentence)]
    toks = [t for t in toks if t[2]]
    if not toks:
        return [], []
    n = len(words)
    target = len(toks)
    norms = [_norm(w[4]) for w in words]  # page-word norms, once
    best = None  # (matched, span, miss, wb, all_boxes); higher matched / lower span wins

    for start in range(n):
        first = norms[start]
        if not first or not (toks[0][2] == first or toks[0][2].startswith(first)):
            continue
        wb, all_boxes, si, pi, miss = [], [], 0, start, 0
        while si < target and pi < n:
            ptok = norms[pi]
            if not ptok:
                pi += 1
                continue
            cs, ce, stok = toks[si]
            if ptok == stok:
                wb.append((cs, ce, [words[pi]])); all_boxes.append(words[pi]); pi += 1; si += 1
                continue
            if stok.startswith(ptok) and len(ptok) >= 2:
                acc, tb = ptok, [words[pi]]
                pi += 1
                while pi < n and len(acc) < len(stok):
                    nx = norms[pi]
                    if not nx:
                        pi += 1
                        continue
                    if stok.startswith(acc + nx):
                        acc += nx; tb.append(words[pi]); pi += 1
                    else:
                        break
                wb.append((cs, ce, tb)); all_boxes.extend(tb); si += 1
                continue
            if ptok.startswith(stok) and len(stok) >= 2:
                # One page word glues several sentence tokens — punctuation with
                # no surrounding space, e.g. "dirty…I" -> dirty + I, or
                # "happy-go-lucky" -> happy + go + lucky. Map them all to this box.
                rem = ptok[len(stok):]
                consumed = [(cs, ce)]
                sj = si + 1
                while sj < target and rem:
                    ncs, nce, nstok = toks[sj]
                    if rem == nstok:
                        consumed.append((ncs, nce)); rem = ""; sj += 1
                    elif rem.startswith(nstok):
                        consumed.append((ncs, nce)); rem = rem[len(nstok):]; sj += 1
                    else:
                        break
                if len(consumed) >= 2:  # genuinely a glued page word (not a coincidental prefix)
                    for ccs, cce in consumed:
                        wb.append((ccs, cce, [words[pi]]))
                    all_boxes.append(words[pi]); si = sj; pi += 1
                    continue
            # Mismatch — resync rather than abandon the rest of the sentence.
            jump = None
            for k in range(1, 6):  # extra page words (footnote marks, split tokens)?
                if pi + k < n and norms[pi + k] == stok:
                    jump = ("pi", k); break
            if jump is None:
                for k in range(1, 4):  # extra sentence tokens not on the page?
                    if si + k < target and ptok == toks[si + k][2]:
                        jump = ("si", k); break
            if jump is None:
                si += 1; pi += 1; miss += 1
                if miss > max(3, target // 2):
                    break
            elif jump[0] == "pi":
                pi += jump[1]
            else:
                si += jump[1]
        matched = len(wb)
        span = pi - start  # page words consumed: a tight run ≈ target, a leaky one is larger
        if (best is None
                or matched > best[0]
                or (matched == best[0] and span < best[1])
                or (matched == best[0] and span == best[1] and miss < best[2])):
            best = (matched, span, miss, wb, all_boxes)
            if matched == target and span == target:  # perfectly contiguous — can't do better
                break

    if not best or best[0] < max(2, target // 3):
        return [], []

    _, _, _, wb, all_boxes = best
    line_rects = _merge_lines(all_boxes)
    word_boxes = []
    for cs, ce, tb in wb:
        if not tb:
            continue
        x0 = min(b[0] for b in tb); y0 = min(b[1] for b in tb)
        x1 = max(b[2] for b in tb); y1 = max(b[3] for b in tb)
        word_boxes.append({"cs": cs, "ce": ce, "box": [float(x0), float(y0), float(x1), float(y1)]})
    return line_rects, word_boxes


def sentence_rects(doc_id: int, filename: str, page_no: int, text: str) -> dict:
    key = (doc_id, page_no, text)
    with _lock:
        if key in _cache:
            return _cache[key]

    result = {"page": page_no, "width": 0.0, "height": 0.0, "rotation": 0, "rects": [], "words": []}
    try:
        doc = fitz.open(str(UPLOAD_DIR / filename))
        try:
            page = doc.load_page(page_no - 1)
            result["width"] = page.rect.width
            result["height"] = page.rect.height
            result["rotation"] = page.rotation
            words = page.get_text("words")
            rects, wbs = _match(words, text)
            if not rects:  # fallback to a plain text search
                r = page.search_for(" ".join(text.split()))
                rects = [[x.x0, x.y0, x.x1, x.y1] for x in r[:24]] if r else []
            result["rects"] = rects
            result["words"] = wbs
        finally:
            doc.close()
    except Exception:
        pass

    with _lock:
        _cache[key] = result
    return result


def _hit_map(doc_id: int, filename: str, page_no: int, sentences):
    """[(x0,y0,x1,y1, idx)] for every sentence's lines on the page (cached)."""
    key = (doc_id, page_no)
    with _lock:
        if key in _hit_cache:
            return _hit_cache[key]
    entries = []
    try:
        doc = fitz.open(str(UPLOAD_DIR / filename))
        try:
            page = doc.load_page(page_no - 1)
            words = page.get_text("words")
            for idx, text in sentences:
                rects, _ = _match(words, text)
                for r in rects:
                    entries.append((r[0], r[1], r[2], r[3], idx))
        finally:
            doc.close()
    except Exception:
        pass
    with _lock:
        _hit_cache[key] = entries
    return entries


def hit_at(doc_id: int, filename: str, page_no: int, sentences, x: float, y: float):
    """Return the sentence idx whose rect contains (x, y), else the nearest."""
    entries = _hit_map(doc_id, filename, page_no, sentences)
    if not entries:
        return None
    best_idx, best_dist = None, 1e18
    for x0, y0, x1, y1, idx in entries:
        if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
            return idx
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d = (cx - x) ** 2 + (cy - y) ** 2
        if d < best_dist:
            best_dist, best_idx = d, idx
    return best_idx
