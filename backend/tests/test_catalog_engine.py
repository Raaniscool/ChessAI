"""Stockfish audit of the topic catalog (skipped when no engine is available).

This is what makes catalog content trustworthy: every exercise's expected
answer must be what the engine considers best, and every opening line must be
free of mistakes. Adding a topic to topics.json automatically adds it here.
"""
import chess
import chess.engine
import pytest

from app.engine import EngineUnavailable, get_engine, set_engine
from app.engine.classification import Classification
from app.planner import get_catalog
from app.planner.planner import screen_line

DEPTH = 16
CATALOG = get_catalog()
POSITIONS = [
    (f"{t.id}[{i}]", pos) for t in CATALOG.topics.values() for i, pos in enumerate(t.positions)
]
OPENINGS = [t for t in CATALOG.topics.values() if t.category == "opening"]


@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


@pytest.mark.parametrize("name, pos", POSITIONS, ids=[n for n, _ in POSITIONS])
def test_catalog_position_answers_are_engine_best(engine, name, pos):
    board = chess.Board(pos["fen"])
    best = engine.analyse(board, depth=DEPTH)
    if pos["advance_on"] == "accepted_move":
        # The engine's top choice must be accepted (never reject the best move)...
        assert best.best_move_san in pos["accepted"], f"{name}: engine prefers {best.best_move_san}"
        # ...and every accepted move must be genuinely good.
        for san in pos["accepted"]:
            fb = engine.evaluate_move(board, board.parse_san(san), depth=DEPTH)
            assert fb.category in (Classification.EXCELLENT, Classification.GOOD), (name, san, fb.category)
    elif pos["min_category"] == "excellent":
        # "Checkmate in one" style tasks: a mate must exist.
        assert best.score.kind == "mate" and abs(best.score.value) == 1, (name, best.score)
    else:
        # Open-ended tasks: several good moves must exist so the exercise is fair.
        info = engine._engine.analyse(board, chess.engine.Limit(depth=12), multipv=3)
        top = info[0]["score"].pov(board.turn).score(mate_score=10000)
        good = [i for i in info if top - i["score"].pov(board.turn).score(mate_score=10000) <= 50]
        assert len(good) >= 2, name


@pytest.mark.parametrize("topic", OPENINGS, ids=[t.id for t in OPENINGS])
def test_catalog_opening_lines_have_no_mistakes(engine, topic):
    verified, note = screen_line(topic.line, engine)
    assert verified == topic.line, f"{topic.id}: {note}"
