"""The /api/games endpoints, end to end, with a scripted engine (and optionally real Stockfish)."""
from __future__ import annotations
from tests.session_helpers import confirm_advance

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.engine import EngineUnavailable, set_engine
from app.session import SessionManager, set_manager
from tests.games_helpers import (FOOLS_MATE, FORK_ENDGAME_1, FORK_ENDGAME_2, FORK_FEN, TRAP_GAME, chesscom_pgn,
                                 fork_engine, fork_game)


def ndjson(res):
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)  # fresh game store per test
    set_engine(fork_engine())
    set_manager(SessionManager())
    from app.main import app
    with TestClient(app) as c:
        yield c
    set_engine(None)
    set_manager(None)


TWO_FORKS = fork_game(game_no=600000001, black="alice") + "\n" + fork_game(game_no=600000002, black="bob")


def import_and_analyze(client, pgn=TWO_FORKS, username="RaanTest"):
    res = client.post("/api/games/import", json={"pgn": pgn, "username": username})
    assert res.status_code == 200, res.text
    events = ndjson(client.post("/api/games/analyze", json={}))
    assert events[-1]["type"] == "done", events[-1]
    return res.json(), events


# --- import ----------------------------------------------------------------------------------
def test_import_reports_games_and_per_game_errors(client):
    bad = chesscom_pgn("e4 e5", game_no=7).replace("1. e4 e5", "1. e4 e5 2. Ke3")
    res = client.post("/api/games/import", json={"pgn": TRAP_GAME + "\n" + bad, "username": "raantest"})
    body = res.json()
    assert res.status_code == 200 and body["source"] == "chesscom"
    assert [g["id"] for g in body["imported"]] == ["chesscom-123456789"] and body["new"] == 1
    assert body["imported"][0]["opponent"] == "knightmare42"
    assert body["errors"][0]["index"] == 2 and "Ke3" in body["errors"][0]["error"]


def test_import_malformed_pgn_is_rejected_cleanly(client):
    res = client.post("/api/games/import", json={"pgn": "not a game"})
    assert res.status_code == 422 and "PGN" in res.json()["error"]
    assert client.get("/api/games").json()["games"] == []


def test_import_asks_which_player_when_it_cannot_tell(client):
    res = client.post("/api/games/import", json={"pgn": TRAP_GAME})
    assert res.status_code == 422
    assert res.json()["needs_player"] is True and res.json()["players"] == ["knightmare42", "RaanTest"]


def test_only_chesscom_is_accepted_for_now(client):
    res = client.post("/api/games/import", json={"pgn": TRAP_GAME, "source": "lichess"})
    assert res.status_code == 422 and "lichess" in res.json()["error"].lower()


# --- analyze and review ----------------------------------------------------------------------
def test_analyze_streams_progress_and_finds_the_recurring_weakness(client):
    _, events = import_and_analyze(client)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and "progress" in kinds and kinds.count("game_done") == 2
    done = events[-1]
    assert [w["key"] for w in done["weaknesses"]["weaknesses"]] == ["knight_fork"]
    listing = client.get("/api/games").json()["games"]
    assert {g["id"] for g in listing} == {"chesscom-600000001", "chesscom-600000002"}
    assert all(g["analysis"]["moments"] == 1 for g in listing)


def test_analyzed_games_are_not_reanalyzed_unless_asked(client):
    import_and_analyze(client)
    again = ndjson(client.post("/api/games/analyze", json={}))
    assert not [e for e in again if e["type"] == "game_done"]
    forced = ndjson(client.post("/api/games/analyze", json={"reanalyze": True, "game_ids": ["chesscom-600000001"]}))
    assert [e["game_id"] for e in forced if e["type"] == "game_done"] == ["chesscom-600000001"]


def test_game_detail_has_review_cards_and_evidence(client):
    import_and_analyze(client)
    game = client.get("/api/games/chesscom-600000001").json()
    assert game["game"]["opponent"] == "alice"
    moment = game["analysis"]["moments"][0]
    assert moment["review"]["title"] == "Move 1: Kd2??"
    assert moment["review"]["headline"] == "You missed a knight fork."
    assert moment["fen_before"] == FORK_FEN and moment["best_move"] == "Nc7+"
    assert moment["best_line"][:3] == ["Nc7+", "Kd7", "Nxa8"]


def test_explain_streams_a_verified_explanation(client):
    import_and_analyze(client)
    events = ndjson(client.post("/api/games/chesscom-600000001/moments/0/explain", json={"level": "beginner"}))
    assert events[0]["type"] == "start" and events[-1]["type"] == "done"
    assert "Nc7+" in events[-1]["text"]
    assert client.post("/api/games/chesscom-600000001/moments/5/explain", json={}).status_code == 404
    assert client.post("/api/games/nope-123/moments/0/explain", json={}).status_code == 404


def test_a_new_game_is_compared_with_earlier_ones(client):
    client.post("/api/games/import", json={"pgn": fork_game(game_no=600000001, black="alice"), "username": "RaanTest"})
    first = ndjson(client.post("/api/games/analyze", json={"game_ids": ["chesscom-600000001"]}))[-1]
    assert first["weaknesses"]["weaknesses"] == []
    client.post("/api/games/import", json={"pgn": fork_game(game_no=600000002, black="bob"), "username": "RaanTest"})
    second = ndjson(client.post("/api/games/analyze", json={"game_ids": ["chesscom-600000002"]}))
    assert [e["game_id"] for e in second if e["type"] == "game_done"] == ["chesscom-600000002"]
    assert [w["key"] for w in second[-1]["weaknesses"]["weaknesses"]] == ["knight_fork"]


def test_weaknesses_endpoint_can_be_limited_to_some_games(client):
    import_and_analyze(client)
    both = client.get("/api/games/weaknesses").json()
    assert [w["key"] for w in both["weaknesses"]] == ["knight_fork"]
    one = client.get("/api/games/weaknesses", params={"ids": "chesscom-600000001"}).json()
    assert one["weaknesses"] == [] and one["seen_once"][0]["key"] == "knight_fork"


def test_engine_unavailable_is_reported(client, monkeypatch):
    client.post("/api/games/import", json={"pgn": TWO_FORKS, "username": "RaanTest"})

    def broken():
        raise EngineUnavailable("no engine here")

    monkeypatch.setattr("app.game_api.get_engine", broken)
    res = client.post("/api/games/analyze", json={})
    assert res.status_code == 503 and "engine" in res.json()["error"]


def test_delete_removes_the_learners_game(client):
    import_and_analyze(client)
    assert client.delete("/api/games/chesscom-600000001").status_code == 200
    assert client.get("/api/games/chesscom-600000001").status_code == 404


# --- training ----------------------------------------------------------------------------------
def test_training_plan_becomes_a_playable_course(client):
    import_and_analyze(client)
    res = client.post("/api/games/training", json={"keys": ["knight_fork"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "games" and body["plan"]["planner"] == "games"
    course = next(c for c in client.get("/api/courses").json()["courses"] if c["id"] == body["course_id"])
    lesson_ids = [lesson["id"] for lesson in course["lessons"]]
    own = next(i for i in lesson_ids if i.endswith("01b"))
    # Play the "your own games" lesson with the tutor's engine: the exercise accepts Stockfish's move.
    from tests.test_api import FakeEngine as TutorEngine
    set_engine(TutorEngine())
    step = client.post(f"/api/lessons/{own}/start").json()
    sid = step["session_id"]
    step = step["step"]
    while step["type"] != "exercise":
        step = confirm_advance(client, sid)["step"]
    assert step["board"]["fen"] == FORK_FEN
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": "b5c7"}).json()
    assert res["accepted"] is True


def test_training_rejects_unknown_weaknesses_and_needs_analyses(client):
    assert client.post("/api/games/training", json={"keys": ["knight_fork"]}).status_code == 422
    import_and_analyze(client)
    assert client.post("/api/games/training", json={"keys": ["smothered_mate"]}).status_code == 422


def test_games_stay_out_of_the_knowledge_library(client):
    from app.knowledge.library import get_knowledge
    before = set(get_knowledge().entries)
    import_and_analyze(client)
    client.post("/api/games/training", json={"keys": ["knight_fork"]})
    assert set(get_knowledge().entries) == before
    stats = client.get("/api/knowledge/stats")
    if stats.status_code == 200:
        assert "user_game" not in json.dumps(stats.json().get("by_source", {}))


# --- real Stockfish (skipped when no engine is installed) -----------------------------------------
def _real_engine():
    try:
        from app.engine.service import UciEngine
        return UciEngine()
    except Exception:
        return None


@pytest.mark.skipif(os.environ.get("CHESSAI_SKIP_ENGINE") == "1", reason="engine tests disabled")
def test_real_stockfish_finds_the_recurring_knight_fork(tmp_path, monkeypatch):
    engine = _real_engine()
    if engine is None:
        pytest.skip("Stockfish is not installed")
    from app.analysis import GameAnalyzer, recurring_weaknesses
    from app.games.importers import get_importer
    try:
        games = get_importer().parse(FORK_ENDGAME_1 + "\n" + FORK_ENDGAME_2 + "\n" + FOOLS_MATE,
                                     username="RaanTest").games
        analyses = []
        for game in games:
            for event in GameAnalyzer(engine, depth=10, confirm_depth=12).iter_analysis(game):
                if event["type"] == "analysis":
                    analyses.append(event["analysis"])
    finally:
        engine.close()
    fools = analyses[2]["moments"]
    assert [m["san"] for m in fools] == ["g4"] and fools[0]["motif"] == "allowed_checkmate"
    result = recurring_weaknesses(analyses)
    assert "knight_fork" in [w["key"] for w in result["weaknesses"]]
