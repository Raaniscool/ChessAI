"""Named opening lines from lichess-org/chess-openings (CC0).

The `opening_line` validator uses this index to prove an opening example
follows real theory, and the importer builds opening examples whose *moves
come from the database* — only the teaching text is written by hand.
"""
from __future__ import annotations

import csv
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import chess

from . import provenance

DATA = Path(__file__).resolve().parent / "data"
TSV_DIR = DATA / "lichess_openings"
UPSTREAM = "https://github.com/lichess-org/chess-openings"
UPSTREAM_COMMIT = "c67912be581f0793dbaa776be5ccf111e01f88d9"
LICENSE = "CC0-1.0"
_NUM = re.compile(r"^\d+\.(\.\.)?$")


@dataclass(frozen=True)
class Opening:
    eco: str
    name: str
    moves: tuple[str, ...]


class OpeningIndex:
    def __init__(self, directory: Path | None = None):
        self.by_moves: dict[tuple[str, ...], Opening] = {}
        self.by_name: dict[str, list[Opening]] = {}
        for path in sorted((directory or TSV_DIR).glob("*.tsv")):
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    moves = self._normalize(row["pgn"])
                    if moves is None:
                        continue
                    op = Opening(row["eco"], row["name"], moves)
                    self.by_moves.setdefault(moves, op)
                    self.by_name.setdefault(row["name"], []).append(op)

    @staticmethod
    def _normalize(pgn: str) -> tuple[str, ...] | None:
        board, out = chess.Board(), []
        for tok in pgn.split():
            if _NUM.match(tok):
                continue
            try:
                move = board.parse_san(tok)
            except ValueError:
                return None
            out.append(board.san(move))
            board.push(move)
        return tuple(out)

    def longest_prefix(self, sans: list[str]) -> Opening | None:
        for k in range(len(sans), 0, -1):
            op = self.by_moves.get(tuple(sans[:k]))
            if op is not None:
                return op
        return None

    def line(self, name: str, pgn_prefix: str | None = None) -> Opening:
        """The database line called `name`: the shortest (defining) one, or the shortest that
        starts with `pgn_prefix` (use the full pgn to pick one exact line)."""
        options = self.by_name.get(name)
        if not options:
            raise KeyError(f"no opening named {name!r} in the database")
        if pgn_prefix:
            want = self._normalize(pgn_prefix) or ()
            options = [o for o in options if o.moves[:len(want)] == want]
            if not options:
                raise KeyError(f"{name!r} has no line starting {pgn_prefix!r}")
        return min(options, key=lambda o: len(o.moves))


_index: OpeningIndex | None = None
_lock = threading.Lock()


def get_opening_index() -> OpeningIndex:
    global _index
    with _lock:
        if _index is None:
            _index = OpeningIndex()
        return _index


def candidates(records_path: Path | None = None, index: OpeningIndex | None = None) -> list[dict]:
    """Opening examples from curated records (sources/data/curated/openings.json).

    Each record names a database line (`opening`, optional `pgn` to pick one of
    several lines with that name) and supplies teaching text. The moves are the
    database's; a record may add a few `extend` moves (checked by Stockfish and
    the opening_line validator, which caps extensions)."""
    index = index or get_opening_index()
    path = records_path or DATA / "curated" / "openings.json"
    with open(path, encoding="utf-8") as fh:
        records = json.load(fh)
    out = []
    for rec in records:
        op = index.line(rec["opening"], rec.get("pgn"))
        moves = list(op.moves) + list(rec.get("extend", []))
        cand = {k: v for k, v in rec.items() if k not in ("opening", "pgn", "extend")}
        cand.update({
            "moves": moves, "start_fen": "startpos", "category": "openings",
            "subcategory": rec.get("subcategory") or op.name,
            "source": provenance("lichess_openings", f"{op.eco} {op.name}", LICENSE,
                                 url=UPSTREAM, reference=f"ECO {op.eco}; upstream commit {UPSTREAM_COMMIT[:10]}"),
        })
        out.append(cand)
    return out
