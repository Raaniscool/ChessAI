"""Shared helpers for the Knowledge Library tests: a deterministic fake engine and
a throwaway library (the real concept graph, an empty or custom example set)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import chess

from app.engine.classification import Score
from app.engine.service import Line
from app.knowledge.library import KnowledgeLibrary
from app.knowledge.store import RuntimeStore

KNOWLEDGE_DATA = Path(__file__).resolve().parents[1] / "app" / "knowledge" / "data"
VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def _material_cp(board: chess.Board) -> int:
    total = 0
    for piece in board.piece_map().values():
        total += VALUES[piece.piece_type] * (1 if piece.color == chess.WHITE else -1)
    return total * 100


class FakeEngine:
    """Scores every legal move by the material after it (mate = mate), White's POV.

    `table` overrides single moves: {epd: {san: cp_or_"M1"/"-M1"}}; `positions` gives every
    move in a position the same score ({epd: cp}), i.e. "this position is worth cp".
    Deterministic, fast, and counts calls so tests can check the engine was consulted."""

    def __init__(self, table: dict[str, dict[str, int | str]] | None = None,
                 positions: dict[str, int] | None = None):
        self.table = table or {}
        self.positions = positions or {}
        self.calls = 0

    def _score(self, board: chess.Board, move: chess.Move) -> Score:
        override = self.table.get(board.epd(), {}).get(board.san(move))
        if override is None and board.epd() in self.positions:
            override = self.positions[board.epd()]
        if isinstance(override, str):
            return Score("mate", int(override.replace("M", "")))
        if override is not None:
            return Score("cp", override)
        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            return Score("mate", 1 if board.turn == chess.WHITE else -1)
        if after.is_stalemate():
            return Score("cp", 0)
        return Score("cp", _material_cp(after))

    def analyse_lines(self, board: chess.Board, depth=None, multipv: int = 3, fresh: bool = False) -> list[Line]:
        self.calls += 1
        lines = [Line(move=m, san=board.san(m), score=self._score(board, m), pv_san=[board.san(m)])
                 for m in board.legal_moves]
        lines.sort(key=lambda ln: (-ln.score.for_side(board.turn), ln.san))
        return lines[:multipv]

    def close(self) -> None:
        pass


def make_library(tmp_path: Path, examples: dict[str, list[dict]] | None = None,
                 runtime: bool = True) -> KnowledgeLibrary:
    """A library over the real concept graph with `examples` ({"<category>/<file>": [records]})."""
    data = tmp_path / "kdata"
    data.mkdir(exist_ok=True)
    shutil.copy(KNOWLEDGE_DATA / "concepts.json", data / "concepts.json")
    for rel, records in (examples or {}).items():
        path = data / "examples" / f"{rel}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records), encoding="utf-8")
    store = RuntimeStore(tmp_path / "runtime") if runtime else None
    return KnowledgeLibrary(data_dir=data, store=store, load_runtime=runtime)


SOURCE = {"source_type": "curated", "source_id": "test", "source_license": "CC0-1.0",
          "reference": "unit test", "import_date": "2026-09-25"}

# Knight on b5: 1.Nc7+ forks king e8 and queen a8.
FORK_FEN = "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"


def fork_record(**over) -> dict:
    rec = {
        "id": "test_knight_fork", "title": "Knight fork", "concept": "knight_fork", "category": "tactics",
        "difficulty": 1, "description": "The knight can attack the king and the queen at once.",
        "start_fen": FORK_FEN, "moves": ["Nc7+", "Kd7", "Nxa8"], "key_move": "1.Nc7+",
        "prompt": "White to move. Find the fork.", "hints": ["Your knight can give check."],
        "notes": {"1.Nc7+": "The knight on c7 attacks the king on e8 and the queen on a8."},
        "explanation": "The knight checks the king and attacks the queen at the same time. After the king "
                       "moves, the knight takes the queen.",
        "tags": ["fork", "test"], "source": dict(SOURCE),
    }
    rec.update(over)
    return rec


def winning_fork_engine() -> FakeEngine:
    """Stockfish's opinion of the fork position: 1.Nc7+ wins the queen."""
    return FakeEngine({chess.Board(FORK_FEN).epd(): {"Nc7+": 850}})
