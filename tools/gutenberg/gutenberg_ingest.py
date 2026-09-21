"""Build the Gutenberg catalog database from Project Gutenberg's RDF dump.

Operator tool, run on the server that hosts the corpus (stdlib only):

    python3 gutenberg_ingest.py --rdf rdf-files.tar.bz2 --out /path/to/data/gutenberg

Reads the official metadata dump (cache/epub/feeds/rdf-files.tar.bz2) straight
from the tar — no extraction — and writes:

  * catalog.db     — SQLite: one row per book + an FTS5 search index
  * texts.list     — rsync --files-from list of plaintext files to mirror
  * covers.list    — rsync --files-from list of cover images to mirror

Two kinds of rule decide what ends up where:

HARD rules — a book failing one is not in catalog.db at all:

  * Text works only, marked "Public domain in the USA" by PG.
  * Languages Cedar's TTS actually speaks: en, fr, es, it, pt, hi.
  * Has a plaintext file in PG's generated collection.

CURATION rules — a book failing one is still a row, with ``curated=0`` and the
reason in ``excluded``. The store lists only ``curated=1`` books by default;
the admin switch "Serve the full catalog" (app setting ``catalog_full``, see
catalog.py) lifts that and serves every row that has text on disk:

  * Copyright-safe in Canada: life+50, non-retroactive — the 2022 CUSMA
    extension to life+70 did not restore works already public domain, so the
    line is authors who died on or before 1971-12-31. EVERY person attached to
    the ebook (author, translator, editor, illustrator, …) must have a death
    year <= 1971. A missing death year only passes when the birth year is
    <= 1850 (nobody born then was alive in 1972). Works with no attributable
    people at all ("Various", "Anonymous") can't be verified, so they're
    held back too.
  * Not on the explicit-content list (EXPLICIT_META / EXPLICIT_WORKS).
  * Not on the racist-ideology list (RACIST_WORKS).

The rsync lists cover every row, curated or not — which books to actually
mirror is the operator's call (a curated-only mirror simply leaves the rest
with has_text=0, and the switch then changes nothing).

After the corpus rsync completes, re-run with --mark-files to record which
texts/covers actually landed on disk (has_text/has_cover become disk truth).

get-classics.sh, next to this file, runs the whole sequence in one command.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

NS_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
NS_DC = "{http://purl.org/dc/terms/}"
NS_PG = "{http://www.gutenberg.org/2009/pgterms/}"

CANADA_DEATH_CUTOFF = 1971   # died <= this year → public domain in Canada
SAFE_BIRTH_CUTOFF = 1850     # born <= this year → certainly dead by 1971
LANGUAGES = {"en", "fr", "es", "it", "pt", "hi"}

# Subject/bookshelf keyword → store genre. First match wins within a rule; a
# book collects every genre whose keywords appear. Order matters only for
# display priority (the API shows genres[0] as the primary chip).
GENRE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Mystery & Crime", ("detective", "mystery", "crime")),
    ("Science Fiction", ("science fiction",)),
    ("Fantasy", ("fantasy",)),
    ("Horror & Gothic", ("horror", "ghost", "gothic", "supernatural")),
    ("Adventure", ("adventure",)),
    ("Romance", ("love stories", "romance")),
    ("Children's", ("juvenile", "children", "fairy tales", "nursery")),
    ("Poetry", ("poetry", "poems", "verse")),
    ("Drama", ("drama", "plays", "tragedies", "comedies")),
    ("Philosophy", ("philosophy", "ethics", "logic")),
    ("History", ("history", "historical", "world war", "civil war")),
    ("Biography", ("biography", "autobiography", "memoir", "diaries", "correspondence")),
    ("Humor", ("humor", "humour", "satire", "wit and humor", "parodies")),
    ("Travel", ("travel", "voyages", "description and travel", "exploration")),
    ("Religion", ("religion", "bible", "christian", "theology", "hymns", "sermons")),
    ("Science & Nature", ("science", "natural history", "mathematics", "physics",
                          "astronomy", "biology", "botany", "zoology", "chemistry",
                          "geology", "evolution")),
    ("Myths & Folklore", ("mythology", "folklore", "legends", "folk tales", "epic")),
    ("Short Stories", ("short stories",)),
    ("Essays", ("essays",)),
    ("Westerns", ("western stories", "west (u.s.)")),
    ("War & Military", ("war stories", "military", "naval")),
    ("Fiction", ("fiction",)),  # broad fallback, matched last
]

# Explicit-content exclusions. The store's App Store age rating is declared with
# no sexual content and no medical/treatment information, so the catalog has to
# carry neither. PG's own shelving catches most of it — "Sexuality & Erotica",
# "Erotic Fiction", and the LCSH "Erotic fiction" subject — which also sweeps in
# the sexology shelf (Havelock Ellis, Freud, the sex-hygiene tracts).
#
# Deliberately matched on "erotic" only, NOT "sexuality": PG shelves Herland,
# Moll Flanders and Mrs. Warren's Profession under "Gender & Sexuality Studies",
# and those are ordinary classics.
EXPLICIT_META = ("erotic",)

# Works PG files under folklore, biography or plain Classics, so EXPLICIT_META
# never sees them. (title substring, author substring) — both matched lowercased,
# an empty string matches anything. The author half matters: only the
# unexpurgated Nights translations are excluded, while Lane's, Lang's, Scott's
# and Forster's bowdlerized Victorian editions stay in the store.
EXPLICIT_WORKS: tuple[tuple[str, str], ...] = (
    ("thousand nights and a night", "burton"),
    ("supplemental nights", "burton"),
    ("thousand nights and one night", "payne"),   # Burton translated from Payne
    ("memoirs", "casanova"),
    ("decameron", "boccaccio"),                   # not his Dante commentary
    ("", "petronius"),                            # the Satyricon, both editions
    ("tales of the heptameron", ""),
    ("ulysses", "joyce"),                         # not his other work
)


def _is_explicit(title: str, authors: str, hay: str) -> bool:
    if any(k in hay for k in EXPLICIT_META):
        return True
    t, a = title.lower(), authors.lower()
    return any(ts in t and as_ in a for ts, as_ in EXPLICIT_WORKS)


# Racist-ideology exclusions. Unrelated to the age rating — this is about what
# the store puts its name on, and about App Review 1.1.1 ("defamatory,
# discriminatory, or mean-spirited content ... about religion, race, ...
# national/ethnic origin", whose only stated exemption is political satire).
#
# The line drawn here is ADVOCACY, NOT VOCABULARY. Almost every book in a
# pre-1972 corpus carries period racial language: a full-text scan of the
# 47k-book corpus found slurs spread across thousands of titles. Excluding on
# that basis would empty the shelves, and — worse — would take the anti-racist
# record out first, because Douglass, Wells-Barnett, Northup, Du Bois, Stowe
# and Chesnutt use those words far more densely than the propaganda does. So a
# book is listed here only when the work exists to argue that a race is
# inferior, or to celebrate the violence done to it.
#
# Kept deliberately, and NOT to be "fixed" by a later keyword sweep:
#   * slave narratives and Black-authored writing — Twelve Years a Slave,
#     Up from Slavery, Darkwater, Our Nig (a slur in the title, by the first
#     Black woman to publish a novel in the US), Chesnutt's colour-line stories;
#   * anti-lynching and anti-Klan work — Wells-Barnett's pamphlets, Fry's and
#     Cook's Klan exposés, Pierson's letter to Sumner documenting outrages on
#     freedmen, Coleman's The Jim Crow Car, Levinger and Davitt on antisemitism;
#   * canonical fiction that depicts racism rather than advocating it —
#     Huckleberry Finn, Pudd'nhead Wilson, Conrad, A Passage to India.
#
# Matched on PG ebook id: stable across dumps, and exact where a title/author
# substring would be brittle or would over-reach onto an innocent namesake.
RACIST_WORKS: dict[int, str] = {
    # Thomas Dixon Jr. — the Klan trilogy (The Leopard's Spots, The Clansman,
    # The Traitor) is the source of Birth of a Nation. Excluded as an author,
    # not work by work: the non-racial novels carry the same byline, and the
    # byline is the problem.
    26240: "Dixon — The Clansman",
    54765: "Dixon — The Leopard's Spots",
    54766: "Dixon — The Traitor",
    36666: "Dixon — The Sins of the Father",
    48089: "Dixon — The Fall of a Nation",
    8462: "Dixon — The Man in Gray",
    35447: "Dixon — Comrades",
    1634: "Dixon — The Foolish Virgin",
    24093: "Dixon — The Root of Evil",
    6037: "Dixon — The One Woman",
    25814: "Dixon — A Man of the People",
    76399: "Dixon — The Way of a Man",
    # Scientific racism.
    68185: "Madison Grant — The Passing of the Great Race",
    37408: "Lothrop Stoddard — The Rising Tide of Color",
    # Antisemitic forgery. Bernstein's and Wolf's debunkings of it stay.
    64977: "Nilus — The Protocols and World Revolution",
    # Pro-slavery tracts (PG's own "Slavery -- Justification" heading).
    35481: "Fitzhugh — Cannibals All!",
    47422: "Dabney — A Defence of Virginia",
    28148: "Elliott — Cotton is King",
    9171: "Ross — Slavery Ordained of God",
    61063: "Van Evrie — Negroes and Negro Slavery",
    25277: "Hoit — The Right of American Slavery",
    # Klan-sympathetic accounts. Fry (34478) and Cook (35976) are exposés — kept.
    35771: "Damer — When the Ku Klux Rode",
    59868: "Brown — Harold the Klansman",
    # Lynching apologia. Wells-Barnett's pamphlets on the same shelf are kept.
    67193: "Collins — The Truth About Lynching and the Negro in the South",
    # Blackface: the pickaninny picture books, and a celebratory who's-who of
    # minstrel performers.
    1330: "Bannerman — Little Black Sambo / Little Black Mingo",
    17824: "Bannerman — Little Black Sambo",
    69826: "Rice — Monarchs of Minstrelsy",
    # Edgar Wallace's Sanders of the River cycle — colonial-administrator
    # adventure built on African savagery. His crime novels are unaffected.
    35545: "Wallace — Sanders of the River",
    49657: "Wallace — Bosambo of the River",
    24450: "Wallace — Bones",
    25803: "Wallace — The Keepers of the King's Peace",
    # Found by slur density rather than by subject heading: works whose entire
    # entertainment value IS the racial caricature. Not advocacy, so a separate
    # category — but a store cannot shelve them either.
    #
    # The same density pass is why Elliott Blaine Henderson's Plantation Echoes
    # is NOT here despite ranking high: PG shelves it "American poetry --
    # African American authors". He was a Black dialect poet writing his own
    # community, as was Chesnutt (The Conjure Woman, the single densest
    # non-raccoon hit in the corpus). Check the author before adding a line.
    59121: "E. K. Means — comic dialect caricature",
    59476: "E. K. Means — More",
    61149: "E. K. Means — Further",
    4992: "Pyrnelle — Diddie, Dumps and Tot (plantation nostalgia, juvenile)",
    17146: "Pyrnelle — Diddie, Dumps & Tot (2nd ed.)",
    71168: "Robert E. Howard — Black Canaan",
}


def _year(el: ET.Element | None) -> int | None:
    """Parse a pgterms birth/death date element to a year (negative = BCE)."""
    if el is None or el.text is None:
        return None
    m = re.match(r"^\s*(-?\d+)", el.text)
    return int(m.group(1)) if m else None


def _flip_name(name: str) -> str:
    """'Austen, Jane' -> 'Jane Austen' for display. Drops PG's parenthetical
    expansions ('Chambers, Robert W. (Robert William)' -> 'Robert W. Chambers').
    Names with extra commas or none pass through."""
    name = re.sub(r"\s*\([^)]*\)", "", name).strip().rstrip(",")
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2 and parts[1] and not parts[1][0].isdigit():
        return f"{parts[1]} {parts[0]}"
    return name


def _norm_title(title: str) -> str:
    """PG multi-line titles are 'Title\\nSubtitle' — join for display. Some
    carry MARC subfield markers ('The bracelets : $b or, …') — drop those."""
    title = re.sub(r"\s*\$[a-z]\s*", " ", title)
    lines = [ln.strip() for ln in title.splitlines() if ln.strip()]
    return ": ".join(lines) if lines else "Untitled"


class Skip(Exception):
    """Raised with a reason-key when a book fails an inclusion rule."""


def parse_ebook(xml_bytes: bytes) -> dict:
    """One pg{id}.rdf → catalog row dict. Raises Skip(reason) when excluded."""
    # stdlib ET is fine here ONLY because we refuse DTDs outright: no DOCTYPE
    # means no external entities (XXE) and no entity-expansion bombs. PG's RDF
    # never carries a DTD, so nothing legitimate is lost.
    if b"<!DOCTYPE" in xml_bytes[:2048] or b"<!ENTITY" in xml_bytes:
        raise Skip("dtd_rejected")
    root = ET.fromstring(xml_bytes)
    ebook = root.find(f"{NS_PG}ebook")
    if ebook is None:
        raise Skip("no_ebook")
    about = ebook.get(f"{NS_RDF}about", "")   # "ebooks/1342"
    m = re.search(r"(\d+)$", about)
    if not m:
        raise Skip("no_id")
    gid = int(m.group(1))

    typ = ebook.find(f"{NS_DC}type/{NS_RDF}Description/{NS_RDF}value")
    if typ is None or (typ.text or "").strip() != "Text":
        raise Skip("not_text")

    rights = (ebook.findtext(f"{NS_DC}rights") or "").strip()
    if not rights.lower().startswith("public domain"):
        raise Skip("not_pd_usa")

    langs = [(v.text or "").strip()
             for v in ebook.findall(f"{NS_DC}language/{NS_RDF}Description/{NS_RDF}value")]
    langs = [l for l in langs if l]
    if not langs or langs[0] not in LANGUAGES:
        raise Skip("language")

    # Curation rules from here on only mark the row (curated=0 + reason); the
    # book stays in the catalog for the full-catalog switch.
    excluded: list[str] = []
    if gid in RACIST_WORKS:
        excluded.append("racist_ideology")

    # --- the Canada rule: every attached person must be verifiably PD --------
    agents = ebook.findall(f".//{NS_PG}agent")
    if not agents:
        excluded.append("no_agents")
    people: list[tuple[str, int | None, int | None]] = []
    for a in agents:
        name = (a.findtext(f"{NS_PG}name") or "").strip()
        birth = _year(a.find(f"{NS_PG}birthdate"))
        death = _year(a.find(f"{NS_PG}deathdate"))
        if death is not None:
            if death > CANADA_DEATH_CUTOFF:
                excluded.append("death_too_recent")
        elif birth is None or birth > SAFE_BIRTH_CUTOFF:
            excluded.append("unverifiable_person")
        people.append((name, birth, death))

    title = _norm_title(ebook.findtext(f"{NS_DC}title") or "")

    # Authors for display: the dcterms:creator agents specifically (the rest —
    # translators, editors — go to authors_full so the detail page can show them).
    creators = []
    for c in ebook.findall(f"{NS_DC}creator/{NS_PG}agent"):
        n = (c.findtext(f"{NS_PG}name") or "").strip()
        if n:
            creators.append(_flip_name(n))
    display_authors = ", ".join(creators) if creators else (
        _flip_name(people[0][0]) if people else "Unknown")

    def _span(b, d):
        if b is None and d is None:
            return ""
        fmt = lambda y: f"{abs(y)} BCE" if y is not None and y < 0 else (str(y) if y is not None else "?")
        return f" ({fmt(b)}–{fmt(d)})"

    authors_full = "; ".join(f"{_flip_name(n)}{_span(b, d)}" for n, b, d in people)

    subjects = []
    for s in ebook.findall(f"{NS_DC}subject/{NS_RDF}Description/{NS_RDF}value"):
        v = (s.text or "").strip()
        # LCSH headings read like "England -- Social life -- Fiction"; LCC codes
        # are bare letter codes ("PR") — skip those.
        if v and not re.fullmatch(r"[A-Z]{1,3}", v):
            subjects.append(v)

    shelves = []
    for s in ebook.findall(f"{NS_PG}bookshelf/{NS_RDF}Description/{NS_RDF}value"):
        v = (s.text or "").strip()
        shelves.append(v.removeprefix("Category: ").strip() if v else v)

    hay = " | ".join(subjects + shelves).lower()
    # Both author strings, so a work still matches when PG credits the
    # translator as editor rather than creator (Burton, Payne).
    if _is_explicit(title, f"{display_authors} {authors_full}", hay):
        excluded.append("explicit")
    genres = [g for g, keys in GENRE_RULES if any(k in hay for k in keys)]

    downloads = 0
    dl = ebook.find(f"{NS_PG}downloads")
    if dl is not None and dl.text and dl.text.strip().isdigit():
        downloads = int(dl.text.strip())

    # PG ships human/AI-written summaries in marc520; keep the first, minus the
    # boilerplate sentence it always ends with.
    desc = (ebook.findtext(f"{NS_PG}marc520") or ebook.findtext(f"{NS_DC}description") or "").strip()
    desc = re.sub(r"\s*\(This is an automatically generated summary\.?\)\s*$", "", desc)

    # File inventory: does the generated collection have plaintext + a cover,
    # and how big is the text (word-count estimate for the "~9 h" label)?
    text_bytes, has_text, has_cover = 0, 0, 0
    for f in ebook.findall(f"{NS_DC}hasFormat/{NS_PG}file"):
        url = f.get(f"{NS_RDF}about", "")
        if re.search(r"\.txt(\.utf-?8)?$", url):
            has_text = 1
            ext = f.findtext(f"{NS_DC}extent")
            if ext and ext.strip().isdigit():
                text_bytes = max(text_bytes, int(ext.strip()))
        elif url.endswith("cover.medium.jpg"):
            has_cover = 1
    if not has_text:
        raise Skip("no_plaintext")

    reasons = list(dict.fromkeys(excluded))  # dedupe, keep first-seen order
    return {
        "gid": gid, "title": title, "authors": display_authors,
        "authors_full": authors_full, "language": langs[0],
        "downloads": downloads, "subjects": "; ".join(subjects),
        "shelves": "; ".join(shelves), "genres": ", ".join(genres),
        "description": desc, "text_bytes": text_bytes, "has_cover": has_cover,
        "curated": 0 if reasons else 1, "excluded": ", ".join(reasons),
    }


def build(rdf_path: Path, out_dir: Path, limit: int | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "catalog.db"
    tmp_path = out_dir / "catalog.db.building"
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    con.executescript(
        """
        CREATE TABLE books (
            gid          INTEGER PRIMARY KEY,
            title        TEXT NOT NULL,
            authors      TEXT NOT NULL,
            authors_full TEXT NOT NULL,
            language     TEXT NOT NULL,
            downloads    INTEGER NOT NULL DEFAULT 0,
            subjects     TEXT NOT NULL DEFAULT '',
            shelves      TEXT NOT NULL DEFAULT '',
            genres       TEXT NOT NULL DEFAULT '',
            description  TEXT NOT NULL DEFAULT '',
            text_bytes   INTEGER NOT NULL DEFAULT 0,
            has_cover    INTEGER NOT NULL DEFAULT 0,
            has_text     INTEGER NOT NULL DEFAULT 0,
            curated      INTEGER NOT NULL DEFAULT 1,
            excluded     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX books_downloads ON books(downloads DESC);
        CREATE INDEX books_language ON books(language);
        CREATE INDEX books_curated ON books(curated);
        CREATE VIRTUAL TABLE books_fts USING fts5(
            title, authors, subjects, content='books', content_rowid='gid'
        );
        """
    )

    skips: dict[str, int] = {}
    held: dict[str, int] = {}
    kept = seen = curated = 0
    t0 = time.time()
    with tarfile.open(rdf_path, "r:bz2") as tar:
        for member in tar:
            if not member.name.endswith(".rdf"):
                continue
            seen += 1
            if limit and kept >= limit:
                break
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                row = parse_ebook(fh.read())
            except Skip as e:
                skips[e.args[0]] = skips.get(e.args[0], 0) + 1
                continue
            except ET.ParseError:
                skips["xml_error"] = skips.get("xml_error", 0) + 1
                continue
            con.execute(
                """INSERT OR REPLACE INTO books
                   (gid,title,authors,authors_full,language,downloads,subjects,
                    shelves,genres,description,text_bytes,has_cover,has_text,
                    curated,excluded)
                   VALUES (:gid,:title,:authors,:authors_full,:language,:downloads,
                           :subjects,:shelves,:genres,:description,:text_bytes,
                           :has_cover,0,:curated,:excluded)""",
                row,
            )
            kept += 1
            if row["curated"]:
                curated += 1
            else:
                for reason in row["excluded"].split(", "):
                    held[reason] = held.get(reason, 0) + 1
            if seen % 5000 == 0:
                con.commit()
                print(f"  …{seen} scanned, {kept} kept ({time.time()-t0:.0f}s)", flush=True)

    con.execute("INSERT INTO books_fts(books_fts) VALUES('rebuild')")
    con.commit()

    with (out_dir / "texts.list").open("w") as tf, (out_dir / "covers.list").open("w") as cf:
        for (gid, has_cover) in con.execute("SELECT gid, has_cover FROM books ORDER BY gid"):
            tf.write(f"{gid}/pg{gid}.txt\n")
            if has_cover:
                cf.write(f"{gid}/pg{gid}.cover.medium.jpg\n")
    con.close()
    tmp_path.replace(db_path)

    print(f"\nDone in {time.time()-t0:.0f}s: {kept} books in catalog of {seen} scanned — "
          f"{curated} listed by default, {kept - curated} held back "
          f"(served only with the full-catalog switch on).")
    for reason, n in sorted(held.items(), key=lambda kv: -kv[1]):
        print(f"  held back {reason}: {n}")
    for reason, n in sorted(skips.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {reason}: {n}")


def write_top_list(out_dir: Path, n: int) -> None:
    """A starter shelf instead of the whole corpus: top.list names the text and
    cover of the N most-downloaded listed books that are not on disk yet, in
    rsync --files-from form. Which books have a cover comes from covers.list
    (what PG publishes) — has_cover in the db is disk truth after --mark-files,
    so it says nothing about a book that was never fetched."""
    raw = out_dir / "raw"
    with (out_dir / "covers.list").open() as cf:
        with_cover = {int(line.split("/", 1)[0]) for line in cf if line.strip()}
    con = sqlite3.connect(out_dir / "catalog.db")
    gids = [g for (g,) in con.execute(
        "SELECT gid FROM books WHERE curated=1 ORDER BY downloads DESC, gid LIMIT ?", (n,))]
    con.close()
    lines = []
    for gid in gids:
        d = raw / str(gid)
        if not (d / f"pg{gid}.txt").exists():
            lines.append(f"{gid}/pg{gid}.txt")
        # A retired auto-generated cover (see gutenberg_debrand_covers.py) counts
        # as fetched: asking again would only download and retire it again.
        if gid in with_cover and not (d / f"pg{gid}.cover.medium.jpg").exists() \
                and not (d / f"pg{gid}.cover.generated.jpg").exists():
            lines.append(f"{gid}/pg{gid}.cover.medium.jpg")
    (out_dir / "top.list").write_text("".join(f"{x}\n" for x in lines))
    print(f"top {len(gids)} listed books: {len(lines)} files still to fetch.")


def mark_files(out_dir: Path) -> None:
    """Post-rsync: record which texts/covers actually exist on disk."""
    raw = out_dir / "raw"
    con = sqlite3.connect(out_dir / "catalog.db")
    text_n = cover_n = 0
    for (gid,) in con.execute("SELECT gid FROM books").fetchall():
        has_text = (raw / str(gid) / f"pg{gid}.txt").exists()
        has_cover = (raw / str(gid) / f"pg{gid}.cover.medium.jpg").exists()
        text_n += has_text
        cover_n += has_cover
        con.execute("UPDATE books SET has_text=?, has_cover=? WHERE gid=?",
                    (int(has_text), int(has_cover), gid))
    total = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    con.commit()
    con.close()
    print(f"{total} books: {text_n} texts, {cover_n} covers on disk.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rdf", type=Path, help="path to rdf-files.tar.bz2")
    ap.add_argument("--out", type=Path, required=True, help="gutenberg data dir")
    ap.add_argument("--limit", type=int, help="stop after N kept books (testing)")
    ap.add_argument("--mark-files", action="store_true",
                    help="post-download: set has_text/has_cover from disk")
    ap.add_argument("--cutoff", type=int, metavar="YEAR",
                    help="with --rdf: list authors who died in or before YEAR "
                         f"(default {CANADA_DEATH_CUTOFF}, life+50; life+70 countries want 1955)")
    ap.add_argument("--top", type=int, metavar="N",
                    help="write top.list: the N most-downloaded listed books, for rsync")
    args = ap.parse_args()
    if args.cutoff:
        CANADA_DEATH_CUTOFF = args.cutoff
        SAFE_BIRTH_CUTOFF = args.cutoff - 121   # nobody born by then outlived the cutoff
    if args.mark_files:
        mark_files(args.out)
    elif args.top:
        write_top_list(args.out, args.top)
    elif args.rdf:
        build(args.rdf, args.out, args.limit)
    else:
        ap.error("one of --rdf, --top or --mark-files is required")
