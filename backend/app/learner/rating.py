"""Skill numbers: onboarding answers → a starting rating, and rating updates from results.

One scale everywhere (roughly Chess.com rapid), shared with knowledge.difficulty's
puzzle ratings, so "this learner" and "this position" can be compared directly.
"""
from __future__ import annotations

import math

# Self-described experience when the learner doesn't know a rating.
EXPERIENCE = {
    "new": ("I'm new to chess", 450),
    "rules": ("I know how the pieces move", 700),
    "casual": ("I play casually", 1000),
    "club": ("I play regularly / at a club", 1400),
    "strong": ("I'm an experienced tournament player", 1850),
}
PLATFORMS = ("chesscom", "lichess", "fide", "otb", "other", "none")
# Rough offsets to the tutor's scale (Chess.com-like). Lichess ratings run higher at club level.
PLATFORM_OFFSET = {"chesscom": 0, "lichess": -250, "fide": 0, "otb": 0, "other": 0, "none": 0}

MIN_RATING, MAX_RATING = 100, 3000
DEFAULT_RATING = 800
LEVEL_BANDS = ((1000, "beginner"), (1600, "intermediate"))


def clamp(value: float) -> int:
    return int(max(MIN_RATING, min(MAX_RATING, round(value))))


def normalize(rating: int | float, platform: str | None) -> int:
    """A rating from `platform` on the tutor's scale."""
    return clamp(float(rating) + PLATFORM_OFFSET.get(platform or "other", 0))


def from_experience(key: str | None) -> int:
    return EXPERIENCE.get(key or "", (None, DEFAULT_RATING))[1]


def level_for(rating: int | float | None) -> str:
    if rating is None:
        return "beginner"
    for top, name in LEVEL_BANDS:
        if rating < top:
            return name
    return "advanced"


def expected(learner: float, puzzle: float) -> float:
    """Chance the learner solves a puzzle of this rating (Elo formula)."""
    return 1.0 / (1.0 + math.pow(10.0, (puzzle - learner) / 400.0))


def score_for(solved: bool, first_try: bool, hints: int, revealed: bool) -> float:
    """1 = solved cleanly; partial credit for hints/retries; 0 = failed or had to look."""
    if revealed or not solved:
        return 0.0
    s = 1.0 if first_try else 0.6
    return max(0.3, s - 0.2 * max(0, hints))


def k_factor(attempts: int) -> float:
    """Big steps while the estimate is uncertain, small once there's evidence."""
    return 80.0 if attempts < 5 else 48.0 if attempts < 15 else 28.0


def updated(learner: float, puzzle: float, score: float, attempts: int) -> int:
    return clamp(learner + k_factor(attempts) * (score - expected(learner, puzzle)))
