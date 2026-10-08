"""Training (lesson) difficulty from the same skill assessment the Puzzles tab uses.

The Puzzles tab aims every set with learner.difficulty: a per-skill estimate built from real
evidence — the learner's analyzed games (error rates overall/opening/endgame, tactics walked
into, winning chances found or missed, split into pattern recognition and calculation), the
rating only as a weak prior, and every finished puzzle or lesson exercise — then a per-concept
estimate (the concept's skill, a little lower for a weakness from the games, moved by results
on that concept). Lessons use the same numbers, so one skill profile drives both:

    target   = the concept's puzzle target (about 58% expected success) + PURPOSE_OFFSET
    examples = verified library examples ranked by their puzzle rating (the same scale)

The lesson's purpose still comes from the learner model (learner.views.lesson_shape): a new
idea is introduced a little below the target, a weak spot lower still, a mastered one above.

It adapts gradually, per concept:
  - every lesson exercise is recorded as a puzzle result (session -> usage.record_resolved),
    so the next lesson on that concept — and only that concept's skill — starts from the
    updated estimate (Elo performance with an anchor: one result moves it a little);
  - inside a lesson, learner.adapt swaps at most MAX_CHANGES examples, one step at a time,
    on the same scale.

Deterministic, no AI; every target carries its evidence (`calibration` in the plan debug).
"""
from __future__ import annotations

from typing import Callable

PURPOSE_OFFSET = {"learn": -60, "simplify": -120, "practice": 0, "challenge": 100}
PRACTICE_STEP = 100   # the review/practice part of a lesson aims this much higher (inside the zone)

# The skill profile as named for the learner (learner.difficulty.SKILLS -> field)
NAMED_SKILLS = {
    "overall_skill_level": "overall", "tactical_skill": "tactics", "calculation_skill": "calculation",
    "defensive_skill": "defense", "opening_skill": "opening", "endgame_skill": "endgame",
    "pattern_recognition": "pattern_recognition",
}


def rating_of(library=None) -> Callable:
    """Example -> rating on the puzzle scale (puzzle index rating; else the library's estimate)."""
    from ..knowledge.difficulty import puzzle_rating
    try:
        from ..puzzles import get_puzzles
        index = get_puzzles(library)
    except Exception:  # no puzzle index: the library estimate (a close, coarser scale)
        index = None

    def rate(example) -> int:
        p = index.get(example.id) if index is not None else None
        return int(p.rating) if p is not None else puzzle_rating(example)
    return rate


def calibrated_target(profile, concept: str | None, purpose: str, library, usage=None) -> dict | None:
    """The lesson's difficulty target for `concept`, from the learner's skill profile (None: no data)."""
    from .difficulty import _clamp, for_learner
    try:
        dp = for_learner(library, profile, usage)
        aim = dp.target(concept, library)
    except Exception:  # the calibration must never break planning a lesson
        return None
    offset = PURPOSE_OFFSET.get(purpose, 0)
    target = _clamp(aim["target"] + offset)
    return {
        "target": target, "practice": _clamp(target + PRACTICE_STEP), "purpose": purpose, "offset": offset,
        "skill": aim["skill"], "skill_level": aim["skill_level"], "estimate": aim["estimate"],
        "zone": aim["zone"], "summary": aim["summary"], "concept": aim["concept"],
        "evidence": aim["skill_evidence"],
        "rule": aim["rule"] + (f"; {offset:+d} for a {purpose} lesson" if offset else ""),
    }


def skill_profile(profile=None, library=None, usage=None) -> dict | None:
    """The learner's skill profile under the names the coach uses, each with level and evidence."""
    from .difficulty import for_learner
    try:
        dp = for_learner(library, profile, usage)
    except Exception:
        return None
    out = {name: {"skill": key, **dp.skills[key].as_dict()} for name, key in NAMED_SKILLS.items()}
    return {"skills": out, "games": dp.games, "puzzles": dp.puzzles, "prior": dp.prior}
