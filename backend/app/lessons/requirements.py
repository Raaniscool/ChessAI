"""Completion guard: a lesson built for an exact request only counts when it still satisfies it.

Plans for exact material ("two rooks against a queen") store their requirements in each
lesson: the material spec, the focus, and every position used with the learner's side. When
the learner finishes, the positions are checked again with the same rule as retrieval and
plan validation (planner.custom.satisfy). A mismatch — e.g. a lesson saved by an older
version that taught rook-against-queen positions for a two-rooks request — is INVALID_LESSON:
no "lesson complete" message, nothing recorded as done.
"""
from __future__ import annotations

import chess

INVALID = "INVALID_LESSON"


def material_requirements(spec, objective: str | None, examples) -> dict:
    """What a lesson for `spec` promises (stored in the lesson dict)."""
    positions = []
    for ex in examples:
        rep = ex.replay()
        key = ex.key_ply if ex.key_ply is not None else 0
        board = rep.boards[key]
        positions.append({"id": ex.id, "start": rep.boards[0].fen(), "fen": board.fen(),
                          "learner": "white" if board.turn == chess.WHITE else "black",
                          "category": ex.category,
                          "objective": (getattr(ex, "concept_params", None) or {}).get("objective")})
    return {"kind": "material", "material": spec.as_dict(), "id": spec.slug(), "label": spec.label(),
            "objective": objective or "general", "positions": positions}


def problems(lesson) -> list[str]:
    """Why the lesson no longer satisfies its request ([] = it does, or it has no requirements)."""
    req = getattr(lesson, "requirements", None) or {}
    if req.get("kind") != "material":
        return []
    from ..planner.custom.satisfy import board_problems
    from ..planner.intent.model import MaterialSpec
    try:
        spec = MaterialSpec.from_dict(req["material"])
    except (KeyError, ValueError, TypeError) as exc:
        return [f"the lesson's requirements are unreadable ({exc})"]
    objective = req.get("objective") or "general"
    out = []
    positions = req.get("positions") or []
    if not positions:
        out.append("the lesson has no verified positions for the request")
    known = set()
    for p in positions:
        try:
            start, board = chess.Board(p["start"]), chess.Board(p["fen"])
        except (KeyError, ValueError) as exc:
            out.append(f"a stored position is unreadable ({exc})")
            continue
        known |= {start.board_fen(), board.board_fen()}
        found = board_problems(spec, p.get("category"), start, board, None,
                               objective if spec.head == "endgame" else None)
        if objective != "general" and p.get("objective") != objective:
            found.append(f"not verified for the requested focus ({objective})")
        out += [f"{p.get('id', 'a position')}: {x}" for x in found]
    for step in lesson.steps:  # every position shown must be one of the checked ones
        fen = getattr(step, "fen", None)
        if fen and chess.Board(fen).board_fen() not in known:
            out.append("the lesson shows a position that wasn't checked against the request")
            break
    return out
