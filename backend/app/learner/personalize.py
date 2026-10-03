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
    """Is there anything to personalize from? (A blank profile keeps the defaults.)

    Skipping onboarding says nothing about the learner: until they answer or show how they do,
    lessons keep their default shape."""
    if profile is None:
        return False
    told_us = profile.onboarding.get("done") and not profile.onboarding.get("skipped")
    games = int((getattr(profile, "game_skill", None) or {}).get("games") or 0)
    return bool(told_us or profile.concepts or profile.weaknesses or games)


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


def retrieval_settings(profile: LearnerProfile, concept: str | None, concepts: list[str], library,
                       usage=None) -> dict:
    """Keyword arguments for RetrievalRequest, plus the shape they came from."""
    shape = lesson_shape(profile, concept, library)
    known = {c for c, st in profile.concepts.items() if st.attempts or st.lessons_completed}
    # How hard: the same skill profile as the Puzzles tab (games, puzzles and lesson results, per
    # concept; learner.training_level). The learner model's own rating is the fallback.
    from .training_level import calibrated_target, rating_of
    calibration = calibrated_target(profile, concept, shape["purpose"], library, usage)
    rate = None
    if calibration is not None:
        shape["target_rating"] = calibration["target"]
        shape["calibration"] = calibration
        rate = rating_of(library)
    settings = {
        "level": shape["level"],
        "target_rating": shape["target_rating"],
        "practice_rating": calibration["practice"] if calibration else shape["target_rating"] + 120,
        "rating_of": rate,
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
        text = "You're a strong player, so we'll keep the watching short."
    elif shape["demos"] >= 2:
        text = "Since this is new, we'll take it step by step."
    else:
        text = "You know the basics, so we'll keep the watching short."
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


def order_topics(topics: list, profile: LearnerProfile | None, library) -> tuple[list, dict[str, str]]:
    """A plan of several requested topics, in the order this learner needs them.

    Topics the learner has struggled with (or that are due for review) come first; topics they
    have mastered go last, as a quick check. Nothing is added or dropped — only what was asked
    for — and a learner the tutor knows nothing about keeps the catalog's order.
    Returns (topics, {topic_id: why it is where it is})."""
    if not personalized(profile) or not profile.concepts or len(topics) < 2:
        return topics, {}
    by_topic: dict[str, list[str]] = {}
    for cid, concept in library.concepts.items():
        for tid in concept.topics:
            by_topic.setdefault(tid, []).append(cid)
    rank, notes = {}, {}
    for topic in topics:
        views = [concept_view(profile, cid, library) for cid in by_topic.get(topic.id, []) if cid in profile.concepts]
        if any(v["status"] == "weak" for v in views):
            rank[topic.id], notes[topic.id] = 0, "This gave you trouble before, so we start here."
        elif any(v["needs_review"] for v in views):
            rank[topic.id], notes[topic.id] = 0, "It's been a while since you practised this — a refresher first."
        elif views and all(v["status"] == "mastered" for v in views):
            rank[topic.id], notes[topic.id] = 2, "You've mastered this, so it's a quick check at the end."
        else:
            rank[topic.id] = 1
    ordered = sorted(topics, key=lambda t: rank[t.id])  # stable: the catalog's order within each group
    return ordered, notes
