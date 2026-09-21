"""Live reading: speech for text the client already has on screen.

The app can capture a page from another reader (its Kindle screen OCRs a page
from Amazon's web reader), split it into sentences here and read it aloud with
the words lit in place — no document is created. The split uses the same
chunker every import uses, so pauses fall where they always do, and each clip
is cached on disk by text+voice like any other.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import lang, settings, tts
from ..chunker import chunk_plain_text

router = APIRouter(prefix="/api/live")
log = logging.getLogger("cedar.live")

# One captured page is a few hundred words; a screenful of a very small font
# might be a couple of thousand. Anything beyond that is not a page.
_MAX_PAGE_CHARS = 60_000
_MAX_SENTENCE_CHARS = 2_000


class PageIn(BaseModel):
    text: str


class SpeakIn(BaseModel):
    text: str
    voice: str | None = None


@router.post("/sentences")
def sentences(body: PageIn):
    """Split one page into sentences and say which voice should read it: the
    page's own language decides, through the per-language voice picks, the
    same way a document's voice is resolved."""
    text = (body.text or "").strip()
    if len(text) > _MAX_PAGE_CHARS:
        raise HTTPException(413, "That is more than one page of text.")
    main, speed = settings.reading_defaults()
    if not text:
        return {"sentences": [], "lang": None, "voice": main, "speed": speed}
    result = chunk_plain_text("", text)
    detected = lang.detect_chunks(result.chunks)
    voice = lang.resolve(detected, settings.chosen_voices(), main)
    return {"sentences": [c.text for c in result.chunks], "lang": detected,
            "voice": voice, "speed": speed}


@router.post("/tts")
async def speak(body: SpeakIn):
    """One sentence → {format, audio_b64, words, duration}, same shape as a
    document's /tts so the client's player needs no special case."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to say.")
    if len(text) > _MAX_SENTENCE_CHARS:
        raise HTTPException(413, "That is more than one sentence.")
    if body.voice is not None and not tts.valid_voice_id(body.voice):
        raise HTTPException(400, "Unknown voice.")
    main, _ = settings.reading_defaults()
    try:
        return await tts.synthesize(text, body.voice or main)
    except Exception:
        # The exception text can carry the engine's internal URL; keep that in
        # the server log and give the client only the fact.
        log.exception("live tts failed")
        raise HTTPException(502, "The voice service could not read that sentence.")
