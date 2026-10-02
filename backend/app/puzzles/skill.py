"""Which puzzles really train a specific weakness (skill targeting).

A weakness is specific: "missed knight forks" is not "forks", and a queen fork doesn't train it.
For a weakness set, a library puzzle *satisfies* a slot only when it trains the same skill:

  exact     its concept is the weakness concept (or a narrower one)
  trains    the generator's mapping for the weakness (walked into forks -> knight forks ...)
  personal  it was built for this weakness
  proven    a broader concept ("fork") whose verified facts prove the specific skill
            (the forking piece is a knight)

Everything else is *partial* — a related concept, a puzzle that merely also uses the idea, a
broader category without proof. Partial matches never fill a weakness slot: the shortfall goes
to constrained generation instead (puzzle_api / game_api). Practice by theme is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass

import chess

from ..analysis.motifs import FORK_CONCEPT
from .select import relevance, targets

SATISFYING = 0.95  # relevance of exact / trains / personal matches (select.targets)
FORK_PIECE = {concept: chess.piece_name(ptype) for ptype, concept in FORK_CONCEPT.items()}  # knight_fork -> knight


@dataclass(frozen=True)
class TrainingSkill:
    concept: str
    attacker: str | None = None   # the piece that must carry out the idea (forks)
    family: str | None = None     # the broader concept the facts are checked under ("fork")

    def describe(self) -> str:
        return f"{self.concept}" + (f" (attacker: {self.attacker})" if self.attacker else "")


def ancestors(knowledge, concept: str) -> set[str]:
    out, todo = set(), [concept]
    while todo:
        c = knowledge.concepts.get(todo.pop())
        for parent in (getattr(c, "parents", None) or []) if c else []:
            if parent not in out:
                out.add(parent)
                todo.append(parent)
    return out


def skill_for(concept: str) -> TrainingSkill:
    piece = FORK_PIECE.get(concept)
    return TrainingSkill(concept, attacker=piece, family="fork" if piece else None)


def proven(puzzle, skill: TrainingSkill, knowledge) -> bool:
    """A broader-concept puzzle whose verified facts show exactly this skill."""
    if not skill.attacker or not skill.family or skill.family not in knowledge.concepts:
        return False
    if puzzle.concept not in ({skill.family} | ancestors(knowledge, skill.concept)):
        return False
    fork = (puzzle.facts or {}).get("fork") or {}
    return ((fork.get("attacker") or {}).get("piece") or "").lower() == skill.attacker


def classify(puzzle, skill: TrainingSkill, knowledge, wanted=None) -> tuple[str, float]:
    """("satisfies" | "partial" | "none", relevance)."""
    wanted = wanted if wanted is not None else targets(skill.concept, knowledge)
    rel, _kind = relevance(puzzle, skill.concept, wanted)
    fork = (puzzle.facts or {}).get("fork") or {}
    piece = ((fork.get("attacker") or {}).get("piece") or "").lower()
    if rel >= SATISFYING:
        if skill.attacker and piece and piece != skill.attacker:
            return "partial", rel   # labelled right, but the facts show another piece
        return "satisfies", rel
    if proven(puzzle, skill, knowledge):
        return "satisfies", SATISFYING
    return ("partial", rel) if rel > 0 else ("none", 0.0)


def split(puzzles, concept: str, knowledge):
    """(satisfying puzzles, {id: relevance} overrides for proven ones, number of partial matches)."""
    skill = skill_for(concept)
    wanted = targets(concept, knowledge)
    keep, overrides, partial = [], {}, 0
    for p in puzzles:
        verdict, rel = classify(p, skill, knowledge, wanted)
        if verdict == "satisfies":
            keep.append(p)
            if relevance(p, concept, wanted)[0] < SATISFYING:
                overrides[p.id] = (rel, "exact")
        elif verdict == "partial":
            partial += 1
    return keep, overrides, partial
