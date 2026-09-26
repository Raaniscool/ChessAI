"""Stockfish (UCI) engine service.

The engine is the objective chess-analysis layer: it decides *what happened*.
It is wrapped behind a small interface so lessons/API never talk to UCI
directly, and so tests can substitute a fake engine.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol

import chess
import chess.engine

from ..config import Settings, get_settings
from .. import chess_system
from .classification import Classification, Score, classify_move


class EngineError(RuntimeError):
    pass


class EngineUnavailable(EngineError):
    pass


@dataclass
class Analysis:
    """Result of analysing a position (all scores White POV, canonical)."""

    fen: str
    best_move_uci: str | None
    best_move_san: str | None
    score: Score | None
    pv_san: list[str] = field(default_factory=list)
    depth: int | None = None

    def as_dict(self) -> dict:
        return {
            "fen": self.fen,
            "best_move": self.best_move_san,
            "best_move_uci": self.best_move_uci,
            "score": self.score.as_dict() if self.score else None,
            "pv": self.pv_san,
            "depth": self.depth,
        }


@dataclass
class MoveFeedback:
    """Structured verdict on the user's move — facts only, no prose."""

    fen_before: str
    fen_after: str
    user_move_uci: str
    user_move_san: str
    category: Classification
    loss_cp: int
    best_move_uci: str | None
    best_move_san: str | None
    eval_before: Score | None
    eval_after: Score | None
    best_pv_san: list[str]
    reply_pv_san: list[str]  # what happens if user's move is played
    notes: list[str] = field(default_factory=list)
    depth: int | None = None

    def as_dict(self) -> dict:
        return {
            "fen_before": self.fen_before,
            "fen_after": self.fen_after,
            "user_move": self.user_move_san,
            "user_move_uci": self.user_move_uci,
            "category": self.category.value,
            "loss_cp": self.loss_cp,
            "best_move": self.best_move_san,
            "best_move_uci": self.best_move_uci,
            "eval_before": self.eval_before.as_dict() if self.eval_before else None,
            "eval_after": self.eval_after.as_dict() if self.eval_after else None,
            "best_pv": self.best_pv_san,
            "reply_pv": self.reply_pv_san,
            "notes": self.notes,
            "depth": self.depth,
        }


@dataclass
class Line:
    """One engine candidate line (multi-PV). `score` is from White's point of view."""

    move: chess.Move
    san: str
    score: Score
    pv_san: list[str] = field(default_factory=list)


class AnalysisEngine(Protocol):
    def analyse(self, board: chess.Board, depth: int | None = None) -> Analysis: ...

    def analyse_lines(self, board: chess.Board, depth: int | None = None,
                      multipv: int = 3, fresh: bool = False) -> list[Line]: ...

    def evaluate_move(
        self, board_before: chess.Board, move: chess.Move, depth: int | None = None
    ) -> MoveFeedback: ...

    def close(self) -> None: ...


class UciEngine:
    """Owns a UCI engine subprocess (Stockfish, native or WASM-via-Node)."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.engine_cmd:
            raise EngineUnavailable(
                "No chess engine found. Set ENGINE_CMD or run `npm install` "
                "to fetch the Stockfish WASM build."
            )
        self._lock = threading.Lock()
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.settings.engine_cmd)
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineUnavailable(f"Failed to start engine {self.settings.engine_cmd}: {exc}") from exc

    def _analyse_score(self, board: chess.Board, depth: int) -> tuple[Score, list[chess.Move]]:
        limit = chess.engine.Limit(depth=depth)
        info = self._engine.analyse(board, limit)
        score = Score.from_pov_white(info["score"])
        pv = list(info.get("pv", []))
        return score, pv

    def analyse(self, board: chess.Board, depth: int | None = None) -> Analysis:
        depth = depth or self.settings.engine_depth
        with self._lock:
            score, pv = self._analyse_score(board, depth)
        best = pv[0] if pv else None
        display = board.copy()
        pv_san = []
        for move in pv[:8]:
            pv_san.append(display.san(move))
            if not display.is_legal(move):
                break
            display.push(move)
        return Analysis(
            fen=board.fen(),
            best_move_uci=best.uci() if best else None,
            best_move_san=board.san(best) if best else None,
            score=score,
            pv_san=pv_san,
            depth=depth,
        )

    def analyse_lines(self, board: chess.Board, depth: int | None = None,
                      multipv: int = 3, fresh: bool = False) -> list[Line]:
        """The engine's top `multipv` moves, best first (used to spot equally good alternatives).

        fresh=True starts a new game first (clears the hash table), so the result depends only
        on the position and depth: library verification must be reproducible."""
        depth = depth or self.settings.engine_depth
        with self._lock:
            infos = self._engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv,
                                         game=object() if fresh else None)
        if isinstance(infos, dict):
            infos = [infos]
        lines = []
        for info in infos:
            pv = list(info.get("pv", []))
            if not pv or "score" not in info:
                continue
            display, pv_san = board.copy(), []
            for m in pv[:6]:
                if not display.is_legal(m):
                    break
                pv_san.append(display.san(m))
                display.push(m)
            lines.append(Line(move=pv[0], san=board.san(pv[0]),
                              score=Score.from_pov_white(info["score"]), pv_san=pv_san))
        return lines

    def evaluate_move(
        self, board_before: chess.Board, move: chess.Move, depth: int | None = None
    ) -> MoveFeedback:
        depth = depth or self.settings.engine_depth
        if move not in board_before.legal_moves:
            raise chess_system.ChessError(f"Illegal move {move.uci()}")

        with self._lock:
            best_score, best_pv = self._analyse_score(board_before, depth)
            after = board_before.copy()
            after.push(move)
            after_score, after_pv = self._analyse_score(after, depth)

        category, loss, notes = classify_move(board_before, move, best_score, after_score)

        best = best_pv[0] if best_pv else None
        reply_san = []
        display = after.copy()
        for m in after_pv[:6]:
            if not display.is_legal(m):
                break
            reply_san.append(display.san(m))
            display.push(m)
        best_san = []
        display = board_before.copy()
        for m in best_pv[:6]:
            if not display.is_legal(m):
                break
            best_san.append(display.san(m))
            display.push(m)

        return MoveFeedback(
            fen_before=board_before.fen(),
            fen_after=after.fen(),
            user_move_uci=move.uci(),
            user_move_san=board_before.san(move),
            category=category,
            loss_cp=loss,
            best_move_uci=best.uci() if best else None,
            best_move_san=board_before.san(best) if best else None,
            eval_before=best_score,
            eval_after=after_score,
            best_pv_san=best_san,
            reply_pv_san=reply_san,
            notes=notes,
            depth=depth,
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._engine.quit()
            except Exception:
                pass


_engine: AnalysisEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> AnalysisEngine:
    """Process-wide engine singleton (lazily started)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = UciEngine()
        return _engine


def set_engine(engine: AnalysisEngine | None) -> None:
    """Testing/DI hook."""
    global _engine
    with _engine_lock:
        _engine = engine


def engine_available() -> bool:
    try:
        get_engine()
        return True
    except EngineUnavailable:
        return False
