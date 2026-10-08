"""HTTP: 'I want to learn ___' → plan → playable lessons."""
from tests.session_helpers import confirm_advance
import pytest
from fastapi.testclient import TestClient

from app.engine import set_engine
from app.lessons import set_library
from app.session import SessionManager, set_manager

from tests.test_api import FakeEngine


@pytest.fixture()
def client():
    set_library(None)
    set_engine(FakeEngine())
    set_manager(SessionManager())
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        yield c
    set_engine(None)
    set_manager(None)
    set_library(None)


def test_topics_endpoint(client):
    topics = client.get("/api/topics").json()["topics"]
    assert any(t["id"] == "italian_game" for t in topics)


def test_create_plan_play_and_delete(client):
    res = client.post("/api/plans", json={"goal": "I want to learn forks"})
    assert res.status_code == 200, res.text
    data = res.json()
    plan = data["plan"]
    assert plan["units"][0]["topic_id"] == "forks"

    courses = client.get("/api/courses").json()["courses"]
    assert courses[0]["id"] == data["course_id"] and courses[0]["kind"] == "plan"
    assert courses[0]["plan"]["goal"] == "I want to learn forks"
    assert any(c["id"] == "italian_game" for c in courses)

    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    assert start["plan_id"] == plan["id"]
    assert start["course_id"] == data["course_id"]
    assert start["step"]["type"] == "teach"
    step = confirm_advance(client, sid)["step"]
    assert step["type"] == "exercise"
    wrong = client.post(f"/api/sessions/{sid}/move", json={"uci": "g2g3"}).json()
    assert wrong["accepted"] is False
    right = client.post(f"/api/sessions/{sid}/move", json={"uci": "d5c7"}).json()
    assert right["accepted"] is True

    assert client.delete(f"/api/plans/{plan['id']}").status_code == 200
    assert all(c["id"] != data["course_id"] for c in client.get("/api/courses").json()["courses"])
    assert client.delete(f"/api/plans/{plan['id']}").status_code == 404


def test_plans_survive_restart(client):
    data = client.post("/api/plans", json={"goal": "back rank mate"}).json()
    set_library(None)  # simulate a server restart: library rebuilt from disk
    ids = [p["id"] for p in client.get("/api/plans").json()["plans"]]
    assert data["plan"]["id"] in ids
    client.delete(f"/api/plans/{data['plan']['id']}")


def test_unknown_goal_returns_422_with_suggestions(client):
    res = client.post("/api/plans", json={"goal": "underwater basket weaving"})
    assert res.status_code == 422
    assert res.json()["suggestions"]


def test_plan_progression_uses_stored_order_and_completion(client, monkeypatch):
    import importlib

    data = client.post("/api/plans", json={"goal": "I want to learn the smothered mate", "library": True}).json()
    plan_id = data["plan"]["id"]
    from app.lessons import get_library
    lessons = get_library().course(data["course_id"]).lessons
    assert len(lessons) >= 2

    initial = client.get(f"/api/plans/{plan_id}/progression").json()
    assert initial["complete"] is False
    assert initial["next_lesson"]["id"] == lessons[0].id

    main = importlib.import_module("app.main")
    completed = {lessons[0].id}
    monkeypatch.setattr(main, "_completed", lambda: completed)
    response = client.get(f"/api/plans/{plan_id}/progression",
                          params={"current_lesson_id": lessons[0].id}).json()
    assert response["complete"] is False
    assert response["completed_lesson"] == {"id": lessons[0].id, "title": lessons[0].title, "number": 1}
    assert response["next_lesson"] == {"id": lessons[1].id, "title": lessons[1].title, "number": 2}

    completed.update(lesson.id for lesson in lessons)
    finished = client.get(f"/api/plans/{plan_id}/progression").json()
    assert finished["complete"] is True and finished["next_lesson"] is None
    assert finished["lesson_count"] == len(lessons)

    restarted = client.get(f"/api/plans/{plan_id}/progression", params={"restart": True}).json()
    assert restarted["complete"] is False
    assert restarted["next_lesson"]["id"] == lessons[0].id


def test_plans_created_in_the_same_second_list_newest_first(client):
    """Plans carry a created time in whole seconds; ties must still put the newest first."""
    first = client.post("/api/plans", json={"goal": "I want to learn forks"}).json()
    second = client.post("/api/plans", json={"goal": "I want to learn pins"}).json()
    from app.lessons import get_library
    lib = get_library()
    for cid in (first["course_id"], second["course_id"]):
        lib.course(cid).meta["plan"]["created"] = "2026-01-01T00:00:00+00:00"
    courses = [c["id"] for c in client.get("/api/courses").json()["courses"] if c["kind"] == "plan"]
    assert courses.index(second["course_id"]) < courses.index(first["course_id"])
    plans = [p["id"] for p in client.get("/api/plans").json()["plans"]]
    assert plans.index(second["plan"]["id"]) < plans.index(first["plan"]["id"])
