"""API tests: the full MVP teaching loop over HTTP, with a fast fake engine.

The real engine is exercised in test_engine.py; here we verify session
progression, board authority, validation, and response contracts.
"""
import chess
import pytest
from fastapi.testclient import TestClient

from app.engine import Classification, MoveFeedback, Score, set_engine
from app.lessons import get_library
from app.session import SessionManager, set_manager


class FakeEngine:
    """Deterministic engine: every legal move is 'excellent' with a stable eval."""

    def analyse(self, board, depth=None):
        from app.engine import Analysis
        best = next(iter(board.legal_moves))
        return Analysis(
            fen=board.fen(), best_move_uci=best.uci(), best_move_san=board.san(best),
            score=Score("cp", 20), pv_san=[board.san(best)], depth=depth or 14,
        )

    def evaluate_move(self, board, move, depth=None):
        after = board.copy()
        after.push(move)
        san = board.san(move)
        return MoveFeedback(
            fen_before=board.fen(), fen_after=after.fen(),
            user_move_uci=move.uci(), user_move_san=san,
            category=Classification.EXCELLENT, loss_cp=0,
            best_move_uci=move.uci(), best_move_san=san,
            eval_before=Score("cp", 20), eval_after=Score("cp", 20),
            best_pv_san=[san], reply_pv_san=[], depth=depth or 14,
        )

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_state():
    set_engine(FakeEngine())
    set_manager(SessionManager())
    yield
    set_engine(None)
    set_manager(None)


@pytest.fixture()
def client(_fresh_state):
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        yield c


def test_health_reports_engine_and_teacher(client):
    data = client.get("/api/health").json()
    assert data["engine"] is True
    assert data["teacher"] in ("fallback", "qwen")
    assert data["lessons"] >= 1


def test_courses_list_available_and_planned(client):
    data = client.get("/api/courses").json()
    course = next(c for c in data["courses"] if c["id"] == "italian_game")
    statuses = {l["id"]: l["status"] for l in course["lessons"]}
    assert statuses["italian_01"] == "available"
    assert statuses["italian_09"] == "planned"


def test_unknown_lesson_404(client):
    assert client.post("/api/lessons/nope/start").status_code == 404


def test_full_lesson_progression(client):
    # start → teach
    res = client.post("/api/lessons/italian_01/start")
    assert res.status_code == 200
    data = res.json()
    sid = data["session_id"]
    step = data["step"]
    assert step["type"] == "teach" and step["index"] == 0
    assert step["board"]["lock"] is True

    # cannot skip an exercise
    # advance → demonstrate
    step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    assert step["type"] == "demonstrate"
    assert step["moves"] == ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
    assert len(step["comments"]) == 5

    # advance → exercise 1 (White to play Bc4)
    step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    assert step["type"] == "exercise" and step["side"] == "white"
    assert step["hints_total"] >= 3

    # advancing before solving → 409
    assert client.post(f"/api/sessions/{sid}/advance").status_code == 409

    # illegal move → 400
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": "e9e5"})
    assert res.status_code == 400

    # legal but not the lesson's move → rejected, position resets
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": "d2d4"})
    body = res.json()
    assert body["accepted"] is False
    assert body["reset_fen"]
    assert body["feedback"]["category"] == "excellent"  # engine verdict is facts
    assert body["explanation"]

    # progressive hint
    hint = client.post(f"/api/sessions/{sid}/hint").json()
    assert "hint" in hint and hint["index"] == 1

    # the Italian bishop move → accepted
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": "f1c4"})
    body = res.json()
    assert body["accepted"] is True and body["continue_text"]

    # advance → exercise 2 (Black to play Nf6/Bc5)
    step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    assert step["type"] == "exercise" and step["side"] == "black"

    res = client.post(f"/api/sessions/{sid}/move", json={"uci": "g8f6"})
    assert res.json()["accepted"] is True

    # advance → closing teach → advance → completed
    step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    assert step["type"] == "teach"
    res = client.post(f"/api/sessions/{sid}/advance").json()
    assert res["completed"] is True
    assert res["completion_text"]

    # progress recorded
    progress = client.get("/api/progress").json()
    assert "italian_01" in progress["completed_lessons"]


def test_reveal_solution_unlocks_progression(client):
    sid = client.post("/api/lessons/italian_01/start").json()["session_id"]
    for _ in range(2):  # teach → demonstrate → exercise
        step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    rev = client.post(f"/api/sessions/{sid}/reveal").json()
    assert rev["accepted"] is True and "Bc4" in rev["accepted_moves"]
    # now advancing is allowed
    step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    assert step["type"] == "exercise"


def test_chat_uses_fallback_teacher_offline(client):
    sid = client.post("/api/lessons/italian_01/start").json()["session_id"]
    res = client.post(f"/api/sessions/{sid}/chat", json={"message": "What is the Italian Game?"})
    assert res.status_code == 200
    body = res.json()
    assert body["reply"]
    if body["teacher"] == "fallback":
        assert "Qwen" in body["reply"]  # honest about being offline


def test_session_resume(client):
    sid = client.post("/api/lessons/italian_01/start").json()["session_id"]
    client.post(f"/api/sessions/{sid}/advance")
    data = client.get(f"/api/sessions/{sid}").json()
    assert data["step"]["type"] == "demonstrate"
    assert data["status"] == "active"
