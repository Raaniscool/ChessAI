"""Game analysis engine: GameRecord -> structured, engine-verified findings (no prose).

    1. python-chess replays every position of the (already validated) game
    2. Stockfish evaluates every position once (quick pass, GAME_ANALYSIS_DEPTH)
    3. each learner move is graded by the tutor's own classify_move (same thresholds
       as lessons: engine/classification.py)
    4. candidate mistakes are re-checked at ENGINE_DEPTH; only confirmed mistakes and
       blunders (and missed forced mates) become "moments" — inaccuracies are counted,
       not paraded, so a beginner isn't buried under every small evaluation change
    5. each moment gets Stockfish's good alternatives (multi-PV) and its concepts
       (motifs.py); opening habits are checked across the game (habits.py)

Stockfish decides what is good or bad; python-chess decides what is legal; this
module only records their verdicts. Nothing here is written to the Knowledge Library.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import chess

from ..config import get_settings
from ..engine import Analysis, Classification, Score, classify_move
from ..engine.classification import DECIDED_POSITION_CP, GOOD_CP, INACCURATE_CP
from ..games.model import GameRecord
from .habits import OPENING_PLIES, detect_habits
from .motifs import detect, non_pawn_material

SCHEMA_VERSION = 1
MAX_MOMENTS = 8                 # per game; the rest are still counted in the stats
SEVERITY_WEIGHT = {"blunder": 3, "mistake": 2, "inaccurate": 1, "habit": 1}
SYMBOL = {"blunder": "??", "mistake": "?", "inaccurate": "?!"}
CANDIDATE_MIN_LOSS = 90         # quick-pass inaccuracies this big get a second look at full depth
CP_CAP = 2000                   # display cap for mate-ish scores (never shown as pawns beyond this)


def phase_of(board: chess.Board, ply: int) -> str:
    npm = non_pawn_material(board)
    queens = board.pieces(chess.QUEEN, chess.WHITE) | board.pieces(chess.QUEEN, chess.BLACK)
    if npm <= 18 or (not queens and npm <= 26):
        return "endgame"
    return "opening" if ply < 20 else "middlegame"


def _terminal_score(board: chess.Board) -> Score | None:
    if board.is_checkmate():
        return Score.checkmate(white_won=board.turn == chess.BLACK)
    if board.is_stalemate() or board.is_insufficient_material():
        return Score("cp", 0)
    return None


def _learner_cp(score: Score | None, side: chess.Color) -> int | None:
    if score is None:
        return None
    return max(-CP_CAP, min(CP_CAP, score.for_side(side)))


def _decided(before: Score | None, after: Score | None, side: chess.Color) -> bool:
    """Clearly won (or clearly lost) both before and after the move."""
    a, b = _learner_cp(before, side), _learner_cp(after, side)
    if a is None or b is None:
        return False
    return (a >= DECIDED_POSITION_CP and b >= DECIDED_POSITION_CP) or \
        (a <= -DECIDED_POSITION_CP and b <= -DECIDED_POSITION_CP)


def _uci_line(board: chess.Board, sans: list[str]) -> list[chess.Move]:
    """Engine lines come back as SAN; turn them into legal moves (stop at anything odd)."""
    probe, out = board.copy(stack=False), []
    for san in sans:
        try:
            move = probe.parse_san(san)
        except ValueError:
            break
        out.append(move)
        probe.push(move)
    return out


class GameAnalyzer:
    def __init__(self, engine, depth: int | None = None, confirm_depth: int | None = None):
        settings = get_settings()
        self.engine = engine
        self.depth = depth or settings.game_analysis_depth
        self.confirm_depth = max(self.depth, confirm_depth or settings.engine_depth)
        self._cache: dict[tuple[str, int], Analysis] = {}

    # ------------------------------------------------------------- engine
    def _analyse(self, board: chess.Board, depth: int) -> Analysis:
        terminal = _terminal_score(board)
        if terminal is not None:
            return Analysis(fen=board.fen(), best_move_uci=None, best_move_san=None, score=terminal,
                            pv_san=[], depth=depth)
        key = (board.epd(), depth)
        if key not in self._cache:
            self._cache[key] = self.engine.analyse(board, depth=depth)
        return self._cache[key]

    def _alternatives(self, board: chess.Board, best_uci: str | None, played: chess.Move,
                      reference: Score | None = None) -> list[str]:
        """Other moves Stockfish rates (nearly) as good as its best one (`reference`)."""
        if not hasattr(self.engine, "analyse_lines"):
            return []
        try:
            lines = self.engine.analyse_lines(board, depth=self.confirm_depth, multipv=3)
        except Exception:
            return []
        if not lines:
            return []
        side = board.turn
        top = max(lines[0].score.for_side(side), reference.for_side(side) if reference else -10**6)
        out = []
        for line in lines:
            if line.move.uci() == best_uci or line.move == played:
                continue
            if top - line.score.for_side(side) <= GOOD_CP:
                out.append(line.san)
        return out

    # ------------------------------------------------------------- main
    def iter_analysis(self, game: GameRecord) -> Iterator[dict]:
        """Yields {"type": "progress", ...} events, then {"type": "analysis", "analysis": {...}}."""
        side = game.learner
        if side is None:
            raise ValueError("the game doesn't say which side the learner played")
        boards = game.boards()
        moves = [chess.Move.from_uci(u) for u in game.moves_uci]
        total = len(boards)

        quick: list[Analysis] = []
        for i, board in enumerate(boards):
            quick.append(self._analyse(board, self.depth))
            yield {"type": "progress", "game_id": game.id, "done": i + 1, "total": total}

        plies, candidates = [], []
        stats = {c.value: 0 for c in Classification}
        losses = []
        for i, move in enumerate(moves):
            before = boards[i]
            entry = {"ply": i, "move_number": before.fullmove_number, "side": "white" if before.turn else "black",
                     "san": game.moves_san[i], "uci": move.uci(), "eval": quick[i + 1].score.as_dict()
                     if quick[i + 1].score else None}
            if before.turn == side:
                best = chess.Move.from_uci(quick[i].best_move_uci) if quick[i].best_move_uci else None
                category, loss, notes = classify_move(before, move, quick[i].score, quick[i + 1].score, best)
                entry.update({"category": category.value, "loss_cp": loss})
                if (category in (Classification.MISTAKE, Classification.BLUNDER) or "missed_mate" in notes
                        or (category == Classification.INACCURATE and loss >= CANDIDATE_MIN_LOSS)):
                    candidates.append(i)
            plies.append(entry)

        yield {"type": "progress", "game_id": game.id, "done": total, "total": total, "stage": "checking"}
        moments = []
        for i in candidates:
            moment = self._moment(game, boards, moves, i, side)
            if moment is not None:
                moments.append(moment)
                plies[i]["category"] = moment["category"]
                plies[i]["loss_cp"] = moment["loss_cp"]

        for entry in plies:
            if "category" in entry:
                stats[entry["category"]] += 1
                losses.append(min(entry["loss_cp"], 1000))

        # Most costly first for the cap, then back into game order.
        moments.sort(key=lambda m: (-m["severity_weight"], -m["loss_cp"]))
        extra = moments[MAX_MOMENTS:]
        moments = sorted(moments[:MAX_MOMENTS], key=lambda m: m["ply"])

        start_cp = _learner_cp(quick[0].score, side) or 0
        end_index = min(OPENING_PLIES, len(moves))
        end_cp = _learner_cp(quick[end_index].score, side) or 0
        king_trouble = any(f["motif"] in ("allowed_checkmate", "king_safety")
                           for m in moments for f in m["findings"])
        habits = []
        for ply, f in detect_habits(boards, moves, side, start_cp - end_cp, king_trouble):
            habits.append(self._habit(game, boards, moves, ply, f))

        phase_counts: dict[str, int] = {}
        for m in moments:
            phase_counts[m["phase"]] = phase_counts.get(m["phase"], 0) + 1
        analysis = {
            "schema_version": SCHEMA_VERSION,
            "game_id": game.id,
            "player_color": game.player_color,
            "engine": "stockfish",
            "depth": self.depth,
            "confirm_depth": self.confirm_depth,
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "plies": plies,
            "evals": [a.score.as_dict() if a.score else None for a in quick],
            "moments": moments,
            "more_moments": len(extra),
            "habits": habits,
            "stats": {
                "learner_moves": sum(1 for e in plies if "category" in e),
                "by_category": stats,
                "avg_loss_cp": round(sum(losses) / len(losses)) if losses else 0,
                "mistakes_by_phase": phase_counts,
            },
        }
        yield {"type": "analysis", "game_id": game.id, "analysis": analysis}

    def analyze(self, game: GameRecord) -> dict:
        result = None
        for event in self.iter_analysis(game):
            if event["type"] == "analysis":
                result = event["analysis"]
        return result

    # ------------------------------------------------------------- pieces
    def _source(self, game: GameRecord) -> dict:
        return {"game_id": game.id, "source": game.source, "url": game.url, "white": game.white,
                "black": game.black, "date": game.date, "opponent": game.opponent,
                "player_color": game.player_color, "result": game.result}

    def _moment(self, game: GameRecord, boards, moves, i: int, side: chess.Color) -> dict | None:
        before, after, move = boards[i], boards[i + 1], moves[i]
        deep_before = self._analyse(before, self.confirm_depth)
        deep_after = self._analyse(after, self.confirm_depth)
        best = chess.Move.from_uci(deep_before.best_move_uci) if deep_before.best_move_uci else None
        category, loss, notes = classify_move(before, move, deep_before.score, deep_after.score, best)
        if category not in (Classification.MISTAKE, Classification.BLUNDER) and "missed_mate" not in notes:
            return None  # the quick pass was noise: the deeper search doesn't confirm it
        if _decided(deep_before.score, deep_after.score, side) and loss < 3 * INACCURATE_CP \
                and "missed_mate" not in notes:
            return None  # already decided either way, and it didn't change much: not worth a beginner's time
        best_line = _uci_line(before, deep_before.pv_san)
        reply_line = _uci_line(after, deep_after.pv_san)
        phase = phase_of(before, i)
        findings = detect(before, move, best_line, reply_line, deep_before.score, deep_after.score, phase)
        from .motifs import replay_moves, swing
        actual_gain = swing(replay_moves(before, [move] + reply_line[:6]), side)
        best_gain = swing(replay_moves(before, best_line[:7]), side) if best_line else 0
        label = f"{before.fullmove_number}.{'' if before.turn else '..'}{game.moves_san[i]}"
        return {
            "id": f"{game.id}:{i}",
            "game_id": game.id,
            "ply": i,
            "move_number": before.fullmove_number,
            "side": "white" if before.turn else "black",
            "label": label,
            "san": game.moves_san[i],
            "uci": move.uci(),
            "symbol": SYMBOL.get(category.value, ""),
            "fen_before": before.fen(),
            "fen_after": after.fen(),
            "phase": phase,
            "category": category.value,
            "loss_cp": loss,
            "notes": notes,
            "eval_before": deep_before.score.as_dict() if deep_before.score else None,
            "eval_after": deep_after.score.as_dict() if deep_after.score else None,
            "eval_before_cp": _learner_cp(deep_before.score, side),
            "eval_after_cp": _learner_cp(deep_after.score, side),
            "best_move": deep_before.best_move_san,
            "best_move_uci": deep_before.best_move_uci,
            "best_line": deep_before.pv_san[:8],
            "reply_line": deep_after.pv_san[:8],
            "alternatives": self._alternatives(before, deep_before.best_move_uci, move, deep_before.score),
            "material_change": actual_gain,
            "best_line_material": best_gain,
            "findings": [f.as_dict() for f in findings],
            "motif": findings[0].motif if findings else None,
            "concept": next((f.concept for f in findings if f.concept), None),
            "severity": category.value,
            "severity_weight": SEVERITY_WEIGHT[category.value] if category.value in SEVERITY_WEIGHT else 2,
            "depth": self.confirm_depth,
            "source": self._source(game),
        }

    def _habit(self, game: GameRecord, boards, moves, ply: int, f) -> dict:
        before = boards[ply]
        return {
            "id": f"{game.id}:habit:{f.motif}",
            "game_id": game.id,
            "ply": ply,
            "move_number": before.fullmove_number,
            "side": "white" if before.turn else "black",
            "san": before.san(moves[ply]),
            "uci": moves[ply].uci(),
            "fen_before": before.fen(),
            "phase": "opening",
            "category": "habit",
            "severity": "habit",
            "severity_weight": SEVERITY_WEIGHT["habit"],
            "findings": [f.as_dict()],
            "motif": f.motif,
            "concept": f.concept,
            "source": self._source(game),
        }
