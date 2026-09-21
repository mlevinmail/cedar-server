"""Kokoro TTS client: synthesize a chunk, get word timestamps, align them to the
source text's character offsets, and cache the result (audio + word map) on disk.

Synthesis is per-chunk and cached by a content hash of (text, voice), so
identical text is never re-synthesized — across documents, users, or app
restarts. Audio is always generated at 1.0×: clients control tempo with their
player's playback rate (timestamps live in the audio's own timeline, so they
stay valid at any rate), which lets one cache entry serve every speed.

Concurrent requests for the same uncached chunk — a class opening one document,
or a single client's prefetch burst — are coalesced into one upstream synthesis
(see _inflight).

Bandwidth, not the GPU, is what a listener actually waits on. Synthesis of a
typical sentence takes ~0.24 s on a mid-range GPU, while the clip itself was 128 kbps —
needing ~129 kbps sustained just to keep ahead of playback, which mobile
connections in the places Cedar is actually being downloaded do not hold. (The
base64 in the JSON payload adds a third on the wire but compresses back off it
again, so it is the audio bitrate that matters, not the encoding of it.) So we
ask Kokoro for wav and encode once ourselves at
AUDIO_BITRATE_KBPS (see _encode), halving the payload. Kokoro's own mp3 also
carries no gapless header, so it decoded 1105 samples (~46 ms) later than its
word timestamps described; encoding from wav reproduces that timeline exactly,
which is why the highlight lines up slightly better than it used to rather than
slightly worse.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .config import (AUDIO_BITRATE_KBPS, AUDIO_DIR, AUDIO_FORMAT,
                     AUDIO_SOURCE_FORMAT, KOKORO_URL, TTS_TIMEOUT)

_CACHE = AUDIO_DIR / "cache"
_CACHE.mkdir(parents=True, exist_ok=True)

# Scratch space for in-progress encodes, deliberately NOT _CACHE. mkstemp hands
# lame a *path*, which lame then re-opens by name and follows if something has
# swapped a symlink in behind it — so the encode must not write into a directory
# anything else can reach (the data volume may well be shared with other
# containers on a home server). 0700 and a private subdirectory close that
# window; it also keeps temp files stranded by a crash out of the directory the
# cache is served from.
_ENC_TMP = AUDIO_DIR / "enc"
_ENC_TMP.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(_ENC_TMP, 0o700)
except OSError:  # best effort — a read-only or foreign-owned volume still works
    pass


def _sweep_enc_tmp(max_age_s: float = 3600.0) -> int:
    """Drop encoder scratch files left behind by a hard kill.

    A `docker rm -f` (SIGKILL, no drain) strands one file per in-flight encode,
    so every redeploy under load leaves a few behind. Nothing else ever
    removes them and the cache has no eviction, so they would accumulate for
    good. Age-gated rather than wholesale so this stays safe if the app is ever
    run with more than one worker process.
    """
    removed, cutoff = 0, time.time() - max_age_s
    try:
        entries = list(_ENC_TMP.iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# Runs at import, i.e. before main._configure_logging() has attached any
# handler, so this deliberately does not try to log what it removed — the count
# would go nowhere. It is recorded so a status endpoint could report it.
stale_encoder_files_cleared = _sweep_enc_tmp()

# "cedar.*", not __name__: main._configure_logging() attaches the rotating
# file handler to the "cedar" and "uvicorn.error" trees only. A "cedar.tts"
# logger under another name would fall through to stderr, i.e. docker logs,
# i.e. erased by the next deploy — and the warning below is the only detector
# for a silently misconfigured bitrate, so it has to land somewhere that
# outlives a deploy.
log = logging.getLogger("cedar.tts")

# lame, not ffmpeg: 7 MB in the image against ~620 MB, and it writes the
# LAME/Info gapless header that keeps audio aligned with the word timestamps.
_ENCODER = "lame"

# Encoding gets its own small pool rather than asyncio's default executor.
# That executor is shared with _read_cache and documents._sentence_for_tts, and it
# holds only min(32, cpu_count + 4) threads — so encoders blocking on a wedged
# `lame` would stall *cached* playback for every listener too, turning an
# encoder problem into a total TTS outage. A dedicated bounded pool means a
# stuck encoder can only ever cost re-encoding. Fixed size, not cpu-derived, so
# the blast radius doesn't quietly grow on a bigger host.
_ENCODE_WORKERS = 4
_ENCODE_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_ENCODE_WORKERS, thread_name_prefix="cedar-encode")

# Seconds the encoder gets for one chunk. Measured on an RTX 2060 host: a 19 s clip (the
# longest CHUNK_MAX_CHARS allows) takes ~88 ms idle and ~121 ms under load, so
# this is ~80x headroom and anything near it is a hung process, not a slow one.
# Kept low deliberately — it is the upper bound on how long a wedged encoder
# occupies one of the four slots above.
_ENCODE_TIMEOUT = 10.0

# After this many consecutive failures the process stops trying to re-encode.
# Without it, a `lame` that is present but broken (wrong build, missing shared
# library, a bitrate it rejects) never flips _encoder_ok, so every single chunk
# pays for TWO Kokoro syntheses forever — double GPU load and up to 2x
# TTS_TIMEOUT of latency — while looking healthy.
_ENCODE_FAIL_LIMIT = 3
# How long re-encoding stays off after the breaker trips, before one more try.
_ENCODER_RETRY_S = 600.0
_encode_fails = 0
_encoder_retry_at = 0.0
_bitrate_checked = False
# Tail of the encoder's own stderr from the last failure. capture_output swallows
# it, and without keeping it a broken encoder is diagnosable only by reproducing
# the exact call by hand.
_last_encode_error = ""
# The gapless header the encoder writes into the first frame, and how far in to
# look. Both spellings matter: CBR writes "Info", ABR/VBR write "Xing". Matching
# only one would false-warn on every single clip the moment the encoder mode
# changed — and ABR is the interesting mode, since (unlike CBR) it writes the
# header at every bitrate, so it is the way below 64 kbps if that is ever wanted.
_GAPLESS_TAGS = (b"Info", b"Xing")
_GAPLESS_SCAN = 2500
# Bitrates lame accepts for MPEG-2 Layer III (what 24 kHz mono produces). It
# silently CLAMPS anything else — 192 and 1000 both become 160, which would make
# payloads bigger than before this change while meta still recorded the number
# that was asked for.
_MPEG2_BITRATES = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
# Lowest --cbr rate whose first frame is wide enough to hold the gapless header.
_CBR_GAPLESS_MIN = 64
# What lame will actually take on stdin. Everything else Kokoro can serve —
# flac, opus, mp3, aac, raw pcm — exits 255, so re-encoding would fail on every
# chunk while looking configured.
_DECODABLE_SOURCES = frozenset({"wav", "aiff"})
# None = not probed yet. Probed once per process, lazily, so import stays cheap
# and a missing encoder degrades to Kokoro's own mp3 instead of failing.
_encoder_ok: Optional[bool] = None
_gapless_warned = False

_WORD_TOKEN = re.compile(r"\w[\w'’-]*", re.UNICODE)

# Characters that make Kokoro return NO word timestamps at all — not for the
# tail, for the entire chunk — which strands the highlight on _estimate_words'
# proportional guess. Measured on the live model: a matched "[...]" or "{...}"
# pair (its G2P reads those as pronunciation markup), a "|", or a "--". A lone
# unmatched bracket is harmless, and em dash / colon / semicolon / digits are
# all fine, so each one is swapped for punctuation that speaks the same:
#   --  -> em dash (a Gutenberg-style dash)
#   |   -> comma   (a verse//poetry line break; a comma keeps the short pause)
#   []{} -> ()     (footnote markers like "[*]" keep their shape)
# 13% of the Spanish scripture one real user reads hit this, all via "|" and
# "[*]". Substitution touches punctuation only — no word token changes — and
# alignment still runs against the *original* text, so highlight offsets hold.
_TS_BREAKING = re.compile(r"--+|[\[\]{}|]")
_TS_SAFE = {"[": "(", "]": ")", "{": "(", "}": ")", "|": ","}


def _speakable(text: str) -> str:
    """The text as sent to Kokoro: same words, timestamp-safe punctuation."""
    return _TS_BREAKING.sub(lambda m: _TS_SAFE.get(m.group(0), "—"), text)


# A letter or a digit in any script — \w minus the underscore, which is not a
# sound. Anything with none of these has no phoneme for Kokoro to produce.
_SPEECH_CHAR = re.compile(r"[^\W_]", re.UNICODE)


def _has_speech(text: str) -> bool:
    """Whether there is anything in here for the voice to actually say.

    Gutenberg scene dividers — "* * * * *", a rule of dashes, a bare "]" — are
    chunked as sentences like every other line, but they hold no word for the
    G2P to turn into phonemes, and Kokoro answers
    500 {"message": "list index out of range"} for *both* wav and mp3. So the
    format retry in _synth_uncached cannot help, the request leaves as a 502,
    the client retries the same sentence, gets the same 502 — and playback stops
    dead mid-book with "Could not synthesize audio."

    Found 2026-09-01, when 3,157 such sentences sat across 167 of the 351
    documents in prod: nearly half the library had a hard stop somewhere in it.
    They are served _SILENCE_MP3 instead — see _silence_payload.
    """
    return _SPEECH_CHAR.search(_speakable(text)) is not None

# MP3 frame tables, for measuring duration without an external dependency.
_BR_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BR_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _gapless_trim(data: bytes, i: int, ver: int, mono: bool) -> Optional[tuple]:
    """(encoder_delay, padding) in samples if the frame at `i` is a Xing/Info tag
    frame, else None.

    That frame carries no audio — it exists to hold the seek table and the
    encoder's delay/padding counts — so a duration that counts it, and counts
    the silence it describes, overstates the clip by ~85 ms. Kokoro writes no
    such frame, so this returns None for every entry cached before we started
    encoding ourselves and their duration is measured exactly as before.
    """
    side_info = (17 if mono else 32) if ver == 3 else (9 if mono else 17)
    p = i + 4 + side_info
    if data[p:p + 4] not in (b"Xing", b"Info"):
        return None
    flags = int.from_bytes(data[p + 4:p + 8], "big")
    q = p + 8
    q += 4 if flags & 1 else 0        # frame count
    q += 4 if flags & 2 else 0        # byte count
    q += 100 if flags & 4 else 0      # seek table
    q += 4 if flags & 8 else 0        # VBR quality
    # The LAME extension sits straight after, and holds the two counts 21 bytes
    # in: 12 bits of delay then 12 bits of padding.
    if data[q:q + 4] != b"LAME" or len(data) < q + 24:
        return (0, 0)
    d = data[q + 21:q + 24]
    return ((d[0] << 4) | (d[1] >> 4), ((d[1] & 0x0F) << 8) | d[2])


def _mp3_duration(data: bytes) -> float:
    """Playable seconds of an MP3 by summing frame durations (CBR+VBR,
    MPEG1/2/2.5), minus anything a gapless decoder will trim.

    Validated against ffprobe to within a millisecond on lame and Kokoro output
    alike; the un-trimmed figure ran ~85 ms long on our own encodes.
    """
    i, n, total = 0, len(data), 0.0
    trim_samples, first_frame, trim_rate = 0, True, 0
    if data[:3] == b"ID3" and n >= 10:
        i = 10 + (((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14)
                  | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F))
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver = (data[i + 1] >> 3) & 0x03     # 3=MPEG1 2=MPEG2 0=MPEG2.5
        layer = (data[i + 1] >> 1) & 0x03   # 1=Layer III
        br_i = (data[i + 2] >> 4) & 0x0F
        sr_i = (data[i + 2] >> 2) & 0x03
        pad = (data[i + 2] >> 1) & 0x01
        if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
            i += 1
            continue
        bitrate = (_BR_V1_L3 if ver == 3 else _BR_V2_L3)[br_i] * 1000
        samplerate = _MP3_SR[ver][sr_i]
        if ver == 3:
            samples, frame_len = 1152, (144 * bitrate) // samplerate + pad
        else:
            samples, frame_len = 576, (72 * bitrate) // samplerate + pad
        if frame_len <= 0:
            i += 1
            continue
        if first_frame:
            first_frame = False
            trim = _gapless_trim(data, i, ver, (data[i + 3] >> 6) == 3)
            if trim is not None:
                # Tag frame: no audio of its own, and it names the silence the
                # decoder will drop from the real frames.
                trim_samples = trim[0] + trim[1]
                i += frame_len
                continue
        total += samples / samplerate
        trim_rate = samplerate
        i += frame_len
    if trim_samples and trim_rate:
        total = max(0.0, total - trim_samples / trim_rate)
    return total


def _encoder_available() -> bool:
    """Probe the mp3 encoder, caching the answer in _encoder_ok.

    Re-probes when a previous failure's cool-off has expired, so neither a
    tripped breaker nor a lame that was missing at boot is permanent.
    """
    global _encoder_ok, _encoder_retry_at, _encode_fails
    if (_encoder_ok is False and _encoder_retry_at
            and time.time() >= _encoder_retry_at):
        log.info("retrying mp3 re-encoding after cool-off")
        _encoder_ok, _encoder_retry_at, _encode_fails = None, 0.0, 0
    if _encoder_ok is None:
        try:
            subprocess.run([_ENCODER, "--version"], capture_output=True,
                           timeout=10, check=True)
            _encoder_ok = True
        except (OSError, subprocess.SubprocessError):
            _encoder_ok = False
    return _encoder_ok


def reencoding() -> bool:
    """Whether this process re-encodes Kokoro's audio itself.

    False falls the whole path back to asking Kokoro for AUDIO_FORMAT directly,
    which is exactly the pre-2026-08 behaviour — so a host without lame, or
    CEDAR_AUDIO_SOURCE_FORMAT="", still serves audio.
    """
    return (bool(AUDIO_SOURCE_FORMAT)
            and AUDIO_SOURCE_FORMAT != AUDIO_FORMAT
            and _encoder_available())


def _probe() -> bool:
    """reencoding(), plus the one-time config validation. Runs off the loop."""
    global _bitrate_checked
    ok = reencoding()
    if not _bitrate_checked:
        _bitrate_checked = True
        _check_bitrate()
    return ok


def _check_bitrate() -> None:
    """Say so once if the configured bitrate is not one lame can actually use.

    lame does not reject an out-of-range --cbr value, it clamps: 192, 320 and
    1000 all silently become 160, which would make every payload *larger* than
    the 128 kbps this change set out to replace, while `kbps` in the cache meta
    still reported the number that was asked for.
    """
    if not reencoding():
        return
    if AUDIO_SOURCE_FORMAT not in _DECODABLE_SOURCES:
        log.error(
            "CEDAR_AUDIO_SOURCE_FORMAT=%r cannot be decoded by %s (only %s "
            "are supported). Every chunk will cost two Kokoro syntheses until "
            "the failure breaker trips.",
            AUDIO_SOURCE_FORMAT, _ENCODER, "/".join(sorted(_DECODABLE_SOURCES)))
    if AUDIO_BITRATE_KBPS not in _MPEG2_BITRATES:
        nearest = min(_MPEG2_BITRATES, key=lambda b: abs(b - AUDIO_BITRATE_KBPS))
        log.warning(
            "CEDAR_AUDIO_BITRATE_KBPS=%d is not a valid MPEG-2 Layer III "
            "bitrate; lame will silently clamp it (nearest valid: %d kbps). "
            "Valid values: %s.", AUDIO_BITRATE_KBPS, nearest,
            ", ".join(str(b) for b in _MPEG2_BITRATES))
    elif AUDIO_BITRATE_KBPS < _CBR_GAPLESS_MIN:
        # In the table, so lame accepts it — and silently drops the gapless
        # header, which is the failure this whole path exists to avoid.
        log.warning(
            "CEDAR_AUDIO_BITRATE_KBPS=%d is below %d, so --cbr cannot fit the "
            "gapless header: every clip will decode ~46 ms late and the word "
            "highlight will run ahead of the voice. Use %d+, or switch the "
            "encode to --abr.", AUDIO_BITRATE_KBPS, _CBR_GAPLESS_MIN,
            _CBR_GAPLESS_MIN)


def _check_gapless(data: bytes) -> None:
    """Warn once if the encode came out without its gapless header.

    The LAME/Info header is what tells a decoder to drop the encoder's leading
    delay. It has to fit inside the first MPEG frame, and at 24 kHz mono a frame
    is only (72 * bitrate / 24000) bytes — so at 56 kbps and below lame gives up
    on it *silently* and every clip decodes ~46 ms late, sliding the word
    highlight ahead of the voice. Audio still plays, so nothing else would catch
    this; hence the explicit check.
    """
    global _gapless_warned
    head = data[:_GAPLESS_SCAN]
    if _gapless_warned or any(tag in head for tag in _GAPLESS_TAGS):
        return
    _gapless_warned = True
    log.warning(
        "mp3 gapless header missing at %d kbps — clips will decode ~46 ms late "
        "and the word highlight will run ahead of the voice. With --cbr the "
        "header only fits at 64 kbps and up (CEDAR_AUDIO_BITRATE_KBPS); --abr "
        "writes it at any rate.", AUDIO_BITRATE_KBPS)


def _encode(source: bytes) -> Optional[bytes]:
    """_encode_once plus failure accounting.

    A `lame` that runs but always fails is the nastiest shape here: the process
    keeps believing it can re-encode, so every chunk asks Kokoro twice — once
    for wav, once for the mp3 fallback — doubling GPU load and latency
    indefinitely while every request still succeeds. Giving up after
    _ENCODE_FAIL_LIMIT consecutive failures degrades to the documented
    pre-change behaviour instead. One success resets the count, so a transient
    (a timeout under momentary load) doesn't disable anything.
    """
    global _encode_fails, _encoder_ok, _encoder_retry_at
    if not source:
        # Nothing to encode: an empty upstream body is not evidence about the
        # encoder, and counting it would let three empty Kokoro responses
        # disable re-encoding on a perfectly healthy lame.
        return None
    data = _encode_once(source)
    if data is not None:
        _encode_fails = 0
        return data
    _encode_fails += 1
    if _encode_fails >= _ENCODE_FAIL_LIMIT and _encoder_ok:
        _encoder_ok = False
        # Time-boxed, not permanent. The four pool workers can fail in parallel,
        # so a single correlated blip (a brief ENOSPC, one slow batch hitting the
        # timeout) can trip a counter meant for a persistent fault — and without
        # an expiry that costs 2x payloads until someone redeploys.
        _encoder_retry_at = time.time() + _ENCODER_RETRY_S
        log.error("mp3 re-encoding disabled after %d consecutive failures; "
                  "serving Kokoro's 128 kbps audio (payloads ~2x larger). "
                  "Check that `%s` works in this image. Last encoder error: %s",
                  _encode_fails, _ENCODER, _last_encode_error or "(none captured)")
    return None


def _encode_once(source: bytes) -> Optional[bytes]:
    """Kokoro's wav -> AUDIO_FORMAT at AUDIO_BITRATE_KBPS. None on any failure.

    Output goes to a temp *file*, not a pipe, and that is load-bearing rather
    than incidental: the LAME/Info header carrying the encoder's delay and
    padding can only be written by seeking back to the start, so piping to
    stdout silently drops it and the clip decodes 1105 samples (~46 ms) late —
    measurably dragging the highlight ahead of the voice. Written to a file the
    same encode lands at a 0-sample offset against Kokoro's own timeline.

    Mono at the source sample rate: resampling would only add loss, and the word
    timestamps live in this timeline.
    """
    tmp = None
    try:
        # Inside the try: on a full disk mkstemp raises, and the whole point of
        # this function's contract is that an encoder problem costs a bigger
        # payload, never the audio itself.
        fd, tmp = tempfile.mkstemp(suffix=f".enc.{AUDIO_FORMAT}", dir=_ENC_TMP)
        os.close(fd)
        proc = subprocess.run(
            [_ENCODER, "--quiet", "-m", "m", "--cbr", "-b", str(AUDIO_BITRATE_KBPS),
             "-", tmp],
            input=source, capture_output=True, timeout=_ENCODE_TIMEOUT)
        if proc.returncode != 0:
            global _last_encode_error
            _last_encode_error = (proc.stderr or b"")[-400:].decode(
                "utf-8", "replace").strip() or f"exit {proc.returncode}"
            return None
        # O_NOFOLLOW: lame re-opened this path by name, so refuse to read back
        # through a symlink someone swapped in. It does not prove the inode is
        # the one mkstemp made — a swapped *regular* file would still be read —
        # so this is one more layer on top of _ENC_TMP being private and 0700,
        # not a guarantee on its own.
        rfd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(rfd, "rb") as fh:
            data = fh.read()  # loops internally; os.read alone may short-read
        if not data:
            return None
        _check_gapless(data)
        return data
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _estimate_words(text: str, duration: float) -> List[dict]:
    """Proportional per-word timing for engines that return no timestamps.

    Kokoro only emits word timing for English voices; for every other language
    `timestamps` comes back empty. We still know the audio's true length, so we
    split it across the source text's word tokens weighted by length — an
    approximate but smoothly-tracking highlight in any language. cs/ce are the
    real character spans (Unicode \\w handles accents), so highlights land right.
    """
    spans = [(m.start(), m.end()) for m in _WORD_TOKEN.finditer(text)]
    if not spans or duration <= 0:
        return []
    weights = [(e - s) + 1 for s, e in spans]  # +1 so single-char words still take time
    total = float(sum(weights))
    out: List[dict] = []
    t = 0.0
    for (s, e), wt in zip(spans, weights):
        dt = duration * wt / total
        out.append({"start": round(t, 3), "end": round(t + dt, 3), "cs": s, "ce": e})
        t += dt
    return out


def _key(text: str, voice: str) -> str:
    # The "1.00" segment is the speed that used to be part of the key. All
    # first-party clients have always requested 1.0 (tempo is playback-rate,
    # client-side), so keeping the literal means every pre-existing cache
    # entry stays valid.
    h = hashlib.sha1(f"{voice}|1.00|{text}".encode("utf-8")).hexdigest()
    return h


_PUNCT_EDGE = "\"'“”‘’()[].,!?;:—–-"
# Same-length character folds so matching survives curly apostrophes/quotes
# ("don’t" vs Kokoro's "don't") without shifting any offsets.
_QUOTE_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
# How far past the cursor a spoken token may match. Kokoro walks the text in
# order, so a genuine match sits just ahead; a distant hit is a look-alike
# (an "and" from a later clause) that would swallow real words into a gap.
_MATCH_WINDOW = 80
# Bump when alignment logic changes: cached entries that stored their source
# text + raw timestamps are re-aligned on first read instead of kept stale.
_ALIGN_VERSION = 3

# How Kokoro speaks digits: "1975" comes back as "nineteen seventy-five",
# "100" as "one hundred", "19th" as "nineteenth". Anchoring those literally
# hunts for a look-alike word ahead in the text ("one's", an "and" two
# clauses on) and swallows everything up to it, so a written number was never
# lit and the highlight lurched past it. Number words are pinned to the next
# written number instead.
_NUM_WORDS = frozenset("""
zero one two three four five six seven eight nine ten eleven twelve thirteen
fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty
sixty seventy eighty ninety hundred thousand million billion trillion
first second third fourth fifth sixth seventh eighth ninth tenth eleventh
twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth
nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth
ninetieth hundredth thousandth millionth billionth
twenties thirties forties fifties sixties seventies eighties nineties hundreds
thousands millions billions
""".split())
# Continue a spoken number without being one: "hundred AND five", "three
# POINT five" only when another number word follows; "four DOLLARS" always.
_NUM_JOIN_MID = frozenset("and point oh".split())
_NUM_JOIN_TAIL = frozenset(
    "dollars dollar cents cent percent pounds pound euros euro".split())
# A number as written: digits with internal separators and range dashes
# ("1,000", "3.5", "100–105", "10:30"), an optional currency sign, and a
# glued suffix ("1990s", "19th").
_NUMERIC = re.compile(r"[$€£]?\d(?:[\d,.:/\-–—]*\d)?\w*")


def _is_num_word(token: str) -> bool:
    parts = [p for p in token.split("-") if p]
    return bool(parts) and all(p in _NUM_WORDS for p in parts)


def _next_numeric(low: str, cursor: int) -> Optional[Tuple[int, int]]:
    """Span of the first written number starting within the match window."""
    pos = cursor
    while True:
        m = _NUMERIC.search(low, pos)
        if m is None or m.start() > cursor + _MATCH_WINDOW:
            return None
        if m.start() == 0 or not _word_char(low[m.start() - 1]):
            return m.start(), m.end()
        pos = m.start() + 1


def _word_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _find_token(low: str, token: str, cursor: int) -> int:
    """Forward, word-boundary-respecting search within the match window."""
    limit = cursor + _MATCH_WINDOW + len(token)
    pos = low.find(token, cursor, limit)
    while pos != -1:
        end = pos + len(token)
        if ((pos == 0 or not _word_char(low[pos - 1]))
                and (end >= len(low) or not _word_char(low[end]))):
            return pos
        pos = low.find(token, pos + 1, limit)
    return -1


_GAP_TRIM_LEAD = " \t\n\"'“”‘’([{,;:"
_GAP_TRIM_TRAIL = " \t\n\"'“”‘’)]},;:"


def _fill_gaps(text: str, entries: List[dict]) -> None:
    """Second pass of _align_words: every run of unmatched entries sits between
    two anchors, and the source text between those anchors is exactly what was
    spoken — assign that whole span to the run (in place)."""
    i, n = 0, len(entries)
    while i < n:
        if entries[i]["cs"] >= 0:
            i += 1
            continue
        j = i
        while j < n and entries[j]["cs"] < 0:
            j += 1
        s = entries[i - 1]["ce"] if i > 0 else 0
        e = entries[j]["cs"] if j < n else len(text)
        while s < e and text[s] in _GAP_TRIM_LEAD:
            s += 1
        # Trailing .!? are kept: the gap may BE an abbreviation ("Dr.").
        while e > s and text[e - 1] in _GAP_TRIM_TRAIL:
            e -= 1
        if any(_word_char(c) for c in text[s:e]):
            for k in range(i, j):
                entries[k]["cs"], entries[k]["ce"] = s, e
        i = j


def _align_words(text: str, words: List[dict]) -> List[dict]:
    """Map each timestamped word to a [char_start, char_end) span in `text`.

    Kokoro reports the *spoken* words — "$4.50" comes back as "four dollars
    and fifty cents" — so mapping runs in two passes:
      1. anchor pass: each token is matched forward of the cursor (word-
         boundary-checked and windowed; never from the very start, which could
         jump the highlight backwards into a look-alike word). A run of spoken
         number words is pinned to the next *written* number instead of being
         matched literally — "one hundred" must light "100", not the "one's"
         two clauses later — and a literal match only wins when it sits before
         that number;
      2. gap pass (_fill_gaps): a run of unmatched tokens shares the source
         span between its surrounding anchors, so whatever is spoken in a way
         we don't recognize stays lit for as long as it plays.
    Punctuation-only tokens extend the previous word's end instead of becoming
    entries — the last word stays lit through the pause instead of flickering.
    """
    low = text.lower().translate(_QUOTE_FOLD)
    toks: List[Tuple[str, Optional[float], Optional[float]]] = []
    raw_punct: List[str] = []  # the raw text of punctuation-only tokens
    for w in words:
        raw = (w.get("word") or "").strip()
        token = raw.strip(_PUNCT_EDGE).lower().translate(_QUOTE_FOLD)
        toks.append((token, w.get("start_time", w.get("start")),
                     w.get("end_time", w.get("end"))))
        raw_punct.append(raw.translate(_QUOTE_FOLD) if not token else "")

    def next_is_num(i: int) -> bool:
        for t, _, _ in toks[i + 1:]:
            if t:
                return _is_num_word(t)
        return False

    entries: List[dict] = []
    cursor = 0
    run: Optional[Tuple[int, int]] = None  # written number the spoken run lights
    for i, (token, start, end) in enumerate(toks):
        if not token:
            if entries and end is not None:
                prev = entries[-1]
                if prev.get("end") is None or end > prev["end"]:
                    prev["end"] = end
            # Punctuation closes a spoken number — unless it is part of the
            # written one ("7.22:1" comes back with its ":" as its own token).
            if run is not None and raw_punct[i] not in low[run[0]:run[1]]:
                cursor, run = run[1], None
            continue
        in_run = run is not None
        numeric = _is_num_word(token) or (in_run and (
            token in _NUM_JOIN_TAIL
            or (token in _NUM_JOIN_MID and next_is_num(i))))
        if in_run and not numeric:
            cursor, run = run[1], None
        pos = _find_token(low, token, cursor)
        cs = ce = -1
        if numeric:
            if run is None:
                target = _next_numeric(low, cursor)
                literal = pos != -1 and (target is None or pos < target[0])
            else:
                # Mid-run, a literal match only wins when it is the very next
                # word after the number ("5 and 6"): anything further on is a
                # look-alike ("one hundred" must not reach an "one's" later).
                target = run
                after = _next_numeric(low, run[1])
                literal = pos != -1 and pos >= run[1] and (
                    after is None or pos < after[0]) and not any(
                    _word_char(c) for c in low[run[1]:pos])
            if literal:
                cs, ce = pos, pos + len(token)
                cursor, run = ce, None
            elif target is not None:
                cs, ce = target
                cursor, run = target[0], target
        elif pos != -1:
            cs, ce = pos, pos + len(token)
            cursor = ce
        entries.append({"start": start, "end": end, "cs": cs, "ce": ce})
    _fill_gaps(text, entries)
    return entries


# One client for all synthesis calls: keep-alive connections to Kokoro instead
# of a new pool + TCP handshake per chunk.
_client = httpx.AsyncClient(
    timeout=TTS_TIMEOUT,
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
)

# In-flight synthesis per cache key. Concurrent requests for the same uncached
# chunk await one task instead of each hitting Kokoro. Callers awaiting the
# task are independent of it: a disconnected request doesn't cancel the
# synthesis, and the result still lands in the disk cache.
_inflight: Dict[str, "asyncio.Task[dict]"] = {}


def _read_cache(key: str) -> Optional[dict]:
    """Disk-cache lookup incl. base64 encode. Blocking — call via to_thread."""
    meta_path = _CACHE / f"{key}.json"
    audio_path = _CACHE / f"{key}.{AUDIO_FORMAT}"
    if not (meta_path.exists() and audio_path.exists()):
        return None
    meta = json.loads(meta_path.read_text())
    # An entry written by an older aligner that kept its inputs is re-aligned
    # in place — cached audio gets alignment fixes without re-synthesis.
    # (Entries from before "text"/"raw" were stored are served as they are.)
    if meta.get("align") != _ALIGN_VERSION and meta.get("text") and meta.get("raw"):
        meta["words"] = _align_words(meta["text"], meta["raw"])
        meta["align"] = _ALIGN_VERSION
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(meta))
        os.replace(tmp_meta, meta_path)
    meta["audio_b64"] = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    meta["cached"] = True
    return meta


def _process_and_store(key: str, text: str, audio_bytes: bytes,
                       raw_words: List[dict], kbps: Optional[int] = None) -> dict:
    """Build the word map for already-final audio and write the cache entry.

    `audio_bytes` is AUDIO_FORMAT and is what ships to the client — the caller
    has already done any re-encoding, so duration is measured on the exact bytes
    the user will hear. Pure CPU + disk (MP3 frame walk, alignment, b64) — call
    via to_thread so a ~400 KB clip doesn't stall the event loop.
    """
    if raw_words:
        words = _align_words(text, raw_words)
        last_end = max((w["end"] for w in words if w.get("end") is not None), default=0.0)
        duration = _mp3_duration(audio_bytes) if AUDIO_FORMAT == "mp3" else 0.0
        if duration <= 0:
            duration = last_end
        elif duration - last_end > 1.0:
            # Kokoro timestamped only part of the chunk (normal clips end
            # within ~0.05s of the last timestamp). Lay estimated timing over
            # the un-timestamped tail so the highlight keeps moving.
            covered = max((w["ce"] for w in words if w["ce"] >= 0), default=0)
            for w in _estimate_words(text[covered:], duration - last_end):
                words.append({"start": round(w["start"] + last_end, 3),
                              "end": round(w["end"] + last_end, 3),
                              "cs": w["cs"] + covered, "ce": w["ce"] + covered})
    else:
        # Non-English voices: Kokoro returns audio but no word timestamps.
        # Measure the audio and lay down proportional timing so it still highlights.
        duration = _mp3_duration(audio_bytes) if AUDIO_FORMAT == "mp3" else 0.0
        words = _estimate_words(text, duration)

    # text + raw timestamps ride along (a few hundred bytes next to ~200 KB of
    # audio) so a future _ALIGN_VERSION bump can re-align without re-synthesis.
    meta = {"format": AUDIO_FORMAT, "words": words, "duration": duration,
            "text": text, "raw": raw_words, "align": _ALIGN_VERSION,
            # Which encode actually produced this entry — None means Kokoro's
            # own mp3, whether because re-encoding is off or because it failed
            # and we fell back. It has to report what happened rather than what
            # is configured, since a silent permanent fallback (missing encoder,
            # full disk) is otherwise invisible. Entries written before the
            # re-encode landed have no "kbps" and stay valid: the cache key
            # deliberately ignores bitrate, so 15k+ existing clips survive a
            # settings change.
            "kbps": kbps}
    # Write-then-rename so a reader never sees a half-written entry, and the
    # pair only becomes visible once both files are complete (meta last: the
    # cache probe requires both, audio first makes the pair appear atomic).
    audio_path = _CACHE / f"{key}.{AUDIO_FORMAT}"
    meta_path = _CACHE / f"{key}.json"
    tmp_audio = audio_path.with_suffix(audio_path.suffix + ".tmp")
    tmp_meta = meta_path.with_suffix(".json.tmp")
    tmp_audio.write_bytes(audio_bytes)
    tmp_meta.write_text(json.dumps(meta))
    os.replace(tmp_audio, audio_path)
    os.replace(tmp_meta, meta_path)

    meta["audio_b64"] = base64.b64encode(audio_bytes).decode("ascii")
    meta["cached"] = False
    return meta


def _kokoro_error(exc: httpx.HTTPError) -> str:
    """One log-safe line naming what Kokoro actually returned."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"{type(exc).__name__}: {exc}"
    body = " ".join((resp.text or "").split())[:200]
    return f"HTTP {resp.status_code}{': ' + body if body else ''}"


async def _ask_kokoro(text: str, voice: str, fmt: str) -> tuple[bytes, List[dict]]:
    payload = {
        "model": "kokoro",
        "input": _speakable(text),
        "voice": voice,
        "speed": 1.0,
        "response_format": fmt,
        "stream": False,
    }
    resp = await _client.post(f"{KOKORO_URL}/dev/captioned_speech", json=payload)
    resp.raise_for_status()
    data = resp.json()
    audio_b64 = data.get("audio") or ""
    return (base64.b64decode(audio_b64) if audio_b64 else b"",
            data.get("timestamps") or [])


async def _synth_uncached(key: str, text: str, voice: str) -> dict:
    # to_thread: the first call per process probes the encoder with a subprocess,
    # which would otherwise block the event loop for ~8 ms.
    want = AUDIO_SOURCE_FORMAT if await asyncio.to_thread(_probe) else AUDIO_FORMAT
    kbps = None

    try:
        audio, raw_words = await _ask_kokoro(text, voice, want)
    except httpx.HTTPError as exc:
        # Asking for the intermediate format is the one new way this call can
        # fail that the old code could not: self-hosters track a floating
        # kokoro-fastapi tag, and a build that rejects wav on
        # /dev/captioned_speech would otherwise 502 every sentence — strictly
        # worse than before the change. Fall back to the format we know works.
        if want == AUDIO_FORMAT:
            raise
        # Report what Kokoro actually said rather than the reason we hope it
        # failed for. This line used to read "Kokoro rejected
        # response_format='wav'" unconditionally, which cost four days of
        # looking at the audio format while the real answer — a 500 on
        # sentences with no words in them — was in the response body all along
        # (see _has_speech).
        log.warning("Kokoro failed for response_format=%r (%s); retrying as %r "
                    "(payloads will be ~2x larger)",
                    want, _kokoro_error(exc), AUDIO_FORMAT)
        want = AUDIO_FORMAT
        audio, raw_words = await _ask_kokoro(text, voice, AUDIO_FORMAT)

    if want != AUDIO_FORMAT:
        # Dedicated pool, not to_thread — see _ENCODE_POOL.
        encoded = await asyncio.get_running_loop().run_in_executor(
            _ENCODE_POOL, _encode, audio)
        if encoded is None:
            # An encoder that fails must never cost the user their audio: take
            # Kokoro's own mp3 for this chunk and carry on. Timestamps are
            # re-fetched with it so they describe the audio actually served.
            log.warning("mp3 re-encode failed; serving Kokoro's own audio for "
                        "this chunk (payload will be ~2x larger)")
            audio, raw_words = await _ask_kokoro(text, voice, AUDIO_FORMAT)
        else:
            audio, kbps = encoded, AUDIO_BITRATE_KBPS

    return await asyncio.to_thread(_process_and_store, key, text, audio,
                                   raw_words, kbps)


# The clip served for a sentence with nothing to say (see _has_speech): 0.4 s of
# digital silence, MPEG-2 Layer III 24 kHz mono — the shape Kokoro's own clips
# have, so it decodes on every client exactly like a synthesized one. Embedded
# rather than encoded on demand because this is the one path that has no upstream
# to fall back to, so it has to work on a host with no `lame` as well. To
# regenerate, encode 0.4 s of 24 kHz mono 16-bit silence with:
#   lame --quiet -m m --abr 32 - out.mp3
# --abr rather than --cbr: it writes the gapless header at any bitrate (--cbr
# only fits it from 64 kbps up), so the clip measures as exactly its 0.4 s and
# stays this small.
_SILENCE_MP3 = base64.b64decode(
    "//OAxAAAAAAAAAAAAFhpbmcAAAAPAAAAEgAAAqQADg4ODg4cHBwcHBwqKioqKjg4ODg4OEdHR0dH"
    "VVVVVVVVY2NjY2NxcXFxcXGAgICAgI6Ojo6OjpycnJycnKqqqqqquLi4uLi4x8fHx8fV1dXV1dXj"
    "4+Pj4/Hx8fHx8f//////AAAAOUxBTUUzLjEwMAJTAAAAAC3+AAAUICQDzCIAACAAAAKkAJ+PtwAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/zEMQAAAADSAAAAABMQU1FMy4x"
    "MDBVVVVV//MQxA0AAANIAAAAAFVVVVVVVVVVVVVVVVX/8xDEGgAAA0gAAAAAVVVVVVVVVVVVVVVV"
    "Vf/zEMQnAAADSAAAAABVVVVVVVVVVVVVVVVV//MQxDQAAANIAAAAAFVVVVVVVVVVVVVVVVX/8xDE"
    "QQAAA0gAAAAAVVVVVVVVVVVVVVVVVf/zEMROAAADSAAAAABVVVVVVVVVVVVVVVVV//MQxFsAAANI"
    "AAAAAFVVVVVVVVVVVVVVVVX/8xDEaAAAA0gAAAAAVVVVVVVVVVVVVVVVVf/zEMR1AAADSAAAAABV"
    "VVVVVVVVVVVVVVVV//MQxIIAAANIAAAAAFVVVVVVVVVVVVVVVVX/8xDEjwAAA0gAAAAAVVVVVVVV"
    "VVVVVVVVVf/zEMScAAADSAAAAABVVVVVVVVVVVVVVVVV//MQxKkAAANIAAAAAFVVVVVVVVVVVVVV"
    "VVX/8xDEtgAAA0gAAAAAVVVVVVVVVVVVVVVVVf/zEMTDAAADSAAAAABVVVVVVVVVVVVVVVVV//MQ"
    "xNAAAANIAAAAAFVVVVVVVVVVVVVVVVX/8xDE3QAAA0gAAAAAVVVVVVVVVVVVVVVVVQ=="
)
# Measured, not asserted, so a regenerated constant reports its own length.
_SILENCE_DURATION = round(_mp3_duration(_SILENCE_MP3), 3)


def _silence_payload() -> dict:
    """A short pause, answered without touching Kokoro or the disk cache.

    The bytes are identical for every voice and language, so there is nothing
    worth keying a cache entry on. Empty `words` is a shape the clients already
    handle — non-English voices return no timestamps either — so nothing
    highlights and the player advances when the clip ends, which is what a
    scene divider should do.
    """
    return {"format": AUDIO_FORMAT, "words": [], "duration": _SILENCE_DURATION,
            "kbps": None, "cached": True,
            "audio_b64": base64.b64encode(_SILENCE_MP3).decode("ascii")}


async def synthesize(text: str, voice: str, speed: float = 1.0) -> dict:
    """Return {format, audio_b64, words:[{start,end,cs,ce}], duration}. Cached on disk.

    `speed` is accepted for API compatibility but ignored: audio is always
    generated at 1.0× and clients set their player's playback rate instead.
    """
    # Before the cache: a divider is the same 676 bytes every time, and asking
    # Kokoro for it is what stalls playback (see _has_speech).
    if not _has_speech(text):
        return _silence_payload()

    key = _key(text, voice)

    cached = await asyncio.to_thread(_read_cache, key)
    if cached is not None:
        return _client_payload(cached)

    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_synth_uncached(key, text, voice))
        _inflight[key] = task
        task.add_done_callback(lambda _t: _inflight.pop(key, None))
    # Shallow copy: coalesced callers share the payload but not the dict.
    return _client_payload(dict(await task))


def _client_payload(meta: dict) -> dict:
    """The cache entry minus alignment inputs — clients don't need them."""
    for k in ("text", "raw", "align"):
        meta.pop(k, None)
    return meta


async def list_voices() -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{KOKORO_URL}/v1/audio/voices")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
    # Endpoint may return {"voices": [...]} or a list of objects with "id".
    if isinstance(data, dict):
        voices = data.get("voices") or data.get("data") or []
    else:
        voices = data
    out: List[str] = []
    for v in voices:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            vid = v.get("id") or v.get("name")
            if vid:
                out.append(vid)
    return sorted(set(out))


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{KOKORO_URL}/health")
            if resp.status_code == 200:
                return True
            resp = await client.get(f"{KOKORO_URL}/v1/audio/voices")
            return resp.status_code == 200
    except Exception:
        return False


# A Kokoro voice id ("af_heart"), or a mix the engine also accepts
# ("af_bella+af_sky", "af_bella(2)+af_sky(1)"). Anything else is refused at
# the route: the value is handed to the engine as-is, and it should not be a
# way to send Kokoro whatever a request author likes.
_VOICE_ID = re.compile(r"^[A-Za-z0-9_+().,\-]{1,80}$")


def valid_voice_id(voice: str | None) -> bool:
    return bool(voice) and _VOICE_ID.match(voice) is not None
