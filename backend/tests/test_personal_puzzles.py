"""Recurring weakness in the learner's games → new, engine-verified puzzles for that skill.

New positions (never the learner's own), saved in the personal tier with the weakness and
games that triggered them, never mixed into the shared library, and not repeated.
"""
from __future__ import annotations

import json

import chess
import pytest
from fastapi.testclient import TestClient

from app.analysis import personal_puzzles
from app.analysis.training import TrainingError, create_puzzle_plan, create_training_plan
from app.engine import EngineUnavailable, set_engine
from app.knowledge.generation import GenerationResult
from app.knowledge.generation.log import GenerationLog
from app.knowledge.library import set_knowledge
from app.lessons.schema import parse_lesson
from app.session import SessionManager, set_manager
from tests.games_helpers import fork_engine, fork_game
from tests.knowledge_helpers import make_library

WEAKNESS = {"key": "knight_fork", "concept": "knight_fork", "title": "Missed knight forks", "game_count": 2,
            "games": ["chesscom-600000001", "chesscom-600000002"], "description": "You missed knight forks.",
            "evidence": [{"game_id": "chesscom-600000001", "moment_id": "m1", "ply": 40, "move_number": 21,
                          "side": "white", "san": "Kf2", "severity": "blunder",
                          "fen": "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"}]}


def ndjson(res):
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


# ------------------------------------------------------------------ mapping and metadata
@pytest.mark.parametrize("concept, target", [
    ("walked_into_fork", "walked_into_fork"), ("hung_piece", "hung_piece"), ("missed_threat", "missed_threat"),
    ("checkmate", "checkmate"), ("knight_fork", "knight_fork"), ("endgames", "endgames"),
    ("early_queen", None), ("king_safety_mistake", None), (None, None),
])
def test_which_weaknesses_get_generated_puzzles(concept, target):
    assert personal_puzzles.target_concept({"key": "k", "concept": concept}) == target


def test_personal_metadata_links_back_to_the_evidence():
    meta = personal_puzzles.personal_meta(WEAKNESS, username="RaanTest")
    assert meta["target_weakness"] == "knight_fork" and meta["username"] == "RaanTest"
    assert meta["evidence"][0] == {"game_id": "chesscom-600000001", "moment_id": "m1", "ply": 40,
                                   "move": "21.Kf2", "severity": "blunder"}
    assert meta["games"] == WEAKNESS["games"]


def test_puzzle_plan_needs_puzzles(tmp_path):
    with pytest.raises(TrainingError):
        create_puzzle_plan(WEAKNESS, [], 10, make_library(tmp_path))


# ------------------------------------------------------------------ real engine
@pytest.fixture(scope="module")
def engine():
    try:
        from app.engine.service import UciEngine
        eng = UciEngine()
    except (EngineUnavailable, Exception):
        pytest.skip("no engine available")
    yield eng
    eng.close()


def test_generated_puzzles_are_personal_new_and_verified(tmp_path, engine):
    library = make_library(tmp_path)
    log = GenerationLog(tmp_path / "log.json")
    from app.knowledge.generation import generate
    result = generate("knight_fork", library, engine, count=2, tier="personal", seed=1, use_qwen=False,
                      personal=personal_puzzles.personal_meta(WEAKNESS), gen_log=log,
                      avoid_positions={chess.Board(WEAKNESS["evidence"][0]["fen"]).board_fen()})
    assert len(result.accepted) == 2, result.rejected
    own = chess.Board(WEAKNESS["evidence"][0]["fen"]).board_fen()
    for ex in result.accepted:
        assert ex.tier == "personal" and ex.status == "verified"
        assert chess.Board(ex.start_fen).board_fen() != own  # not a copy of the learner's position
        assert ex.source["personal"]["target_weakness"] == "knight_fork"
        assert ex not in library.examples_for("knight_fork")  # separate from the shared library
    # unseen_for: only this weakness, only verified, only not-yet-shown
    assert {e.id for e in personal_puzzles.unseen_for(WEAKNESS, library, shown={})} == \
        {e.id for e in result.accepted}
    first = result.accepted[0].id
    assert first not in {e.id for e in personal_puzzles.unseen_for(WEAKNESS, library, shown={first: "now"})}
    other = {**WEAKNESS, "key": "pawn_fork"}
    assert personal_puzzles.unseen_for(other, library, shown={}) == []

    plan = create_puzzle_plan(WEAKNESS, result.accepted, 10, library)
    lesson = plan["lessons"][0]
    parse_lesson(lesson, course_id="t")
    assert lesson["personal"] is True and lesson["origin"]["generated"] == [e.id for e in result.accepted]
    assert "not from your games" in lesson["steps"][0]["text"]
    assert plan["plan"]["units"][0]["generated"] is True

    # the training plan for the weakness picks up the unseen generated puzzles
    record = create_training_plan([WEAKNESS], {}, 10, library, generated={"knight_fork": result.accepted})
    titles = [les["title"] for les in record["lessons"]]
    assert "Knight fork: new puzzles for you" in titles


# ------------------------------------------------------------------ the API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_knowledge(make_library(tmp_path))
    set_engine(fork_engine())
    set_manager(SessionManager())
    from app.main import app
    with TestClient(app) as c:
        yield c
        set_engine(None)  # app shutdown closes the global engine; the module's real one must survive
    set_manager(None)
    set_knowledge(None)


def _analyzed(client):
    pgn = fork_game(game_no=600000001, black="alice") + "\n" + fork_game(game_no=600000002, black="bob")
    assert client.post("/api/games/import", json={"pgn": pgn, "username": "RaanTest"}).status_code == 200
    events = ndjson(client.post("/api/games/analyze", json={}))
    assert "knight_fork" in [w["key"] for w in events[-1]["weaknesses"]["weaknesses"]]


def test_api_streams_new_puzzles_for_a_weakness(client, engine, tmp_path):
    _analyzed(client)
    set_engine(engine)  # analysis with the scripted engine; generation with real Stockfish
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 2}))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and kinds[-1] == "done", events
    done = events[-1]
    assert done["new"] == 2 and done["reused"] == 0 and kinds.count("puzzle") == 2
    ids = done["plan"]["units"][0]["example_ids"]
    for eid in ids:
        stored = json.loads((tmp_path / "runtime" / "personal" / f"{eid}.json").read_text())
        assert stored["status"] == "verified" and stored["source"]["personal"]["target_weakness"] == "knight_fork"
        assert set(stored["source"]["personal"]["games"]) == {"chesscom-600000001", "chesscom-600000002"}
    start = client.post(f"/api/lessons/{done['first_lesson_id']}/start")
    assert start.status_code == 200, start.text
    # shown puzzles are remembered: the next request makes different ones
    again = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 1}))
    assert again[-1]["type"] == "done", again
    assert not set(again[-1]["plan"]["units"][0]["example_ids"]) & set(ids)


def test_training_plan_includes_unseen_generated_puzzles_once(client, engine):
    _analyzed(client)
    from app.knowledge.library import get_knowledge
    weakness = {**WEAKNESS, "games": ["chesscom-600000001", "chesscom-600000002"]}
    made = personal_puzzles.generate_for(weakness, get_knowledge(), engine, count=1, seed=4, use_qwen=False)
    assert made.accepted
    res = client.post("/api/games/training", json={"keys": ["knight_fork"]}).json()
    unit_lessons = res["plan"]["units"][0]["lesson_ids"]
    assert any(lid.endswith("g") for lid in unit_lessons)  # the generated-puzzles lesson
    res2 = client.post("/api/games/training", json={"keys": ["knight_fork"]}).json()
    assert not any(lid.endswith("g") for lid in res2["plan"]["units"][0]["lesson_ids"])  # shown once


def test_api_refuses_what_it_cannot_generate(client, monkeypatch):
    _analyzed(client)
    monkeypatch.setattr(personal_puzzles, "supported_weakness", lambda w: False)
    res = client.post("/api/games/puzzles", json={"key": "knight_fork"})
    assert res.status_code == 422 and "can't build new puzzles" in res.json()["error"]


def test_api_needs_the_engine_to_verify(client, monkeypatch):
    _analyzed(client)
    import app.game_api as game_api

    def no_engine():
        raise EngineUnavailable("gone")

    monkeypatch.setattr(game_api, "get_engine", no_engine)
    res = client.post("/api/games/puzzles", json={"key": "knight_fork"})
    assert res.status_code == 503 and "checked by Stockfish" in res.json()["error"]


def test_nothing_verified_means_nothing_shown(client, monkeypatch):
    _analyzed(client)
    monkeypatch.setattr(personal_puzzles, "generate_for",
                        lambda *a, **k: GenerationResult("knight_fork", attempts=40, stopped="time budget"))
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork"}))
    assert events[-1]["type"] == "error" and "passed every check" in events[-1]["error"]


def test_generation_crash_is_reported_not_raised(client, monkeypatch):
    _analyzed(client)

    def boom(*a, **k):
        raise RuntimeError("engine died")

    monkeypatch.setattr(personal_puzzles, "generate_for", boom)
    events = ndjson(client.post("/api/games/puzzles", json={"key": "knight_fork"}))
    assert any("failed" in e.get("text", "") for e in events) and events[-1]["type"] == "error"


def test_unknown_weakness_and_no_games(client):
    assert client.post("/api/games/puzzles", json={"key": "knight_fork"}).status_code == 422  # nothing analyzed
    _analyzed(client)
    assert client.post("/api/games/puzzles", json={"key": "smothered_mate"}).status_code == 422
    assert client.post("/api/games/puzzles", json={"key": "knight_fork", "count": 50}).status_code == 422
