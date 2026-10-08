"""Verified puzzle sets: data integrity, lesson generation, search, planning, and the builder."""
import importlib.util
from pathlib import Path

import chess
import chess.engine
import pytest

from app.engine import EngineUnavailable, get_engine, set_engine
from app.lessons import parse_lesson
from app.planner import create_plan, get_catalog
from app.planner.catalog import MIN_PUZZLES
from app.planner.generator import PATTERN_PUZZLES, puzzle_lessons

CATALOG = get_catalog()
PUZZLE_TOPICS = [t for t in CATALOG.topics.values() if t.puzzle_theme]
ALL_PUZZLES = [(f"{t.id}:{p['id']}", t, p) for t in PUZZLE_TOPICS for p in t.puzzles]

spec = importlib.util.spec_from_file_location(
    "build_puzzle_library", Path(__file__).resolve().parents[2] / "scripts" / "build_puzzle_library.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


# ---- data ------------------------------------------------------------------

def test_every_puzzle_topic_has_a_full_verified_set():
    assert len(PUZZLE_TOPICS) >= 35
    for topic in PUZZLE_TOPICS:
        assert len(topic.puzzles) >= MIN_PUZZLES, topic.id
        assert topic.puzzle_text.get("task") and topic.puzzle_text.get("hint"), topic.id
    assert "CC0" in CATALOG.puzzle_meta.get("license", "")


@pytest.mark.parametrize("name, topic, puzzle", ALL_PUZZLES, ids=[n for n, _, _ in ALL_PUZZLES])
def test_puzzle_replays_legally_and_final_answers_are_right(name, topic, puzzle):
    board = chess.Board(puzzle["fen"])
    moves = puzzle["moves"]
    assert len(moves) >= 2 and len(moves) % 2 == 0  # setup move, then the learner's moves
    for uci in moves[:-1]:
        move = chess.Move.from_uci(uci)
        assert move in board.legal_moves
        board.push(move)
    final_before = board.copy()
    board.push(chess.Move.from_uci(moves[-1]))
    assert board.is_checkmate() == puzzle["mate"]
    assert puzzle["final_accepted"]
    for san in puzzle["final_accepted"]:
        move = final_before.parse_san(san)  # legal
        if puzzle["mate"]:
            after = final_before.copy()
            after.push(move)
            assert after.is_checkmate(), f"{san} accepted but isn't mate"


def test_smothered_mate_puzzles_really_are_smothered():
    """The mating knight's victim has no free square: every neighbour is blocked by its own
    pieces or covered — and it's a knight that mates."""
    topic = CATALOG.get("smothered_mate")
    for p in topic.puzzles:
        board = chess.Board(p["fen"])
        for uci in p["moves"]:
            board.push_uci(uci)
        assert board.is_checkmate()
        last = board.peek()
        assert board.piece_at(last.to_square).piece_type == chess.KNIGHT, p["id"]
        king = board.king(board.turn)
        neighbours = chess.SquareSet(chess.BB_KING_ATTACKS[king])
        own = [sq for sq in neighbours if board.color_at(sq) == board.turn]
        assert len(own) >= len(neighbours) - 1, p["id"]  # boxed in by its own pieces


# ---- lessons ---------------------------------------------------------------

def test_puzzle_topic_becomes_pattern_and_practice_lessons():
    topic = CATALOG.get("smothered_mate")
    pattern, practice = puzzle_lessons("t_sm_", topic)
    for lesson in (pattern, practice):
        parse_lesson(lesson, course_id="x")
    ex = [s for s in pattern["steps"] if s["type"] == "exercise"]
    assert len(ex) == PATTERN_PUZZLES and all(s["prompt"] == "Find the best move." for s in ex)
    # no spoiler: the task never names the idea ("Find the smothered mate") — the hint can
    assert all("smother" not in s["prompt"].lower() for s in ex) and all(s["hints"] for s in ex)
    # every exercise is preceded by the animated opponent move it answers
    steps = practice["steps"]
    for i, step in enumerate(steps):
        if step["type"] == "exercise":
            demo = steps[i - 1]
            assert demo["type"] == "demonstrate"
            board = chess.Board(demo["fen"])
            board.push_uci(demo["moves"][0])
            assert board.fen() == step["fen"]
    last = [s for s in steps if s["type"] == "exercise"][-1]
    assert last["continue_text"].startswith("Checkmate!") and "lichess.org/training/" in last["continue_text"]


def test_existing_hand_made_topic_keeps_its_lesson_and_gains_practice():
    lessons = puzzle_lessons("t_fk_", CATALOG.get("forks"))
    assert [lesson["title"] for lesson in lessons] == ["Forks", "Forks: practice"]


# ---- search & plans --------------------------------------------------------

@pytest.mark.parametrize("goal, expected", [
    ("I want to learn the smothered mate", ["smothered_mate"]),
    ("smothered checkmate", ["smothered_mate"]),          # not + back-rank "checkmate"
    ("Philidor's legacy", ["smothered_mate"]),
    ("discovered attack", ["discovered_attacks"]),       # not + king attack "attack"
    ("x-ray attacks", ["x_ray"]),
    ("I want to learn the Sicilian defense", ["sicilian_defense"]),  # not + "defend"
    ("I want to learn the two knights defense", ["two_knights_defense"]),
])
def test_search_returns_only_what_was_asked(goal, expected):
    assert [t.id for t in CATALOG.search(goal)] == expected


def test_checkmate_patterns_gives_the_pattern_family():
    ids = [t.id for t in CATALOG.search("I want to learn checkmate patterns")]
    assert {"back_rank_mate", "smothered_mate", "anastasia_mate", "arabian_mate", "boden_mate"} <= set(ids)


def test_smothered_mate_plan_is_about_smothered_mate():
    """The user's report: 'I want to learn the smothered mate' made a forks/pins/skewers plan."""
    record = create_plan("I want to learn the smothered mate", use_qwen=False)
    plan = record["plan"]
    assert [u["topic_id"] for u in plan["units"]] == ["smothered_mate"]
    assert plan["title"] == "Learn: Smothered mate"
    assert [lesson["title"] for lesson in record["lessons"]] == [
        "Smothered mate: learn the pattern", "Smothered mate: practice"]


# ---- builder ---------------------------------------------------------------

SAMPLE_CSV = (
    '"[Event ""Puzzle""]\n[Site ""https://www.lichess.org/training/Txysn""]\n'
    '[Themes ""mate mateIn1 smotheredMate""]\n[FEN ""5rQk/p5pp/7N/8/4P1PP/6K1/8/3b4 b - - 5 50""]\n'
    '[SetUp ""1""]\n\n50... Rxg8 51. Nf7# *",mate mateIn1 smotheredMate\r\n'
)


def test_builder_parses_lichess_pgn_csv(tmp_path):
    path = tmp_path / "smotheredMate.csv"
    path.write_text(SAMPLE_CSV, encoding="utf-8")
    [rec] = builder.parse_pgn_csv(path)
    assert rec["id"] == "Txysn" and rec["sans"] == ["Rxg8", "Nf7#"]
    assert builder.to_ucis(rec["fen"], rec["sans"]) == ["f8g8", "h6f7"]


def test_builder_parses_windows_line_endings(tmp_path):
    """Regression (seen on Windows): CRLF inside the quoted PGN hid the header/moves separator."""
    path = tmp_path / "smotheredMate.csv"
    path.write_bytes(SAMPLE_CSV.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    [rec] = builder.parse_pgn_csv(path)
    assert rec["id"] == "Txysn" and rec["sans"] == ["Rxg8", "Nf7#"]


class FakeEngine:
    """analyse() returns scripted multipv infos (learner-POV scores given as White POV)."""

    def __init__(self, lines):
        self.lines = lines

    def analyse(self, board, limit, multipv=1):
        out = []
        for uci, cp in self.lines:
            pov = chess.engine.PovScore(chess.engine.Cp(cp), board.turn)
            out.append({"pv": [chess.Move.from_uci(uci)], "score": pov})
        return out


# White to move after Black's setup ...Kh8: 1.Qxd8 wins the queen (non-mate final move).
REC = {"id": "t", "fen": "3q3k/8/8/8/8/8/8/3Q2K1 b - - 0 1", "ucis": ["h8g8", "d1d8"]}


def test_builder_accepts_unique_best_move():
    puzzle = builder.verify(FakeEngine([("d1d8", 900), ("g1f2", 0)]), REC, depth=1)
    assert puzzle and puzzle["final_accepted"] == ["Qxd8+"] and puzzle["mate"] is False


def test_builder_rejects_when_engine_prefers_another_move():
    assert builder.verify(FakeEngine([("g1f2", 50), ("d1d8", 0)]), REC, depth=1) is None


def test_builder_rejects_when_an_alternative_is_almost_as_good():
    assert builder.verify(FakeEngine([("d1d8", 900), ("d1d7", 850)]), REC, depth=1) is None


def test_builder_accepts_every_mate_on_the_final_move():
    # Setup ...Rxg8, then 1.Nf7# — the only mate; no engine call needed for a final mate.
    rec = {"id": "Txysn", "fen": "5rQk/p5pp/7N/8/4P1PP/6K1/8/3b4 b - - 5 50", "ucis": ["f8g8", "h6f7"]}
    puzzle = builder.verify(FakeEngine([]), rec, depth=1)
    assert puzzle["mate"] and puzzle["final_accepted"] == ["Nf7#"]


# ---- independent Stockfish audit (skipped without an engine) ---------------

FIRST_OF_EACH = [(t.id, t.puzzles[-1]) for t in PUZZLE_TOPICS]  # hardest (longest) of each set


@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


@pytest.mark.parametrize("topic_id, puzzle", FIRST_OF_EACH, ids=[t for t, _ in FIRST_OF_EACH])
def test_audit_every_learner_move_is_engine_best(engine, topic_id, puzzle):
    board = chess.Board(puzzle["fen"])
    board.push_uci(puzzle["moves"][0])
    solution = puzzle["moves"][1:]
    for i in range(0, len(solution), 2):
        move = chess.Move.from_uci(solution[i])
        final = i + 1 >= len(solution)
        if not (final and puzzle["mate"]):
            analysis = engine.analyse(board, depth=14)
            assert analysis.best_move_uci == move.uci(), (topic_id, puzzle["id"], i, analysis.best_move_san)
        board.push(move)
        if not final:
            board.push_uci(solution[i + 1])
