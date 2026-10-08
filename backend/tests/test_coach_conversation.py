"""Multi-turn regressions for context-aware coach intent routing."""
from __future__ import annotations

from app.planner.intent.conversation import route_message


def reading(action, *, topic=None, mode=None, confidence=0.95, preserve=True, needs_engine=False,
            clarification=None):
    return {
        "action": action,
        "topic": topic,
        "subtopic": None,
        "mode": mode,
        "objective": None,
        "confidence": confidence,
        "preserve_context": preserve,
        "requires_engine": needs_engine,
        "clarification": clarification,
    }


def test_realistic_multi_turn_lesson_and_context_switches():
    """The old topic is temporary context, never an override for a clear new request."""
    prompts = {
        "hi": reading("greeting"),
        "I want to learn the Sicilian": reading("start_lesson", topic="Sicilian Defense", mode="lesson",
                                                preserve=False),
        "What is a pin?": reading("explanation", topic="pin", mode="explanation"),
        "Switch to the London System": reading("start_lesson", topic="London System", mode="lesson",
                                                preserve=False),
        "Give me a puzzle": reading("puzzle", mode="puzzle"),
        "work on pins": reading("clarify", topic="pin", clarification={
            "question": "Would you like an explanation of pins or positions to practice them?",
            "options": [
                {"id": "explain", "label": "Explain pins", "message": "Explain pins"},
                {"id": "practice", "label": "Practice pin positions", "message": "Give me a pin puzzle"},
            ],
        }),
        "Whose move is it on this board?": reading("board_question"),
        "Switch to the Italian Game": reading("start_lesson", topic="Italian Game", mode="lesson",
                                                preserve=False),
        "Let's switch to the Sicilian again": reading("start_lesson", topic="Sicilian Defense", mode="lesson",
                                                      preserve=False),
        "Why do bishops sometimes beat knights in open positions?": reading("general_chat"),
        "Continue the lesson": reading("continue_lesson", mode="lesson"),
    }
    seen = []

    def semantic(message, active_state, history):
        seen.append((message, active_state, history))
        return prompts[message]

    # 1. A greeting before any lesson remains ordinary conversation.
    empty = {}
    result = route_message("hi", state=empty, history=[], interpreter=semantic)
    assert result.action == "greeting"
    assert empty == {}  # routing is not a hidden plan/session mutation

    # 2. First explicit learning request starts Sicilian.
    result = route_message("I want to learn the Sicilian", state=None, history=[], interpreter=semantic)
    assert result.action == "start_lesson" and result.topic == "Sicilian Defense"
    sicilian = {
        "topic": result.topic, "topic_id": "sicilian_defense", "subtopic": "opening ideas",
        "mode": "lesson", "objective": "Understand the opening's plans", "stage": "teaching",
        "progress": {"current_step": 1, "total_steps": 4}, "difficulty": "beginner",
        "board_state_ref": {"kind": "session_board", "session_id": "sicilian-1"},
    }

    # 3. A quick concept question is temporary; the Sicilian remains active.
    result = route_message("What is a pin?", state=sicilian, history=[
        {"role": "user", "content": "I want to learn the Sicilian"},
        {"role": "assistant", "content": "Let's begin with the Sicilian."},
    ], interpreter=semantic)
    assert result.action == "explanation" and result.preserve_context
    assert result.topic == "pin" and sicilian["topic"] == "Sicilian Defense"

    # 4. A clear London request supersedes the active Sicilian lesson.
    result = route_message("Switch to the London System", state=sicilian, history=[], interpreter=semantic)
    assert result.action == "topic_change" and result.topic == "London System"
    london = {**sicilian, "topic": "London System", "topic_id": "london_system",
              "board_state_ref": {"kind": "session_board", "session_id": "london-1"}}

    # 5. An underspecified puzzle request inherits the active topic, not a stale earlier one.
    result = route_message("Give me a puzzle", state=london, history=[], interpreter=semantic)
    assert result.action == "puzzle" and result.topic == "London System"
    assert result.topic_id == "london_system" and result.mode == "puzzle"

    # 6. "Work on pins" is genuinely ambiguous and asks the requested concise clarification.
    result = route_message("work on pins", state=london, history=[], interpreter=semantic)
    assert result.action == "clarify" and result.preserve_context
    assert "explanation of pins or positions to practice" in result.clarification["question"]
    assert london["topic"] == "London System"

    # 7. The route carries only the board reference to the interpreter, never a browser FEN.
    result = route_message("Whose move is it on this board?", state={**london, "board_fen": "untrusted"},
                           history=[], interpreter=semantic)
    assert result.action == "board_question" and result.preserve_context
    board_context = seen[-1][1]
    assert board_context["board_state_ref"]["session_id"] == "london-1"
    assert "board_fen" not in board_context and "fen" not in board_context

    # 8. Repeated topic switches always use the new request's target.
    result = route_message("Switch to the Italian Game", state=london, history=[], interpreter=semantic)
    assert result.action == "topic_change" and result.topic == "Italian Game"
    italian = {**london, "topic": "Italian Game", "topic_id": "italian_game"}
    result = route_message("Let's switch to the Sicilian again", state=italian, history=[], interpreter=semantic)
    assert result.action == "topic_change" and result.topic == "Sicilian Defense"

    # 9. An unrelated chess question is answered in context without replacing the lesson;
    #    a later continuation resumes that same topic.
    result = route_message("Why do bishops sometimes beat knights in open positions?", state=sicilian,
                           history=[], interpreter=semantic)
    assert result.action == "general_chat" and result.preserve_context
    result = route_message("Continue the lesson", state=sicilian, history=[], interpreter=semantic)
    assert result.action == "continue_lesson" and result.topic == "Sicilian Defense"


def test_semantic_confidence_gate_clarifies_uncertain_new_topic():
    uncertain = reading("start_lesson", topic="London System", confidence=0.51, preserve=False)
    result = route_message("Maybe the London?", state={"topic": "Sicilian Defense"},
                           interpreter=lambda *_: uncertain)
    assert result.action == "clarify"
    assert result.preserve_context
    assert result.confidence == 0.51


def test_offline_fallback_never_turns_a_greeting_into_a_plan():
    result = route_message("hello")
    assert result.action == "greeting"
    assert result.preserve_context

    discovery = route_message("What can you teach me?")
    assert discovery.action == "discovery"

    active = {"topic": "Sicilian Defense", "topic_id": "sicilian_defense", "mode": "lesson"}
    switch = route_message("Actually, teach me the London System.", state=active)
    assert switch.action == "topic_change" and switch.topic == "London System"


def test_confident_fast_routes_skip_semantic_model_and_ambiguous_routes_do_not(monkeypatch):
    from app.planner.intent import conversation

    def no_model(*args, **kwargs):
        raise AssertionError("a high-confidence route should not call the semantic model")

    monkeypatch.setattr(conversation, "_semantic_reading", no_model)
    assert route_message("hi").source == "fast"
    assert route_message("What can you teach me?").source == "fast"

    active = {"topic": "Sicilian Defense", "topic_id": "sicilian_defense", "mode": "lesson"}
    legality = route_message("Why can't Black take that pawn?", state=active)
    assert legality.action == "board_question" and legality.source == "fast"
    assert not legality.requires_engine

    status = route_message("What is our lesson goal?", state=active, fast_only=True)
    assert status.action == "lesson_question" and status.source == "fast"
    assert status.topic == "Sicilian Defense" and status.preserve_context
    assert not status.requires_engine

    # An ordinary contextual chess question still receives a full answer stream, but the local
    # question classifier already proves it cannot mutate lesson state, so a second route model
    # call is unnecessary.
    question = route_message("Why do bishops sometimes beat knights in open positions?", state=active)
    assert question.action == "lesson_question" and question.source == "fast"
    assert question.topic == "Sicilian Defense" and question.preserve_context

    # Fast-only orchestration is deliberately conservative; genuine new-topic intent is left to
    # the existing semantic route (and its confidence-aware fallback).
    assert route_message("Teach me the Sicilian.", fast_only=True) is None


def test_board_questions_request_engine_facts_only_when_chess_strength_is_needed():
    active = {"topic": "Sicilian Defense", "topic_id": "sicilian_defense", "mode": "lesson"}
    # Engine-backed evaluations remain with the semantic router; the fast-only preflight must
    # not consume them before the existing route model sees the request.
    assert route_message("Why is this move good?", state=active, fast_only=True) is None
    move_quality = route_message("Why is this move good?", state=active)
    assert move_quality.action == "board_question" and move_quality.requires_engine

    # A semantic route that calls it a generic lesson question is corrected by the existing
    # high-confidence position-question classifier, then receives the required engine facts.
    semantic = reading("lesson_question", topic="Sicilian Defense", needs_engine=False)
    corrected = route_message("Why is this move good?", state=active,
                              interpreter=lambda *_: semantic)
    assert corrected.action == "board_question" and corrected.requires_engine

    legality = route_message("Why can't Black take that pawn?", state=active)
    assert legality.action == "board_question" and not legality.requires_engine


def test_offline_fallback_uses_active_context_for_puzzles_and_clarifies_work_on():
    active = {"topic": "Sicilian Defense", "topic_id": "sicilian_defense", "mode": "lesson"}
    mode = route_message("switch to practice mode", state=active)
    assert mode.action == "mode_change" and mode.mode == "practice"
    assert mode.topic == "Sicilian Defense" and mode.preserve_context

    resume = route_message("back to the Sicilian", state={"topic": "London System"})
    assert resume.action == "resume_lesson" and resume.topic.lower() in {"sicilian", "sicilian defense"}

    puzzle = route_message("give me a puzzle", state=active)
    assert puzzle.action == "puzzle"
    assert puzzle.topic == "Sicilian Defense"
    assert puzzle.topic_id == "sicilian_defense"

    pin_puzzle = route_message("give me a pin puzzle", state=active)
    assert pin_puzzle.action == "puzzle" and pin_puzzle.topic_id == "pin"

    board = route_message("Whose move is it?", state=active)
    assert board.action == "board_question" and not board.requires_engine

    ambiguous = route_message("work on pins", state=active)
    assert ambiguous.action == "clarify"
    assert "explanation of" in ambiguous.clarification["question"]
    assert "positions to practice" in ambiguous.clarification["question"]
