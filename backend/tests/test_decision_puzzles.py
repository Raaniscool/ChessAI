"""Decision Puzzles: engine truth, separate explanation grading, durable adaptation, and de-duplication."""
from __future__ import annotations

import hashlib
import json
import time

import chess
import httpx
import pytest

from app.engine import Classification, EngineUnavailable, MoveFeedback, Score, get_engine, set_engine
from app.knowledge.library import get_knowledge
from app.learner import LearnerProfile, LearnerStore, get_profile, get_store, set_store
from app.learner.decision_profile import (decision_profile_summary, ensure_decision_profile,
                                          record_decision_exposure, record_decision_result,
                                          refresh_decision_weaknesses)
from app.puzzles.decision import (DecisionPuzzleError, DecisionTaskStore, _classify_explanation,
                                  _parse_assessment,
                                  _evaluate_move_cached, canonical_task_identity,
                                  clear_decision_candidate_cache, clear_decision_engine_cache,
                                  decision_candidates, get_decision_store, issue_decision_task,
                                  set_decision_store, submit_decision_answer, validate_position_move)
from tests.test_api import _fresh_state, client as api_client  # noqa: F401 (shared isolated API fixtures)


@pytest.fixture(autouse=True)
def fresh_decision_tasks():
    clear_decision_candidate_cache()
    clear_decision_engine_cache()
    set_decision_store(DecisionTaskStore())
    yield
    clear_decision_candidate_cache()
    clear_decision_engine_cache()
    set_decision_store(None)


class ScriptedEngine:
    """Engine double with explicit verdicts; API logic must use its result, never the judge's."""

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
        return MoveFeedback(
            fen_before=board.fen(), fen_after=after.fen(),
            user_move_uci=move.uci(), user_move_san=board.san(move),
            category=self.category, loss_cp=self.loss,
            best_move_uci=best.uci() if self.complete else None,
            best_move_san=board.san(best) if self.complete else None,
            eval_before=Score("cp", 25) if self.complete else None,
            eval_after=Score("cp", 25 - self.loss) if self.complete else None,
            best_pv_san=[board.san(best)], reply_pv_san=[], depth=depth or 8,
        )

    def close(self):
        pass


class FixedTeacher:
    name = "qwen"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, max_tokens=None, timeout=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "timeout": timeout})
        return json.dumps(self.payload)


class MalformedTeacher:
    name = "qwen"

    def __init__(self):
        self.calls = []

    def complete(self, messages, max_tokens=None, timeout=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "timeout": timeout})
        return "not JSON"


def judgment(assessment, *, recognized=(), missing=(), misconceptions=(), confidence="high"):
    return {
        "assessment": assessment,
        "confidence": confidence,
        "recognized_fact_ids": list(recognized),
        "missing_fact_ids": list(missing),
        "misconception_fact_ids": list(misconceptions),
    }


def issue(client, quality="good"):
    response = client.post("/api/puzzles/decision/next", json={"quality": quality})
    assert response.status_code == 200, response.text
    return response.json()


def answer(client, task, choice="yes", explanation="The best move wins material in the line."):
    return client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                       json={"choice": choice, "explanation": explanation})


def _record(profile, index, *, concept="knight_fork", assessment="correct", judgment_correct=True,
            difficulty=720, confidence=None, selection_reason="broad_practice", assessment_reason=None):
    identity = hashlib.sha256(f"{concept}:{index}".encode()).hexdigest()
    if confidence is None:
        confidence = "low" if assessment == "uncertain" else "high"
    return record_decision_result(
        profile, identity=identity, source_id=f"test:{concept}:{index}", concept=concept,
        difficulty=difficulty, metadata_rating=difficulty, engine_bonus=0,
        judgment_correct=judgment_correct,
        selected_choice="yes" if judgment_correct else "no",
        explanation_assessment=assessment, explanation_confidence=confidence,
        assessment_source="qwen" if assessment in {"correct", "partial", "incorrect"} else "qwen_unavailable",
        assessment_reason=assessment_reason,
        engine_category="excellent" if judgment_correct else "blunder",
        recognized_fact_ids=["best_move"] if assessment in {"correct", "partial"} else [],
        missing_fact_ids=["verified_idea"] if assessment == "partial" else [],
        misconception_fact_ids=["best_move"] if assessment == "incorrect" else [],
        selection_target=difficulty, selection_reason=selection_reason,
    )


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
    assert {candidate.side_to_move for candidate in candidates} == {"white", "black"}
    for candidate in candidates:
        board, move, san = validate_position_move(candidate.fen, candidate.move_uci)
        assert board.is_valid() and move in board.legal_moves and san == candidate.move_san
        assert candidate.example.status == "verified"
        assert candidate.example.tier != "personal"
        assert 400 <= candidate.rating <= 2400
        assert candidate.metadata_rating > 0


def test_candidate_pool_is_cached_and_invalidated_when_library_changes(monkeypatch):
    knowledge = get_knowledge()
    first = decision_candidates(knowledge)
    second = decision_candidates(knowledge)
    assert first == second
    assert first is not second  # callers cannot mutate the cached list

    candidate = first[0]
    original = candidate.example.status
    candidate.example.status = "needs_review"
    try:
        rebuilt = decision_candidates(knowledge)
        assert len(rebuilt) < len(first)
    finally:
        candidate.example.status = original
        clear_decision_candidate_cache()


def test_color_swapped_and_other_rules_preserving_symmetries_share_identity():
    start = chess.Board()
    white_move = chess.Move.from_uci("e2e4")
    black = start.mirror()
    black_move = chess.Move.from_uci("e7e5")
    common = {"concept": "center_control", "concepts": ["center_control"],
              "category": "strategy", "kind": "good"}
    assert canonical_task_identity(start.fen(), white_move.uci(), **common) == \
        canonical_task_identity(black.fen(), black_move.uci(), **common)

    no_castling = chess.Board("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    rook_move = chess.Move.from_uci("h1h2")
    file_mirror = no_castling.transform(chess.flip_horizontal)
    mirrored_move = chess.Move.from_uci("a1a2")
    assert canonical_task_identity(no_castling.fen(), rook_move.uci(), **common) == \
        canonical_task_identity(file_mirror.fen(), mirrored_move.uci(), **common)


def test_identity_preserves_state_and_task_semantics_that_are_not_symmetries():
    board = chess.Board()
    move = "e2e4"
    baseline = canonical_task_identity(board.fen(), move, concept="fork", kind="good")
    different_concept = canonical_task_identity(board.fen(), move, concept="pin", kind="good")
    different_quality = canonical_task_identity(board.fen(), move, concept="fork", kind="bad")
    later_clock = board.copy()
    later_clock.halfmove_clock = 2
    different_clock = canonical_task_identity(later_clock.fen(), move, concept="fork", kind="good")
    assert len({baseline, different_concept, different_quality, different_clock}) == 4


def test_real_stockfish_confirms_a_candidate_when_available(monkeypatch):
    knowledge = get_knowledge()
    candidates = decision_candidates(knowledge)
    example = next(candidate.example for candidate in candidates
                   if candidate.example.id == "mate_fools_mate")

    class OneExampleKnowledge:
        concepts = knowledge.concepts

        def examples_for(self, concept):
            return [example] if concept == example.concept else []

    class ShallowEngine:
        def evaluate_move(self, board, move):
            return real_engine.evaluate_move(board, move, depth=6)

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


def test_next_task_is_hidden_until_answer_and_has_explicit_mover_context(api_client):
    engine = ScriptedEngine(Classification.EXCELLENT, 5)
    set_engine(engine)
    task = issue(api_client)

    board = chess.Board(task["fen"])
    assert board.is_valid()
    assert board.parse_san(task["proposed_move"]) in board.legal_moves
    assert task["side_to_move"] == ("white" if board.turn else "black")
    assert task["mover"] == task["side_to_move"].title()
    assert task["mover"] in task["question"] and task["proposed_move"] in task["question"]
    assert "engine" not in task and "best_move" not in task and "concept" not in task
    assert len(engine.calls) == 1
    profile = get_profile()
    assert profile.decision_puzzles["history"][-1]["identity"]
    assert profile.decision_puzzles["history"][-1]["result"] is None


def test_stockfish_scores_yes_no_independently_of_qwen_explanation(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.BLUNDER, 480))
    task = issue(api_client, "bad")
    teacher = FixedTeacher(judgment("correct", recognized=("best_move", "proposed_line")))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    response = answer(api_client, task, "yes", "Black's best defense is the engine's line after the move.")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["judgment"] == {"choice": "yes", "correct": False, "expected": "no",
                                  "side_to_move": task["side_to_move"]}
    assert result["explanation_assessment"] == "correct"  # separate from the incorrect button
    assert result["assessment_status"] == "graded" and result["assessment_reason"] is None
    assert result["engine"]["category"] == "blunder"
    assert result["engine"]["quality"] == "bad"
    assert result["engine"]["loss_cp"] == 480
    assert result["engine"]["best_move"]
    assert result["engine"]["proposed_line"][0] == task["proposed_move"]
    assert result["recorded"] is True and result["reasoning_recorded"] is True
    assert len(teacher.calls) == 1
    message = json.dumps(teacher.calls[0]["messages"])
    assert task["fen"] not in message and task["mover"] in message
    assert "separate Yes/No choice" in message
    assert teacher.calls[0]["max_tokens"] <= 160 and teacher.calls[0]["timeout"] <= 5
    profile = get_profile()
    assert result["concept"] in profile.concepts
    event = profile.exercise_log[-1]
    assert event["source"] == "decision_puzzle" and event["exercise_id"].startswith("decision:")
    assert event["decision"]["selected_choice"] == "yes"
    assert event["decision"]["judgment_correct"] is False
    assert event["decision"]["explanation_assessment"] == "correct"
    assert "answer" not in event and "learner_explanation" not in event


def test_completed_task_replays_only_an_identical_answer_fingerprint(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 12))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("correct", recognized=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    explanation = "The move preserves the position's coordination and best continuation."

    first = answer(api_client, task, "yes", explanation)
    profile = get_profile()
    attempts = profile.decision_puzzles["attempts_total"]
    replay = answer(api_client, task, "yes", explanation)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert len(teacher.calls) == 1
    assert get_profile().decision_puzzles["attempts_total"] == attempts
    assert answer(api_client, task, "no", explanation).status_code == 409
    assert answer(api_client, task, "yes", explanation + " (a new reason)").status_code == 409
    assert get_profile().decision_puzzles["attempts_total"] == attempts


def test_actual_qwen_http_client_completes_the_full_grading_pipeline_with_paraphrased_reason(
        api_client, monkeypatch):
    from app.config import Settings
    from app.teacher.qwen import QwenTeacher
    import app.teacher.qwen as qwen_module

    set_engine(ScriptedEngine(Classification.GOOD, 12))
    task = issue(api_client)
    settings = Settings()
    settings.qwen_model = "qwen3:1.7b-test-double"
    settings.qwen_base_url = "http://127.0.0.1:11434/v1"
    settings.qwen_thinking = "off"
    settings.decision_assessment_timeout = 4.0
    settings.decision_assessment_max_tokens = 140
    teacher = QwenTeacher(settings)
    capture = {}

    def fake_post(url, **kwargs):
        payload = kwargs["json"]
        request = json.loads(payload["messages"][1]["content"].split("\n\n/no_think")[0])
        capture.update(url=url, payload=payload, request=request, timeout=kwargs["timeout"])
        grade = judgment("correct", recognized=("best_move",))
        return httpx.Response(200, request=httpx.Request("POST", url),
                               json={"choices": [{"message": {"content": json.dumps(grade)}}]})

    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    monkeypatch.setattr(qwen_module.httpx, "post", fake_post)
    explanation = f"{task['mover']}'s move keeps the position coordinated, and the engine's continuation supports that plan."
    started = time.perf_counter()
    result = answer(api_client, task, "yes", explanation).json()
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0  # deterministic fake endpoint; model inference is not included
    assert result["assessment_status"] == "graded"
    assert result["assessment_reason"] is None
    assert result["explanation_assessment"] == "correct"
    assert result["judgment"]["correct"] is True
    assert capture["request"]["learner_explanation_untrusted"] == explanation
    assert capture["request"]["proposed_move"] == task["proposed_move"]
    assert capture["request"]["mover"] == task["mover"]
    assert "best_line" in capture["request"]["evidence_by_fact_id"]
    assert capture["payload"]["max_tokens"] == 140
    assert isinstance(capture["timeout"], httpx.Timeout)
    assert capture["timeout"].read == pytest.approx(4.0)
    assert capture["timeout"].connect == pytest.approx(1.0)


def test_unconfigured_model_is_the_reproduced_reason_for_not_graded_explanations(api_client, monkeypatch):
    from app.config import Settings

    settings = Settings()
    settings.qwen_model = ""
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    set_engine(ScriptedEngine(Classification.GOOD, 12))
    task = issue(api_client)

    result = answer(api_client, task, "yes", "The move keeps pressure on the center.").json()

    assert result["judgment"]["correct"] is True
    assert result["assessment_status"] == "unavailable"
    assert result["assessment_reason"] == "model_not_configured"
    assert result["assessment_source"] == "fallback"
    assert result["reasoning_recorded"] is False
    assert "QWEN_MODEL is not configured" in result["explanation_feedback"]
    saved = get_profile().decision_puzzles["history"][-1]["result"]
    assert saved["assessment_reason"] == "model_not_configured"
    assert saved["reasoning_assessed"] is False


def test_qwen_connection_failure_keeps_objective_result_and_reports_cause(api_client, monkeypatch):
    from app.config import Settings
    from app.teacher.qwen import QwenTeacher
    import app.teacher.qwen as qwen_module

    settings = Settings()
    settings.qwen_model = "qwen-test"
    settings.qwen_base_url = "http://127.0.0.1:11434/v1"
    teacher = QwenTeacher(settings)
    set_engine(ScriptedEngine(Classification.GOOD, 12))
    task = issue(api_client)

    def refused(url, **kwargs):
        raise httpx.ConnectError("private connection details", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    monkeypatch.setattr(qwen_module.httpx, "post", refused)
    result = answer(api_client, task, "yes", "The move keeps the center under control.").json()

    assert result["judgment"]["correct"] is True
    assert result["assessment_status"] == "unavailable"
    assert result["assessment_reason"] == "endpoint_unreachable"
    assert result["reasoning_recorded"] is False
    assert "could not be reached" in result["explanation_feedback"]
    assert "private connection details" not in result["explanation_feedback"]


def test_assessment_parser_accepts_a_single_json_fence_and_normalizes_only_known_labels():
    evidence = {"best_move": "Stockfish's best move is Nf3.",
                "verified_idea": "The move wins a tempo.", "_objective_quality": "good"}
    payload = judgment("Partially correct", recognized=("best_move",), missing=("verified_idea",),
                       confidence="MEDIUM")
    parsed = _parse_assessment(f"```json\n{json.dumps(payload)}\n```", evidence)
    assert parsed["assessment"] == "partial"
    assert parsed["confidence"] == "medium"
    assert parsed["recognized_fact_ids"] == ["best_move"]
    assert _parse_assessment(json.dumps({**payload, "assessment": "maybe"}), evidence) is None
    assert _parse_assessment("Here is the grade: " + json.dumps(payload), evidence) is None
    assert _parse_assessment(json.dumps({**payload, "recognized_fact_ids": ["made_up_fact"]}), evidence) is None


def _single_example_knowledge(example, concepts):
    class OneExampleKnowledge:
        def __init__(self):
            self.concepts = concepts

        def examples_for(self, _concept):
            return [example]

    return OneExampleKnowledge()


@pytest.mark.parametrize("side", ["white", "black"])
def test_yes_no_is_relative_to_the_side_to_move_for_both_colors(side, monkeypatch):
    knowledge = get_knowledge()
    candidate = next(item for item in decision_candidates(knowledge)
                     if item.side_to_move == side and item.kind == "good")
    engine = ScriptedEngine(Classification.GOOD, 10)
    store = DecisionTaskStore()
    profile = get_profile()
    task = issue_decision_task(_single_example_knowledge(candidate.example, knowledge.concepts), engine,
                               profile=profile, store=store, concept=candidate.concept, quality="good")
    assert task.candidate.side_to_move == side
    teacher = FixedTeacher(judgment("correct", recognized=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    result = submit_decision_answer(task.task_id, "yes", "The best move wins the key material.", store=store)
    assert result["judgment"]["correct"] is True
    assert result["judgment"]["expected"] == "yes"
    assert result["engine"]["mover"] == side.title()
    prompt = json.loads(teacher.calls[0]["messages"][1]["content"])
    assert prompt["side_to_move"] == side
    assert prompt["mover"] == side.title()


def test_partial_explanation_is_separate_and_receives_partial_profile_score(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 35))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("partial", recognized=("engine_verdict",),
                                     missing=("best_move",), confidence="medium"))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "yes", "I think it is good, but I am missing the exact line.").json()
    state = get_profile().concepts[result["concept"]]
    assert result["judgment"]["correct"] is True
    assert result["explanation_assessment"] == "partial" and result["reasoning_recorded"] is True
    assert state.recent[-1] == pytest.approx(0.798)
    assert "Partly correct" in result["explanation_feedback"]


def test_incorrect_explanation_requires_specific_contradictory_evidence(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 0))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("incorrect", recognized=("engine_verdict",),
                                     misconceptions=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    result = answer(api_client, task, "yes", "It is good because the engine has no better move.").json()
    assert result["judgment"]["correct"] is True
    assert result["explanation_assessment"] == "incorrect"
    assert result["reasoning_recorded"] is True
    assert "Not supported" in result["explanation_feedback"]

    # An unexplained "incorrect" label cannot become a learner mistake.
    unsupported = {"assessment": "incorrect", "confidence": "high", "recognized_fact_ids": [],
                   "missing_fact_ids": [], "misconception_fact_ids": []}
    evidence = {"best_move": "Stockfish's best move is Nf3.", "_objective_quality": "good"}
    assert _classify_explanation(unsupported)["assessment"] == "uncertain"


def test_ambiguous_explanation_is_not_a_reasoning_miss_but_choice_is_persisted(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("uncertain", confidence="low"))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "no", "Maybe, I'm not sure what I see.").json()
    assert result["explanation_assessment"] == "uncertain"
    assert result["judgment"]["correct"] is False
    assert result["recorded"] is True and result["reasoning_recorded"] is False
    assert "uncertain" in result["explanation_feedback"].lower()
    assert "not scored" in result["explanation_feedback"].lower()
    assert get_profile().decision_puzzles["history"][-1]["result"]["explanation_assessment"] == "uncertain"
    weakness = get_profile().decision_puzzles["weaknesses"][result["concept"]]
    assert weakness["observed_misses"] == 1  # the objective choice is wrong for this verified task
    assert weakness["choice_misses"] == 1
    assert weakness["reasoning_misses"] == 0
    assert weakness["assessable_attempts"] == 0


def test_irrelevant_explanation_is_uncertain_not_an_incorrect_reasoning_grade(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("uncertain", confidence="low"))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "yes", "The weather is sunny and I enjoy this color.").json()

    assert result["judgment"]["correct"] is True
    assert result["assessment_status"] == "uncertain"
    assert result["assessment_reason"] == "insufficient_evidence"
    assert result["explanation_assessment"] == "uncertain"
    assert result["reasoning_recorded"] is False
    weakness = get_profile().decision_puzzles["weaknesses"][result["concept"]]
    assert weakness["reasoning_misses"] == 0
    assert weakness["observed_misses"] == 0


def test_invalid_model_label_is_not_converted_into_a_reasoning_miss(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("good", recognized=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    result = answer(api_client, task, "yes", "This keeps the pieces active and coordinated.").json()

    assert result["assessment_status"] == "unavailable"
    assert result["assessment_reason"] == "invalid_grading_response"
    assert result["reasoning_recorded"] is False
    assert get_profile().decision_puzzles["weaknesses"][result["concept"]]["reasoning_misses"] == 0


def test_unavailable_fallback_keeps_stockfish_truth_without_inventing_reasoning(api_client, monkeypatch):
    from app.teacher import FallbackTeacher

    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    monkeypatch.setattr("app.teacher.get_teacher", lambda: FallbackTeacher())
    before = get_profile().stats["puzzles"]["attempts"]

    result = answer(api_client, task, "yes").json()
    assert result["explanation_assessment"] == "unavailable"
    assert result["assessment_status"] == "unavailable"
    assert result["assessment_reason"] == "model_not_configured"
    assert result["assessment_source"] == "fallback"
    assert result["recorded"] is True and result["reasoning_recorded"] is False
    assert result["engine"]["quality"] == "good" and result["engine"]["best_move"]
    assert "qwen_model is not configured" in result["explanation_feedback"].lower()
    assert "not scored" in result["explanation_feedback"].lower()
    assert get_profile().stats["puzzles"]["attempts"] == before + 1
    assert get_profile().decision_puzzles["weaknesses"][result["concept"]]["observed_misses"] == 0


def test_malformed_or_failed_model_output_uses_honest_unassessed_fallback(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    malformed = MalformedTeacher()
    monkeypatch.setattr("app.teacher.get_teacher", lambda: malformed)
    invalid = answer(api_client, task, "yes").json()
    assert invalid["explanation_assessment"] == "unavailable"
    assert invalid["assessment_status"] == "unavailable"
    assert invalid["assessment_reason"] == "invalid_grading_response"
    assert invalid["assessment_source"] == "qwen_invalid"
    assert invalid["recorded"] is True and invalid["reasoning_recorded"] is False
    assert malformed.calls[0]["timeout"] <= 5
    assert malformed.calls[0]["max_tokens"] <= 160

    class FailingTeacher:
        name = "qwen"

        def complete(self, messages, max_tokens=None, timeout=None):
            raise TimeoutError("model deadline")

    set_engine(ScriptedEngine(Classification.BLUNDER, 480))
    second = issue(api_client, "bad")
    monkeypatch.setattr("app.teacher.get_teacher", lambda: FailingTeacher())
    failed = answer(api_client, second, "no").json()
    assert failed["explanation_assessment"] == "unavailable"
    assert failed["assessment_status"] == "unavailable"
    assert failed["assessment_reason"] == "timeout"
    assert failed["assessment_source"] == "qwen_unavailable"
    assert failed["engine"]["quality"] == "bad"


def test_model_response_timeout_is_distinct_from_a_connection_failure(api_client, monkeypatch):
    from app.config import Settings
    from app.teacher.qwen import QwenTeacher
    import app.teacher.qwen as qwen_module

    settings = Settings()
    settings.qwen_model = "qwen-test"
    settings.qwen_base_url = "http://127.0.0.1:11434/v1"
    teacher = QwenTeacher(settings)
    set_engine(ScriptedEngine(Classification.GOOD, 12))
    task = issue(api_client)

    def slow_response(url, **kwargs):
        raise httpx.ReadTimeout("private model timing", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)
    monkeypatch.setattr(qwen_module.httpx, "post", slow_response)
    result = answer(api_client, task, "yes", "The move keeps the center under control.").json()

    assert result["assessment_status"] == "unavailable"
    assert result["assessment_reason"] == "response_timeout"
    assert result["judgment"]["correct"] is True
    assert result["reasoning_recorded"] is False
    assert "DECISION_ASSESSMENT_TIMEOUT" in result["explanation_feedback"]
    assert "private model timing" not in result["explanation_feedback"]


def test_answer_is_idempotent_and_unknown_or_invalid_submissions_fail(api_client, monkeypatch):
    set_engine(ScriptedEngine(Classification.GOOD, 0))
    task = issue(api_client)
    teacher = FixedTeacher(judgment("correct", recognized=("best_move",)))
    monkeypatch.setattr("app.teacher.get_teacher", lambda: teacher)

    first = answer(api_client, task, "yes")
    repeated = answer(api_client, task, "yes")
    assert repeated.status_code == first.status_code == 200
    assert repeated.json() == first.json()
    assert answer(api_client, task, "no", "A different answer must not overwrite the graded result.").status_code == 409
    assert len(teacher.calls) == 1
    assert get_profile().stats["puzzles"]["attempts"] == 1
    assert api_client.post("/api/puzzles/decision/missing/answer",
                           json={"choice": "yes", "explanation": "Long enough explanation."}).status_code == 404
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"choice": "maybe", "explanation": "Long enough explanation."}).status_code == 422
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"choice": "yes", "explanation": "short"}).status_code == 422
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"choice": "yes", "explanation": ""}).status_code == 422
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"choice": "yes", "explanation": "        "}).status_code == 422
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"choice": "yes", "explanation": "x" * 2001}).status_code == 422
    assert api_client.post(f"/api/puzzles/decision/{task['task_id']}/answer",
                           json={"explanation": "Long enough explanation."}).status_code == 422


def test_issue_persists_selection_reason_and_target_in_existing_profile(api_client):
    set_engine(ScriptedEngine(Classification.GOOD, 10))
    task = issue(api_client)
    entry = get_profile().decision_puzzles["history"][-1]
    assert entry["identity"]
    assert entry["selection_reason"] in {"targeted_practice", "broad_practice"}
    assert isinstance(entry["selection_target"], int)
    assert entry["result"] is None
    assert task["selection_note"]


def test_decision_profile_endpoint_explains_prior_and_thresholds(api_client):
    response = api_client.get("/api/puzzles/decision/profile")
    assert response.status_code == 200
    data = response.json()
    assert data["ability"]["rating"] == 800
    assert data["ability"]["confidence"] == pytest.approx(0.05)
    assert data["current_difficulty"] == 720
    assert data["selection_policy"]["weakness_threshold"]["independent_tasks"] == 5
    assert data["selection"]["concept_target"]["mix_current_and_concept_rating"] == [0.7, 0.3]
    assert data["selection"]["focus_cycle"]["targeted_exposures_before_broad_practice"] == 2
    assert data["selection"]["variety_penalties"]["same_as_last_concept"] == 55


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
    assert "complete evaluation" in response.json()["error"] or "complete evaluation" in response.json()["error"].lower()


def test_unknown_concept_is_rejected(api_client):
    response = api_client.post("/api/puzzles/decision/next", json={"concept": "not_a_chess_concept"})
    assert response.status_code == 404


def test_exhausted_pools_report_conflict_instead_of_repeating(api_client):
    knowledge = get_knowledge()
    candidate = next(item for item in decision_candidates(knowledge) if item.kind == "good")
    profile = get_profile()
    profile.decision_puzzles["history"] = [{"identity": candidate.identity, "concept": candidate.concept,
                                             "result": None}]
    engine = ScriptedEngine(Classification.GOOD, 0)
    set_engine(engine)
    # Restricting the request to the candidate's concept yields a fully exhausted pool.
    class OneExampleKnowledge:
        concepts = knowledge.concepts

        def examples_for(self, _concept):
            return [candidate.example]

    with pytest.raises(DecisionPuzzleError) as error:
        issue_decision_task(OneExampleKnowledge(), engine, profile=profile,
                            store=DecisionTaskStore(), concept=candidate.concept, quality="good")
    assert error.value.status_code == 409
    assert "including color-swapped equivalents" in str(error.value)
    assert engine.calls == []


def test_candidate_engine_analysis_cache_is_exact_and_bound_to_engine_config():
    from types import SimpleNamespace
    from app.engine import UciEngine

    class ProbeUciEngine(UciEngine):
        def __init__(self):
            self.settings = SimpleNamespace(engine_cmd=["stockfish"], engine_depth=14)
            self.calls = []
            self.category = Classification.GOOD
            self.loss = 5
            self.complete = True

        def evaluate_move(self, board, move, depth=None, timeout=None):
            return ScriptedEngine.evaluate_move(self, board, move, depth)

    engine = ProbeUciEngine()
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    from app.config import get_settings
    settings = get_settings()
    old_depth, old_timeout = settings.decision_engine_depth, settings.decision_engine_timeout
    settings.decision_engine_depth, settings.decision_engine_timeout = 10, 6
    try:
        first = _evaluate_move_cached(engine, board, move)
        second = _evaluate_move_cached(engine, board.copy(), move)
        assert len(engine.calls) == 1
        assert first is not second  # callers receive independent result objects
        different_clock = board.copy()
        different_clock.halfmove_clock = 1
        _evaluate_move_cached(engine, different_clock, move)
        assert len(engine.calls) == 2
    finally:
        settings.decision_engine_depth, settings.decision_engine_timeout = old_depth, old_timeout
        clear_decision_engine_cache()


def test_new_learner_has_sensible_baseline_and_rating_is_only_a_rough_prior():
    new = LearnerProfile()
    data = ensure_decision_profile(new)
    assert data["ability"]["rating"] == 800
    assert data["ability"]["prior_source"] == "baseline"
    assert data["ability"]["confidence"] == pytest.approx(0.05)
    assert data["current_difficulty"] == 720

    rated = LearnerProfile()
    rated.set_onboarding(rating=1650, platform="chesscom")
    prior = ensure_decision_profile(rated)
    assert prior["ability"]["rating"] == rated.rating == 1650
    assert prior["ability"]["prior_source"] == "chesscom_rating"
    assert prior["ability"]["confidence"] < 0.2
    assert prior["current_difficulty"] == 1570


def test_sustained_success_increases_difficulty_one_small_step_at_a_time():
    profile = LearnerProfile()
    data = ensure_decision_profile(profile)
    baseline = data["current_difficulty"]
    initial_ability = data["ability"]["rating"]
    for index in range(4):
        _record(profile, index, assessment="correct", judgment_correct=True)
    data = profile.decision_puzzles
    assert data["current_difficulty"] == baseline + 50
    assert data["ability"]["rating"] > initial_ability
    assert data["ability"]["confidence"] > 0.05
    for index in range(4, 7):
        _record(profile, index, assessment="correct", judgment_correct=True)
    assert profile.decision_puzzles["current_difficulty"] == baseline + 100


def test_repeated_mistakes_lower_difficulty_but_one_or_two_do_not():
    profile = LearnerProfile()
    baseline = ensure_decision_profile(profile)["current_difficulty"]
    _record(profile, 0, assessment="incorrect", judgment_correct=False)
    _record(profile, 1, assessment="incorrect", judgment_correct=False)
    assert profile.decision_puzzles["current_difficulty"] == baseline
    assert profile.decision_puzzles["weaknesses"]["knight_fork"]["status"] == "insufficient"
    _record(profile, 2, assessment="incorrect", judgment_correct=False)
    _record(profile, 3, assessment="incorrect", judgment_correct=False)
    assert profile.decision_puzzles["current_difficulty"] == baseline - 50
    assert profile.decision_puzzles["ability"]["rating"] < 800


def test_mixed_results_leave_current_difficulty_stable():
    profile = LearnerProfile()
    baseline = ensure_decision_profile(profile)["current_difficulty"]
    for index, (assessment, correct) in enumerate((
        ("correct", True), ("incorrect", False), ("correct", True), ("incorrect", False),
    )):
        _record(profile, index, assessment=assessment, judgment_correct=correct)
    assert profile.decision_puzzles["current_difficulty"] == baseline


def test_unavailable_explanations_do_not_enter_difficulty_windows_or_erase_choice_evidence():
    profile = LearnerProfile()
    baseline = ensure_decision_profile(profile)["current_difficulty"]
    for index in range(4):
        _record(profile, index, assessment="unavailable", judgment_correct=False)
    assert profile.decision_puzzles["current_difficulty"] == baseline
    assert profile.decision_puzzles["ability"]["evidence"] == 4
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["choice_misses"] == 4
    assert weakness["reasoning_misses"] == 0

    # Only assessable explanations start/advance the four-result difficulty window.
    for index in range(4, 8):
        _record(profile, index, assessment="correct", judgment_correct=True)
    assert profile.decision_puzzles["current_difficulty"] == baseline + 50


def test_one_or_two_misses_do_not_create_a_confirmed_weakness():
    profile = LearnerProfile()
    _record(profile, 0, assessment="incorrect", judgment_correct=False)
    _record(profile, 1, assessment="incorrect", judgment_correct=False)
    item = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert item["status"] == "insufficient"
    assert item["observed_misses"] == 2
    assert item["confidence"] < 0.5


def test_repeated_independent_mistakes_create_weakness_with_auditable_evidence():
    profile = LearnerProfile()
    for index in range(5):
        _record(profile, index, assessment="incorrect", judgment_correct=False)
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["status"] == "needs_work"
    assert weakness["independent_tasks"] == 5
    assert weakness["observed_misses"] == 5
    assert weakness["sources"]["decision_puzzles"]["recent_evidence"]
    assert len({e["task"] for e in weakness["sources"]["decision_puzzles"]["recent_evidence"]}) == 5
    assert weakness["trend"] in ("insufficient", "stable", "slipping")


def test_recurring_game_evidence_can_corroborate_but_not_replace_puzzle_evidence():
    profile = LearnerProfile()
    profile.weaknesses = [{"concept": "knight_fork", "key": "fork_missed", "tier": "recurring",
                           "game_count": 3, "occurrences": 4, "updated": "2026-01-01T00:00:00+00:00"}]
    _record(profile, 0, assessment="incorrect", judgment_correct=False)
    _record(profile, 1, assessment="incorrect", judgment_correct=False)
    assert profile.decision_puzzles["weaknesses"]["knight_fork"]["status"] == "insufficient"
    _record(profile, 2, assessment="incorrect", judgment_correct=False)
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["status"] == "needs_work"
    assert weakness["threshold"]["game_corroboration"] is True
    assert weakness["sources"]["analyzed_games"]["game_count"] == 3


def test_consistent_success_reduces_weakness_confidence_and_can_mark_skill_mastered():
    profile = LearnerProfile()
    for index in range(5):
        _record(profile, index, assessment="incorrect", judgment_correct=False)
    before = profile.decision_puzzles["weaknesses"]["knight_fork"]["confidence"]
    for index in range(5, 10):
        _record(profile, index, assessment="correct", judgment_correct=True)
    after = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert after["confidence"] < before
    assert after["trend"] == "improving"
    assert after["status"] == "mastered"
    assert "knight_fork" in profile.decision_puzzles["strengths"]


def test_targeted_practice_exposures_track_per_skill_improvement():
    profile = LearnerProfile()
    for index in range(5):
        _record(profile, index, assessment="incorrect", judgment_correct=False,
                selection_reason="targeted_practice")
    for index in range(5, 10):
        _record(profile, index, assessment="correct", judgment_correct=True,
                selection_reason="targeted_practice")

    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    targeted = weakness["practice_effectiveness"]["targeted"]
    assert weakness["status"] == "mastered"
    assert targeted["attempts"] == 10
    assert targeted["mean_score"] == pytest.approx(0.5)
    assert targeted["trend"] == "improving"
    assert profile.decision_puzzles["recent"][-1]["selection_reason"] == "targeted_practice"


def test_uncertain_or_unavailable_explanations_never_confirm_reasoning_weakness():
    profile = LearnerProfile()
    for index in range(7):
        _record(profile, index, assessment="uncertain", judgment_correct=True, confidence="low")
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["status"] == "insufficient"
    assert weakness["assessable_attempts"] == 0
    assert weakness["observed_misses"] == 0
    assert weakness["reasoning_misses"] == 0
    assert profile.concepts["knight_fork"].recent[-1] == pytest.approx(0.75)


def test_uncertain_reasoning_is_not_a_miss_but_repeated_stockfish_choice_errors_are():
    profile = LearnerProfile()
    for index in range(5):
        _record(profile, index, assessment="uncertain", judgment_correct=False, confidence="low")
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["status"] == "needs_work"
    assert weakness["assessable_attempts"] == 0
    assert weakness["observed_misses"] == 5
    assert weakness["choice_misses"] == 5
    assert weakness["reasoning_misses"] == 0


def test_decision_profile_persists_through_learner_store_restart(tmp_path):
    root = tmp_path / "learners"
    store = LearnerStore(root)
    profile = store.get("local")
    profile.set_onboarding(rating=1420, platform="chesscom")
    for index in range(5):
        _record(profile, index, assessment="incorrect", judgment_correct=False)
    _record(profile, 5, assessment="unavailable", judgment_correct=True,
            assessment_reason="endpoint_unreachable")
    summary = decision_profile_summary(profile)
    store.save(profile)

    restarted = LearnerStore(root)
    loaded = restarted.get("local")
    loaded_summary = decision_profile_summary(loaded)
    assert loaded_summary["ability"] == summary["ability"]
    assert loaded_summary["current_difficulty"] == summary["current_difficulty"]
    assert loaded_summary["weaknesses"][0]["status"] == "needs_work"
    assert len(loaded.decision_puzzles["history"]) == 6
    assert loaded.decision_puzzles["history"][-1]["result"]["assessment_reason"] == "endpoint_unreachable"
    assert loaded_summary["recent"][-1]["assessment_reason"] == "endpoint_unreachable"
    assert loaded.decision_puzzles["weaknesses"]["knight_fork"]["sources"]["decision_puzzles"]["attempts"] == 6


def test_future_task_selection_uses_saved_weakness_after_profile_reload(tmp_path):
    knowledge = get_knowledge()
    candidates = decision_candidates(knowledge)
    pair = []
    for item in candidates:
        if item.kind != "good":
            continue
        if not pair or item.concept != pair[0].concept:
            pair = [item] if not pair else [pair[0], item]
        if len(pair) == 2 and pair[0].concept != pair[1].concept:
            break
    assert len(pair) == 2
    weak_candidate, other_candidate = pair
    profile_store = LearnerStore(tmp_path / "learners")
    profile = profile_store.get("local")
    for index in range(5):
        _record(profile, index, concept=weak_candidate.concept, assessment="incorrect", judgment_correct=False,
                difficulty=weak_candidate.rating)
    data = profile.decision_puzzles
    # Fill recent exposure slots with broad practice so the target scheduler deliberately returns to the
    # supported weakness rather than over-serving it without variety.
    for index in range(4):
        record_decision_exposure(profile, identity=hashlib.sha256(f"breadth:{index}".encode()).hexdigest(),
                                 source_id=f"breadth:{index}", concept=other_candidate.concept,
                                 difficulty=other_candidate.rating, metadata_rating=other_candidate.metadata_rating)
    data["current_difficulty"] = int((weak_candidate.rating + other_candidate.rating) / 2)
    profile_store.save(profile)
    restarted_store = LearnerStore(tmp_path / "learners")
    loaded = restarted_store.get("local")

    class SmallKnowledge:
        concepts = knowledge.concepts

        def verified(self):
            return list({item.example.id: item.example for item in pair}.values())

    # Use the reloaded profile's saved weakness and a tiny verified pool; no unrelated learner state is read.
    task_store = DecisionTaskStore()
    engine = ScriptedEngine(Classification.GOOD, 10)
    task = issue_decision_task(SmallKnowledge(), engine, profile=loaded, store=task_store, quality="good")
    assert task.candidate.concept == weak_candidate.concept
    assert task.selection_reason == "targeted_practice"
    assert task.candidate.identity in {item.identity for item in pair}


def test_uncertain_choice_contradiction_is_recorded_separately_not_as_reasoning_weakness():
    profile = LearnerProfile()
    _record(profile, 0, assessment="correct", judgment_correct=False)
    weakness = profile.decision_puzzles["weaknesses"]["knight_fork"]
    assert weakness["status"] == "insufficient"
    assert weakness["observed_misses"] == 1
    assert weakness["choice_misses"] == 1
    assert weakness["reasoning_misses"] == 0
    assert profile.decision_puzzles["history"][-1]["result"]["judgment_correct"] is False
    assert profile.decision_puzzles["history"][-1]["result"]["explanation_assessment"] == "correct"
