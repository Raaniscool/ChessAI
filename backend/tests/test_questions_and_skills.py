"""Questions are requests too ("what is en passant?" gets the verified en passant lesson, not
a text-only fallback); rules questions get instant answers; a *skill* like calculation gets
verified positions that actually train it, at the learner's level; the no-lesson message
reads cleanly."""
import statistics

import pytest

from app.knowledge.answers import quick_answer
from app.knowledge.difficulty import _learner_moves, puzzle_rating
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge
from app.knowledge.retrieval import resolve_concepts
from app.learner import LearnerProfile
from app.planner.planner import PlanError, create_plan, plan_for_goal
from app.planner.catalog import get_catalog


@pytest.fixture(scope="module")
def library():
    return get_knowledge()


def _examples(library):
    return {e.id: e for c in library.concepts for e in library.examples_for(c)}


@pytest.mark.parametrize("text,concept", [
    ("what is en passant", "en_passant"), ("how does en passant work", "en_passant"),
    ("explain en passant", "en_passant"), ("what is castling", "castling"),
    ("tell me about stalemate", "stalemate"), ("what is the rule for castling", "castling"),
    ("what are forks", "fork"),
])
def test_question_phrasing_is_a_confident_request(library, text, concept):
    assert resolve_concepts(library, text) == ([concept], True)


def test_broad_roots_still_need_nothing_else_of_substance(library):
    assert resolve_concepts(library, "opening principles")[1] is False
    assert resolve_concepts(library, "what is a good opening")[1] is False


@pytest.mark.parametrize("goal", ["what is en passant", "how does castling work", "tell me about stalemate"])
def test_rule_questions_get_the_verified_lesson(goal):
    record = plan_for_goal(goal, use_qwen=False, clarify=True)
    assert record["plan"]["planner"] == "knowledge", record["plan"]["title"]
    assert any(step.get("fen") for lesson in record["lessons"] for step in lesson["steps"])


@pytest.mark.parametrize("piece", ["king", "queen", "rook", "bishop", "knight", "pawn"])
def test_how_does_a_piece_move_is_answered_instantly(library, piece):
    a = quick_answer(f"how does the {piece} move", library, get_glossary())
    assert a and a["term"] == f"How the {piece} moves" and a["source"] == "rules"
    assert piece in a["text"].lower()


def test_rules_answers_prefer_the_specific_thing_asked(library):
    g = get_glossary()
    assert quick_answer("How do pawns capture?", library, g)["term"] == "How the pawn moves"
    assert quick_answer("how does a pawn capture en passant", library, g)["term"] == "En passant"
    assert quick_answer("what is a capture", library, g)["term"] == "Captures"
    for not_a_definition in ["how do I win with a rook", "how can you stop a fork", "how does this position look"]:
        assert quick_answer(not_a_definition, library, g) is None, not_a_definition


def _skill_lesson(goal, profile=None):
    record = plan_for_goal(goal, use_qwen=False, clarify=True, profile=profile)
    assert record["plan"]["planner"] == "fallback"
    lessons = [lesson for lesson in record["lessons"] if lesson.get("examples")]
    assert len(lessons) == 1, [lesson["title"] for lesson in record["lessons"]]
    assert "broader idea" not in lessons[0]["title"]
    return record, lessons[0]


def _player(rating, experience):
    p = LearnerProfile()
    p.set_onboarding(rating=rating, experience=experience)
    return p


@pytest.mark.parametrize("goal", ["improve my calculation", "help me calculate better", "I want to think ahead more"])
def test_calculation_gets_positions_that_need_calculating(library, goal):
    _, lesson = _skill_lesson(goal)
    examples = [_examples(library)[e] for e in lesson["examples"]]
    assert len(examples) >= 3
    assert all(_learner_moves(e) >= 2 and e.status == "verified" for e in examples)
    assert len({e.concepts[0] for e in examples}) == len(examples)  # varied, not one trick four times


def test_calculation_practice_follows_the_learner(library):
    idx = _examples(library)
    got = {}
    for rating, exp in [(500, "new"), (1700, "strong")]:
        _, lesson = _skill_lesson("improve my calculation", _player(rating, exp))
        got[rating] = [idx[e] for e in lesson["examples"]]
    assert statistics.median(map(puzzle_rating, got[500])) + 400 < statistics.median(map(puzzle_rating, got[1700]))
    assert all(_learner_moves(e) >= 3 for e in got[1700])  # stronger players: longer lines
    assert all(puzzle_rating(e) < 1200 for e in got[500])  # within reach for a beginner


def test_no_lesson_message_ends_with_the_list_it_introduces():
    with pytest.raises(PlanError) as err:
        create_plan("xyzzy", get_catalog(), use_qwen=False, engine=None)
    message = str(err.value)
    assert message.endswith("I can teach:") and "teach: (" not in message
    assert err.value.suggestions
