"""New puzzles for a weakness found in the learner's games — new positions, not copies.

    recurring weakness (evidence from several games)
      → the concept it maps to (walked into forks → knight forks; hung pieces → spotting
        threats; missed mates → mate in one ...)
      → knowledge.generation: constructed positions, validated by python-chess, the concept
        validator and Stockfish, each checked to really need the idea
      → saved in the PERSONAL tier with the weakness and the games that triggered it —
        never in the shared library, never shown as a real game
      → the generation log remembers what was shown, so the next batch is different.
"""
from __future__ import annotations

import chess

from ..knowledge.generation import PLANS, generate, supported
from ..knowledge.generation.log import get_log

MAX_EVIDENCE = 5
MAX_COUNT = 5


def target_concept(weakness: dict) -> str | None:
    """The generator concept for a weakness, or None when it can't be generated."""
    concept = weakness.get("concept")
    return concept if concept and supported(concept) else None


def supported_weakness(weakness: dict) -> bool:
    return target_concept(weakness) is not None


def personal_meta(weakness: dict, username: str | None = None) -> dict:
    """What triggered the puzzles: stored with each one (provenance, not a copy)."""
    evidence = [{"game_id": e["game_id"], "moment_id": e.get("moment_id"), "ply": e.get("ply"),
                 "move": f"{e.get('move_number')}{'.' if e.get('side') == 'white' else '...'}{e.get('san')}",
                 "severity": e.get("severity")}
                for e in weakness.get("evidence", [])[:MAX_EVIDENCE]]
    return {"target_weakness": weakness["key"], "weakness_title": weakness.get("title"),
            "weakness_concept": weakness.get("concept"), "game_count": weakness.get("game_count"),
            "games": list(weakness.get("games", []))[:20], "evidence": evidence, "username": username}


def unseen_for(weakness: dict, library, shown: dict | None = None) -> list:
    """Verified personal puzzles already made for this weakness that haven't been shown."""
    shown = shown if shown is not None else get_log().shown()
    out = []
    for e in library.entries.values():
        if e.tier != "personal" or e.status != "verified" or e.id in shown:
            continue
        meta = (e.source or {}).get("personal") or {}
        if meta.get("target_weakness") == weakness["key"]:
            out.append(e)
    return sorted(out, key=lambda e: e.id)


def generate_for(weakness: dict, library, engine, count: int = 3, username: str | None = None,
                 time_budget: float = 45.0, use_qwen: bool | None = None, on_progress=None, seed=None):
    """New verified puzzles for `weakness` (personal tier). Returns a GenerationResult."""
    concept = target_concept(weakness)
    if concept is None:
        raise ValueError(f"no puzzle generator for {weakness.get('concept') or weakness['key']}")
    # never hand the learner their own position back as a "new" puzzle
    own = {chess.Board(e["fen"]).board_fen() for e in weakness.get("evidence", []) if e.get("fen")}
    return generate(concept, library, engine, count=max(1, min(count, MAX_COUNT)), tier="personal",
                    personal=personal_meta(weakness, username), time_budget=time_budget, use_qwen=use_qwen,
                    on_progress=on_progress, seed=seed, avoid_positions=own)


def puzzle_names(library, weakness: dict) -> list[str]:
    concept = target_concept(weakness)
    targets = dict.fromkeys(t for t, _ in PLANS.get(concept, []))
    return [library.concepts[t].name for t in targets if t in library.concepts]
