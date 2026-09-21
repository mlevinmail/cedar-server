"""What language a document is in, and which voice should read it aloud.

Kokoro's voices are language-specific: an English voice fed French text runs it
through English letter-to-sound rules and the result is unlistenable. Voice used
to be one account-wide setting, which cannot be right for anyone who reads in
more than one language — set it for your Spanish and every English book breaks,
set it for English and every Spanish book breaks.

So a voice is a preference **per language**. Each document records the language
it was detected to be in (once, at import), and the voice it's read with is
resolved from that:

    document's own override  ->  the user's voice for that language
                             ->  the curated default for that language
                             ->  the user's main voice

Nothing is copied onto the document, which is the point: change your Spanish
voice and every Spanish book you own changes with it, past and future. The
per-document override exists only for a deliberate cross-language pick (reading
an English text in a French voice), because that can't mean "all my French".

Detection is deliberately dependency-free and biased towards doing nothing:
non-Latin scripts are settled by codepoint, the five Latin-script languages
Kokoro speaks are separated by function-word frequency, and anything short,
mixed, or simply not one of those eight languages detects as None — which
leaves the reader on the user's main voice, as it was before any of this.
"""
from __future__ import annotations

import re

from .config import DEFAULT_VOICE

# Languages Kokoro has voices for. Everything else detects as None: pinning a
# German document to an English voice is what already happens by default, and
# claiming to have "detected" it would only be a worse-sounding lie.
LANG_LABEL = {
    "en": "English", "es": "Spanish", "fr": "French", "it": "Italian",
    "pt": "Portuguese", "hi": "Hindi", "ja": "Japanese", "zh": "Chinese",
}

# Voice id prefixes that speak each language (af_heart = [lang][gender]_[name];
# English has two accents, a = American, b = British).
_SPEAKS = {
    "en": ("a", "b"), "es": ("e",), "fr": ("f",), "it": ("i",),
    "pt": ("p",), "hi": ("h",), "ja": ("j",), "zh": ("z",),
}

# The voice a language falls back to until the user picks one for it — the best
# of what Kokoro offers in each. Fixed per language, deliberately: an earlier
# version matched the gender of whatever voice the account last used, which
# meant choosing a male Spanish voice quietly restyled your English as well.
# Languages are independent here or the promise this file makes isn't true.
_DEFAULT = {
    "en": DEFAULT_VOICE, "es": "ef_dora", "fr": "ff_siwis", "it": "if_sara",
    "pt": "pf_dora", "hi": "hf_alpha", "ja": "jf_alpha", "zh": "zf_xiaoxiao",
}

# Voices the previous release could assign to a language on its own (it picked
# by gender). Kept only so the migration can recognise one of its pins and drop
# it — a voice in this set was never actually chosen by anyone.
_ASSIGNED = {
    "en": ("af_heart", "am_michael"), "es": ("ef_dora", "em_alex"),
    "fr": ("ff_siwis",), "it": ("if_sara", "im_nicola"),
    "pt": ("pf_dora", "pm_alex"), "hi": ("hf_alpha", "hm_omega"),
    "ja": ("jf_alpha", "jm_kumo"), "zh": ("zf_xiaoxiao", "zm_yunxi"),
}

# Function words: the cheapest high-signal fingerprint there is, and unlike
# content words they show up in any subject matter. Words that are common in
# more than one of these five (de, que, no, in, come, me …) are kept out of
# every list rather than credited to all — a tie is what we want them to
# produce, and the margin test below turns a tie into "don't touch anything".
_WORDS = {
    "en": {
        "the", "of", "and", "to", "that", "is", "was", "it", "for", "with",
        "his", "be", "at", "by", "this", "had", "not", "are", "but", "from",
        "have", "they", "which", "you", "were", "her", "all", "she", "there",
        "would", "their", "we", "him", "been", "has", "when", "who", "will",
        "more", "if", "out", "said", "what", "up", "its", "about", "into",
        "than", "them", "can", "only", "other", "some", "could", "these",
        "two", "may", "then", "do", "does", "did", "should", "because",
        "through", "while", "after", "before", "where", "how", "any", "our",
        "your", "much", "must", "made", "such", "each", "over", "just",
        "like", "well", "even", "most", "many", "own", "why", "both", "off",
        "being", "very", "one", "upon", "shall", "himself", "herself",
        "myself", "us", "am", "here", "now", "them", "day", "know", "think",
    },
    "es": {
        "el", "los", "las", "del", "al", "por", "con", "para", "pero", "más",
        "sus", "este", "esta", "esto", "estos", "estas", "ese", "esa", "esos",
        "esas", "muy", "sin", "también", "ya", "todo", "todos", "toda",
        "todas", "cuando", "hasta", "entre", "después", "donde", "mientras",
        "siempre", "aunque", "era", "eran", "fue", "fueron", "ser", "está",
        "están", "había", "habían", "hay", "hacer", "puede", "tiene",
        "tienen", "dijo", "así", "ella", "él", "ellos", "ellas", "nos",
        "les", "le", "mi", "mis", "tu", "tus", "nuestro", "algo", "nada",
        "otro", "otra", "otros", "otras", "mismo", "misma", "cada", "poco",
        "mucho", "mucha", "bien", "aquí", "allí", "ahora", "entonces", "sí",
        "qué", "quien", "cual", "señor", "usted", "ustedes", "nosotros",
        "vez", "veces", "años", "porque", "sobre", "desde", "hacia",
    },
    "fr": {
        "le", "les", "des", "du", "et", "à", "une", "qui", "dans", "pour",
        "ce", "cette", "ces", "il", "elle", "ils", "elles", "ne", "pas",
        "plus", "sur", "par", "avec", "tout", "tous", "toute", "toutes",
        "mais", "nous", "vous", "comme", "ou", "leur", "leurs", "son", "sa",
        "ses", "est", "sont", "était", "étaient", "être", "avoir", "avait",
        "avaient", "faire", "dit", "aussi", "encore", "même", "très", "où",
        "deux", "alors", "quand", "après", "avant", "sans", "sous", "entre",
        "chez", "peu", "cela", "ceux", "celui", "celle", "moi", "toi", "lui",
        "au", "aux", "qu", "quelque", "quelques", "bien", "ainsi", "donc",
        "jamais", "toujours", "fut", "furent", "j'ai", "c'est", "n'est",
        "monsieur", "madame", "chose", "faut", "ils", "vers", "depuis",
    },
    "it": {
        "il", "lo", "gli", "un", "uno", "che", "è", "per", "non", "con",
        "su", "da", "se", "ma", "si", "del", "dello", "della", "dei",
        "degli", "delle", "nel", "nella", "nei", "negli", "alla", "allo",
        "agli", "alle", "sono", "erano", "hanno", "ho", "essere", "avere",
        "più", "anche", "quando", "tutto", "tutti", "tutta", "questo",
        "questa", "quello", "quella", "perché", "molto", "cosa", "dove",
        "ancora", "dopo", "prima", "senza", "sotto", "tra", "fra", "mi",
        "ti", "ci", "vi", "loro", "suo", "sua", "poi", "così", "già",
        "ogni", "mentre", "allora", "ecco", "fu", "furono", "disse",
        "sempre", "quale", "quali", "aveva", "avevano", "signore", "egli",
        "ella", "come", "essi", "cui", "nostro", "verso", "essa",
    },
    "pt": {
        "os", "da", "das", "dos", "não", "uma", "em", "na", "nas", "com",
        "mais", "muito", "você", "ele", "ela", "eles", "elas", "foi",
        "são", "estão", "tem", "têm", "seu", "sua", "seus", "suas",
        "quando", "ainda", "já", "até", "depois", "onde", "sem", "sempre",
        "então", "assim", "isso", "isto", "aquele", "aquela", "quem",
        "mesmo", "outro", "outra", "tudo", "todos", "nada", "bem", "agora",
        "aqui", "ali", "pelo", "pela", "pelos", "pelas", "ao", "aos", "às",
        "num", "numa", "meu", "minha", "nós", "lhe", "dizer", "disse",
        "era", "eram", "ser", "estar", "fazer", "para", "por", "senhor",
        "vez", "coisa", "porém", "também", "só", "às", "desde", "entre",
    },
}

# Letters that only one of the five uses habitually. Worth a nudge, not a
# verdict: an English text quoting one Spanish name shouldn't move at all,
# which is why these are scaled by how often they occur, like the words.
_CHARS = {"ñ": "es", "¿": "es", "¡": "es", "ã": "pt", "õ": "pt", "è": "it",
          "ò": "it", "ù": "fr", "û": "fr", "ê": "fr", "œ": "fr"}

# Tokens: letters only, so digits, punctuation and markup drop out. Apostrophes
# split ("l'homme" -> l, homme), which is why the French list carries "qu".
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this there isn't enough text to be sure of anything, and a wrong guess
# on a two-line paste is more annoying than no guess at all.
_MIN_WORDS = 25
# The winner has to look like a real hit (this share of all tokens are its
# function words; a correct match usually runs 3-5x this) and has to beat the
# runner-up by this much. Spanish/Portuguese/Italian share a lot of grammar,
# so an ambiguous document deliberately falls through to None.
_MIN_SCORE = 0.045
_MIN_MARGIN = 1.30
# ...and it has to be carried by a spread of different words, not one common
# word that happens to belong to another language too.
_MIN_DISTINCT = 8

# Share of letters that must be in a script before it decides the document.
# High enough that a Chinese quotation or a line of Devanagari in an English
# paper doesn't drag the whole book off to another voice.
_SCRIPT_SHARE = 0.15

# _script_lang's answer when the writing system settles that nothing here can
# read the text — distinct from None, which hands the decision on to the words.
_NO_VOICE = ""


def _script_lang(text: str) -> str | None:
    """Language settled by writing system; `_NO_VOICE` when the script is one
    no voice reads; None for Latin text, which the words then decide.

    Japanese and Chinese share Han characters, so kana is the tiebreak: any
    meaningful amount of hiragana/katakana means Japanese.
    """
    dev = kana = han = hangul = latin = letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        o = ord(ch)
        if 0x0900 <= o <= 0x097F:
            dev += 1
        elif 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            han += 1
        elif 0xAC00 <= o <= 0xD7AF:
            hangul += 1
        elif 0x0041 <= o <= 0x024F:
            latin += 1
    if letters < 40:
        return None
    if dev / letters >= _SCRIPT_SHARE:
        return "hi"
    if kana / letters >= 0.05:
        return "ja"
    if han / letters >= _SCRIPT_SHARE:
        return "ja" if kana > han * 0.1 else "zh"
    if hangul / letters >= _SCRIPT_SHARE:
        return _NO_VOICE  # Korean: no Kokoro voice, so leave the account voice alone
    if latin / letters < 0.5:
        # Mostly a script none of the rules above speak — Cyrillic, Greek,
        # Arabic, Hebrew, Thai... No voice here reads it, and scoring whatever
        # Latin it carries would only give a confident wrong answer: a Russian
        # novel that opens in French is not a French book.
        return _NO_VOICE
    return None


def detect(text: str) -> str | None:
    """The document's language, or None if it isn't one Kokoro speaks — or
    isn't clear enough to act on. None always means "change nothing"."""
    if not text:
        return None
    text = text[:200_000]
    by_script = _script_lang(text)
    if by_script is not None:
        return by_script or None

    low = text.lower()
    words = _WORD_RE.findall(low)
    total = len(words)
    if total < _MIN_WORDS:
        return None

    hits: dict[str, int] = {}
    distinct: dict[str, set] = {lg: set() for lg in _WORDS}
    for w in words:
        for lg, vocab in _WORDS.items():
            if w in vocab:
                hits[lg] = hits.get(lg, 0) + 1
                distinct[lg].add(w)
    scores = {lg: hits.get(lg, 0) / total for lg in _WORDS}

    # Accent nudge, capped so a handful of borrowed words can't decide a book.
    for ch, lg in _CHARS.items():
        n = low.count(ch)
        if n:
            scores[lg] += min(n / total, 0.02)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < _MIN_SCORE or len(distinct[best]) < _MIN_DISTINCT:
        return None
    if second > 0 and top < second * _MIN_MARGIN:
        return None
    return best


def _sample(chunks, budget: int = 8000) -> str:
    """Text to detect on: real sentences from the body, not the cover page.

    Front matter is the worst possible evidence — an English title page on a
    French book, a publisher's boilerplate, a scanned header — so on anything
    long enough we start a tenth of the way in, and headings and page numbers
    are skipped for being too short to carry grammar.
    """
    texts = [((c if isinstance(c, str) else getattr(c, "text", "")) or "").strip()
             for c in chunks]  # Chunk objects on import, plain rows on backfill
    start = len(texts) // 10 if len(texts) >= 20 else 0
    for begin in (start, 0):  # a short document falls back to everything
        picked, size = [], 0
        for t in texts[begin:]:
            if len(t) < 12:
                continue
            picked.append(t)
            size += len(t)
            if size >= budget:
                break
        if size >= 400 or begin == 0:
            return " ".join(picked)
    return ""


def detect_chunks(chunks) -> str | None:
    """The language of an import, from its own text. Never raises: a detector
    bug must not be able to fail somebody's import."""
    try:
        return detect(_sample(chunks))
    except Exception:
        return None


def language_of(voice: str | None) -> str | None:
    """The language a voice speaks, from its id prefix. None if unrecognised."""
    prefix = (voice or "")[:1]
    for language, prefixes in _SPEAKS.items():
        if prefix in prefixes:
            return language
    return None


def speaks(voice: str | None, language: str | None) -> bool:
    """Can this voice pronounce this language? Unknown language = yes, since
    there's no better voice to move to and no reason to fight the user."""
    if not language or language not in _SPEAKS:
        return True
    return (voice or "")[:1] in _SPEAKS[language]


def default_voice(language: str | None) -> str | None:
    """The voice a language falls back to before anyone has chosen one."""
    return _DEFAULT.get(language or "")


def defaults() -> dict[str, str]:
    """Every language's fallback voice, so a client can show "Spanish — Dora"
    without keeping a second copy of this table."""
    return dict(_DEFAULT)


def was_assigned(voice: str | None, language: str | None) -> bool:
    """True if this voice is one the app could have put on a document by
    itself, rather than one the user chose (migration only)."""
    return bool(voice) and voice in _ASSIGNED.get(language or "", ())


def resolve(doc_language: str | None, chosen: dict[str, str] | None,
            main_voice: str | None, override: str | None = None) -> str:
    """Which voice reads this document.

    `chosen` is the user's voice per language — their own picks, nothing
    inferred. A language they haven't picked for falls back to the curated
    default, and only a document whose language we never worked out falls back
    to their main voice, the one thing still sensibly account-wide.
    """
    main = main_voice or DEFAULT_VOICE
    if override:
        return override
    if doc_language:
        picked = (chosen or {}).get(doc_language)
        if picked:
            return picked
        if not speaks(main, doc_language):
            return default_voice(doc_language) or main
    return main
