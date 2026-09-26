"""Planner: catalog search, lesson generation, Qwen-plan parsing, line screening, storage."""
import json

import chess
import pytest

from app.engine import Classification, MoveFeedback, Score
from app.lessons import LessonLibrary, parse_lesson
from app.lessons.schema import LessonError
from app.planner import PlanError, create_plan, get_catalog
from app.planner import planner as planner_mod
from app.planner.generator import move_hints, topic_lessons
from app.planner.planner import (
    QwenPlan,
    extract_json,
    parse_qwen_plan,
    screen_line,
    tokenize_moves,
)
from app.planner.store import load_saved_plans, register_record, save_record


class ScriptedEngine:
    """Rates every move excellent, except the plies listed in `bad`."""

    def __init__(self, bad=()):
        self.bad = set(bad)
        self.calls = 0

    def evaluate_move(self, board, move, depth=None):
        ply = self.calls
        self.calls += 1
        san = board.san(move)
        after = board.copy()
        after.push(move)
        cat = Classification.BLUNDER if ply in self.bad else Classification.EXCELLENT
        return MoveFeedback(
            fen_before=board.fen(), fen_after=after.fen(), user_move_uci=move.uci(),
            user_move_san=san, category=cat, loss_cp=0, best_move_uci=move.uci(),
            best_move_san=san, eval_before=Score("cp", 0), eval_after=Score("cp", 0),
            best_pv_san=[san], reply_pv_san=[], depth=depth or 10,
        )


# ---- catalog ---------------------------------------------------------------

def test_catalog_loads_and_every_topic_generates_valid_lessons():
    catalog = get_catalog()
    assert len(catalog.topics) >= 20
    for topic in catalog.topics.values():
        lessons = topic_lessons(f"t_{topic.id}_", topic)
        assert lessons
        for lesson in lessons:
            parse_lesson(lesson, course_id="x")  # raises on anything invalid


@pytest.mark.parametrize("goal, expected_first", [
    ("I want to learn the Sicilian", "sicilian_defense"),
    ("teach me the sicillian defence", "sicilian_defense"),  # typo tolerated
    ("I want to learn the Queen's Gambit", "queens_gambit"),
    ("how do knight forks work", "forks"),
    ("I want to learn how to checkmate with a rook", "kr_vs_k"),
    ("london system please", "london_system"),
    ("caro-kann", "caro_kann"),
    ("what is the opposition in pawn endgames", "king_pawn_opposition"),
])
def test_search_specific_topics(goal, expected_first):
    assert get_catalog().search(goal)[0].id == expected_first


def test_search_categories_and_general():
    catalog = get_catalog()
    tactic_ids = {t.id for t in catalog.search("I want to get better at tactics")}
    assert tactic_ids == {t.id for t in catalog.by_category("tactic")}
    assert {t.category for t in catalog.search("endgames")} == {"endgame"}
    general = [t.id for t in catalog.search("I want to learn chess")]
    assert general == catalog.general["topics"]
    assert catalog.search("xyzzy quantum knitting") == []


def test_search_side_preference():
    ids = [t.id for t in get_catalog().search("an opening like the italian but as black, two knights")]
    assert ids[0] == "two_knights_defense"


def test_prerequisites_come_first():
    catalog = get_catalog()
    ordered = catalog.with_prerequisites([catalog.get("sicilian_defense")])
    assert [t.id for t, _ in ordered] == ["opening_principles", "sicilian_defense"]
    assert ordered[0][1] == "Sicilian Defense"


# ---- generator -------------------------------------------------------------

def test_opening_lessons_follow_the_line_for_the_learner_side():
    catalog = get_catalog()
    topic = catalog.get("french_defense")  # learner plays black
    intro, drill = topic_lessons("p_", topic)
    exercises = [s for s in drill["steps"] if s["type"] == "exercise"]
    assert all(s["side"] == "black" for s in exercises)
    board = chess.Board()
    black_moves = []
    for ply, san in enumerate(topic.line):
        if ply % 2 == 1:
            black_moves.append((board.fen(), san))
        board.push_san(san)
    for step, (fen, san) in zip(exercises, black_moves):
        assert step["fen"] == fen
        assert step["accepted"] == [san]
        assert len(step["hints"]) == 3
    demo = next(s for s in intro["steps"] if s["type"] == "demonstrate")
    assert len(demo["moves"]) == len(topic.line)


def test_move_hints_castling_and_capture():
    board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/3P1N2/PPP2PPP/RNBQK2R w KQkq - 1 5")
    assert "Castle kingside" in move_hints(board, board.parse_san("O-O"))[1]
    board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
    assert move_hints(board, board.parse_san("exd5"))[0].startswith("It's a capture")


# ---- Qwen plan parsing / line screening ------------------------------------

def test_extract_json_tolerates_chatter_and_fences():
    text = 'Sure!\n```json\n{"title": "A {tricky} plan", "topics": [{"id": "forks"}]}\n```\nEnjoy'
    assert extract_json(text)["title"] == "A {tricky} plan"
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_qwen_plan_drops_unknown_topics_and_bad_openings():
    data = {
        "title": "  Sharp   play  ", "summary": "s",
        "topics": [{"id": "forks", "reason": "r"}, {"id": "made_up"}, "pins", {"id": "forks"}],
        "new_openings": [
            {"name": "Vienna Game", "side": "white", "moves": "e4 e5 Nc3", "reason": "x"},
            {"name": "No side", "moves": "e4"},
            "garbage",
        ],
    }
    plan = parse_qwen_plan(data, get_catalog())
    assert plan.title == "Sharp play"
    assert plan.topic_ids == ["forks", "pins"]
    assert [o["name"] for o in plan.new_openings] == ["Vienna Game"]


def test_tokenize_moves_strips_numbers_and_annotations():
    assert tokenize_moves("1. e4 e5 2.Nf3 Nc6!? 3...Bc5 0-0 *") == ["e4", "e5", "Nf3", "Nc6", "Bc5", "O-O"]


def test_screen_line_cuts_at_illegal_or_bad_moves():
    line = "e4 e5 Nc3 Nf6 f4 d5 fxe5 Nxe4".split()
    verified, note = screen_line(line, ScriptedEngine())
    assert verified == line and "Stockfish" in note
    verified, note = screen_line(line, ScriptedEngine(bad={4}))
    assert verified == line[:4] and "blunder" in note
    verified, note = screen_line(["e4", "e5", "Ke3"], ScriptedEngine())
    assert verified == ["e4", "e5"] and "illegal" in note


# ---- create_plan -----------------------------------------------------------

def test_create_plan_from_catalog():
    record = create_plan("I want to learn the Sicilian Defense", use_qwen=False)
    plan = record["plan"]
    assert [u["topic_id"] for u in plan["units"]] == ["opening_principles", "sicilian_defense"]
    assert plan["planner"] == "catalog"
    ids = [lesson["id"] for lesson in record["lessons"]]
    assert len(ids) == len(set(ids)) == 3  # principles (1) + sicilian (2)
    assert all(i.startswith(f"plan_{plan['id']}_") for i in ids)
    assert record["course"]["lessons"][0]["id"] == ids[0]


def test_create_plan_unknown_goal_gives_suggestions():
    with pytest.raises(PlanError) as err:
        create_plan("underwater basket weaving", use_qwen=False)
    assert err.value.suggestions


def test_create_plan_with_qwen_custom_opening_is_engine_screened(monkeypatch):
    qwen = QwenPlan(
        title="Vienna fan", summary="Custom", topic_ids=["forks"], reasons={"forks": "because"},
        new_openings=[
            {"name": "Vienna Game", "side": "white", "moves": "1.e4 e5 2.Nc3 Nf6 3.f4 d5 4.fxe5 Nxe4", "reason": "asked"},
            {"name": "Nonsense Attack", "side": "white", "moves": "e4 e5 Ke3 Kd6", "reason": "bad"},
        ],
    )
    monkeypatch.setattr(planner_mod, "ask_qwen", lambda goal, catalog: qwen)
    record = create_plan("I want to learn the Vienna Game", engine=ScriptedEngine())
    plan = record["plan"]
    assert plan["planner"] == "qwen" and plan["title"] == "Vienna fan"
    titles = [u["title"] for u in plan["units"]]
    assert titles == ["Forks", "Vienna Game"]
    assert plan["units"][0]["reason"] == "because"
    assert plan["units"][1]["verified_by"].startswith("Stockfish")
    assert [s["title"] for s in plan["skipped"]] == ["Nonsense Attack"]


# ---- storage ---------------------------------------------------------------

def test_save_and_reload_plans(tmp_path):
    record = create_plan("forks", use_qwen=False)
    save_record(record, tmp_path)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    library = LessonLibrary()
    assert load_saved_plans(library, tmp_path) == 1
    course = library.course(record["course"]["id"])
    assert course.kind == "plan"
    assert library.lesson(record["lessons"][0]["id"]).course_id == course.id


def test_register_course_cannot_replace_curated_course():
    library = LessonLibrary()
    record = create_plan("pins", use_qwen=False)
    record["course"]["id"] = "italian_game"
    with pytest.raises(LessonError):
        register_record(library, record)
