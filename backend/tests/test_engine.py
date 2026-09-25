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
