"""Puzzle Library: puzzle metadata from verified entries, per-puzzle stats, and the
"most useful now" selection (relevance, level, novelty/spaced retry, variety, easy -> hard)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from app.engine import EngineUnavailable, set_engine
from app.knowledge.generation import GenerationResult
from app.knowledge.library import get_knowledge, set_knowledge
from app.knowledge.usage import MAX_PUZZLE_SECONDS, UsageTracker, set_usage
from app.learner import LearnerProfile
from app.puzzles import PuzzleLibrary, from_example, get_puzzles, select
from app.puzzles.model import puzzle_type, uniqueness_of
from app.puzzles.personal import library_selection
from app.puzzles.select import MAX_GAP, novelty, slots, targets
from app.session import SessionManager, set_manager
from tests.games_helpers import fork_engine, fork_game
from tests.knowledge_helpers import FORK_FEN, fork_record as _fork_record, make_library
from tests.session_helpers import confirm_advance, confirm_reveal
from tests.test_api import _fresh_state, client as api_client  # noqa: F401  (fixtures)

LIB = get_knowledge()
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def fork_record(**over) -> dict:
    return _fork_record(**{"status": "verified", **over})


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class FakeUsage:
    def __init__(self, stats: dict[str, dict] | None = None):
        self.stats = stats or {}

    def puzzle_stats(self, pid):
        return self.stats.get(pid, {})


@pytest.fixture(scope="module")
def puzzles():
    return PuzzleLibrary(LIB).all()


# ------------------------------------------------------------------ the puzzle model
def test_a_puzzle_is_the_position_at_the_key_move(tmp_path):
    lib = make_library(tmp_path, {"tactics/t": [fork_record()]})
    p = from_example(lib.get("test_knight_fork"), lib)
    assert p.fen == FORK_FEN and p.side_to_move == "white"
    assert p.solution == ("Nc7+", "Kd7", "Nxa8") and p.learner_moves == 2 and p.accepted_first == ("Nc7+",)
    assert p.type == "tactic" and p.concept == "knight_fork" and p.verification_state == "verified"
    assert p.source["license"] == "CC0-1.0" and p.tier == "global"
    assert p.uniqueness == "unchecked"  # no engine record in the test entry


def test_unverified_or_keyless_entries_are_not_puzzles(tmp_path):
    lib = make_library(tmp_path, {"tactics/t": [fork_record(), fork_record(id="nokey", key_move=None)]})
    assert lib.get("nokey") is not None and from_example(lib.get("nokey"), lib) is None
    entry = lib.get("test_knight_fork")
    lib.entries["test_knight_fork"] = type(entry)(**{**entry.__dict__, "status": "candidate"})
    assert from_example(lib.entries["test_knight_fork"], lib) is None


@pytest.mark.parametrize("category, concept, mistake, moves, kind", [
    ("tactics", "knight_fork", False, 1, "tactic"), ("tactics", "knight_fork", False, 3, "calculation"),
    ("checkmates", "smothered_mate", True, 2, "checkmate"),  # opponent's blunder punished: still a mate
    ("mistakes", "walked_into_fork", True, 1, "mistake_correction"), ("mistakes", "hung_piece", True, 1, "defense"),
    ("endgames", "endgames", False, 4, "endgame"), ("openings", "italian_game", False, 1, "opening"),
])
def test_puzzle_types(category, concept, mistake, moves, kind):
    assert puzzle_type(category, concept, mistake, moves) == kind


def test_uniqueness_comes_from_stockfish_alternatives():
    alt = {"move": "Qh5", "loss_cp": 10}
    assert uniqueness_of({"engine": {"key_move": {"alternatives": []}, "learner_moves": []}}, []) == "unique"
    assert uniqueness_of({"engine": {"key_move": {"alternatives": [alt]}}}, []) == "multiple"
    assert uniqueness_of({"engine": {"key_move": {}, "learner_moves": [{"alternatives": [alt]}]}}, []) == "multiple"
    assert uniqueness_of({"engine": {"key_move": {}}}, ["Nf7"]) == "multiple"
    assert uniqueness_of({}, []) == "unchecked"


def test_every_library_puzzle_is_playable(puzzles):
    assert len(puzzles) > 300
    for p in puzzles:
        board = chess.Board(p.fen)
        assert ("white" if board.turn else "black") == p.side_to_move
        for san in p.accepted_first:
            board.parse_san(san)  # raises if illegal
        for san in p.solution:
            board.push_san(san)
        assert p.verification_state == "verified" and p.rating > 0 and p.type in (
            "tactic", "checkmate", "calculation", "defense", "endgame", "opening", "mistake_correction")
    summary = PuzzleLibrary(LIB).summary()
    assert summary["total"] == len(puzzles) and summary["by_uniqueness"].get("unique", 0) > 200


def test_the_index_follows_library_changes(tmp_path):
    lib = make_library(tmp_path, {"tactics/t": [fork_record()]})
    index = PuzzleLibrary(lib)
    assert [p.id for p in index.all()] == ["test_knight_fork"]
    entry = lib.entries["test_knight_fork"]
    lib.entries["test_knight_fork"] = type(entry)(**{**entry.__dict__, "status": "rejected"})
    assert index.all() == []
    assert get_puzzles(lib) is get_puzzles(lib)


# ------------------------------------------------------------------ per-puzzle stats
def test_usage_records_whole_puzzles_with_time(tmp_path):
    u = UsageTracker(tmp_path / "usage.json")
    assert u.puzzle_stats("x")["seen"] is False and u.puzzle_stats("x")["attempts"] == 0
    u.record_resolved("x", solved=True, first_try=True, hints=0, seconds=20)
    u.record_resolved("x", solved=False, first_try=False, hints=2, seconds=5000, revealed=True)
    st = UsageTracker(tmp_path / "usage.json").puzzle_stats("x")  # persisted
    assert st["seen"] and st["attempts"] == 2 and st["success_rate"] == 0.5 and st["first_try_rate"] == 0.5
    assert st["hints_used"] == 2 and st["last_result"] == "revealed"
    assert st["average_seconds"] == (20 + MAX_PUZZLE_SECONDS) / 2  # an idle tab is capped
    u.record_attempt("x", True)  # per-move attempts stay separate
    assert u.puzzle_stats("x")["attempts"] == 2


def test_a_lesson_records_the_puzzle_outcome_and_time(api_client, tmp_path):  # noqa: F811
    set_usage(UsageTracker(tmp_path / "usage.json"))
    try:
        body = api_client.post("/api/plans", json={"goal": "checkmate patterns", "library": True}).json()
        start = api_client.post(f"/api/lessons/{body['first_lesson_id']}/start").json()
        sid, step = start["session_id"], start["step"]
        while step["type"] != "exercise":
            step = confirm_advance(api_client, sid)["step"]
        while True:  # reveal every move of the first example
            confirm_reveal(api_client, sid)
            nxt = confirm_advance(api_client, sid)
            if nxt["completed"] or nxt["step"]["type"] != "exercise" or nxt["step"].get("example") != step.get("example"):
                break
        from app.knowledge.usage import get_usage
        resolved = {k: v for k, v in get_usage().all_puzzle_stats().items() if v["attempts"]}
        assert len(resolved) == 1
        (st,) = resolved.values()
        assert st["last_result"] == "revealed" and st["average_seconds"] is not None and st["average_seconds"] >= 0
    finally:
        set_usage(None)


# ------------------------------------------------------------------ selection
def test_targets_follow_the_concept_tree_and_generator_plans():
    t = targets("walked_into_fork", LIB)
    assert t["walked_into_fork"][1] == "exact" and t["knight_fork"] == (0.95, "trains")
    assert targets("fork", LIB)["knight_fork"][1] == "exact"  # sub-concepts count as the concept
    assert targets("hung_piece", LIB)["hanging_queen"][1] == "exact"


def test_selection_is_relevant_at_level_and_easy_to_hard(puzzles):
    profile = LearnerProfile()
    sel = select(puzzles, LIB, "fork", count=5, profile=profile, usage=FakeUsage(), now=NOW)
    assert len(sel.chosen) == 5 and sel.shortfall == 0
    ratings = [c.puzzle.rating for c in sel.chosen]
    assert ratings == sorted(ratings)
    wanted = targets("fork", LIB)
    for c in sel.chosen:
        assert c.puzzle.concept in wanted or set(c.puzzle.concepts) & set(wanted)
        assert abs(c.puzzle.rating - c.slot_rating) <= MAX_GAP and c.reasons and c.reasons[0]
        assert "New to you" in c.reasons
    assert len({c.puzzle.fen.split()[0] for c in sel.chosen}) == 5


def test_stronger_learners_get_harder_sets(puzzles):
    weak = LearnerProfile()
    strong = LearnerProfile()
    strong.set_onboarding(rating=2000, platform="chesscom")
    easy = select(puzzles, LIB, "checkmate", count=5, profile=weak, usage=FakeUsage(), now=NOW)
    hard = select(puzzles, LIB, "checkmate", count=5, profile=strong, usage=FakeUsage(), now=NOW)
    mean = lambda s: sum(c.puzzle.rating for c in s.chosen) / len(s.chosen)  # noqa: E731
    assert hard.target_rating > easy.target_rating + 300 and mean(hard) > mean(easy) + 200


def test_recent_puzzles_are_not_repeated_and_misses_come_back(puzzles):
    first = select(puzzles, LIB, "fork", count=5, profile=LearnerProfile(), usage=FakeUsage(), now=NOW)
    ids = [c.puzzle.id for c in first.chosen]
    # the retry is the easiest one: two misses lower the level, so it still fits a slot
    stats = {ids[3]: {"seen": True, "last_result": "solved_first_try", "last_resolved": ago(1)},
             ids[1]: {"seen": True, "last_result": None, "last_used": ago(0.2)},
             ids[2]: {"seen": True, "last_result": "failed", "last_resolved": ago(0.5)},
             ids[0]: {"seen": True, "last_result": "failed", "last_resolved": ago(4)}}
    again = select(puzzles, LIB, "fork", count=5, profile=LearnerProfile(), usage=FakeUsage(stats), now=NOW)
    chosen = {c.puzzle.id: c for c in again.chosen}
    assert not {ids[3], ids[1], ids[2]} & set(chosen)  # solved/shown/missed too recently
    assert ids[0] in chosen and any(r.startswith("Retry: you missed this 4 days ago") for r in chosen[ids[0]].reasons)


def test_novelty_rules():
    assert novelty({}, NOW)[0] == 1.0
    assert novelty({"seen": True, "last_result": "solved", "last_resolved": ago(2)}, NOW)[0] == 0
    assert 0 < novelty({"seen": True, "last_result": "solved", "last_resolved": ago(7)}, NOW)[0] < 0.5
    assert novelty({"seen": True, "last_result": "solved", "last_resolved": ago(30)}, NOW)[1].startswith("Review")


def test_recent_results_nudge_the_level(puzzles):
    base = select(puzzles, LIB, "fork", count=5, profile=LearnerProfile(), usage=FakeUsage(), now=NOW)
    pool = [p for p in puzzles if p.concept in targets("fork", LIB)]
    missed = {p.id: {"seen": True, "last_result": "revealed", "last_resolved": ago(0.1)} for p in pool[:4]}
    clean = {p.id: {"seen": True, "last_result": "solved_first_try", "last_resolved": ago(0.1),
                    "average_seconds": 15} for p in pool[:4]}
    easier = select(puzzles, LIB, "fork", count=5, profile=LearnerProfile(), usage=FakeUsage(missed), now=NOW)
    harder = select(puzzles, LIB, "fork", count=5, profile=LearnerProfile(), usage=FakeUsage(clean), now=NOW)
    assert easier.target_rating == base.target_rating - 100 and "missed" in easier.level_note
    assert harder.target_rating == base.target_rating + 80 and "cleanly" in harder.level_note


def test_slots_span_easy_to_hard():
    assert slots(1000, 1) == [1000]
    assert slots(1000, 5) == [850, 925, 1000, 1075, 1150]


def test_nothing_relevant_means_a_shortfall_not_filler(puzzles):
    sel = select(puzzles, LIB, "spotting_threats", count=5, profile=LearnerProfile(), usage=FakeUsage(), now=NOW)
    wanted = targets("spotting_threats", LIB)
    assert all(c.puzzle.concept in wanted or set(c.puzzle.concepts) & set(wanted) for c in sel.chosen)
    assert sel.shortfall == 5 - len(sel.chosen)


def test_library_selection_never_hands_back_the_learners_own_position(tmp_path):
    lib = make_library(tmp_path, {"tactics/t": [fork_record()]})
    weakness = {"key": "knight_fork", "concept": "knight_fork", "title": "Missed knight forks",
                "evidence": [{"fen": FORK_FEN}]}
    assert library_selection(weakness, lib, 3).chosen == []
    assert [c.puzzle.id for c in library_selection({**weakness, "evidence": []}, lib, 3).chosen] == ["test_knight_fork"]
    assert library_selection({**weakness, "concept": None}, lib, 3) is None


# ------------------------------------------------------------------ the API
def ndjson(res):
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    # test_knight_fork is the very position the learner's analyzed games are played from
    set_knowledge(make_library(tmp_path, {"tactics/t": [
        fork_record(), fork_record(id="test_knight_fork_2", start_fen="q3k3/8/8/1N6/8/8/8/5K2 w - - 0 1"),
        fork_record(id="test_knight_fork_3", start_fen="q3k3/8/8/1N6/8/8/8/6K1 w - - 0 1")]}))
    set_usage(UsageTracker(tmp_path / "usage.json"))
    set_engine(fork_engine())
    set_manager(SessionManager())
    from app.main import app
    with TestClient(app) as c:
        yield c
    set_engine(None)
    set_manager(None)
    set_usage(None)
    set_knowledge(None)


def _analyzed(client):
    pgn = fork_game(game_no=600000001, black="alice") + "\n" + fork_game(game_no=600000002, black="bob")
    assert client.post("/api/games/import", json={"pgn": pgn, "username": "RaanTest"}).status_code == 200
    ndjson(client.post("/api/games/analyze", json={}))


def test_training_uses_library_puzzles_first(client, monkeypatch):
    import app.analysis.personal_puzzles as pp
    monkeypatch.setattr(pp, "generate_for", lambda *a, **k: GenerationResult("knight_fork", attempts=3))
    _analyzed(client)
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 2}))
    done = events[-1]
    assert done["type"] == "done", events
    assert done["library"] == 2 and done["new"] == 0 and "puzzle" not in [e["type"] for e in events]
    plan = done["plan"]
    assert {p["id"] for p in plan["puzzles"]} == {"test_knight_fork_2", "test_knight_fork_3"}  # never their own
    assert all(p["origin"] == "library" and p["reasons"] for p in plan["puzzles"])
    debug = plan["debug"]
    assert debug["kind"] == "puzzles" and debug["user_weakness"]["key"] == "knight_fork"
    assert debug["user_weakness"]["of"] == 2 and debug["source"]
    assert debug["library_match"]["chosen"] == 2 and debug["custom_generation"]["status"] == "not needed"
    assert [v["status"] for v in debug["puzzle_validation"]] == ["verified", "verified"]
    lesson = client.post(f"/api/lessons/{done['first_lesson_id']}/start").json()
    assert "I found this in your games" in lesson["step"]["text"]
    # they were shown: the next request doesn't repeat them (nothing else fits -> generation,
    # which here builds nothing -> nothing is shown rather than a repeat)
    again = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 2}))
    assert again[-1]["type"] == "error" and "passed every check" in again[-1]["error"]


def test_only_the_shortfall_is_generated(client, monkeypatch):
    _analyzed(client)
    import app.analysis.personal_puzzles as pp
    asked = {}

    def fake_generate(weakness, library, engine, count=3, **kw):
        asked["count"] = count
        return GenerationResult("knight_fork", attempts=7, stopped="time budget")

    monkeypatch.setattr(pp, "generate_for", fake_generate)
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 4}))
    done = events[-1]
    assert asked["count"] == 2 and done["type"] == "done" and done["library"] == 2
    gen = done["plan"]["debug"]["custom_generation"]
    assert gen["needed"] == 2 and gen["generated"] == 0 and gen["attempts"] == 7 and gen["status"] == "ran"


def test_library_puzzles_still_work_without_the_engine(client, monkeypatch):
    _analyzed(client)
    import app.game_api as game_api

    def no_engine():
        raise EngineUnavailable("gone")

    monkeypatch.setattr(game_api, "get_engine", no_engine)
    done = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 3}))[-1]
    assert done["type"] == "done" and done["library"] == 2
    assert done["plan"]["debug"]["custom_generation"]["status"] == "engine unavailable"


def test_puzzle_api(client):
    summary = client.get("/api/puzzles").json()
    assert summary["total"] == 3 and summary["by_type"] == {"tactic": 3}
    one = client.get("/api/puzzles/test_knight_fork").json()
    assert one["fen"] == FORK_FEN and one["stats"]["seen"] is False
    assert client.get("/api/puzzles/nope").status_code == 404
    sel = client.get("/api/puzzles/select", params={"concept": "fork", "count": 2}).json()
    assert len(sel["puzzles"]) == 2 and sel["shortfall"] == 0 and sel["puzzles"][0]["reasons"]
    assert client.get("/api/puzzles/select", params={"concept": "nope"}).status_code == 404


def test_fresh_skips_the_library(client, monkeypatch):
    _analyzed(client)
    import app.analysis.personal_puzzles as pp
    monkeypatch.setattr(pp, "generate_for", lambda *a, **k: GenerationResult("knight_fork", attempts=3))
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 2, "fresh": True}))
    assert events[-1]["type"] == "error"  # only generated positions, and none passed


def test_history_analysis_stores_skill_evidence_from_all_games(client):
    """learner.difficulty reads profile.game_skill: computed over every analyzed game, not one."""
    from app.learner import get_profile
    _analyzed(client)
    gs = get_profile().game_skill
    assert gs["games"] == 2 and gs["moves"] > 0 and gs["errors"] >= 2   # both games' missed forks
    assert set(gs["phases"]) == {"opening", "middlegame", "endgame"}
    assert "items" in gs["chances"]
    assert get_profile().as_dict()["game_skill"] == gs                       # persisted with the profile
