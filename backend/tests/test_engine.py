"""Engine integration tests — require Node + the `stockfish` npm package.

Skipped automatically when the engine is unavailable.
"""
import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import EngineUnavailable, get_engine, set_engine
from app.engine.classification import Classification


@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def test_analyse_start_position(engine):
    result = engine.analyse(chess.Board(), depth=8)
    assert result.best_move_uci is not None
    assert result.score is not None
    assert result.score.kind == "cp"


def test_mate_in_one_detected(engine):
    # White to play: Qf7# (scholar's mate position)
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    result = engine.analyse(board, depth=10)
    assert result.score.kind == "mate" and result.score.value > 0
    assert result.best_move_san == "Qxf7#"


def test_evaluate_move_classifies_blunder(engine):
    # 1.e4 e5 2.Qh5 Nc6 3.Qxf7?? — grabbing f7 loses the queen to Kxf7.
    board = chess.Board()
    for uci in ["e2e4", "e7e5", "d1h5", "b8c6"]:
        board.push_uci(uci)
    feedback = engine.evaluate_move(board, chess.Move.from_uci("h5f7"), depth=10)
    assert feedback.category == Classification.BLUNDER
    assert feedback.best_move_uci is not None
    assert feedback.eval_before is not None


@pytest.mark.parametrize("fen,san", [
    ("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", "Rd8#"),
    ("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "Qxf7#"),
    ("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2", "Qh4#"),
    ("6rk/6bp/7N/5p2/q7/4p2P/6P1/3R2K1 w - - 0 39", "Nf7#"),
])
def test_checkmating_move_is_graded_excellent(engine, fen, san):
    """Regression: every mating move was graded Blunder (White) / Mistake (Black)."""
    board = chess.Board(fen)
    feedback = engine.evaluate_move(board, board.parse_san(san), depth=10)
    assert feedback.category == Classification.EXCELLENT
    assert feedback.loss_cp == 0 and "missed_mate" not in feedback.notes
    assert feedback.eval_after.kind == "checkmate"
    assert feedback.eval_after.is_mate_for(board.turn)


def test_underpromotion_that_misses_mate_is_still_flagged(engine):
    board = chess.Board("8/4P3/8/8/8/k7/8/K7 w - - 0 1")
    feedback = engine.evaluate_move(board, board.parse_san("e8=B"), depth=10)
    assert feedback.category != Classification.EXCELLENT  # a bishop can't mate: the win is gone
