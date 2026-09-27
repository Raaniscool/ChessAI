"""Which provider speaks, plus a disk cache so nothing is synthesized twice.

The cache (DATA_DIR/tts-cache) is keyed by provider model + voice + speed + text, and
trimmed to TTS_CACHE_MB, oldest first. Repeated phrases ("Correct!", hints, lesson
intros) are instant the second time, and preloading a UI phrase is just a request.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path

from .base import SPEEDS, Audio, TTSError
from .providers import KokoroProvider, OpenAICompatibleProvider

MAX_CHARS = 600          # one request = about one or two sentences; the client splits longer text
_EXT = {"audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg"}
_MIME = {v: k for k, v in _EXT.items() if k != "audio/mp3"}


def _settings():
    from ..config import get_settings
    return get_settings()


class AudioCache:
    def __init__(self, root: Path | None = None, max_mb: int | None = None):
        self.root = Path(root or Path(_settings().data_dir) / "tts-cache")
        self.max_bytes = (max_mb if max_mb is not None else _settings().tts_cache_mb) * 1024 * 1024
        self._lock = threading.Lock()

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]

    def get(self, key: str) -> Audio | None:
        for ext, mime in _MIME.items():
            path = self.root / f"{key}{ext}"
            if path.is_file():
                try:
                    os.utime(path)  # most recently used
                    return Audio(path.read_bytes(), mime)
                except OSError:
                    return None
        return None

    def put(self, key: str, audio: Audio) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{key}{_EXT.get(audio.mime, '.bin')}"
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(audio.data)
            os.replace(tmp, path)
            self._trim()

    def _trim(self) -> None:
        files = [p for p in self.root.iterdir() if p.is_file() and not p.name.endswith(".tmp")]
        total = sum(p.stat().st_size for p in files)
        for p in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= self.max_bytes:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


class TTSService:
    def __init__(self, providers: list | None = None, cache: AudioCache | None = None):
        self.providers = providers if providers is not None else [KokoroProvider(), OpenAICompatibleProvider()]
        self.cache = cache or AudioCache()
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    def provider(self, wanted: str | None = None):
        """The provider to use: an explicit one if usable, else by TTS_PROVIDER, else the first usable."""
        by_id = {p.id: p for p in self.providers}
        configured = _settings().tts_provider
        order = [wanted] if wanted else []
        if configured in by_id:
            order.append(configured)
        if configured == "browser" and not wanted:
            return None
        order += [p.id for p in self.providers]
        for pid in order:
            p = by_id.get(pid)
            if p is not None and p.available()[0]:
                return p
        return None

    def status(self) -> dict:
        out = []
        for p in self.providers:
            ok, why = p.available()
            out.append({"id": p.id, "name": p.name, "available": ok, "reason": why,
                        "voices": [v.as_dict() for v in p.voices()] if ok else []})
        chosen = self.provider()
        return {"providers": out, "default": chosen.id if chosen else "browser",
                "speeds": list(SPEEDS), "max_chars": MAX_CHARS}

    def speak(self, text: str, voice: str | None = None, speed: str = "normal",
              provider: str | None = None) -> tuple[Audio, bool]:
        """(audio, from_cache). Raises TTSError when no server voice can speak it."""
        text = clean(text)
        if not text:
            raise TTSError("nothing to say")
        if len(text) > MAX_CHARS:
            raise TTSError(f"too long for one request ({len(text)} > {MAX_CHARS} characters)")
        p = self.provider(provider)
        if p is None:
            raise TTSError("no server voice is set up; the browser's voice is used instead")
        ids = [v.id for v in p.voices()]
        voice = voice if voice in ids else ids[0]
        rate = SPEEDS.get(speed, 1.0)
        key = self.cache.key(p.cache_key(), voice, f"{rate:.2f}", text)
        hit = self.cache.get(key)
        if hit is not None:
            self.stats["hits"] += 1
            return hit, True
        self.stats["misses"] += 1
        try:
            audio = p.synthesize(text, voice, rate)
        except TTSError:
            self.stats["errors"] += 1
            raise
        try:
            self.cache.put(key, audio)
        except OSError:
            pass  # a full disk must not stop the voice
        return audio, False


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return "".join(ch for ch in text if ch.isprintable())


_service: TTSService | None = None
_lock = threading.Lock()


def get_tts() -> TTSService:
    global _service
    with _lock:
        if _service is None:
            _service = TTSService()
        return _service


def set_tts(service: TTSService | None) -> None:
    global _service
    with _lock:
        _service = service
