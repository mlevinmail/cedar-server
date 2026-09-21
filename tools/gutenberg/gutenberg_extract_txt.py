"""Extract catalog books' plaintext from PG's txt-files.tar.zip feed.

Companion to gutenberg_ingest.py, run on the corpus server:

    python3 gutenberg_extract_txt.py --bundle txt-files.tar.zip --out .

The feed bundles every ebook's generated plaintext (11+ GB) — one HTTP
download instead of ~47k rsync round-trips. This streams the inner tar
without extracting the archive to disk, writes only books present in
catalog.db, and normalizes names to raw/{gid}/pg{gid}.txt (what the app
serves). Existing files are skipped, so it composes with a partial rsync.
Run gutenberg_ingest.py --mark-files afterwards.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import tarfile
import time
import zipfile
from pathlib import Path

_MEMBER_GID = re.compile(r"(?:^|/)(\d+)/pg\1(?:\.txt(?:\.utf-?8)?)$")


def extract(bundle: Path, out_dir: Path) -> None:
    con = sqlite3.connect(out_dir / "catalog.db")
    wanted = {gid for (gid,) in con.execute("SELECT gid FROM books")}
    con.close()
    raw = out_dir / "raw"

    written = skipped = ignored = 0
    t0 = time.time()
    zf = zipfile.ZipFile(bundle)
    names = zf.namelist()
    tars = [n for n in names if n.endswith(".tar")]
    inner = tars[0] if tars else None

    def walk(tf: tarfile.TarFile) -> None:
        nonlocal written, skipped, ignored
        for m in tf:
            if not m.isfile():
                continue
            g = _MEMBER_GID.search(m.name)
            if not g or int(g.group(1)) not in wanted:
                ignored += 1
                continue
            gid = int(g.group(1))
            dest = raw / str(gid) / f"pg{gid}.txt"
            if dest.exists():
                skipped += 1
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            with tmp.open("wb") as w:
                while chunk := fh.read(1 << 20):
                    w.write(chunk)
            tmp.replace(dest)
            written += 1
            if written % 2000 == 0:
                print(f"  …{written} written ({time.time()-t0:.0f}s)", flush=True)

    if inner:
        with zf.open(inner) as f, tarfile.open(fileobj=f, mode="r|") as tf:
            walk(tf)
    else:
        # No inner tar: the zip holds the txt files directly.
        for n in names:
            g = _MEMBER_GID.search(n)
            if not g or int(g.group(1)) not in wanted:
                ignored += 1
                continue
            gid = int(g.group(1))
            dest = raw / str(gid) / f"pg{gid}.txt"
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            with zf.open(n) as fh, tmp.open("wb") as w:
                while chunk := fh.read(1 << 20):
                    w.write(chunk)
            tmp.replace(dest)
            written += 1
            if written % 2000 == 0:
                print(f"  …{written} written ({time.time()-t0:.0f}s)", flush=True)

    print(f"Done in {time.time()-t0:.0f}s: {written} written, "
          f"{skipped} already present, {ignored} not in catalog.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    extract(args.bundle, args.out)
