"""Explanation requests as a soft, per-concept learner signal (learner.help).

Performance tells us what the learner did; explanation requests tell us how much additional
understanding they sought. The two are stored side by side and never mixed.
"""
import json

import pytest

from app.knowledge.library import get_knowledge
from app.learner import LearnerProfile, get_profile, get_store
from app.learner.adapt import Outcome, decide
from app.learner.difficulty import build
from app.learner.help import MIN_EXPLAINED, TARGET_EASE, is_help_request, signal
from app.learner.views import concept_view, lesson_shape
from app.session import get_manager
from tests.test_api import _fresh_state, client  # noqa: F401  (fixtures)

LIB = get_knowledge()


def attempt(p, concept="boden_mate", *, solved=True, explained=None, rating=1000, hints=0, revealed=False):
    return p.record_attempt(concept, rating, solved=solved, first_try=solved, hints=hints, revealed=revealed,
                            explanations=explained,
                            context={"source": "lesson", "lesson_id": "L1", "session_id": "S1",
                                     "exercise_id": f"ex{p.concept(concept).attempts}"})


def twins():
    """Two identical learners: one asks for explanations, the other doesn't."""
    return LearnerProfile(), LearnerProfile()


# ------------------------------------------------------------------ 1-4: the four combinations
def test_correct_without_an_explanation_request():
    p = LearnerProfile()
    attempt(p)
    st = p.concepts["boden_mate"]
    assert st.recent == [1.0] and st.recent_explained == [0] and st.explained == 0 and st.explain_requests == 0
    entry = p.exercise_log[-1]
    assert entry["result"] == "solved" and entry["solved"] and entry["explanation_requested"] is False
    assert entry["concept"] == "boden_mate" and entry["difficulty"] == 1000 and entry["lesson_id"] == "L1"
    assert entry["exercise_id"] == "ex0" and entry["session_id"] == "S1" and entry["at"]
    assert concept_view(p, "boden_mate")["explanations"]["signal"] == "none"


def test_correct_with_an_explanation_request_is_still_a_correct_answer():
    asked, plain = twins()
    a = attempt(asked, explained={"deeper": 1})
    b = attempt(plain)
    # the result is unchanged: same score, same rating, same overall skill
    assert a == b and asked.rating == plain.rating
    assert asked.concepts["boden_mate"].recent == plain.concepts["boden_mate"].recent == [1.0]
    st = asked.concepts["boden_mate"]
    assert st.recent_explained == [1] and st.explained == 1 and st.explain_kinds == {"deeper": 1}
    assert asked.stats["explanations"] == {"explain": 0, "deeper": 1, "question": 0}
    entry = asked.exercise_log[-1]
    assert entry["solved"] and entry["explanation_requested"] and entry["explanations"]["deeper"] == 1


def test_incorrect_with_an_explanation_request():
    asked, plain = twins()
    attempt(asked, solved=False, revealed=True, explained={"question": 2})
    attempt(plain, solved=False, revealed=True)
    assert asked.concepts["boden_mate"].recent == plain.concepts["boden_mate"].recent == [0.0]
    assert asked.concepts["boden_mate"].rating == plain.concepts["boden_mate"].rating
    entry = asked.exercise_log[-1]
    assert entry["result"] == "revealed" and not entry["solved"] and entry["revealed"]
    assert entry["explanation_requested"] and entry["explanations"]["question"] == 2
    assert asked.concepts["boden_mate"].explained == 1 and asked.concepts["boden_mate"].explain_requests == 2


def test_incorrect_without_a_request_is_a_performance_failure_not_a_teaching_need():
    p = LearnerProfile()
    for _ in range(4):
        attempt(p, solved=False)
    view = concept_view(p, "boden_mate")
    assert view["status"] == "weak"                       # performance still counts
    assert view["explanations"]["signal"] == "none"       # ... but nobody assumes they needed teaching
    assert "understanding" not in view["gaps"]
    shape = lesson_shape(p, "boden_mate")
    assert shape["teaching"] is False and shape["explanations"] == "none"


# ------------------------------------------------------------------ 5: repeated requests
def test_repeated_requests_while_solving_mean_teach_first_not_harder():
    asked, plain = twins()
    for _ in range(5):
        attempt(asked, explained={"deeper": 1})
        attempt(plain)
    a, b = concept_view(asked, "boden_mate"), concept_view(plain, "boden_mate")
    assert b["status"] == "mastered" and b["explanations"]["signal"] == "none"
    # "I can solve this, but I still don't fully understand why"
    assert a["status"] == "practicing" and a["explanations"]["signal"] == "understanding"
    assert "understanding" in a["gaps"] and a["rating"] == b["rating"]   # performance is the same
    sa, sb = lesson_shape(asked, "boden_mate"), lesson_shape(plain, "boden_mate")
    assert sb["purpose"] == "challenge"                                  # strong + rarely asks: step up
    assert sa["purpose"] != "challenge" and sa["teaching"] and sa["demos"] > sb["demos"]
    from app.learner.personalize import reason
    assert "asked why" in reason(sa, asked, "boden_mate", "Boden's mate")


def test_repeated_requests_while_failing_are_evidence_of_difficulty():
    p = LearnerProfile()
    for _ in range(4):
        attempt(p, solved=False, explained={"question": 1})
    view = concept_view(p, "boden_mate")
    assert view["explanations"]["signal"] == "difficulty"
    assert lesson_shape(p, "boden_mate")["teaching"] is True


def test_occasional_requests_stay_below_the_threshold():
    p = LearnerProfile()
    for i in range(6):
        attempt(p, explained={"explain": 1} if i in (1, 4) else None)   # 2 of 6
    assert concept_view(p, "boden_mate")["explanations"] == {
        "signal": "none", "explained": 2, "results": 6, "share": 0.33, "explained_success": 1.0, "requests": 2}
    assert concept_view(p, "boden_mate")["status"] == "mastered"


def test_signal_rules():
    assert signal([1.0] * 6, [1, 1, 1, 0, 0, 0], 6)["signal"] == "understanding"
    assert signal([1.0] * 6, [1, 1, 0, 0, 0, 0], 6)["signal"] == "none"            # under MIN_EXPLAINED
    assert signal([1.0] * 10, [1] * 3 + [0] * 7, 10)["signal"] == "none"            # under half
    assert signal([0.0] * 4, [1, 1, 1, 0], 6)["signal"] == "difficulty"
    assert signal([1.0, 1.0], [], 6)["explained"] == 0                               # no data: no request
    assert MIN_EXPLAINED >= 3


# ------------------------------------------------------------------ 6: concept-specific
def test_requests_on_one_concept_leave_unrelated_concepts_and_skills_alone():
    asked, plain = twins()
    for p in (asked, plain):
        for _ in range(4):
            attempt(p, "knight_fork")
            attempt(p, "italian_game")
    for _ in range(5):
        attempt(asked, "boden_mate", explained={"deeper": 1, "question": 1})
        attempt(plain, "boden_mate")
    for cid in ("knight_fork", "italian_game"):
        assert concept_view(asked, cid) == concept_view(plain, cid)
        assert lesson_shape(asked, cid, LIB) == lesson_shape(plain, cid, LIB)
    assert asked.rating == plain.rating and asked.skill == plain.skill
    da, dp = build(asked), build(plain)
    assert {k: s.estimate for k, s in da.skills.items()} == {k: s.estimate for k, s in dp.skills.items()}
    for cid in ("knight_fork", "italian_game", "fork"):
        assert da.target(cid, LIB)["target"] == dp.target(cid, LIB)["target"]
    eased = da.target("boden_mate", LIB)
    assert eased["target"] == dp.target("boden_mate", LIB)["target"] - TARGET_EASE
    assert eased["concept"]["explanations"] == "understanding" and eased["concept"]["explanation_shift"] == -TARGET_EASE
    assert "asked why" in eased["summary"]


# ------------------------------------------------------------------ 7: one request: no major drop
def test_a_single_request_changes_no_difficulty():
    asked, plain = twins()
    for i in range(5):
        attempt(asked, explained={"deeper": 1} if i == 2 else None)
        attempt(plain)
    assert build(asked).target("boden_mate", LIB) == build(plain).target("boden_mate", LIB)
    assert lesson_shape(asked, "boden_mate", LIB) == lesson_shape(plain, "boden_mate", LIB)
    assert concept_view(asked, "boden_mate")["status"] == "mastered"


def test_in_a_lesson_a_request_is_not_a_failure():
    exs = sorted(LIB.examples_for("checkmate"), key=lambda e: e.id)
    a, b = exs[0], exs[1]
    one = [Outcome(a.id, "checkmate", 900, 1.0, explained=True)]
    assert decide(one, [], {a.id}, 0, LIB) is None         # never "easier" because they asked
    clean = Outcome(b.id, "checkmate", 900, 1.0)
    d = decide([one[0], clean], [], {a.id, b.id}, 0, LIB)
    assert d is None or d.kind not in ("easier", "prerequisite", "teach")


def test_two_explained_solves_in_a_lesson_add_a_worked_example_not_a_harder_one():
    from app.knowledge.difficulty import puzzle_rating
    exs = sorted(LIB.examples_for("checkmate"), key=puzzle_rating)
    mid = len(exs) // 2   # mid-rated: there are easier worked examples of the idea to show
    a, b, c = exs[mid], exs[mid + 1], exs[mid + 2]
    outs = [Outcome(e.id, "checkmate", puzzle_rating(e), 1.0, explained=True) for e in (a, b)]
    upcoming = [(c.id, puzzle_rating(c), "practice")]
    d = decide(outs, upcoming, {a.id, b.id, c.id}, 0, LIB)
    assert d.kind == "teach" and d.role == "demonstration" and d.replace is None and d.note
    assert puzzle_rating(LIB.get(d.example_id)) <= puzzle_rating(b)
    assert decide(outs, upcoming, {a.id, b.id, c.id, d.example_id}, 1, LIB, done_kinds={"teach"}) is None
    # the same two solves without requests: the existing step up
    plain = [Outcome(e.id, "checkmate", puzzle_rating(e), 1.0) for e in (a, b)]
    easy_next = [(exs[0].id, puzzle_rating(exs[0]), "practice")]
    assert decide(plain, easy_next, {a.id, b.id, exs[0].id}, 0, LIB).kind == "harder"
    assert decide(outs, easy_next, {a.id, b.id, exs[0].id}, 1, LIB, done_kinds={"teach"}) is None


# ------------------------------------------------------------------ 8-9: existing tracking intact
def test_existing_tracking_still_works():
    p = LearnerProfile()
    out = attempt(p, solved=True, hints=2)
    assert set(out) == {"concept", "score", "rating_before", "rating_after"}
    st = p.concepts["boden_mate"]
    assert st.attempts == 1 and st.hints == 2 and p.stats["hints_used"] == 2 and p.stats["puzzles"]["attempts"] == 1
    attempt(p, solved=False, revealed=True)
    assert st.reveals == 1 and p.stats["reveals"] == 1 and st.explained == 0
    # the old call signature (no explanations, no context) still works
    p.record_attempt("knight_fork", 900, solved=True, first_try=True)
    assert p.exercise_log[-1]["source"] == "lesson" and p.exercise_log[-1]["explanation_requested"] is False


def test_profiles_without_explanation_data_still_work():
    """A profile saved before this feature: no explain fields, no exercise log."""
    old = LearnerProfile()
    for _ in range(5):
        attempt(old)
    data = old.as_dict()
    data.pop("exercise_log")
    data["stats"].pop("explanations")
    for st in data["concepts"].values():
        for key in ("explain_requests", "explain_kinds", "explained", "recent_explained"):
            st.pop(key)
    p = LearnerProfile.from_dict(json.loads(json.dumps(data)))
    view = concept_view(p, "boden_mate", LIB)
    assert view["status"] == "mastered" and view["explanations"]["signal"] == "none"
    assert build(p).target("boden_mate", LIB) == build(old).target("boden_mate", LIB)
    p.record_explanation("boden_mate", "deeper")       # and recording works on it
    assert p.stats["explanations"]["deeper"] == 1
    attempt(p, explained={"explain": 1})
    assert p.concepts["boden_mate"].recent_explained[-1] == 1 and len(p.concepts["boden_mate"].recent_explained) == 6


def test_a_late_request_marks_the_right_result():
    p = LearnerProfile()
    attempt(p)  # ex0
    attempt(p)  # ex1
    attempt(p)  # ex2
    out = p.record_explanation("boden_mate", "deeper", exercise_id="ex1", session_id="S1")
    assert out["attached"] and out["newly_explained"]
    assert p.concepts["boden_mate"].recent_explained == [0, 1, 0]
    again = p.record_explanation("boden_mate", "question", exercise_id="ex1", session_id="S1")
    assert again["attached"] and not again["newly_explained"]          # one exercise counts once
    assert p.concepts["boden_mate"].explained == 1 and p.concepts["boden_mate"].explain_requests == 2
    with pytest.raises(ValueError):
        p.record_explanation("boden_mate", "hint")                     # hints are not explanations


def test_help_requests_in_chat():
    for text in ("why is Bxh7 good?", "What was wrong with my move", "I don't understand this",
                 "can you explain the idea", "how does the knight help"):
        assert is_help_request(text), text
    for text in ("thanks", "ok", "nice!", "cool, next one", ""):
        assert not is_help_request(text), text


# ------------------------------------------------------------------ the lesson API, end to end
def _to_exercise(client, sid, step):  # noqa: F811
    while step["type"] != "exercise":
        r = client.post(f"/api/sessions/{sid}/advance").json()
        assert not r["completed"]
        step = r["step"]
    return step


def _solve(client, sid):  # noqa: F811
    """Play the stored solution of the current example until its result is recorded."""
    from app.lessons.schema import ExerciseStep
    mgr = get_manager()
    session = mgr.get(sid)
    key = session.steps[session.step_index].example
    for _ in range(30):
        step = mgr.current_step(session)
        if isinstance(step, ExerciseStep) and step.example == key and not session.exercise_accepted:
            uci = session.board.parse_san(step.accepted_san[0]).uci()
            res = client.post(f"/api/sessions/{sid}/move", json={"uci": uci}).json()
            assert res["accepted"], res
            if any(o.example_id == key for o in session.outcomes):
                return res, key
        r = client.post(f"/api/sessions/{sid}/advance").json()
        assert not r["completed"]
    raise AssertionError("the example never resolved")


def test_lesson_flow_counts_only_explicit_requests(client):  # noqa: F811
    body = client.post("/api/plans", json={"goal": "knight forks", "library": True}).json()
    start = client.post(f"/api/lessons/{body['first_lesson_id']}/start").json()
    sid = start["session_id"]
    _to_exercise(client, sid, start["step"])
    session = get_manager().get(sid)
    key = session.steps[session.step_index].example
    assert key
    # a "thanks" in the chat is not a request; a question while solving is (attached on resolve)
    client.post(f"/api/sessions/{sid}/chat", json={"message": "thanks"})
    assert get_profile().stats["explanations"]["question"] == 0
    client.post(f"/api/sessions/{sid}/chat", json={"message": "Why would a knight move work here?"})
    assert session.example_state[key]["explanations"] == {"question": 1}
    # the instant verified feedback, and the automatic AI explanation stream, never count
    res, key = _solve(client, sid)
    assert res["teacher"] == "library"
    client.post(f"/api/sessions/{sid}/explain")
    profile = get_profile()
    logged = [e for e in profile.exercise_log if e.get("exercise_id") == key]
    assert len(logged) == 1 and logged[0]["solved"] and logged[0]["explanation_requested"]
    assert logged[0]["explanations"] == {"explain": 0, "deeper": 0, "question": 1}
    assert profile.stats["explanations"] == {"explain": 0, "deeper": 0, "question": 1}
    assert logged[0]["lesson_id"] == body["first_lesson_id"] and logged[0]["session_id"] == sid
    # "Explain deeper" after the last move is attached to that same result
    assert res["deeper"] is True
    assert client.post(f"/api/sessions/{sid}/example/explain").status_code == 200
    profile = get_profile()
    assert profile.stats["explanations"]["deeper"] == 1
    logged = [e for e in profile.exercise_log if e.get("exercise_id") == key]
    assert logged[0]["explanations"]["deeper"] == 1 and len(logged) == 1
    concept = LIB.get(key).concept
    assert profile.concepts[concept].explained == 1      # one exercise, counted once
    assert profile.concepts[concept].explain_kinds == {"question": 1, "deeper": 1}
    assert any(o.explained for o in session.outcomes)


def test_puzzle_results_keep_hints_reveals_and_explanations_apart(client):  # noqa: F811
    from app.puzzles import get_puzzles
    p = next(iter(get_puzzles(LIB).all()))
    r = client.post(f"/api/puzzles/{p.id}/result", json={"solved": False, "first_try": False, "mistakes": 1,
                                                         "hints": 2, "revealed": True, "seconds": 30})
    assert r.status_code == 200 and r.json()["recorded"]
    prof = get_profile()
    entry = prof.exercise_log[-1]
    assert entry["source"] == "puzzle" and entry["exercise_id"] == p.id
    assert entry["hints"] == 2 and entry["revealed"] and entry["result"] == "revealed"
    assert entry["explanation_requested"] is False      # a revealed solution is not an explanation request
    assert prof.stats["explanations"] == {"explain": 0, "deeper": 0, "question": 0}
    assert prof.stats["hints_used"] == 2 and prof.stats["reveals"] == 1


def test_explanation_data_stays_with_the_learner():
    before = len(LIB.verified())
    p = get_store().get()
    for _ in range(4):
        attempt(p, explained={"deeper": 1})
    get_store().save(p)
    saved = json.loads(get_store().path(p.id).read_text(encoding="utf-8"))
    assert saved["exercise_log"] and saved["concepts"]["boden_mate"]["explained"] == 4
    lib = get_knowledge()
    assert len(lib.verified()) == before and not hasattr(lib.concepts["boden_mate"], "explained")
