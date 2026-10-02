"""The Puzzle Library: every verified, solvable position in the Knowledge Library, as puzzles.

It is an *index*, not a second store: puzzles are derived from Knowledge Library entries
(global, generated and the learner's personal tier) and rebuilt when the library changes
(new generated or personal entries, status changes). Each entry still goes through the
normal verification pipeline before it can appear here (only `verified` entries qualify).
"""
from __future__ import annotations

import threading
from collections import Counter

from .model import Puzzle, from_example


class PuzzleLibrary:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self._lock = threading.Lock()
        self._signature: int | None = None
        self._puzzles: dict[str, Puzzle] = {}

    def _current(self) -> dict[str, Puzzle]:
        entries = self.knowledge.entries
        signature = hash(frozenset((eid, e.status, e.tier) for eid, e in entries.items()))
        with self._lock:
            if signature != self._signature:
                built = {}
                for example in entries.values():
                    puzzle = from_example(example, self.knowledge)
                    if puzzle is not None:
                        built[puzzle.id] = puzzle
                self._puzzles, self._signature = built, signature
            return self._puzzles

    def all(self, include_personal: bool = True) -> list[Puzzle]:
        return [p for p in self._current().values() if include_personal or p.tier != "personal"]

    def get(self, puzzle_id: str) -> Puzzle | None:
        return self._current().get(puzzle_id)

    def summary(self) -> dict:
        puzzles = self.all()
        return {
            "total": len(puzzles),
            "by_type": dict(Counter(p.type for p in puzzles)),
            "by_tier": dict(Counter(p.tier for p in puzzles)),
            "by_uniqueness": dict(Counter(p.uniqueness for p in puzzles)),
            "concepts": len({p.concept for p in puzzles}),
        }


_cache: dict[int, PuzzleLibrary] = {}
_cache_lock = threading.Lock()


def get_puzzles(knowledge=None) -> PuzzleLibrary:
    """The Puzzle Library for the current (or given) Knowledge Library."""
    if knowledge is None:
        from ..knowledge.library import get_knowledge
        knowledge = get_knowledge()
    with _cache_lock:
        lib = _cache.get(id(knowledge))
        if lib is None or lib.knowledge is not knowledge:
            _cache.clear()  # one live knowledge library at a time
            lib = _cache[id(knowledge)] = PuzzleLibrary(knowledge)
        return lib
