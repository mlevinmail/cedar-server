#!/usr/bin/env bash
# Fill the book store with public-domain classics from Project Gutenberg.
#
#   docker compose run --rm classics                # the 1,000 most-read (~1 GB, tens of minutes)
#   docker compose run --rm classics --top 5000
#   docker compose run --rm classics --all          # the whole corpus (~27 GB, hours)
#   docker compose run --rm classics --cutoff 1955  # life+70 countries (default 1971, life+50)
#
# Without Docker: tools/gutenberg/get-classics.sh --out <data>/gutenberg
# (needs python3, curl and rsync; Pillow too if watermarked covers are to be retired).
#
# Safe to run again: it fetches only what is missing, so a bigger --top, an
# interrupted --all, or a later --refresh of the catalog all pick up where the
# last run stopped. The server needs no restart; the store tab appears in the
# app the next time it opens.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${CEDAR_DATA:-/data}/gutenberg"
TOP=1000
ALL=0
REFRESH=0
CUTOFF=""
FEEDS="https://www.gutenberg.org/cache/epub/feeds"
MIRROR="aleph.gutenberg.org::gutenberg-epub"
JOBS=4   # parallel connections to the mirror; polite, and ~4x a single one

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# A feed file, resumable: an interrupted download continues, a finished one is kept.
fetch() {
    [ -f "$1" ] && return 0
    curl -fL --retry 3 -C - -o "$1.part" "$FEEDS/$1"
    mv "$1.part" "$1"
}

# rsync from the mirror, over a few connections at once: the files are small
# and each costs a round trip, so one connection spends its time waiting.
# Exit 23/24 = some listed files are not on the mirror, which is normal (not
# every book has a cover); anything else is the network.
mirror() {
    local rc=0 c pid pids=()
    rm -f .fetch.*
    awk -v n="$JOBS" '{ print > (".fetch." NR % n) }' "$1"
    for part in .fetch.*; do
        rsync -a --timeout=120 --ignore-missing-args --files-from="$part" "$MIRROR" raw/ &
        pids+=("$!")
    done
    for pid in "${pids[@]}"; do
        c=0; wait "$pid" || c=$?
        case "$c" in 0|23|24) ;; *) rc=$c ;; esac
    done
    rm -f .fetch.*
    if [ "$rc" != 0 ]; then
        echo "rsync stopped (exit $rc) — run the same command again to continue." >&2
        exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --top)     TOP="${2:?--top needs a number}"; shift 2 ;;
        --all)     ALL=1; shift ;;
        --cutoff)  CUTOFF="${2:?--cutoff needs a year}"; shift 2 ;;
        --refresh) REFRESH=1; shift ;;
        --out)     OUT="${2:?--out needs a directory}"; shift 2 ;;
        -h|--help) usage ;;
        *)         echo "unknown option: $1" >&2; usage 2 ;;
    esac
done
case "$TOP" in ''|*[!0-9]*) echo "--top needs a number" >&2; exit 2 ;; esac
case "$CUTOFF" in *[!0-9]*) echo "--cutoff needs a year" >&2; exit 2 ;; esac

for tool in python3 curl rsync; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
if ! mkdir -p "$OUT/raw" 2>/dev/null || [ ! -w "$OUT" ]; then
    echo "cannot write to $OUT — start the server once first (docker compose up -d)," >&2
    echo "so the data volume exists and belongs to the cedar user." >&2
    exit 1
fi
cd "$OUT"

# Room for what is about to land: the text bundle is ~11 GB and unpacks to ~27 GB.
if [ "$ALL" = 1 ]; then
    free_gb=$(( $(df -Pk . | awk 'NR==2 {print $4}') / 1048576 ))
    if [ "$free_gb" -lt 40 ]; then
        echo "--all needs about 40 GB free here; $OUT has ${free_gb} GB. Try --top 5000." >&2
        exit 1
    fi
fi

# 1. The catalog: every book's metadata, filtered and curated (see gutenberg_ingest.py).
#    Built once; rebuilt on --refresh or when the copyright cutoff changes.
[ -n "$CUTOFF" ] || CUTOFF="$(cat .cutoff 2>/dev/null || echo 1971)"
if [ ! -f catalog.db ] || [ "$REFRESH" = 1 ] || [ "$CUTOFF" != "$(cat .cutoff 2>/dev/null || echo 1971)" ]; then
    echo "==> catalog (authors who died in or before $CUTOFF)"
    fetch rdf-files.tar.bz2
    python3 "$HERE/gutenberg_ingest.py" --rdf rdf-files.tar.bz2 --out . --cutoff "$CUTOFF"
    echo "$CUTOFF" > .cutoff
    rm -f rdf-files.tar.bz2
    # A rebuilt catalog starts with nothing marked as on disk.
    python3 "$HERE/gutenberg_ingest.py" --mark-files --out . >/dev/null
fi

# 2. The books.
if [ "$ALL" = 1 ]; then
    echo "==> every text, in one bundle (~11 GB)"
    fetch txt-files.tar.zip
    python3 "$HERE/gutenberg_extract_txt.py" --bundle txt-files.tar.zip --out .
    rm -f txt-files.tar.zip
    echo "==> covers"
    # Only the ones not here yet; a retired auto-cover counts as here.
    while read -r f; do
        g="${f%%/*}"
        [ -e "raw/$f" ] || [ -e "raw/$g/pg$g.cover.generated.jpg" ] || echo "$f"
    done < covers.list > covers.need
    if [ -s covers.need ]; then mirror covers.need; fi
    rm -f covers.need
else
    echo "==> the $TOP most-read books"
    python3 "$HERE/gutenberg_ingest.py" --top "$TOP" --out .
    if [ -s top.list ]; then mirror top.list; fi
    rm -f top.list
fi

# 3. Record what landed, and retire Project Gutenberg's watermarked auto-covers
#    (the texts are public domain; the trademark is not).
python3 "$HERE/gutenberg_ingest.py" --mark-files --out .
if python3 -c "import PIL" 2>/dev/null; then
    python3 -W ignore::DeprecationWarning "$HERE/gutenberg_debrand_covers.py" --out . --apply \
        | grep -E "covers in catalog|applied" || true
else
    echo "Pillow not installed: auto-generated covers kept (pip install pillow, then run again)."
fi
echo "Done. Open the Cedar app: the book store is there."
