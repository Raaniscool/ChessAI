"""Read-aloud providers.

    kokoro   Kokoro-82M running in this process (pip install kokoro-onnx + two model
             files). Apache-2.0, free, offline, natural; ~3-5x faster than real time
             on a laptop CPU. See scripts/setup_tts.py.
    openai   any server speaking the OpenAI /v1/audio/speech API: a local
             Kokoro-FastAPI container (free), or OpenAI itself (paid, needs a key).

The browser's own voices (Web Speech API) are the third option and live entirely in
the frontend; they need no server at all.
"""
from __future__ import annotations

import io
import threading
import wave
from pathlib import Path

from .base import Audio, TTSError, Voice
from . import voices as V


def _settings():
    from ..config import get_settings
    return get_settings()


class KokoroProvider:
    id = "kokoro"
    name = "Kokoro (natural voice, runs on this computer)"

    def __init__(self, model: str | Path | None = None, voices_file: str | Path | None = None):
        s = _settings()
        base = Path(s.data_dir) / "tts"
        self.model = Path(model or s.tts_kokoro_model or base / "kokoro-v1.0.int8.onnx")
        self.voices_file = Path(voices_file or s.tts_kokoro_voices or base / "voices-v1.0.bin")
        self._engine = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False, "the kokoro-onnx package isn't installed (pip install kokoro-onnx)"
        if not self.model.is_file() or not self.voices_file.is_file():
            return False, f"model files not found ({self.model.name}, {self.voices_file.name}): run scripts/setup_tts.py"
        return True, ""

    def voices(self) -> list[Voice]:
        return list(V.KOKORO)

    def cache_key(self) -> str:
        return f"kokoro:{self.model.name}"

    def _load(self):
        with self._lock:
            if self._engine is None:
                from kokoro_onnx import Kokoro
                self._engine = Kokoro(str(self.model), str(self.voices_file))
            return self._engine

    def synthesize(self, text: str, voice: str, speed: float) -> Audio:
        ok, why = self.available()
        if not ok:
            raise TTSError(why)
        try:
            engine = self._load()
            lang = "en-gb" if voice.startswith("b") else "en-us"
            samples, rate = engine.create(text, voice=voice, speed=speed, lang=lang)
        except Exception as exc:  # the model failing must never break the lesson
            raise TTSError(f"Kokoro failed: {exc}") from exc
        return Audio(to_wav(samples, rate), "audio/wav")


class OpenAICompatibleProvider:
    id = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 transport=None):
        s = _settings()
        self.base_url = (base_url if base_url is not None else s.tts_openai_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else s.tts_openai_api_key
        self.model = model or s.tts_openai_model
        self.transport = transport
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker"))
        self.name = ("Kokoro server (natural voice)" if "kokoro" in self.model.lower() else
                     f"OpenAI-compatible speech ({self.model})") + (" on this computer" if local else "")

    def available(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "TTS_OPENAI_BASE_URL isn't set"
        if "api.openai.com" in self.base_url and not self.api_key:
            return False, "TTS_OPENAI_API_KEY isn't set"
        return True, ""

    def voices(self) -> list[Voice]:
        return V.for_model(self.model)

    def cache_key(self) -> str:
        return f"openai:{self.base_url}:{self.model}"

    def synthesize(self, text: str, voice: str, speed: float) -> Audio:
        import httpx
        ok, why = self.available()
        if not ok:
            raise TTSError(why)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "input": text, "voice": voice, "response_format": "mp3", "speed": speed}
        try:
            with httpx.Client(timeout=_settings().tts_timeout, transport=self.transport) as client:
                r = client.post(f"{self.base_url}/audio/speech", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TTSError(f"speech server unreachable: {exc}") from exc
        if r.status_code != 200 or not r.content:
            raise TTSError(f"speech server answered {r.status_code}")
        return Audio(r.content, r.headers.get("content-type", "audio/mpeg").split(";")[0])


def to_wav(samples, rate: int) -> bytes:
    """Float samples in [-1, 1] → 16-bit mono WAV (standard library only)."""
    try:
        import numpy as np
        pcm = (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    except ImportError:  # pragma: no cover - kokoro-onnx always brings numpy
        import struct
        pcm = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    return buf.getvalue()
