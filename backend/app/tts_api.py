"""/api/tts: which natural voices are available, and speech for one short piece of text.

    GET  /api/tts          -> {providers: [{id, name, available, reason, voices}], default, speeds, max_chars}
    POST /api/tts/speak    {text, voice?, speed?, provider?} -> audio bytes (audio/wav or audio/mpeg)
                            503 {error, fallback: "browser"} when no server voice can speak

The browser splits long text into sentences, asks for the next one while the current
one plays, and falls back to its own voice (then to plain text) on any error.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .tts import TTSError, get_tts

router = APIRouter(prefix="/api/tts")


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: str = "normal"
    provider: str | None = None


@router.get("")
def tts_status() -> dict:
    return get_tts().status()


@router.post("/speak")
def tts_speak(body: SpeakRequest):
    try:
        audio, cached = get_tts().speak(body.text, voice=body.voice, speed=body.speed, provider=body.provider)
    except TTSError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc), "fallback": "browser"})
    return Response(content=audio.data, media_type=audio.mime,
                    headers={"X-TTS-Cache": "hit" if cached else "miss", "Cache-Control": "private, max-age=86400"})
