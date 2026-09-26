"""Core tests: chess system, classification, lessons, fallback teacher.

Engine integration tests live in test_engine.py (they need Node + the
stockfish npm package).
"""
import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chess_system import ChessError, parse_fen, parse_move, validate_board_spec
from app.engine.classification import Classification, Score, classify_move
from app.lessons import get_library
from app.teacher import FallbackTeacher, LessonContext


# --- chess system ---

def test_start_position_legal_moves():
    board = parse_fen(chess.STARTING_FEN)
    assert len(list(board.legal_moves)) == 20
    assert parse_move(board, "e2e4").uci() == "e2e4"
    assert parse_move(board, "e4").uci() == "e2e4"  # SAN also accepted


def test_illegal_move_rejected():
    board = parse_fen(chess.STARTING_FEN)
    with pytest.raises(ChessError):
        parse_move(board, "e2e5")
    with pytest.raises(ChessError):
        parse_move(board, "Qh5")  # queen can't reach h5 yet


def test_same_square_uci_is_rejected_not_crashing():
    """Regression: "a1a1" matched the UCI pattern and raised InvalidMoveError (HTTP 500)."""
    board = parse_fen(chess.STARTING_FEN)
    for bad in ("a1a1", "e2e2", "e7e7q"):
        with pytest.raises(ChessError):
            parse_move(board, bad)


def test_invalid_fen_rejected():
    with pytest.raises(ChessError):
        parse_fen("not a fen")


def test_move_history_and_replay():
    board = parse_fen(chess.STARTING_FEN)
    for uci in ["e2e4", "e7e5", "g1f3"]:
        board.push(parse_move(board, uci))
    assert [m.uci() for m in board.move_stack] == ["e2e4", "e7e5", "g1f3"]
    assert board.fen() == "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


def test_board_spec_validation():
    spec = validate_board_spec({
        "fen": chess.STARTING_FEN,
        "highlights": [{"square": "e4", "color": "green"}],
        "lock": True,
    })
    assert spec["lock"] is True and spec["highlights"][0]["square"] == "e4"
    with pytest.raises(ChessError):
        validate_board_spec({"highlights": [{"square": "z9", "color": "green"}]})
    with pytest.raises(ChessError):
        validate_board_spec({"mystery_key": 1})


# --- classification ---

def _score_cp(cp):
    return Score("cp", cp)


def test_classification_thresholds():
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    for loss, expected in [
        (0, Classification.EXCELLENT),
        (30, Classification.GOOD),
        (100, Classification.INACCURATE),
        (250, Classification.MISTAKE),
        (500, Classification.BLUNDER),
    ]:
        cat, got_loss, _ = classify_move(board, move, _score_cp(30), _score_cp(30 - loss))
        assert cat == expected and got_loss == loss


def test_missed_mate_escalates():
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    # Losing a forced mate is always catastrophic (huge eval loss).
    cat, _, notes = classify_move(board, move, Score("mate", 3), _score_cp(20))
    assert cat == Classification.BLUNDER
    assert "missed_mate" in notes


def test_decided_position_damps_blunder():
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    # 1100 -> 710: a big loss, but the position stays winning for White.
    cat, _, notes = classify_move(board, move, _score_cp(1100), _score_cp(710))
    assert cat == Classification.MISTAKE  # blunder de-escalated
    assert "decided_position" in notes


def test_black_perspective_loss():
    board = chess.Board(chess.STARTING_FEN)
    board.push_uci("e2e4")
    move = chess.Move.from_uci("e7e5")
    # White POV swings -20 -> +60 (White gains 80) ⇒ Black's move loses 80 cp.
    cat, loss, _ = classify_move(board, move, _score_cp(-20), _score_cp(60))
    assert loss == 80 and cat == Classification.INACCURATE



# --- checkmate on the board (regression: every mating move was graded a blunder) ---

def test_engine_mate_zero_keeps_the_winner():
    """Stockfish reports a mated position as "mate 0"; python-chess keeps who won
    (MateGiven vs Mate(-0)) but both have .mate() == 0 — the sign must survive."""
    import chess.engine as ce
    white_mated_black = Score.from_pov_white(ce.PovScore(ce.Mate(0), chess.BLACK))
    black_mated_white = Score.from_pov_white(ce.PovScore(ce.Mate(0), chess.WHITE))
    assert white_mated_black == Score.checkmate(white_won=True)
    assert black_mated_white == Score.checkmate(white_won=False)
    assert white_mated_black.is_mate_for(True) and not white_mated_black.is_mate_for(False)
    assert black_mated_white.is_mate_for(False) and not black_mated_white.is_mate_for(True)
    assert white_mated_black.to_cp() > Score("mate", 1).to_cp()  # mating beats "mate in 1"


@pytest.mark.parametrize("fen,uci", [
    ("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", "d1d8"),  # White: back-rank mate
    ("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2", "d8h4"),  # Black: fool's mate
])
def test_delivering_checkmate_is_excellent(fen, uci):
    board = chess.Board(fen)
    move = chess.Move.from_uci(uci)
    before = Score("mate", 1 if board.turn == chess.WHITE else -1)
    after = Score.checkmate(white_won=board.turn == chess.WHITE)
    cat, loss, notes = classify_move(board, move, before, after)
    assert (cat, loss, notes) == (Classification.EXCELLENT, 0, [])


def test_checkmate_formats_as_checkmate_not_mate_in_zero():
    from app.teacher.base import _fmt_eval
    won = Score.checkmate(white_won=False).as_dict()
    assert _fmt_eval(won, for_white=False) == "checkmate - Black wins"
    assert _fmt_eval(won, for_white=True) == "checkmate - White is mated"


# --- lessons ---

def test_italian_course_loads_and_validates():
    lib = get_library()
    course = lib.course("italian_game")
    assert course.title == "Italian Game"
    assert any(p.status == "planned" for p in course.lessons)
    lesson = lib.lesson("italian_01")
    assert [s.type for s in lesson.steps] == ["teach", "demonstrate", "exercise", "exercise", "teach"]


def test_demonstration_moves_are_legal():
    lesson = get_library().lesson("italian_01")
    demo = next(s for s in lesson.steps if s.type == "demonstrate")
    board = parse_fen(demo.fen)
    for uci in demo.moves:
        board.push(parse_move(board, uci))
    assert len(demo.comments) == len(demo.moves)


def test_exercises_consistent():
    lesson = get_library().lesson("italian_01")
    exercises = [s for s in lesson.steps if s.type == "exercise"]
    assert len(exercises) == 2
    for ex in exercises:
        board = parse_fen(ex.fen)
        side = "white" if board.turn else "black"
        assert side == ex.side
        for san in ex.accepted_san:
            board.parse_san(san)  # must be legal
        assert len(ex.hints) >= 3


# --- teacher ---

def test_fallback_explains_from_facts_only():
    lesson = get_library().lesson("italian_01")
    ex = next(s for s in lesson.steps if s.type == "exercise")
    ctx = LessonContext(
        course_title="Italian Game",
        lesson_title=lesson.title,
        concepts=lesson.concepts,
        exercise_prompt=ex.prompt,
        accepted_moves=ex.accepted_san,
    )
    feedback = _make_feedback()
    text = FallbackTeacher().explain_move(feedback, ctx)
    assert "blunder" in text.lower() and "c5" in text  # references real engine facts


def test_fallback_refuses_without_engine_facts():
    ctx = LessonContext(course_title="c", lesson_title="l", concepts=[])
    feedback = _make_feedback()
    feedback.eval_before = None
    feedback.eval_after = None
    text = FallbackTeacher().explain_move(feedback, ctx)
    assert "engine analysis" in text.lower()


def test_fallback_points_to_lesson_goal_move():
    """Sound engine move that isn't the taught line must reference the goal."""
    lesson = get_library().lesson("italian_01")
    ex = next(s for s in lesson.steps if s.type == "exercise")
    ctx = LessonContext(
        course_title="Italian Game",
        lesson_title=lesson.title,
        concepts=lesson.concepts,
        exercise_prompt=ex.prompt,
        accepted_moves=ex.accepted_san,
    )
    feedback = _make_feedback()
    feedback.category = Classification.EXCELLENT  # sound, but off-line
    feedback.user_move_san = "d4"
    text = FallbackTeacher().explain_move(feedback, ctx)
    assert "Bc4" in text and "try the lesson's move" in text


def _make_feedback():
    from app.engine import MoveFeedback

    board = chess.Board()
    board.push_uci("e2e4")
    fen_before = board.fen()  # Black to move — the position before their blunder
    board.push_uci("d7d5")  # a real blunder (loses a pawn)
    move = chess.Move.from_uci("d7d5")
    return MoveFeedback(
        fen_before=fen_before,
        fen_after=board.fen(),
        user_move_uci="d7d5",
        user_move_san="d5",
        category=Classification.BLUNDER,
        loss_cp=350,
        best_move_uci="c7c5",
        best_move_san="c5",
        eval_before=_score_cp(-30),
        eval_after=_score_cp(-380),
        best_pv_san=["c5", "d4", "dxe4"],
        reply_pv_san=["exd5", "Qxd5"],
        depth=14,
    )
