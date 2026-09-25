"""Move classification.

Design notes (important — this is a product-defining policy):

We classify a user's move by the *evaluative loss* it incurs, measured by the
engine itself, NOT by raw centipawn deltas alone:

    loss = score(best move line) - score(after the user's move)
           (both converted to the user's point of view)

Thresholds are a documented policy in one place, with two position-aware
modifiers so we don't blindly trust arbitrary numbers:

1. Missed forced mate — if the engine saw a forced mate for the user and the
   user's move does not keep it, escalate at least to MISTAKE (a mate > any
   cp evaluation).

2. Decided-position damping — when the position is already completely decided
   for or against the user (|eval| >= 700 cp both before and after, same sign),
   a BLUNDER is de-escalated to MISTAKE. The game outcome is not at stake, so
   calling it a catastrophic blunder would be noise; the explanation still
   tells the user what they missed.

These thresholds live only here; tuning them should never require touching
engine or UI code.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import chess

# Effective centipawn value used when comparing against mate scores.
MATE_CP = 10000
MATE_STEP_CP = 10  # per move closer to/away from mate

# Loss thresholds in centipawns (from the mover's perspective).
EXCELLENT_CP = 15
GOOD_CP = 50
INACCURATE_CP = 120
MISTAKE_CP = 300

DECIDED_POSITION_CP = 700  # |eval| above this counts as "decided"


class Classification(str, Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    INACCURATE = "inaccurate"
    MISTAKE = "mistake"
    BLUNDER = "blunder"


LABELS = {
    Classification.EXCELLENT: "Excellent",
    Classification.GOOD: "Good",
    Classification.INACCURATE: "Inaccurate",
    Classification.MISTAKE: "Mistake",
    Classification.BLUNDER: "Blunder",
}

_ORDER = [
    Classification.EXCELLENT,
    Classification.GOOD,
    Classification.INACCURATE,
    Classification.MISTAKE,
    Classification.BLUNDER,
]


@dataclass(frozen=True)
class Score:
    """Engine score from White's point of view (canonical in this codebase)."""

    kind: str  # "cp" | "mate"
    value: int

    @classmethod
    def from_pov_white(cls, score: chess.engine.PovScore) -> "Score":
        white = score.white()
        mate = white.mate()
        if mate is not None:
            return cls("mate", int(mate))
        cp = white.score()
        return cls("cp", int(cp if cp is not None else 0))

    def to_cp(self, white_pov: bool = True) -> int:
        """Convert to centipawns; mates become ±(MATE_CP - moves*MATE_STEP)."""
        if self.kind == "cp":
            cp = self.value
        elif self.value > 0:
            cp = MATE_CP - self.value * MATE_STEP_CP
        else:
            cp = -MATE_CP + (-self.value) * MATE_STEP_CP
        return cp if white_pov else -cp

    def for_side(self, is_white: bool) -> int:
        return self.to_cp(white_pov=True) if is_white else self.to_cp(white_pov=False)

    def is_mate_for(self, is_white: bool) -> bool:
        if self.kind != "mate":
            return False
        return (self.value > 0) if is_white else (self.value < 0)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}


def classify_loss(loss_cp: int) -> Classification:
    if loss_cp <= EXCELLENT_CP:
        return Classification.EXCELLENT
    if loss_cp <= GOOD_CP:
        return Classification.GOOD
    if loss_cp <= INACCURATE_CP:
        return Classification.INACCURATE
    if loss_cp <= MISTAKE_CP:
        return Classification.MISTAKE
    return Classification.BLUNDER


def _escalate(category: Classification) -> Classification:
    idx = min(_ORDER.index(category) + 1, len(_ORDER) - 1)
    return _ORDER[idx]


def _deescalate(category: Classification) -> Classification:
    idx = max(_ORDER.index(category) - 1, 0)
    return _ORDER[idx]


def classify_move(
    board_before: chess.Board,
    user_move: chess.Move,
    eval_before: Score,
    eval_after: Score,
) -> tuple[Classification, int, list[str]]:
    """Classify the user's move.

    Args:
        board_before: position before the user's move (user to move).
        user_move: the legal move the user played.
        eval_before: engine score of the position (White POV), best play.
        eval_after: engine score after the user's move (White POV), opponent
            best play — i.e. what the user's move actually achieves.

    Returns:
        (category, loss_cp, notes) — notes are machine-readable modifier flags
        that the teacher can use in its explanation.
    """
    user_is_white = board_before.turn
    before_cp = eval_before.for_side(user_is_white)
    after_cp = eval_after.for_side(user_is_white)
    loss = max(0, before_cp - after_cp)
    notes: list[str] = []

    category = classify_loss(loss)

    # Modifier 1: missed forced mate escalates.
    if eval_before.is_mate_for(user_is_white) and not eval_after.is_mate_for(user_is_white):
        if category in (Classification.GOOD, Classification.EXCELLENT, Classification.INACCURATE):
            category = Classification.MISTAKE
        notes.append("missed_mate")

    # Modifier 2: already-decided positions dampen the harshest label.
    if (
        category == Classification.BLUNDER
        and abs(before_cp) >= DECIDED_POSITION_CP
        and abs(after_cp) >= DECIDED_POSITION_CP
        and (before_cp > 0) == (after_cp > 0)
    ):
        category = Classification.MISTAKE
        notes.append("decided_position")

    return category, loss, notes
