"""One Training segment: the position, the moves played, when it ends, and where it is kept.

Length: the learner plays at least MIN_MOVES and at most MAX_MOVES of their own moves. Between the
two, the segment ends at the first quiet moment — the learner to move, not in check, and the bot's
last move was not a capture (an exchange in progress is finished first). The game ending (mate,
stalemate, a draw by rule) ends it at once. So the length follows the position, not a fixed count.

Segments live in memory (MAX_SEGMENTS, oldest dropped): a segment is a few minutes of play, and
only its analysed result is persistent (assessment.record, in the learner profile).
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import chess

from .positions import TrainingPosition

MIN_MOVES, MAX_MOVES = 5, 10
MAX_SEGMENTS = 64


@dataclass
class Segment:
    id: str
    mode: str
    position: TrainingPosition
    bot: dict                                   # strength_for(...) result
    seed: int
    moves_uci: list[str] = field(default_factory=list)
    moves_san: list[str] = field(default_factory=list)
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished: bool = False
    end_reason: str | None = None
    result: dict | None = None

    @property
    def side(self) -> chess.Color:
        return chess.WHITE if self.position.side == "white" else chess.BLACK

    def board(self) -> chess.Board:
        board = chess.Board(self.position.fen)
        for uci in self.moves_uci:
            board.push_uci(uci)
        return board

    def learner_moves(self) -> int:
        board = chess.Board(self.position.fen)
        n = 0
        for uci in self.moves_uci:
            n += board.turn == self.side
            board.push_uci(uci)
        return n

    def public(self) -> dict:
        """The state the board needs. No concept, no source, no solution until it's over."""
        board = self.board()
        out = {"id": self.id, "mode": self.mode, "phase": self.position.phase, "start_fen": self.position.fen,
               "fen": board.fen(), "side": self.position.side, "moves": list(self.moves_san),
               "moves_uci": list(self.moves_uci), "learner_moves": self.learner_moves(),
               "min_moves": MIN_MOVES, "max_moves": MAX_MOVES, "bot_rating": self.bot.get("rating"),
               "finished": self.finished, "end_reason": self.end_reason}
        return out


def end_reason(seg: Segment, board: chess.Board) -> str | None:
    """Why the segment is over now (None: keep playing)."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material() or board.can_claim_draw():
        return "draw"
    if board.turn != seg.side:
        return None        # the bot is to move: never stop on the bot's turn
    played = seg.learner_moves()
    if played >= MAX_MOVES:
        return "length"
    if played >= MIN_MOVES and not board.is_check():
        last = board.peek() if board.move_stack else None
        if last is None:
            return "quiet"
        probe = board.copy(stack=True)
        probe.pop()
        if not probe.is_capture(last):
            return "quiet"
    return None


class SegmentStore:
    def __init__(self):
        self._items: OrderedDict[str, Segment] = OrderedDict()
        self._lock = threading.Lock()

    def new(self, mode: str, position: TrainingPosition, bot: dict, seed: int) -> Segment:
        seg = Segment(id=uuid.uuid4().hex[:16], mode=mode, position=position, bot=bot, seed=seed)
        with self._lock:
            self._items[seg.id] = seg
            while len(self._items) > MAX_SEGMENTS:
                self._items.popitem(last=False)
        return seg

    def get(self, seg_id: str) -> Segment | None:
        with self._lock:
            return self._items.get(seg_id)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_store: SegmentStore | None = None


def get_segments() -> SegmentStore:
    global _store
    if _store is None:
        _store = SegmentStore()
    return _store


def set_segments(store: SegmentStore | None) -> None:
    global _store
    _store = store
