"""The read-aloud provider interface. Providers are interchangeable; nothing else in the
app knows which one is speaking."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


class TTSError(Exception):
    """Synthesis failed (the frontend then falls back to the browser voice, then to text)."""


@dataclass(frozen=True)
class Voice:
    id: str             # the provider's own voice id (sent to it verbatim)
    name: str           # what the learner sees
    persona: str        # coach persona it belongs to: "female" | "male"
    accent: str         # "US" | "UK"
    description: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Audio:
    data: bytes
    mime: str           # audio/wav | audio/mpeg


class Provider(Protocol):
    id: str             # "kokoro" | "openai"
    name: str

    def available(self) -> tuple[bool, str]:
        """(usable now, why not)."""

    def voices(self) -> list[Voice]: ...

    def synthesize(self, text: str, voice: str, speed: float) -> Audio: ...

    def cache_key(self) -> str:
        """Identifies the model, so a model change never serves stale audio."""


SPEEDS = {"slow": 0.85, "normal": 1.0, "fast": 1.2}
