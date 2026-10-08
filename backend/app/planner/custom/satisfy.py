"""Does this position satisfy the learner's exact request? One rule, used everywhere.

Retrieval (content.material), the plan validator (validate.py) and the request-satisfaction
check all call `position_satisfies`, so a position can't be "close enough" in one place and
rejected in another. For a material endgame request the position must:

  * for two-sided requests (X against Y): be an endgame position — not a mate puzzle or
    tactic that happens to have those pieces, unless the requested focus is itself tactical
    (attacking the king / winning material). One-sided requests keep their old categories.
  * have exactly the requested material per side at the start AND when the learner moves,
  * give the learner the requested side (owner="learner": the learner holds `spec.pieces`).

Nothing approximate is accepted unless the learner explicitly allowed related material.
"""
from __future__ import annotations

import chess

from ..intent.model import MaterialSpec

ENDGAME_CATEGORIES = frozenset({"endgames"})
TACTICAL_OBJECTIVES = frozenset({"attack_king", "win_material"})
TACTICAL_CATEGORIES = frozenset({"endgames", "tactics"})
MATE_CATEGORIES = frozenset({"checkmates", "tactics", "endgames"})
ONE_SIDED_CATEGORIES = frozenset({"endgames", "tactics", "checkmates", "mistakes"})


def categories_for(spec: MaterialSpec, objective: str | None = None) -> frozenset[str]:
    if spec.head == "mate":
        return MATE_CATEGORIES
    if spec.relation != "versus":  # one-sided material ("rook endgames", "the bishop pair"): as before
        return ONE_SIDED_CATEGORIES
    return TACTICAL_CATEGORIES if objective in TACTICAL_OBJECTIVES else ENDGAME_CATEGORIES


def board_problems(spec: MaterialSpec, category: str | None, start: chess.Board, board: chess.Board,
                   final: chess.Board | None = None, objective: str | None = None) -> list[str]:
    """Reasons a position fails the request ([] = it satisfies it). `start` is the first
    position of the exercise, `board` the one where the learner makes the key move."""
    problems = []
    # a known non-endgame category (a mate puzzle, a tactic) is excluded; an unclassified new
    # position (proposed for this plan) is judged by its material and by Stockfish
    if category is not None and category not in categories_for(spec, objective):
        problems.append(f"a {category} position, not {spec.label().lower()}")
    learner = board.turn
    for b, where in ((start, "at the start"), (board, "when the learner moves")):
        if not spec.matches(b, learner):
            problems.append(f"{where} the material isn't {spec.short()} for the learner")
            break
    if spec.head == "mate":
        if final is None or not final.is_checkmate():
            problems.append("doesn't end in checkmate")
        elif spec.winner(board) != learner:  # None (nobody / both) fails too
            problems.append("the learner isn't the side with the mating pieces")
    return problems


def objective_problems(objective: str | None, example) -> list[str]:
    """A focused request ("avoid perpetual checks") needs positions verified for that focus."""
    if not objective or objective == "general":
        return []
    got = (getattr(example, "concept_params", None) or {}).get("objective")
    return [] if got == objective else [f"not verified for the requested focus ({objective})"]


def item_satisfies(spec: MaterialSpec, item, objective: str | None = None) -> list[str]:
    """board_problems + objective_problems for a ContentItem."""
    ex = item.example
    return board_problems(spec, getattr(ex, "category", None), ex.replay().boards[0], item.board, item.final,
                          objective) + objective_problems(objective, ex)
