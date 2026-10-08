"""Progression inside one skill: which *kind* of recognition problem to train next.

Rating alone can't tell "solves obvious knight forks" from "finds the fork when five moves look
reasonable". Every puzzle's critical decision is already profiled (puzzles.profile), so each one
falls in a recognition stage:

    spot     the key move is a check or capture with few real candidates (< 4)
    choose   several plausible candidates (4-6) and only one works
    deep     a quiet key move, 7+ plausible candidates, or more than one real decision

Defensive positions (find the move that stops the threat) are a separate track, served by the
set's "Defend" item (puzzle_api), because they train a different question.

The learner's results per stage (whole puzzles, first-try = critical move found at once without
a mistake, hint or reveal) decide the focus:

    focus = the first stage that isn't mastered (>= 3 puzzles, >= 75% first-try)

and selection prefers puzzles of the focus stage (+FOCUS_BONUS), a little of the next stage, and
avoids stages already mastered (MASTERED_PENALTY). So a learner who keeps solving easy forks is
moved to forks with more candidates — not just to higher-rated puzzles of the same easy kind —
and a learner who misses the "choose" puzzles keeps getting them until they land.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STAGES = ("spot", "choose", "deep")
LABEL = {"spot": "Spot it", "choose": "Choose among candidates", "deep": "Quiet moves & deeper ideas"}
DESCRIBE = {"spot": "the key move is a check or capture",
            "choose": "several moves look reasonable and only one works",
            "deep": "a quiet key move or more than one real decision"}
MASTERY_ATTEMPTS = 3
MASTERY_CLEAN = 0.75
FOCUS_BONUS = 0.12
NEXT_BONUS = 0.04
MASTERED_PENALTY = 0.06


def stage_of(puzzle) -> str:
    basis = puzzle.difficulty_basis or {}
    plausible = int(basis.get("plausible") or 1)
    if basis.get("quiet") or plausible >= 7 or puzzle.meaningful_moves >= 2:
        return "deep"
    if plausible >= 4:
        return "choose"
    return "spot"


@dataclass
class Ladder:
    bands: dict = field(default_factory=lambda: {s: {"attempts": 0, "clean": 0} for s in STAGES})
    focus: str = "spot"
    note: str | None = None

    def mastered(self, stage: str) -> bool:
        b = self.bands[stage]
        return b["attempts"] >= MASTERY_ATTEMPTS and b["clean"] / b["attempts"] >= MASTERY_CLEAN

    def bonus(self, puzzle) -> float:
        s = stage_of(puzzle)
        if s == self.focus:
            return FOCUS_BONUS
        i, f = STAGES.index(s), STAGES.index(self.focus)
        if i == f + 1:
            return NEXT_BONUS
        return -MASTERED_PENALTY if i < f and self.mastered(s) else 0.0

    def as_dict(self) -> dict:
        return {"focus": self.focus, "label": LABEL[self.focus], "note": self.note, "bands": self.bands}


def ladder(puzzles, stats_of) -> Ladder:
    """The ladder from the learner's finished puzzles among `puzzles` (those of this skill)."""
    lad = Ladder()
    for p in puzzles:
        st = stats_of(p.id) or {}
        n = int(st.get("attempts") or 0)
        if not n:
            continue
        b = lad.bands[stage_of(p)]
        b["attempts"] += n
        b["clean"] += round((st.get("first_try_rate") or 0) * n)
    lad.focus = next((s for s in STAGES if not lad.mastered(s)), STAGES[-1])
    f = STAGES.index(lad.focus)
    fb = lad.bands[lad.focus]
    if f > 0:
        lower = lad.bands[STAGES[f - 1]]
        lad.note = (f"You solve the ones where {DESCRIBE[STAGES[f - 1]]} ({lower['clean']}/{lower['attempts']} "
                    f"first try) — next: {DESCRIBE[lad.focus]}")
    elif fb["attempts"] >= 2 and fb["clean"] / fb["attempts"] < 0.5:
        lad.note = f"Building the basics first: positions where {DESCRIBE['spot']}"
    return lad
