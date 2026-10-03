"""The Puzzles tab API: dashboard, practice/personalized sets, results feeding personalization.

Regression tests from the puzzle redesign brief:
  6 personalized puzzles target the weakness     8 generated puzzles are validated before serving
"""
from __future__ import annotations

from app.knowledge.generation import GenerationResult
from app.knowledge.library import get_knowledge
from app.learner import get_profile

from tests.test_puzzle_library import _analyzed, client, fork_record, ndjson  # noqa: F401  (fixtures)


def test_dashboard_and_practice_work_without_any_analysis(client):  # noqa: F811
    dash = client.get("/api/puzzles/dashboard").json()
    assert dash["personalized"]["has_data"] is False and dash["personalized"]["main"] is None
    forks = next(t for t in dash["practice"] if t["concept"] == "fork")
    assert forks["label"] == "Forks" and forks["count"] == 3
    body = client.post("/api/puzzles/set", json={"mode": "practice", "concept": "fork", "count": 3}).json()
    assert body["title"] == "Practice: Fork" and len(body["puzzles"]) == 3
    p = body["puzzles"][0]
    assert p["side"] == "white" and p["objective"] == "material" and p["objective_text"] == "Find the best move"
    # the idea and the real objective only in the post-solve reveal
    assert p["reveal"]["objective_text"] == "Win material" and p["reveal"]["concept_name"]
    assert p["critical_moves"] == ["Nc7+"] and p["forced_moves"] == ["Nxa8"] and p["critical_decision_points"] == [1]
    assert p["expected_solution_length"] == 2 and p["solution"] == ["Nc7+", "Kd7", "Nxa8"]
    assert p["steps"][0] == {"uci": "b5c7", "san": "Nc7+", "kind": "critical", "accepted": [], "good": [],
                             "reply_uci": "e8d7", "reply_san": "Kd7"}
    assert p["steps"][-1]["reply_uci"] is None and p["hint"] and p["explanation"]
    assert "debug" not in body  # practice has no weakness behind it


def test_personalized_without_weakness_data_points_to_practice(client):  # noqa: F811
    r = client.post("/api/puzzles/set", json={"mode": "personalized"})
    assert r.status_code == 422 and "Practice" in r.json()["error"]
    assert client.post("/api/puzzles/set", json={"mode": "practice", "concept": "nope"}).status_code == 404


def test_personalized_puzzles_target_the_weakness(client, monkeypatch):  # noqa: F811
    import app.analysis.personal_puzzles as pp
    monkeypatch.setattr(pp, "generate_for", lambda *a, **k: GenerationResult("knight_fork", attempts=1))
    _analyzed(client)
    dash = client.get("/api/puzzles/dashboard").json()["personalized"]
    assert dash["has_data"] and dash["main"]["concept"] == "knight_fork" and dash["main"]["source"] == "games"
    assert "analyzed games" in dash["main"]["evidence"] and dash["profile"]["main_weakness"]
    body = client.post("/api/puzzles/set", json={"mode": "personalized", "count": 5}).json()
    ids = [p["id"] for p in body["puzzles"]]
    assert body["weakness"] == "knight_fork" and all(p["concept"] == "knight_fork" for p in body["puzzles"])
    assert "test_knight_fork" not in ids and len(ids) == 2   # never the learner's own position
    assert body["debug"]["user_weakness"]["key"] == "knight_fork"
    assert body["debug"]["puzzle_validation"][0]["critical"] == ["Nc7+"]


def test_generated_puzzles_are_validated_before_serving(client, monkeypatch):  # noqa: F811
    import app.analysis.personal_puzzles as pp
    from app.knowledge.schema import parse_example
    _analyzed(client)
    lib = get_knowledge()
    # a "generated" entry that did not pass verification (the generator must never return one,
    # but the tab checks again before serving)
    unverified = parse_example(fork_record(id="test_candidate", status="candidate",
                                           start_fen="q3k3/8/8/1N6/8/8/8/2K5 w - - 0 1"),
                               lib.concepts, tier="personal")
    assert unverified.status != "verified"
    monkeypatch.setattr(pp, "generate_for", lambda *a, **k: GenerationResult(
        "knight_fork", accepted=[unverified], attempts=2))
    body = client.post("/api/puzzles/set", json={"mode": "personalized", "weakness": "knight_fork",
                                                 "count": 5}).json()
    assert "test_candidate" not in [p["id"] for p in body["puzzles"]]
    assert body["debug"]["custom_generation"]["needed"] == 3 and body["debug"]["custom_generation"]["status"] == "ran"


def test_results_are_recorded_and_change_personalization(client):  # noqa: F811
    body = client.post("/api/puzzles/set", json={"mode": "practice", "concept": "fork", "count": 3}).json()
    for p in body["puzzles"]:
        r = client.post(f"/api/puzzles/{p['id']}/result",
                        json={"solved": False, "mistakes": 1, "hints": 1, "seconds": 40}).json()
        assert r["recorded"] and r["concept"] == "knight_fork" and r["solved"] is False
        assert r["stats"]["attempts"] == 1
    st = get_profile().concepts["knight_fork"]
    assert st.attempts == 3 and st.solved == 0
    dash = client.get("/api/puzzles/dashboard").json()["personalized"]
    card = dash["main"]
    assert card["key"] == "puzzles:knight_fork" and card["source"] == "puzzles"
    assert card["evidence"] == "You solved 0 of your last 3 knight fork puzzles"
    # the struggle card drives the personalized set; these 3 were just missed, so they are not
    # repeated today (spaced retry) and the tab says so instead of serving something unrelated
    again = client.post("/api/puzzles/set", json={"mode": "personalized"})
    assert again.status_code == 422 and "knight fork" in again.json()["error"] and "recently" in again.json()["error"]
    assert client.post("/api/puzzles/nope/result", json={"solved": True}).status_code == 404


def test_clean_solves_lower_a_weakness_and_failures_raise_it(client):  # noqa: F811
    _analyzed(client)

    def priority():
        return client.get("/api/puzzles/dashboard").json()["personalized"]["main"]["priority"]

    before = priority()
    for pid in ("test_knight_fork_2", "test_knight_fork_3"):
        client.post(f"/api/puzzles/{pid}/result", json={"solved": True, "first_try": True, "seconds": 8})
    improved = priority()
    main = client.get("/api/puzzles/dashboard").json()["personalized"]["main"]
    assert improved < before and "improving" in main["progress"]
    for _ in range(4):
        client.post("/api/puzzles/test_knight_fork_2/result", json={"solved": False, "mistakes": 2})
    assert priority() > improved
