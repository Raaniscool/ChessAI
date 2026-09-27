"""Regression tests from the system audit: API contracts the UI relies on.

The UI reads errors from {"error": ...}. FastAPI's own validation/404 responses used
{"detail": ...} and reached the learner as a bare "Request failed (422)".
"""
import json

from app.engine import EngineUnavailable
from app.games import store
from app.session import MAX_CHAT_CHARS
from tests.test_game_history import client, engine, import_games, run_history  # noqa: F401


def test_validation_errors_are_readable(client):
    res = client.post("/api/plans")
    assert res.status_code == 422
    assert res.json()["error"].startswith("Invalid request")
    res = client.post("/api/games/import", json={"username": "x"})
    assert res.status_code == 422 and "pgn" in res.json()["error"]
    res = client.get("/api/games/history?count=abc")
    assert res.status_code == 422 and "count" in res.json()["error"]


def test_unknown_routes_are_readable(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404 and res.json() == {"error": "Not found: /api/does-not-exist"}


def test_analyze_needs_no_engine_when_everything_is_cached(client, monkeypatch):
    import_games(client, ["clean"] * 10)
    run_history(client)

    def broken():
        raise EngineUnavailable("no engine here")

    monkeypatch.setattr("app.game_api.get_engine", broken)
    res = client.post("/api/games/analyze", json={})
    assert res.status_code == 200, res.text  # was 503 even with nothing to analyze
    events = [json.loads(line) for line in res.text.splitlines() if line.strip()]
    assert events[0] == {"type": "start", "count": 0, "games": []} and events[-1]["type"] == "done"
    assert client.post("/api/games/analyze", json={"reanalyze": True}).status_code == 503


def test_stale_analyses_are_not_used_for_weaknesses_or_training(client):
    import_games(client, {1: "fork", 2: "fork", 3: "fork"})
    client.post("/api/games/analyze", json={})
    assert client.get("/api/games/weaknesses").json()["weaknesses"]
    for doc in store.list_docs():  # as if analyzed by an older analyzer version
        store.save_analysis(doc["game"]["id"], {**doc["analysis"], "schema_version": -1})
    assert client.get("/api/games/weaknesses").json() == {"total_games": 0, "min_games": 2,
                                                           "weaknesses": [], "seen_once": []}
    res = client.post("/api/games/training", json={"keys": ["knight_fork"]})
    assert res.status_code == 422 and res.json()["error"] == "analyze some games first"


def test_a_pasted_essay_is_capped_before_it_becomes_a_plan(client):
    res = client.post("/api/plans", json={"goal": "teach me zugzwang " * 400})
    assert res.status_code == 200, res.text
    assert len(res.json()["plan"]["goal"]) <= 300


def test_chat_messages_are_capped(client, monkeypatch):
    seen = {}

    def fake_chat(teacher, message, context, history):
        seen["message"] = message
        return "ok", "fallback"

    monkeypatch.setattr("app.teacher.chat_or_fallback", fake_chat)
    courses = client.get("/api/courses").json()["courses"]
    lesson = next(l for c in courses for l in c["lessons"] if l["status"] == "available")
    sid = client.post(f"/api/lessons/{lesson['id']}/start").json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/chat", json={"message": "x" * 20000}).status_code == 200
    assert len(seen["message"]) == MAX_CHAT_CHARS
