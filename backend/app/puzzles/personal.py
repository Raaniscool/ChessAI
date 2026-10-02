"""Puzzles for one weakness found in the learner's games: library first, generation for the rest.

    weakness (evidence from several analyzed games)
      → Puzzle Library selection (puzzles.select): verified puzzles that train it, at the
        learner's level, new or due for a retry, easy → hard
      → only the shortfall is generated (analysis.personal_puzzles → knowledge.generation,
        full python-chess + Stockfish verification), saved to the personal tier
      → never the learner's own positions: entries copied from their games and any puzzle whose
        board equals one of the weakness's evidence positions are excluded.
"""
from __future__ import annotations

import chess

from .index import get_puzzles
from .select import Selection, select


class _SeenUsage:
    """Usage stats where puzzles listed in the generation log count as seen (older batches
    were only recorded there)."""

    def __init__(self, usage, shown: dict[str, str]):
        self.usage, self.shown = usage, shown

    def puzzle_stats(self, pid: str) -> dict:
        st = self.usage.puzzle_stats(pid) if self.usage is not None else {}
        if pid in self.shown and not st.get("seen"):
            st = {**st, "seen": True, "last_used": st.get("last_used") or self.shown[pid]}
        return st


def own_boards(weakness: dict) -> set[str]:
    out = set()
    for e in weakness.get("evidence", []):
        try:
            if e.get("fen"):
                out.add(chess.Board(e["fen"]).board_fen())
        except ValueError:
            continue
    return out


def library_selection(weakness: dict, knowledge, count: int, profile=None, usage=None,
                      shown: dict[str, str] | None = None, exclude: set[str] | None = None) -> Selection | None:
    """The best verified library puzzles for this weakness (None when it has no concept)."""
    concept = weakness.get("concept")
    if not concept or concept not in knowledge.concepts:
        return None
    mine = own_boards(weakness)
    candidates = [p for p in get_puzzles(knowledge).all()
                  if (p.source or {}).get("type") != "user_game" and p.fen.split(" ")[0] not in mine]
    return select(candidates, knowledge, concept, count=count, profile=profile,
                  usage=_SeenUsage(usage, shown or {}), exclude=exclude or set(),
                  concept_name=weakness.get("title"))


def debug_block(weakness: dict, total_games: int, selection: Selection | None, generation: dict,
                examples: list, origins: dict[str, str], knowledge=None) -> dict:
    """USER WEAKNESS / SOURCE / LIBRARY MATCH / CUSTOM GENERATION / PUZZLE VALIDATION."""
    puzzles = get_puzzles(knowledge)
    validation = []
    for ex in examples:
        p = puzzles.get(ex.id)
        validation.append({"id": ex.id, "origin": origins.get(ex.id), "status": ex.status,
                           "uniqueness": p.uniqueness if p else "unchecked",
                           "accepted": list(p.accepted_first) if p else [], "moves": p.learner_moves if p else None,
                           "rating": p.rating if p else None})
    source = [{"game_id": e.get("game_id"), "severity": e.get("severity"),
               "move": f"{e.get('move_number')}{'.' if e.get('side') == 'white' else '...'}{e.get('san')}"}
              for e in weakness.get("evidence", [])[:3]]
    return {
        "kind": "puzzles",
        "user_weakness": {"key": weakness["key"], "title": weakness.get("title"), "concept": weakness.get("concept"),
                          "games": weakness.get("game_count"), "of": total_games, "tier": weakness.get("tier")},
        "source": source,
        "library_match": ({"considered": selection.considered, "chosen": len(selection.chosen),
                           "target_rating": selection.target_rating, "level_note": selection.level_note,
                           "puzzles": [{"id": c.puzzle.id, "rating": c.puzzle.rating, "match": c.match,
                                        "type": c.puzzle.type, "score": round(c.score, 3)} for c in selection.chosen]}
                          if selection is not None else {"considered": 0, "chosen": 0, "note": "no concept for this weakness"}),
        "custom_generation": generation,
        "puzzle_validation": validation,
    }
