"""The Puzzles tab: personalized weakness cards, practice themes, solver payloads.

Personalized cards come from two kinds of evidence, both already in the learner model:
  games    recurring/occasional patterns from the learner's analyzed games
           (LearnerProfile.weaknesses, written by the Game History report)
  puzzles  concepts where the learner's recent puzzle results are poor
           (ConceptState.recent: 1 = clean solve ... 0 = failed)
Priority = how often it happens in games (share of games, recurring > occasional) and how the
learner is doing on its puzzles right now: recent failures push a weakness up, clean solves push
it down ("improving"). So every solved or failed puzzle changes what is recommended next.

Practice themes are plain concepts from the concept graph (Forks, Pins, Checkmates ...); a theme
is listed only when the verified library has enough puzzles for it. Selection for both modes is
puzzles.select (level fit, novelty, no repeats) — this module only decides *what* to train.
"""
from __future__ import annotations

import re

from .select import MIN_RELEVANCE, relevance, targets

RECOMMENDED = 5
MIN_THEME = 3          # a practice theme needs this many verified puzzles
STRUGGLE_ATTEMPTS = 3  # puzzle-based cards need this many recent attempts in a concept
STRUGGLE_MEAN = 0.5    # ... and a recent average score below this
TIER_WEIGHT = {"recurring": 1.0, "occasional": 0.6}
THEMES = [  # (concept id, label) in display order
    ("fork", "Forks"), ("pin", "Pins"), ("skewer", "Skewers"), ("discovered_attack", "Discovered attacks"),
    ("checkmate", "Checkmates"), ("hanging_piece", "Hanging pieces"), ("beginner_mistakes", "Punishing mistakes"),
    ("spotting_threats", "Defending"), ("deflection", "Deflection"), ("removing_defender", "Removing the defender"),
    ("overloaded_piece", "Overloaded pieces"), ("trapped_piece", "Trapped pieces"), ("sacrifice", "Sacrifices"),
    ("zwischenzug", "In-between moves"), ("endgames", "Endgames"), ("tactics", "Mixed tactics"),
]
OBJECTIVE_TEXT = {"material": "Win material", "defense": "Defend against the threat", "idea": "Find the best move"}


def candidates(puzzles) -> list:
    """Puzzles the tab may serve: a clear first decision, never a position from the learner's games."""
    return [p for p in puzzles if p.clear_start and (p.source or {}).get("type") != "user_game"]


def available(pool, knowledge, concept: str) -> int:
    if concept not in knowledge.concepts:
        return 0
    wanted = targets(concept, knowledge)
    return sum(1 for p in pool if relevance(p, concept, wanted)[0] >= MIN_RELEVANCE)


def themes(pool, knowledge) -> list[dict]:
    out = []
    for cid, label in THEMES:
        n = available(pool, knowledge, cid)
        if n >= MIN_THEME:
            out.append({"concept": cid, "label": label, "count": n})
    return out


def _recent(st) -> list[float]:
    return list(st.recent[-5:]) if st is not None else []


def _progress(st) -> tuple[str | None, float]:
    """(text, factor): how puzzles in this concept are going lately and how that moves priority."""
    recent = _recent(st)
    if len(recent) < 2:
        return None, 1.0
    good = sum(1 for s in recent if s >= 0.75)
    mean = sum(recent) / len(recent)
    text = f"Puzzles lately: {good} of {len(recent)} solved cleanly"
    if mean >= 0.8:
        text += " — improving"
    return text, round(1.35 - 0.8 * mean, 3)  # all failed x1.35 ... all clean x0.55


def _name(knowledge, concept: str | None) -> str:
    if concept and concept in knowledge.concepts:
        return knowledge.concepts[concept].name
    return (concept or "").replace("_", " ").title()


def personalized(profile, knowledge, pool) -> dict:
    """Weakness cards (most useful first) and the profile box."""
    cards: list[dict] = []
    covered: set[str] = set()
    for w in profile.weaknesses if profile is not None else []:
        concept = w.get("concept")
        if not concept or concept not in knowledge.concepts:
            continue
        games, total = int(w.get("game_count") or 0), int(w.get("total_games") or 0)
        occ = int(w.get("occurrences") or 0)
        evidence = (f"{occ} times in {games} of your last {total} analyzed games" if occ > games
                    else f"In {games} of your last {total} analyzed games") if total else "Seen in your games"
        progress, factor = _progress(profile.concepts.get(concept))
        share = games / total if total else 0.2
        cards.append({
            "key": w["key"], "concept": concept, "concept_name": _name(knowledge, concept),
            "title": w.get("title") or _name(knowledge, concept), "source": "games", "tier": w.get("tier"),
            "kind": w.get("kind"), "evidence": evidence, "progress": progress,
            "priority": round(share * TIER_WEIGHT.get(w.get("tier"), 0.5) * factor, 4),
            "available": available(pool, knowledge, concept), "recommended": RECOMMENDED})
        covered.add(concept)
    for concept, st in (profile.concepts.items() if profile is not None else []):
        recent = _recent(st)
        if concept in covered or concept not in knowledge.concepts or len(recent) < STRUGGLE_ATTEMPTS:
            continue
        mean = sum(recent) / len(recent)
        if mean >= STRUGGLE_MEAN:
            continue
        solved = sum(1 for s in recent if s >= 0.75)
        name = _name(knowledge, concept)
        cards.append({
            "key": f"puzzles:{concept}", "concept": concept, "concept_name": name, "title": name,
            "source": "puzzles", "tier": None, "kind": None,
            "evidence": f"You solved {solved} of your last {len(recent)} {name.lower()} puzzles",
            "progress": None, "priority": round(0.3 * (1 - mean), 4),
            "available": available(pool, knowledge, concept), "recommended": RECOMMENDED})
    cards = [c for c in cards if c["available"] > 0 or c["source"] == "games"]
    cards.sort(key=lambda c: (-c["priority"], c["title"]))
    main = cards[0] if cards else None
    obs = profile.game_observations if profile is not None else {}
    return {
        "has_data": bool(cards),
        "main": main,
        "weaknesses": cards,
        "profile": {
            "games_analyzed": (obs or {}).get("games_analyzed", 0),
            "puzzles_solved": profile.stats["puzzles"]["solved"] if profile is not None else 0,
            "puzzles_attempted": profile.stats["puzzles"]["attempts"] if profile is not None else 0,
            "rating": profile.rating if profile is not None else None,
            "main_weakness": main["title"] if main else None,
            "evidence": main["evidence"] if main else None,
            "recommendation": (f"{RECOMMENDED} puzzles on {main['concept_name'].lower()}, easy to hard"
                               if main else None),
        },
    }


# ---------------------------------------------------------------------- solver payload
def _first_sentences(text: str, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for part in parts:
        if len(out) + len(part) + 1 > limit:
            break
        out = f"{out} {part}".strip()
    return out or text[:limit].rsplit(" ", 1)[0] + "…"


def objective_text(puzzle) -> str:
    if puzzle.objective == "mate":
        n = puzzle.learner_moves
        return "Checkmate" if n > 3 else f"Mate in {n}"
    return OBJECTIVE_TEXT.get(puzzle.objective, "Find the best move")


def source_label(puzzle) -> str:
    kind = (puzzle.source or {}).get("type") or ""
    if puzzle.tier == "personal":
        return "Built for your weakness · verified by Stockfish"
    if puzzle.tier == "generated" or kind in ("generated", "procedural"):
        return "Constructed · verified by Stockfish"
    if kind == "lichess_puzzle":
        return "Real game · Lichess puzzle database (CC0)"
    return "Verified library"


def payload(puzzle, example, knowledge, reasons: list[str] | None = None, origin: str = "library") -> dict:
    """Everything the board-focused solver needs, and nothing more (no AI text)."""
    name = _name(knowledge, puzzle.concept)
    hints = [h for h in (getattr(example, "hints", None) or []) if h][:1] or [f"Look for a {name.lower()}."]
    side = "white" if " w " in f" {puzzle.fen} " else "black"
    return {
        "id": puzzle.id, "fen": puzzle.fen, "side": side,
        "concept": puzzle.concept, "concept_name": name, "primary_concept": puzzle.primary_concept,
        "objective": puzzle.objective, "objective_text": objective_text(puzzle),
        "difficulty": puzzle.difficulty, "rating": puzzle.rating,
        "solution": list(puzzle.solution),
        "steps": [{k: s[k] for k in ("uci", "san", "kind", "accepted", "good", "reply_uci", "reply_san")}
                  for s in puzzle.steps],
        "critical_moves": list(puzzle.critical_moves), "forced_moves": list(puzzle.forced_moves),
        "critical_decision_points": list(puzzle.decision_points), "meaningful_moves": puzzle.meaningful_moves,
        "expected_solution_length": puzzle.expected_solution_length,
        "hint": hints[0], "explanation": _first_sentences(getattr(example, "explanation", "") or ""),
        "origin": origin, "source": source_label(puzzle),
        "uniqueness": puzzle.uniqueness, "reasons": list(reasons or []),
    }
