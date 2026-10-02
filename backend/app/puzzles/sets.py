"""A personalized training set for one weakness: what each item is for, and why it was chosen.

    1. Your game   the learner's own mistake (puzzles.from_game), re-verified by Stockfish
    2. Same pattern  verified puzzles of the exact skill near the learner's level
    3. Easier        ... a step below (a warm-up / recovery)
    4. Harder        ... a step above
    5. Defend        only for weaknesses the learner *walked into* (forks, hung pieces):
                     a position where the right move is seeing and stopping the opponent's
                     threat (library defence puzzles, else a verified generated one)

Roles 2-4 come from puzzles.select (skill-targeted, levelled, progression-aware); the role is
the puzzle's rating relative to the learner's target. Generated puzzles (Source C) fill the
skill slots only when the library runs short. Every item carries one short "why" line for the
solver — the evidence from the learner's games, never AI text.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .model import DEFENSIVE

ROLE_LABEL = {"your_game": "Your game", "same": "Same pattern", "easier": "Easier", "harder": "Harder",
              "defend": "Defend"}
ORDER = ("your_game", "same", "easier", "harder", "defend")
ROLE_GAP = 75   # rating points from the target before an item counts as easier / harder
DEFEND_GENERATION_BUDGET = 15.0


def role_for(puzzle, target: int) -> str:
    gap = puzzle.rating - target
    return "easier" if gap < -ROLE_GAP else "harder" if gap > ROLE_GAP else "same"


def walked_into(weakness: dict) -> bool:
    """The learner allowed it (walked into a fork, hung a piece) rather than missed a chance."""
    families = {e.get("family") for e in weakness.get("evidence", []) if e.get("family")}
    if families:
        return "allowed" in families
    key = weakness.get("key") or ""
    return key.startswith(("walked_into", "hung", "allowed")) or weakness.get("concept") in DEFENSIVE


def evidence_phrase(weakness: dict) -> str:
    games = int(weakness.get("game_count") or 0)
    occ = int(weakness.get("occurrences") or len(weakness.get("evidence", [])) or 0)
    if not games:
        return "a pattern from your recent puzzles"
    where = f"in {games} recent game{'s' if games != 1 else ''}"
    if walked_into(weakness):
        return f"it cost you {occ} times {where}" if occ > games else f"it cost you material {where}"
    return f"you missed this {occ} times {where}" if occ > games else f"you missed this pattern {where}"


def why(weakness: dict, role: str, puzzle=None) -> str:
    title = weakness.get("title") or "This skill"
    if role == "your_game":
        ev = ((puzzle.facts or {}).get("game") if puzzle is not None else None) or {}
        vs = f" vs {ev['opponent']}" if ev.get("opponent") else ""
        n = ev.get("move_number")
        dots = "." if ev.get("side") == "white" else "..."
        played = f" — you played {n}{dots}{ev['san']} here" if n and ev.get("san") else ""
        return f"Your own game{vs}{played}. Find what you missed."
    if role == "defend":
        return f"{title}: the defensive side — spot the opponent's threat and stop it."
    step = {"easier": " (a step easier)", "harder": " (a step harder)"}.get(role, "")
    return f"{title} — {evidence_phrase(weakness)}{step}."


def order(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: (ORDER.index(it["role"]), it["puzzle"].rating, it["puzzle"].id))


def defend_item(weakness: dict, knowledge, index, usage, shown: dict, target: int, avoid: set[str],
                engine=None, username: str | None = None) -> tuple[dict | None, dict]:
    """One "stop the threat" puzzle for a walked-into weakness: (item or None, debug)."""
    from .select import MIN_NOVELTY, novelty
    debug = {"applies": walked_into(weakness), "source": None}
    if not debug["applies"]:
        return None, debug
    now = datetime.now(timezone.utc)

    def due(p) -> bool:
        st = usage.puzzle_stats(p.id) if usage is not None else {}
        if p.id in shown and not st.get("seen"):
            st = {**st, "seen": True, "last_used": shown[p.id]}
        return novelty(st, now)[0] >= MIN_NOVELTY

    pool = [p for p in index.all() if p.type == "defense" and p.clear_start
            and (p.source or {}).get("type") != "user_game" and p.fen.split(" ")[0] not in avoid and due(p)]
    if pool:
        p = min(pool, key=lambda p: (abs(p.rating - target), p.id))
        debug["source"] = "library" if p.tier != "personal" else "personal"
        return {"puzzle": p, "example": knowledge.get(p.id), "role": "defend",
                "origin": "library" if p.tier != "personal" else "personal"}, debug
    if engine is None:
        debug["source"] = "none (no engine to verify a new one)"
        return None, debug
    from ..analysis.personal_puzzles import personal_meta
    from ..knowledge.generation import generate
    from ..knowledge.generation.generator import PLANS
    try:
        result = generate("spotting_threats", knowledge, engine, count=1, tier="personal",
                          personal={**personal_meta(weakness, username), "role": "defend"}, time_budget=DEFEND_GENERATION_BUDGET,
                          avoid_positions=avoid, plans=PLANS["spotting_threats"])
    except Exception as exc:  # a failed generation must not lose the rest of the set
        debug["source"] = f"generation failed: {exc}"
        return None, debug
    debug.update(source="generated", generated=len(result.accepted), rejected=len(result.rejected))
    for ex in result.accepted:
        p = index.get(ex.id)
        if ex.status == "verified" and p is not None and p.clear_start:
            return {"puzzle": p, "example": ex, "role": "defend", "origin": "generated"}, debug
    return None, debug
