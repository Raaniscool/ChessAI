"""Recurring weaknesses, library retrieval, personalized training plans and privacy."""
from __future__ import annotations

import copy
import json

import chess
import pytest

from app.analysis import recurring_weaknesses
from app.analysis.training import TrainingError, create_training_plan
from app.knowledge.library import get_knowledge
from app.knowledge.schema import KnowledgeError, parse_example
from app.lessons.schema import parse_lesson
from tests.games_helpers import (BACK_RANK_DEFENCE_FEN, FORK_FEN, analyze, chesscom_pgn, fork_engine, fork_game,
                                 import_one, ScriptedEngine)


@pytest.fixture(scope="module")
def fork_analyses():
    """Two different games, each with a missed knight fork; one also has an allowed back-rank mate."""
    a = analyze(import_one(fork_game(game_no=500000001, black="alice")), fork_engine())
    b = analyze(import_one(fork_game(game_no=500000002, black="bob")), fork_engine())
    mate = analyze(import_one(chesscom_pgn(["Ra4", "Rd8#"], fen=BACK_RANK_DEFENCE_FEN, white="carol",
                                           black="RaanTest", result="1-0", game_no=500000003)), ScriptedEngine())
    return [a, b, mate]


def moments_of(analyses):
    return {m["id"]: m for a in analyses for m in a["moments"]}


# --- recurring weaknesses ------------------------------------------------------------------
def test_a_weakness_needs_evidence_from_two_different_games(fork_analyses):
    one = recurring_weaknesses(fork_analyses[:1], get_knowledge())
    assert one["weaknesses"] == [] and [w["key"] for w in one["seen_once"]] == ["knight_fork"]
    two = recurring_weaknesses(fork_analyses[:2], get_knowledge())
    assert [w["key"] for w in two["weaknesses"]] == ["knight_fork"]


def test_repeating_a_mistake_inside_one_game_is_still_not_a_weakness(fork_analyses):
    analysis = copy.deepcopy(fork_analyses[0])
    twin = copy.deepcopy(analysis["moments"][0])
    twin.update(id=twin["id"].replace(":0", ":40"), ply=40, move_number=21)
    analysis["moments"].append(twin)
    result = recurring_weaknesses([analysis], get_knowledge())
    assert result["weaknesses"] == []
    assert result["seen_once"][0]["occurrences"] == 2 and result["seen_once"][0]["game_count"] == 1


def test_weakness_records_games_moves_positions_severity_and_frequency(fork_analyses):
    result = recurring_weaknesses(fork_analyses, get_knowledge())
    assert result["total_games"] == 3 and result["min_games"] == 2
    w = next(x for x in result["weaknesses"] if x["key"] == "knight_fork")
    assert w["title"] == "Missed knight fork" and w["concept"] == "knight_fork"
    assert w["games"] == ["chesscom-500000001", "chesscom-500000002"] and w["game_count"] == 2
    assert w["occurrences"] == 2 and w["frequency"] == pytest.approx(2 / 3, abs=0.01)
    assert w["severity_score"] == 6  # two blunders
    assert "2 of your 3 games" in w["description"]
    ev = w["evidence"][0]
    assert ev["moment_id"] == "chesscom-500000001:0" and ev["move_number"] == 1
    assert ev["fen"] == FORK_FEN and ev["san"] == "Kd2" and ev["severity"] == "blunder"
    assert {e["opponent"] for e in w["evidence"]} == {"alice", "bob"}


def test_weakness_links_to_verified_library_examples(fork_analyses):
    library = get_knowledge()
    w = recurring_weaknesses(fork_analyses, library)["weaknesses"][0]
    assert w["library_examples"] == len([e for e in library.examples_for("knight_fork")]) > 0
    assert "forks" in w["topics"]


def test_back_rank_mate_seen_in_one_game_is_not_recurring(fork_analyses):
    result = recurring_weaknesses(fork_analyses, get_knowledge())
    assert "back_rank_mate" not in [w["key"] for w in result["weaknesses"]]
    assert "back_rank_mate" in [w["key"] for w in result["seen_once"]]


# --- training plans ------------------------------------------------------------------------
def make_plan(fork_analyses, keys=("knight_fork",), targets=None):
    library = get_knowledge()
    found = recurring_weaknesses(fork_analyses, library)
    chosen = [w for w in found["weaknesses"] + found["seen_once"] if w["key"] in keys]
    return create_training_plan(chosen, moments_of(fork_analyses), found["total_games"], library,
                                record_usage=False, targets=targets)


def test_training_from_games_is_pitched_at_the_learners_rating(fork_analyses):
    """The same weakness in the games of a 700 and a 1900 player → different library positions,
    the stronger player's harder on average."""
    from app.knowledge.difficulty import puzzle_rating
    library = get_knowledge()
    concept = next(w for w in recurring_weaknesses(fork_analyses, library)["weaknesses"]
                   if w["key"] == "knight_fork")["concept"]

    def ratings(target):
        record = make_plan(fork_analyses, targets={concept: target})
        used = [s["example"] for s in record["lessons"][0]["steps"] if s.get("example")]
        return [puzzle_rating(library.entries[e]) for e in dict.fromkeys(used)]

    low, high = ratings(700), ratings(1900)
    assert low and high and low != high
    assert sum(high) / len(high) > sum(low) / len(low)


def test_training_plan_follows_the_teaching_sequence(fork_analyses):
    record = make_plan(fork_analyses)
    ids = [lesson["id"] for lesson in record["lessons"]]
    assert ids[0].endswith("01a") and ids[1].endswith("01b")  # library pattern, then your own games
    assert record["plan"]["planner"] == "games"
    assert record["plan"]["personal"]["weaknesses"] == ["knight_fork"]
    assert "2 of the 3 games" in record["lessons"][0]["steps"][0]["text"]
    for lesson in record["lessons"]:
        parse_lesson(lesson, course_id=record["course"]["id"])  # every lesson is a normal, valid lesson


def test_library_lessons_use_only_verified_shared_examples(fork_analyses):
    library = get_knowledge()
    record = make_plan(fork_analyses)
    used = {s["example"] for lesson in record["lessons"] for s in lesson["steps"] if s.get("example")}
    assert used
    for eid in used:
        example = library.entries[eid]
        assert example.status == "verified" and example.tier == "global"


def test_own_positions_are_exercises_checked_by_stockfish(fork_analyses):
    record = make_plan(fork_analyses)
    own = [lesson for lesson in record["lessons"] if lesson["id"].endswith("01b")][0]
    assert own["personal"] is True
    exercises = [s for s in own["steps"] if s["type"] == "exercise"]
    assert len(exercises) == 2  # one per game
    for ex in exercises:
        assert ex["fen"] == FORK_FEN and ex["accepted"][0] == "Nc7+"
        board = chess.Board(ex["fen"])
        assert all(board.parse_san(san) in board.legal_moves for san in ex["accepted"])
        assert "Kd2" in ex["prompt"]


def test_training_needs_a_weakness():
    with pytest.raises(TrainingError):
        create_training_plan([], {}, 0, get_knowledge(), record_usage=False)


# --- privacy: learner positions never enter the shared library -------------------------------
def test_user_game_entries_are_refused_outside_the_personal_tier():
    library = get_knowledge()
    raw = json.loads(json.dumps(library.verified()[0].to_record()))
    raw["id"] = "from_my_game"
    raw["source"] = {"source_type": "user_game", "reference": "chesscom-1"}
    for tier in ("global", "generated"):
        with pytest.raises(KnowledgeError, match="personal tier"):
            parse_example(raw, library.concepts, tier=tier)
    assert parse_example(raw, library.concepts, tier="personal").tier == "personal"


def test_planning_from_games_leaves_the_library_untouched(fork_analyses, tmp_path):
    from app.config import get_settings
    library = get_knowledge()
    before = set(library.entries)
    knowledge_runtime = get_settings().data_dir / "knowledge"
    runtime_before = sorted(p.name for p in knowledge_runtime.rglob("*.json")) if knowledge_runtime.exists() else []
    make_plan(fork_analyses)
    assert set(library.entries) == before
    after = sorted(p.name for p in knowledge_runtime.rglob("*.json")) if knowledge_runtime.exists() else []
    assert after == runtime_before
    assert not any(FORK_FEN.split()[0] in e.start_fen for e in library.entries.values())
