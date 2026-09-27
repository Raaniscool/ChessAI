"""Verified content, described by features a plan can be built from.

Every trusted position we have — Knowledge Library entries with status "verified" and the
catalog's Stockfish-checked puzzles — becomes a ContentItem with the features a request
can constrain: the concepts it shows, the material on the board when the learner moves,
whether it ends in mate, its difficulty and which side the learner plays.

Catalog puzzles are converted to library Examples (same schema, same lesson renderer),
with the puzzle's setup move shown first and the learner's move as the key move.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

import chess

from ..intent.model import MaterialSpec

log = logging.getLogger(__name__)

# Material says which pieces are on the board, not what the position teaches: a rules
# example with a rook and a bishop is not a rook-against-bishop endgame.
_ENDGAME_CATEGORIES = {"endgames", "tactics", "checkmates", "mistakes"}
_MATE_CATEGORIES = {"checkmates", "tactics", "endgames"}
LEVEL_DIFFICULTY = {"beginner": 2, "intermediate": 3, "advanced": 4}
_CATEGORY_CONCEPT = {"endgame": "endgames", "tactic": "tactics", "opening": "openings", "strategy": "tactics"}


@dataclass
class ContentItem:
    ref: str                 # library example id, or "catalog:<topic>/<puzzle>"
    kind: str                # library | catalog
    example: object          # knowledge.schema.Example
    board: chess.Board       # the position when the learner makes the key move
    final: chess.Board
    concepts: tuple[str, ...]
    difficulty: int
    topic: str | None = None
    personal: bool = False

    @property
    def learner(self) -> str:
        return "white" if self.board.turn == chess.WHITE else "black"

    def board_key(self) -> str:
        return f"{self.board.board_fen()} {'w' if self.board.turn else 'b'}"


def _key_board(example) -> tuple[chess.Board, chess.Board]:
    rep = example.replay()
    ply = example.key_ply if example.key_ply is not None else 0
    return rep.boards[ply].copy(stack=False), rep.final.copy(stack=False)


def catalog_example(topic, puzzle: dict, library):
    """A catalog puzzle as a library Example (setup move, then the learner's solution)."""
    from ...knowledge.schema import parse_example

    board = chess.Board(puzzle["fen"])
    sans = []
    for uci in puzzle["moves"]:
        move = chess.Move.from_uci(uci)
        sans.append(board.san(move))
        board.push(move)
    concept = library.concept_for_topic(topic.id)
    cid = concept.id if concept else _CATEGORY_CONCEPT.get(topic.category, "tactics")
    if cid not in library.concepts:
        cid = next(iter(library.concepts))
    from ...knowledge.positions import replay

    labels = replay(puzzle["fen"], sans).labels
    pt = topic.puzzle_text if isinstance(topic.puzzle_text, dict) else {"task": topic.puzzle_text or ""}
    pid = re.sub(r"[^a-z0-9_]", "_", f"catalog_{topic.id}_{puzzle['id']}".lower())
    raw = {
        "id": pid, "status": "verified", "title": f"{topic.title}: puzzle", "concept": cid,
        "category": library.concepts[cid].category, "description": topic.summary,
        "start_fen": puzzle["fen"], "moves": sans, "key_move": labels[1] if len(labels) > 1 else labels[0],
        "explanation": topic.summary, "difficulty": LEVEL_DIFFICULTY.get(topic.level, 3),
        "prompt": pt.get("task") or "Find the best move.", "presentation_modes": ["interactive", "practice"],
        "hints": [h for h in [pt.get("hint")] if h] or list(topic.ideas[:1]) or ["Look at every check, capture and threat first."],
        "source": {"source_type": "lichess_puzzle", "source_id": puzzle["id"], "source_license": "CC0",
                   "reference": f"https://lichess.org/training/{puzzle['id']}",
                   "checked": "Stockfish (catalog build)"},
    }
    return parse_example(raw, library.concepts, path=f"catalog:{topic.id}", tier="catalog")


class ContentIndex:
    def __init__(self, library, catalog, learner_items: bool = False):
        self.library = library
        self.catalog = catalog
        self.items: list[ContentItem] = []
        self.by_ref: dict[str, ContentItem] = {}
        for ex in library.verified(include_personal=learner_items):
            try:
                board, final = _key_board(ex)
            except Exception:  # a broken entry never breaks planning
                continue
            self._add(ContentItem(ex.id, "library", ex, board, final, tuple(ex.concepts or [ex.concept]),
                                  int(ex.difficulty or 3), personal=ex.tier == "personal"))
        for topic in catalog.topics.values():
            for pz in topic.puzzles or []:
                try:
                    ex = catalog_example(topic, pz, library)
                    board, final = _key_board(ex)
                except Exception as exc:
                    log.debug("catalog puzzle %s/%s skipped: %s", topic.id, pz.get("id"), exc)
                    continue
                ref = f"catalog:{topic.id}/{pz['id']}"
                self._add(ContentItem(ref, "catalog", ex, board, final, (ex.concept,), int(ex.difficulty),
                                      topic=topic.id))

    def _add(self, item: ContentItem) -> None:
        self.items.append(item)
        self.by_ref[item.ref] = item

    # ------------------------------------------------------------------ queries
    def material(self, spec: MaterialSpec, exclude: set[str] = frozenset()) -> list[ContentItem]:
        out, seen = [], set()
        allowed = _MATE_CATEGORIES if spec.head == "mate" else _ENDGAME_CATEGORIES
        for it in self.items:
            if it.ref in exclude or it.example.category not in allowed or not spec.matches(it.board):
                continue
            if spec.head == "mate":
                if not it.final.is_checkmate():
                    continue
                winner = spec.winner(it.board)
                if winner is None or winner != it.board.turn:
                    continue  # the learner must be the side with the pieces
            if it.board_key() in seen:
                continue
            seen.add(it.board_key())
            out.append(it)
        # real-game solutions before "spot the mistake" entries, then easiest first
        return sorted(out, key=lambda i: (i.ref.endswith("_mistake"), i.difficulty, i.ref))

    def concept(self, cid: str, exclude: set[str] = frozenset(), excluded_concepts: set[str] = frozenset()) -> list[ContentItem]:
        family = {cid, *self.library.descendants(cid)}
        out, seen = [], set()
        for it in self.items:
            if it.ref in exclude or it.kind != "library" or not family & set(it.concepts):
                continue
            if excluded_concepts & set(it.concepts):
                continue
            if it.board_key() in seen:
                continue
            seen.add(it.board_key())
            out.append(it)
        return sorted(out, key=lambda i: (i.difficulty, i.ref))


_index: ContentIndex | None = None
_lock = threading.Lock()


def get_content_index(library=None, catalog=None) -> ContentIndex:
    """The shared index of global verified content (rebuilt when the library changes size)."""
    global _index
    from ...knowledge.library import get_knowledge
    from ..catalog import get_catalog

    library = library or get_knowledge()
    catalog = catalog or get_catalog()
    with _lock:
        if _index is None or _index.library is not library or _index.catalog is not catalog \
                or sum(1 for i in _index.items if i.kind == "library") != len(library.verified()):
            _index = ContentIndex(library, catalog)
        return _index
