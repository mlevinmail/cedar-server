"""Retire Project Gutenberg's auto-generated cover images from the catalog.

Companion to gutenberg_ingest.py, run on the corpus server (needs Pillow):

    python3 gutenberg_debrand_covers.py --out .            # report only
    python3 gutenberg_debrand_covers.py --out . --apply    # rename + update db

PG generates typographic covers for books that never had cover art, and the
newer generator stamps a "Project Gutenberg" watermark into the image — which
the store must not display (the texts are public domain; the trademark isn't).
There is no metadata flag distinguishing generated covers from scanned
originals, but generated ones are synthetic flat-color graphics while scans
are photographs, so a color-population test separates them cleanly:

  * generated covers open with a near-pure-white title band; scanned covers
    never do — even cream/white book jackets photograph as off-white paper
    (measured: generated ≥ 0.72 near-white in the top fifth, scans ≤ 0.003);
  * as a backstop, the synthetic art below is flat-colored, so few coarse
    RGB buckets cover most pixels, where photographs need dozens.

Old-template generated covers (no watermark) match too — that's fine, we'd
rather show the app's own placeholder than any auto-generated cover.

--apply renames matches to *.generated.jpg (bytes kept for reversibility) and
sets has_cover=0 so the app renders its placeholder instead.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from PIL import Image

# Fraction of near-white pixels in the top fifth that marks a title band
# (measured: generated covers ≥ 0.72, scans ≤ 0.003 — huge margin).
WHITE_BAND_MIN = 0.35
# Backstop: fewest 16-level RGB buckets covering 85% of pixels. Synthetic art
# measures ≤ 7; photographic scans of pale covers still spread wider.
FLAT_BUCKETS_MAX = 14


def looks_generated(path: Path) -> bool:
    with Image.open(path) as im:
        im = im.convert("RGB").resize((100, 150))
        px = list(im.getdata())

    top = px[: 100 * 30]  # top fifth of the 100x150 thumbnail
    white = sum(1 for r, g, b in top if min(r, g, b) > 235)
    if white / len(top) < WHITE_BAND_MIN:
        return False

    counts: dict[tuple, int] = {}
    for r, g, b in px:
        k = (r >> 4, g >> 4, b >> 4)
        counts[k] = counts.get(k, 0) + 1
    need, covered, buckets = 0.85 * len(px), 0, 0
    for n in sorted(counts.values(), reverse=True):
        covered += n
        buckets += 1
        if covered >= need:
            break
    return buckets <= FLAT_BUCKETS_MAX


def main(out_dir: Path, apply: bool) -> None:
    raw = out_dir / "raw"
    con = sqlite3.connect(out_dir / "catalog.db")
    gids = [g for (g,) in con.execute("SELECT gid FROM books WHERE has_cover=1")]
    generated, kept, missing, errors = [], 0, 0, 0
    for gid in gids:
        p = raw / str(gid) / f"pg{gid}.cover.medium.jpg"
        if not p.exists():
            missing += 1
            continue
        try:
            if looks_generated(p):
                generated.append(gid)
            else:
                kept += 1
        except Exception:
            errors += 1
    print(f"{len(gids)} covers in catalog: {kept} scans kept, "
          f"{len(generated)} generated, {missing} not on disk, {errors} unreadable.")
    print("sample generated:", generated[:15])

    if apply and generated:
        for gid in generated:
            p = raw / str(gid) / f"pg{gid}.cover.medium.jpg"
            p.rename(p.with_name(f"pg{gid}.cover.generated.jpg"))
        con.executemany("UPDATE books SET has_cover=0 WHERE gid=?",
                        [(g,) for g in generated])
        con.commit()
        print(f"applied: {len(generated)} covers retired (renamed *.generated.jpg).")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main(args.out, args.apply)
