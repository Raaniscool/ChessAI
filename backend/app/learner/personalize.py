"""Learner model → how a lesson is built (which examples, in which roles, with what words).

The Knowledge Library supplies verified building blocks; this decides how to use them
for *this* learner. Two learners asking "teach me knight forks" get different lessons:

    new beginner       two worked examples, a guided one, then one to solve; easy
                       positions; a reminder of how the knight moves; prerequisites first
    club player        one example, then positions to solve, rated near their level
    mastered it        no demonstrations, harder positions ("challenge")
    struggling         clearer positions first ("simplify"), a guided step
    calculation gap    positions that need several moves, not one-movers
    puzzles ≠ games    positions from real games

A learner the tutor knows nothing about (no onboarding, no results) gets the library's
defaults unchanged.
"""
from __future__ import annotations

import re

from .profile import LearnerProfile
from .views import concept_view, lesson_shape

PIECES = {
    "knight": "a knight moves in an L: two squares one way, then one to the side, and it jumps over pieces",
    "bishop": "a bishop moves any distance diagonally and stays on squares of one colour",
    "rook": "a rook moves any distance along a rank or a file",
    "queen": "the queen moves like a rook and a bishop together: any distance, straight or diagonally",
    "pawn": "a pawn moves straight ahead but captures one square diagonally forward",
    "king": "the king moves one square in any direction and may never move into check",
}


def personalized(profile: LearnerProfile | None) -> bool:
    """Is there anything to personalize from? (A blank profile keeps the defaults.)"""
    return profile is not None and not profile.is_new


def roles_for(shape: dict) -> list[str]:
    roles = ["demonstration"] * shape["demos"]
    if shape["purpose"] == "challenge":
        return roles + ["practice"] * 3
    if shape["level"] != "advanced" or shape["purpose"] == "simplify":
        roles.append("guided")
    while len(roles) < 3:
        roles.append("practice")
    if len(roles) < 4 and shape["level"] == "beginner" and shape["demos"] >= 2:
        roles.append("practice")
    return roles


def _multi_move_only(library, concepts: list[str]) -> set[str]:
    """One-move examples to leave out when the learner needs calculation practice."""
    out: set[str] = set()
    for cid in concepts:
        examples = library.examples_for(cid)
        longer = [e for e in examples if e.key_ply is not None and len(e.moves) - e.key_ply >= 3]
        if len(longer) >= 3:
            out |= {e.id for e in examples if e.id not in {x.id for x in longer}}
    return out


def retrieval_settings(profile: LearnerProfile, concept: str | None, concepts: list[str], library) -> dict:
    """Keyword arguments for RetrievalRequest, plus the shape they came from."""
    shape = lesson_shape(profile, concept, library)
    known = {c for c, st in profile.concepts.items() if st.attempts or st.lessons_completed}
    settings = {
        "level": shape["level"],
        "target_rating": shape["target_rating"],
        "practice_rating": shape["target_rating"] + 120,
        "roles": roles_for(shape),
        "practice_count": shape["practice"],
        "include_prerequisites": shape["prerequisites"],
        "known_concepts": known,
        "prefer_real": shape["realistic"],
        "weak_concepts": [c for c in concepts_weak(profile, library) if c != concept],
        "exclude": _multi_move_only(library, concepts) if shape["calculation"] else set(),
    }
    return {"settings": settings, "shape": shape}


def concepts_weak(profile: LearnerProfile, library=None) -> list[str]:
    return [c for c in profile.concepts if concept_view(profile, c, library)["status"] == "weak"]


def reason(shape: dict, profile: LearnerProfile, concept: str | None, name: str) -> str:
    """One sentence telling the learner why the lesson looks the way it does."""
    subject = name.lower()
    weakness = next((w for w in profile.weaknesses if concept and w.get("concept") == concept
                     and w.get("tier") == "recurring"), None)
    if shape["realistic"]:
        text = (f"You solve {subject} puzzles well but it still slips by in your games, so these positions "
                "come from real games.")
    elif shape["calculation"]:
        text = f"You spot {subject} ideas well; these positions need a few moves of calculation to finish."
    elif shape["status"] == "mastered":
        text = f"You've already got the hang of {subject}, so these are harder positions to keep you sharp."
    elif shape["status"] == "weak":
        text = f"{name} gave you some trouble before, so we'll start with clearer positions and build up."
    elif shape["status"] in ("practicing", "learned"):
        text = f"You've practised {subject} before, so these positions are a step up."
    elif shape["level"] == "advanced":
        text = "One quick example, then straight to positions you solve yourself."
    elif shape["demos"] >= 2:
        text = f"Since this is new, I'll show you {shape['demos']} examples before you try one."
    else:
        text = "I'll show you one example, then you'll find the key move yourself."
    if weakness and not shape["realistic"]:
        games = weakness.get("game_count")
        total = weakness.get("total_games")
        if games and total:
            text += f" It came up in {games} of your last {total} games."
    return text


def piece_reminder(library, concept: str | None) -> str | None:
    """How the piece at the heart of the idea moves (for players who are just starting)."""
    if not concept or concept not in library.concepts:
        return None
    c = library.concepts[concept]
    words = set(re.findall(r"[a-z]+", " ".join([concept.replace("_", " "), c.name.lower()])))
    for piece, text in PIECES.items():
        if piece in words:
            return f"Quick reminder: {text}."
    return None


def plan_note(profile: LearnerProfile, shape: dict, roles: list[str], why: str) -> dict:
    """What the plan remembers about how it was personalized (shown in the UI)."""
    return {"rating": profile.rating, "level": shape["level"], "status": shape["status"],
            "gaps": shape["gaps"], "target_rating": shape["target_rating"], "roles": roles, "reason": why,
            "purpose": shape["purpose"]}
