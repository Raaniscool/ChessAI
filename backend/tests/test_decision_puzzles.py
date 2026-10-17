"""Decision Puzzles: validated positions, engine truth, conservative reasoning feedback, and learner evidence."""
from __future__ import annotations

import json

import chess
import pytest

from app.engine import Classification, EngineUnavailable, MoveFeedback, Score, get_engine, set_engine
from app.knowledge.library import get_knowledge
from app.learner import get_profile
from app.puzzles.decision import (DecisionTaskStore, decision_candidates, get_decision_store,
                                  issue_decision_task, set_decision_store, validate_position_move)
from tests.test_api import _fresh_state, client as api_client  # noqa: F401  (shared isolated API fixtures)


@pytest.fixture(autouse=True)
def fresh_decision_tasks():
    set_decision_store(DecisionTaskStore())
    yield
    set_decision_store(None)


class ScriptedEngine:
    """Engine double with explicit verdicts; API logic must use its result, never the LLM's."""

    def __init__(self, category=Classification.EXCELLENT, loss=0, *, complete=True):
        self.category = category
        self.loss = loss
        self.complete = complete
        self.calls = []

    def evaluate_move(self, board, move, depth=None):
        self.calls.append((board.fen(), move.uci()))
        alternatives = [candidate for candidate in board.legal_moves if candidate != move]
        best = alternatives[0] if alternatives else move
        after = board.copy()
        after.push(move)
        best_line = [board.san(best)]
        return MoveFeedback(
            fen_before=board.fen(), fen_after=after.fen(),
            user_move_uci=move.uci(), user_move_san=board.san(move),
            category=self.category, loss_cp=self.loss,
            best_move_uci=best.uci() if self.complete else None,
            best_move_san=board.san(best) if self.complete else None,
            eval_before=Score("cp", 25) if self.complete else None,
            eval_after=Score("cp", 25 - self.loss) if self.complete else None,
            best_pv_san=best_line, reply_pv_san=[], depth=depth or 8,
        )

    def close(self):
        pass


class FixedTeacher:
    name = "qwen"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, max_tokens=None, timeout=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return json.dumps(self.payload)


class MalformedTeacher:
    name = "qwen"

    def complete(self, messages, max_tokens=None, timeout=None):
        return "not JSON"


def judgment(assessment, quality, *, recognized=(), missing=(), misconceptions=(), confidence="high"):
    return {
        "assessment": assessment,
        "confidence": confidence,
        "claimed_quality": quality,
        "recognized_fact_ids": list(recognized),
        "missing_fact_ids": list(missing),
        "misconception_fact_ids": list(misconceptions),
    }


def issue(client, quality="good"):
    response = client.post("/api/puzzles/decision/next", json={"quality": quality})
    assert response.status_code == 200, response.text
    return response.json()


def answer(client, task, explanation="I think this move is good because the line wins material."):
    return client.post(f"/api/puzzles/decision/{task['task_id']}/answer", json={"answer": explanation})


def test_python_chess_validates_position_and_proposed_move():
    board, move, san = validate_position_move(chess.STARTING_FEN, "e2e4")
    assert board.is_valid() and move in board.legal_moves and san == "e4"
    with pytest.raises(ValueError, match="illegal"):
        validate_position_move(chess.STARTING_FEN, "e2e5")
    with pytest.raises(ValueError, match="Invalid Decision Puzzle position"):
        validate_position_move("not-a-fen", "e2e4")
    with pytest.raises(ValueError, match="not a valid chess position"):
        validate_position_move("4K2K/8/8/8/8/8/8/4k3 w - - 0 1", "e1e2")


def test_candidate_library_contains_only_replayable_verified_moves():
    candidates = decision_candidates(get_knowledge())
    assert len(candidates) > 100
    assert {candidate.kind for candidate in candidates} == {"good", "bad"}
    for candidate in candidates:
        board, move, san = validate_position_move(candidate.fen, candidate.move_uci)
        assert board.is_valid()
        assert move in board.legal_moves
        assert san == candidate.move_san
        assert candidate.example.status == "verified"
        assert candidate.example.tier != "personal"


def test_decision_service_uses_real_stockfish_when_available(monkeypatch):
    knowledge = get_knowledge()
    example = knowledge.get("mate_fools_mate")
    assert example is not None

    class OneExampleKnowledge:
        concepts = knowledge.concepts

        def examples_for(self, concept):
            return [example] if concept == example.concept else []

    class ShallowEngine:
        def evaluate_move(self, board, move):
            return real_engine.evaluate_move(board, move, depth=6)

    monkeypatch.setattr("app.puzzles.decision._need_ranks", lambda *_args: {})
    set_engine(None)
    try:
        real_engine = get_engine()
    except EngineUnavailable:
        pytest.skip("Stockfish is not installed")
    try:
        task = issue_decision_task(OneExampleKnowledge(), ShallowEngine(), profile=get_profile(),
                                   store=get_decision_store(), concept=example.concept, quality="good")
        assert task.candidate.example.id == example.id
        assert task.engine_feedback.category.value in {"excellent", "good"}
        assert task.engine_feedback.best_move_san == "Qh4#"
        assert task.engine_feedback.loss_cp <= 50
    finally:
        real_engine.close()
        set_engine(None)


def test_next_task_is_hidden_until_submitted_and_stockfish_evaluates_it(api_client):
    engine = ScriptedEngine(Classification.EXCELLENT, 5)
    set_engine(engine)
    task = issue(api_client)

    board = chess.Board(task["fen"])
    assert board.is_valid()
    assert board.parse_san(task["proposed_move"]) in board.legal_moves
    assert task["side_to_move"] == ("white" if board.turn else "black")
    assert "engine" not in task and "best_move" not in task and "concept" not in task
    assert len(engine.calls) == 1


def test_stockfish_verdict_is_returned_as_the_objective_truth(api_client, monkeypatch):
    engine = ScriptedEngine(Classification.BLUNDER, 480)
    set_engine(engine)
    task = issue(api_client, "bad")
    teacher = FixedTeacher(judgment("correct", "good", recognized=("engine_verdict", "best_move")))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    response = answer(api_client, task, "It is good because it keeps the pressure on.")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["assessment"] == "misconception"  # the model cannot overrule Stockfish
    assert result["engine"]["category"] == "blunder"
    assert result["engine"]["quality"] == "bad"
    assert result["engine"]["loss_cp"] == 480
    assert result["engine"]["best_move"]
    assert result["engine"]["proposed_line"][0] == task["proposed_move"]
    assert result["recorded"] is True
    assert teacher.calls and task["fen"] not in json.dumps(teacher.calls[0]["messages"])
    assert "objective_quality" in teacher.calls[0]["messages"][1]["content"]
    assert result["concept"] in get_profile().concepts
    event = get_profile().exercise_log[-1]
    assert event["source"] == "decision_puzzle" and event["exercise_id"].startswith("decision:")
    assert event["score"] == 0.0


def test_correct_explanation_records_full_concept_evidence(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 20))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("correct", "good", recognized=("engine_verdict", "best_move")))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task).json()
    profile = get_profile()
    assert result["assessment"] == "correct" and result["recorded"] is True
    concept = result["concept"]
    assert profile.concepts[concept].attempts == 1
    assert profile.concepts[concept].recent[-1] == 1.0
    assert profile.exercise_log[-1]["source"] == "decision_puzzle"


def test_partial_reasoning_gets_partial_credit_in_the_existing_learner_model(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 35))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("partial", "good", recognized=("engine_verdict",), missing=("best_move",),
                                     confidence="medium"))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "The move looks good, though I am not sure of the reason.").json()
    state = get_profile().concepts[result["concept"]]
    assert result["assessment"] == "partial" and result["recorded"] is True
    assert state.recent[-1] == 0.6
    assert "Partly right" in result["feedback"]


def test_reason_without_a_good_bad_judgment_cannot_receive_full_credit(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 20))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("correct", "unclear", recognized=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "The engine has a stronger alternative.").json()
    assert result["assessment"] == "partial" and result["recorded"] is True
    assert get_profile().concepts[result["concept"]].recent[-1] == 0.6


def test_misconception_about_a_good_move_is_recorded_as_a_miss(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 0))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("misconception", "bad", recognized=("engine_verdict",),
                                     misconceptions=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "This is bad because it is a blunder.").json()
    state = get_profile().concepts[result["concept"]]
    assert result["assessment"] == "misconception" and result["recorded"] is True
    assert state.recent[-1] == 0.0
    assert "Stockfish rates this move as good" in result["feedback"]


def test_unavailable_or_invalid_reasoning_judge_is_uncertain_and_not_recorded(api_client, monkeypatch):
    from app.teacher import FallbackTeacher

    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    monkeypatch.setattr("app.teacher.get_teacher", lambda: FallbackTeacher())
    before = get_profile().stats["puzzles"]["attempts"]

    result = answer(api_client, task).json()
    assert result["assessment"] == "uncertain" and result["recorded"] is False
    assert result["assessment_source"] == "fallback"
    assert result["engine"]["quality"] == "good" and result["engine"]["best_move"]
    assert "couldn't reliably judge" in result["feedback"]

    malformed = MalformedTeacher()
    monkeypatch.setattr("app.teacher.get_teacher", lambda: malformed)
    next_task = issue(api_client, "good")
    invalid = answer(api_client, next_task).json()
    assert invalid["assessment"] == "uncertain" and invalid["recorded"] is False
    assert invalid["assessment_source"] == "qwen"
    assert get_profile().stats["puzzles"]["attempts"] == before


def test_answer_is_idempotent_and_unknown_or_short_submissions_fail(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 0))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("correct", "good", recognized=("engine_verdict", "best_move")))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    first = answer(api_client, task).json()
    repeated = answer(api_client, task, "A different answer must not overwrite a graded result.").json()
    assert repeated == first
    assert len(teacher.calls) == 1
    assert get_profile().stats["puzzles"]["attempts"] == 1
    assert api_client.post("/api/puzzles/decision/missing/answer", json={"answer": "Long enough answer."}).status_code == 404
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer", json={"answer": "short"}).status_code == 422


def test_missing_stockfish_returns_service_unavailable_without_a_fake_task(api_client, monkeypatch):
    def unavailable():
        raise EngineUnavailable("Stockfish is not installed")

    monkeypatch.setattr("app.engine.get_engine", unavailable)
    response = api_client.post("/api/puzzles/decision/next", json={})
    assert response.status_code == 503
    assert "Stockfish" in response.json()["error"]


def test_incomplete_engine_evaluation_is_not_presented_as_truth(api_client):
    set_engine(ScriptedEngine(Classification.GOOD, 20, complete=False))
    response = api_client.post("/api/puzzles/decision/next", json={"quality": "good"})
    assert response.status_code == 503
    assert "complete evaluation" in response.json()["error"]


def test_unknown_concept_is_rejected(api_client):
    response = api_client.post("/api/puzzles/decision/next", json={"concept": "not_a_chess_concept"})
    assert response.status_code == 404
