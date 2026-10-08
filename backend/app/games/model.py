"""The general game representation every importer produces and the analysis engine reads.

Nothing here knows about Chess.com (or any other site): importers translate their
PGN dialect into a ``GameRecord``; ``analysis/`` only ever sees ``GameRecord``s.
Adding another source later means writing another importer, not touching analysis.

Imported games are *learner data*: they live under DATA_DIR/games (see store.py),
never in the global Knowledge Library.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import chess

RESULTS = ("1-0", "0-1", "1/2-1/2", "*")


@dataclass
class GameRecord:
    id: str                      # stable per game: "chesscom-<game number>" or a content hash
    source: str                  # importer id, e.g. "chesscom"
    white: str
    black: str
    result: str                  # one of RESULTS
    start_fen: str
    moves_san: list[str]
    moves_uci: list[str]
    date: str | None = None      # ISO "2024-05-01"
    time_control: str | None = None  # raw PGN value, e.g. "180+2"
    time_class: str | None = None    # bullet | blitz | rapid | daily
    time_label: str | None = None    # human-readable, e.g. "3 min + 2 s"
    eco: str | None = None
    opening: str | None = None
    white_elo: int | None = None
    black_elo: int | None = None
    termination: str | None = None
    url: str | None = None           # link back to the game on its site
    clocks: list[str | None] = field(default_factory=list)  # clock after each ply, if recorded
    headers: dict[str, str] = field(default_factory=dict)
    pgn: str = ""                    # the PGN text as imported
    player: str | None = None        # the learner's name in this game
    player_color: str | None = None  # "white" | "black"
    imported_at: str = ""

    # ------------------------------------------------------------------ helpers
    @property
    def plies(self) -> int:
        return len(self.moves_uci)

    @property
    def learner(self) -> chess.Color | None:
        if self.player_color is None:
            return None
        return self.player_color == "white"

    @property
    def opponent(self) -> str | None:
        if self.player_color is None:
            return None
        return self.black if self.player_color == "white" else self.white

    def boards(self) -> list[chess.Board]:
        """boards[i] = position before ply i; boards[-1] = final position (python-chess replay)."""
        board = chess.Board(self.start_fen)
        out = [board.copy(stack=False)]
        for uci in self.moves_uci:
            board.push_uci(uci)
            out.append(board.copy(stack=False))
        return out

    def learner_result(self) -> str | None:
        """win | loss | draw | unknown, from the learner's side."""
        if self.player_color is None or self.result == "*":
            return None
        if self.result == "1/2-1/2":
            return "draw"
        white_won = self.result == "1-0"
        return "win" if white_won == (self.player_color == "white") else "loss"

    def summary(self) -> dict:
        return {
            "id": self.id, "source": self.source, "white": self.white, "black": self.black,
            "white_elo": self.white_elo, "black_elo": self.black_elo, "result": self.result,
            "date": self.date, "time_control": self.time_control, "time_class": self.time_class,
            "time_label": self.time_label, "eco": self.eco, "opening": self.opening,
            "termination": self.termination, "url": self.url, "plies": self.plies,
            "moves": (self.plies + 1) // 2, "player": self.player, "player_color": self.player_color,
            "opponent": self.opponent, "learner_result": self.learner_result(),
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GameRecord":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
