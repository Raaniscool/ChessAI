"""Choose the puzzles that are most useful *now* for one weakness or concept.

Deterministic and documented, so every choice has a reason a learner can read.

1. Relevance (which puzzles train the weakness)
     1.0   the concept itself or a sub-concept (hung_piece -> hanging_queen)
     1.0   a personal puzzle generated for exactly this weakness
     0.95  a concept the generator trains for it (generation.generator.PLANS,
           e.g. walked_into_fork -> knight_fork, hung_piece -> hanging_piece)
     0.5   a concept the Knowledge Library lists as related
     x0.7  when the match is only a secondary concept of the puzzle
   Puzzles below 0.5 are never chosen.
2. Level: the learner's rating for the concept (learner.views.target_rating, "practice"),
   nudged by recent results on these puzzles (fast clean solves raise it, hints/misses/slow
   solves lower it). The set spans target-150 .. target+150, one "slot" per puzzle, so it
   runs easy -> medium -> hard. A puzzle more than 600 points away from its slot is never used.
3. Novelty / spaced retry: new puzzles first; a puzzle missed at least a day ago comes back
   as a retry. Never repeated: puzzles shown in the last day, solved in the last 3 days, or
   missed earlier the same day. Solved 3-14 days ago ranks low, older comes back as review.
4. Quality: a single clear solution is preferred; puzzles with several accepted moves are
   fine (all of them are accepted); entries without an engine record rank last.
5. Variety: never the same position twice; a small penalty for repeating a sub-concept.

    slot score = relevance * (0.50 * fit + 0.35 * novelty + 0.15 * quality) - variety
    fit        = exp(-((rating - slot_rating) / 250)^2)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Puzzle

SPREAD = 150
MAX_GAP = 600
FIT_WIDTH = 250
MIN_RELEVANCE = 0.5
MIN_NOVELTY = 0.1     # below this a puzzle is too recent to repeat (generation fills the gap)
QUALITY = {"unique": 1.0, "multiple": 0.85, "unchecked": 0.6}
RECENT_WINDOW = 3     # resolved puzzles needed before the level is nudged


@dataclass
class Choice:
    puzzle: Puzzle
    score: float
    slot_rating: int
    reasons: list[str]
    match: str
    stats: dict

    def as_dict(self) -> dict:
        return {**self.puzzle.as_dict(), "score": round(self.score, 3), "slot_rating": self.slot_rating,
                "reasons": self.reasons, "match": self.match, "stats": self.stats}


@dataclass
class Selection:
    concept: str
    target_rating: int
    chosen: list[Choice] = field(default_factory=list)
    considered: int = 0
    level_note: str | None = None
    requested: int = 0

    @property
    def shortfall(self) -> int:
        return max(0, self.requested - len(self.chosen))

    def as_dict(self) -> dict:
        return {"concept": self.concept, "target_rating": self.target_rating, "considered": self.considered,
                "requested": self.requested, "shortfall": self.shortfall, "level_note": self.level_note,
                "puzzles": [c.as_dict() for c in self.chosen]}


def targets(concept: str, knowledge) -> dict[str, tuple[float, str]]:
    """concept id -> (relevance, kind) for puzzles that train `concept`."""
    from ..knowledge.generation.generator import PLANS

    out: dict[str, tuple[float, str]] = {}

    def add(cid: str, weight: float, kind: str) -> None:
        if cid not in knowledge.concepts:
            return
        for d in knowledge.descendants(cid):
            if out.get(d, (0.0, ""))[0] < weight:
                out[d] = (weight, kind)

    add(concept, 1.0, "exact")
    for target, motif in PLANS.get(concept, []):
        add(target, 0.95, "trains")
        add(motif, 0.95, "trains")
    c = knowledge.concepts.get(concept)
    for rel in (c.related if c else []):
        add(rel, 0.5, "related")
    return out


def relevance(puzzle: Puzzle, concept: str, wanted: dict[str, tuple[float, str]]) -> tuple[float, str]:
    if puzzle.tier == "personal" and puzzle.weakness == concept:
        return 1.0, "personal"
    if puzzle.concept in wanted:
        return wanted[puzzle.concept]
    best = max((wanted[c] for c in puzzle.concepts if c in wanted), default=(0.0, ""), key=lambda t: t[0])
    return best[0] * 0.7, (best[1] + "-secondary") if best[1] else ""


def _days(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds() / 86400)


def novelty(stats: dict, now: datetime) -> tuple[float, str | None]:
    if not stats or not stats.get("seen"):
        return 1.0, "New to you"
    days = _days(stats.get("last_resolved") or stats.get("last_used"), now)
    last = stats.get("last_result")
    if last in ("failed", "revealed"):
        if days is not None and days >= 1:
            return 0.9, f"Retry: you missed this {_ago(days)}"
        return 0.0, None
    if last is None:  # shown before but never finished
        return (0.0, None) if days is None or days < 1 else (0.7, None)
    if days is None or days < 3:
        return 0.0, None
    if days < 14:
        return 0.3, None
    return 0.6, f"Review: you solved this {_ago(days)}"


def _ago(days: float) -> str:
    d = int(days)
    return "yesterday" if d == 1 else f"{d} days ago" if d < 60 else "a while ago"


def level(base: int, recent: list[dict]) -> tuple[int, str | None]:
    """Nudge the concept rating by the latest finished puzzles of this concept."""
    if len(recent) < RECENT_WINDOW:
        return base, None
    n = len(recent)
    clean = sum(1 for s in recent if s.get("last_result") == "solved_first_try") / n
    missed = sum(1 for s in recent if s.get("last_result") in ("failed", "revealed")) / n
    times = [s["average_seconds"] for s in recent if s.get("average_seconds") is not None]
    slow = bool(times) and sum(times) / len(times) > 150
    if missed >= 0.5:
        return base - 100, "Easier set: you missed several of these recently"
    if slow:
        return base - 50, "A bit easier: recent puzzles of this kind took you a while"
    if clean >= 0.8:
        return base + 80, "Harder set: you've been solving these cleanly"
    return base, None


def slots(target: int, count: int) -> list[int]:
    if count <= 1:
        return [target]
    step = 2 * SPREAD / (count - 1)
    return [round(target - SPREAD + i * step) for i in range(count)]


def select(puzzles: list[Puzzle], knowledge, concept: str, count: int = 5, profile=None, usage=None,
           exclude: set[str] | frozenset = frozenset(), now: datetime | None = None,
           concept_name: str | None = None) -> Selection:
    from ..learner.views import target_rating

    now = now or datetime.now(timezone.utc)
    wanted = targets(concept, knowledge)
    stats_of = usage.puzzle_stats if usage is not None else (lambda _id: {})
    base = target_rating(profile, concept, "practice", knowledge) if profile is not None else 1200

    pool: list[tuple[Puzzle, float, str, dict]] = []
    for p in puzzles:
        if p.id in exclude or not p.clear_start:   # no clear first decision: not a puzzle
            continue
        rel, kind = relevance(p, concept, wanted)
        if rel < MIN_RELEVANCE:
            continue
        pool.append((p, rel, kind, stats_of(p.id)))

    recent = sorted((s for _p, _r, _k, s in pool if s.get("last_resolved")),
                    key=lambda s: s["last_resolved"], reverse=True)[:6]
    target, note = level(base, recent)
    selection = Selection(concept=concept, target_rating=target, considered=len(pool), level_note=note,
                          requested=count)
    name = concept_name or (knowledge.concepts[concept].name if concept in knowledge.concepts else concept)

    used_ids: set[str] = set()
    used_boards: set[str] = set()
    used_concepts: dict[str, int] = {}
    for slot in slots(target, count):
        best: tuple[float, Puzzle, float, str, dict, str | None] | None = None
        for p, rel, kind, st in pool:
            board = p.fen.split(" ")[0]
            if p.id in used_ids or board in used_boards or abs(p.rating - slot) > MAX_GAP:
                continue
            nov, nov_reason = novelty(st, now)
            if nov < MIN_NOVELTY:
                continue
            fit = math.exp(-((p.rating - slot) / FIT_WIDTH) ** 2)
            score = rel * (0.50 * fit + 0.35 * nov + 0.15 * QUALITY.get(p.uniqueness, 0.6))
            score -= 0.05 * used_concepts.get(p.concept, 0)
            key = (score, -abs(p.rating - slot), p.id)
            if best is None or key > (best[0], -abs(best[1].rating - slot), best[1].id):
                best = (score, p, rel, kind, st, nov_reason)
        if best is None:
            continue
        score, p, rel, kind, st, nov_reason = best
        used_ids.add(p.id)
        used_boards.add(p.fen.split(" ")[0])
        used_concepts[p.concept] = used_concepts.get(p.concept, 0) + 1
        selection.chosen.append(Choice(p, score, slot, _reasons(p, kind, name, slot, nov_reason, knowledge),
                                       kind, st))
    selection.chosen.sort(key=lambda c: (c.puzzle.rating, c.puzzle.id))
    return selection


def _reasons(p: Puzzle, kind: str, name: str, slot: int, nov_reason: str | None, knowledge) -> list[str]:
    pname = knowledge.concepts[p.concept].name if p.concept in knowledge.concepts else p.concept
    base_kind = kind.replace("-secondary", "")
    if kind.endswith("-secondary"):
        why = f"{pname} that also uses {name.lower()}"
    elif base_kind == "personal":
        why = f"Made for your {name.lower()} weakness"
    elif base_kind == "exact":
        why = f"Trains {name.lower()}" + (f" ({pname.lower()})" if pname != name else "")
    elif base_kind == "trains":
        why = f"{pname} trains the skill behind {name.lower()}"
    else:
        why = f"{pname} is closely related to {name.lower()}"
    out = [why]
    gap = p.rating - slot
    out.append("Right at your level" if abs(gap) <= 100 else "A step up" if gap > 0 else "A warm-up")
    if nov_reason:
        out.append(nov_reason)
    out.append("One clear solution" if p.uniqueness == "unique"
               else "Several good moves are accepted" if p.uniqueness == "multiple" else "")
    return [r for r in out if r]
