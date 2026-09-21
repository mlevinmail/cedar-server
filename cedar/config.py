"""Central configuration. Everything comes from the environment, prefixed CEDAR_."""
from __future__ import annotations

import os
from pathlib import Path

# Project root = parent of this package
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("CEDAR_DATA", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"       # original PDFs
AUDIO_DIR = DATA_DIR / "audio"          # disk cache for synthesized audio + timestamps
MEDIA_DIR = DATA_DIR / "media"          # cached article images (per document)
GUTENBERG_DIR = DATA_DIR / "gutenberg"  # optional book-store corpus (catalog.db + raw/)
DB_PATH = DATA_DIR / "cedar.db"
KEY_PATH = DATA_DIR / "cedar.key"       # the generated owner key, when CEDAR_KEY isn't set

for d in (DATA_DIR, UPLOAD_DIR, AUDIO_DIR, MEDIA_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Kokoro-FastAPI, the voice engine. In the compose bundle it is reachable by
# service name; running the API by hand on the same machine, localhost.
KOKORO_URL = os.environ.get("CEDAR_KOKORO_URL", "http://localhost:8880").rstrip("/")

# The owner key. Every /api/* request except /api/health must present it. Empty
# here means "generate one on first start and keep it in KEY_PATH" (see auth.py).
CEDAR_KEY = os.environ.get("CEDAR_KEY", "").strip()

# A name for this server, shown in the app's Profile tab. Purely cosmetic.
SERVER_NAME = os.environ.get("CEDAR_SERVER_NAME", "My Cedar server").strip()[:60]

# Default voice/speed until the owner picks their own.
DEFAULT_VOICE = os.environ.get("CEDAR_DEFAULT_VOICE", "af_heart")
DEFAULT_SPEED = float(os.environ.get("CEDAR_DEFAULT_SPEED", "1.0"))

# Audio transfer format sent to clients. mp3 keeps payloads small, every client
# plays it, and it is what every cache entry on disk is already named — do not
# change it without a client release.
AUDIO_FORMAT = "mp3"

# What we ask Kokoro for, before re-encoding to AUDIO_FORMAT ourselves.
#
# Kokoro's own mp3 is fixed at 128 kbps, which is roughly double what 24 kHz
# mono speech needs, and it is the payload size — not synthesis — that a
# listener on mobile data waits on. Asking for wav and encoding once ourselves is
# also *more* accurate, not less: Kokoro's mp3 writes no gapless header, so its
# audio decodes 1105 samples (~46 ms) later than the word timestamps describe,
# which drags the highlight ahead of the voice. Encoding from wav reproduces the
# reference timeline exactly (measured: 0 sample offset).
#
# "wav" and "" are the only supported values. lame reads WAV (and AIFF) from
# stdin and rejects everything else Kokoro can serve. "" disables re-encoding
# and asks Kokoro for AUDIO_FORMAT directly, which is also the automatic
# fallback when lame is unavailable.
AUDIO_SOURCE_FORMAT = os.environ.get("CEDAR_AUDIO_SOURCE_FORMAT", "wav").strip()

# Bitrate for our own encode, in kbps. 64 halves the payload against Kokoro's
# 128 with no audible loss on 24 kHz mono speech.
#
# **Do not drop below 64 while the encode uses --cbr.** The LAME/Info gapless
# header must fit in the first MPEG frame, which at 24 kHz mono is only
# 72 * kbps / 24 bytes wide; at 56 kbps and under lame silently omits it and
# every clip then decodes ~46 ms late, sliding the word highlight ahead of the
# voice. Valid MPEG-2 Layer III rates are 8,16,24,32,40,48,56,64,80,96,112,
# 128,144,160; lame clamps anything else without complaint (tts._check_bitrate
# warns at startup).
AUDIO_BITRATE_KBPS = int(os.environ.get("CEDAR_AUDIO_BITRATE_KBPS", "64"))

# TTS chunking targets (characters). Short chunks = fast first-audio + reliable timestamps.
CHUNK_MAX_CHARS = 350
CHUNK_MIN_CHARS = 30

# Timeout for a single TTS synthesis call to Kokoro (seconds). CPU synthesis of
# the longest chunk can take a while on a small box, so this is generous.
TTS_TIMEOUT = float(os.environ.get("CEDAR_TTS_TIMEOUT", "120"))

# Largest accepted PDF/EPUB upload. An uploaded file is held in memory (and a PDF
# is then written to UPLOAD_DIR), so this bounds what one request can cost.
MAX_UPLOAD_BYTES = int(os.environ.get("CEDAR_MAX_UPLOAD_MB", "100")) * 1024 * 1024

# Largest body "import from link" will pull down. An upload is bounded by the
# client's own file; a URL is not — whatever it points at is read into memory,
# so without a ceiling one link to a huge file takes the process down. Kept
# below MAX_UPLOAD_BYTES: a link-to-PDF goes through the same pipeline.
MAX_FETCH_BYTES = int(os.environ.get("CEDAR_MAX_FETCH_MB", "60")) * 1024 * 1024

# Largest body of any other request (JSON: pasted text, a live page, settings).
# Enforced before parsing by cedar/bodylimit.py; uploads have their own limit.
MAX_BODY_BYTES = int(os.environ.get("CEDAR_MAX_BODY_MB", "8")) * 1024 * 1024
