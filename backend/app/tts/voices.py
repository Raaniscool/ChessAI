"""Curated real voices: 2-3 per coach persona, per provider. Every id is a voice the
provider actually ships (nothing is faked by pitch-shifting one voice)."""
from __future__ import annotations

from .base import Voice

# Kokoro-82M v1.0 English voices (the highest-rated ones in its own voice grades).
KOKORO = [
    Voice("af_heart", "Heart", "female", "US", "warm and friendly"),
    Voice("af_bella", "Bella", "female", "US", "bright and lively"),
    Voice("bf_emma", "Emma", "female", "UK", "calm and clear"),
    Voice("am_michael", "Michael", "male", "US", "relaxed and steady"),
    Voice("am_fenrir", "Fenrir", "male", "US", "deep and confident"),
    Voice("bm_george", "George", "male", "UK", "measured, classic"),
]

# OpenAI speech voices (gpt-4o-mini-tts / tts-1).
OPENAI = [
    Voice("coral", "Coral", "female", "US", "warm and friendly"),
    Voice("nova", "Nova", "female", "US", "bright and upbeat"),
    Voice("shimmer", "Shimmer", "female", "US", "soft and calm"),
    Voice("ash", "Ash", "male", "US", "clear and direct"),
    Voice("onyx", "Onyx", "male", "US", "deep and steady"),
    Voice("echo", "Echo", "male", "US", "light and even"),
]


def for_model(model: str) -> list[Voice]:
    """Voices of an OpenAI-compatible server, by the model it runs."""
    return KOKORO if "kokoro" in (model or "").lower() else OPENAI
