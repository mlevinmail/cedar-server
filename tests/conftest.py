"""Every test runs the real app against a throwaway data directory, with the
voice engine replaced by a stub so no Kokoro is needed."""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="cedar-test-")
os.environ["CEDAR_DATA"] = _TMP
os.environ["CEDAR_KEY"] = "test-owner-key-0123456"
os.environ["CEDAR_KOKORO_URL"] = "http://kokoro.invalid:8880"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from cedar import tts  # noqa: E402
from cedar.main import app  # noqa: E402

KEY = os.environ["CEDAR_KEY"]
AUTH = {"Authorization": f"Bearer {KEY}"}


async def _fake_synthesize(text: str, voice: str, speed: float = 1.0) -> dict:
    words = text.split()
    step = 0.3
    return {
        "format": "mp3",
        "audio_b64": base64.b64encode(b"\xff\xfb\x90\x00" + b"\0" * 64).decode(),
        "words": [{"start": i * step, "end": i * step + 0.25, "cs": -1, "ce": -1}
                  for i in range(len(words))],
        "duration": len(words) * step,
    }


async def _fake_health() -> bool:
    return True


async def _fake_list_voices() -> list[str]:
    return ["af_heart", "am_michael", "ef_dora", "ff_siwis"]


@pytest.fixture(scope="session")
def client():
    tts.synthesize = _fake_synthesize
    tts.health = _fake_health
    tts.list_voices = _fake_list_voices
    with TestClient(app) as c:
        yield c
