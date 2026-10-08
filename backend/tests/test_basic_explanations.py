"""The Basic Explanation Library is independent data with safe links to existing chess sources."""
from collections import defaultdict

import pytest

from app.basic_explanations import LEVELS, get_basic_explanations
from app.knowledge.answers import is_definition_question, quick_answer
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge
from app.planner.catalog import _normalize
from app.planner import plan_for_goal
from app.planner.missing import TEXT_VERIFIED_BY, fallback_plan, resolve
from app.knowledge.usage import UsageTracker


@pytest.fixture(scope="module")
def sources():
    return get_basic_explanations(), get_knowledge(), get_glossary()


def test_library_is_separate_structured_and_cross_references_are_valid(sources):
    basic, knowledge, glossary = sources
    assert len(basic.entries) >= 50
    basic.validate_references(knowledge, glossary)
    for entry in basic.all():
        record = entry.as_dict(knowledge, glossary)
        assert set(record["levels"]) == set(LEVELS)
        assert all(record["levels"][level].strip() for level in LEVELS)
        assert record["key_idea"].strip() and record["common_misconception"].strip()
        assert all(related in basic.entries for related in record["related"])
        assert all(knowledge.get(eid) is not None and knowledge.get(eid).status == "verified"
                   for eid in record["related_verified_examples"])


def test_retrieval_aliases_and_specificity(sources):
    basic, knowledge, glossary = sources
    requests = {
        "Explain pins": "pin",
        "Can you explain a relative pin?": "relative_pin",
        "What does en passant mean?": "en_passant",
        "What's a half-open file?": "half_open_file",
        "What is the 50 move rule?": "fifty_move_rule",
        "What is a decoy?": "attraction",
        "What are hanging pieces?": "hanging_piece",
        "Explain the rule of the square": "rule_of_the_square",
    }
    for text, expected in requests.items():
        assert basic.match(text, knowledge, glossary).id == expected, text
    # Existing, more-specific Knowledge Library concepts still beat a broader Basic alias.
    assert quick_answer("Define smothered mate", knowledge, glossary)["concept"] == "smothered_mate"
    assert quick_answer("What is mate in one?", knowledge, glossary)["concept"] == "mate_in_one"
    assert quick_answer("What is a queen fork?", knowledge, glossary)["concept"] == "queen_fork"


def test_aliases_are_not_ambiguous_inside_the_basic_library(sources):
    basic, knowledge, glossary = sources
    owners = defaultdict(set)
    for entry in basic.all():
        for alias in basic._aliases(entry, knowledge, glossary):
            owners[tuple(_normalize(alias))].add(entry.id)
    assert all(len(ids) == 1 for ids in owners.values())


def test_rules_and_tactics_keep_important_chess_nuance(sources):
    basic, knowledge, glossary = sources
    half_open = basic.get("half_open_file").as_dict(knowledge, glossary)
    assert "enemy pawns" in half_open["levels"]["beginner"]
    assert "none of your own" in half_open["levels"]["beginner"]
    assert "not frozen" in basic.get("absolute_pin").as_dict(knowledge, glossary)["common_misconception"]
    assert "claim" in basic.get("threefold_repetition").as_dict(knowledge, glossary)["levels"]["intermediate"]
    assert "75" in basic.get("fifty_move_rule").as_dict(knowledge, glossary)["levels"]["intermediate"]
    assert "threshold" in basic.get("blunder").as_dict(knowledge, glossary)["levels"]["advanced"]


def test_natural_language_answers_include_the_requested_level_or_detail(sources):
    _, knowledge, glossary = sources
    advanced = quick_answer("Could you explain a pin at an advanced level?", knowledge, glossary)
    assert advanced["source"] == "basic_explanations"
    assert advanced["level"] == "advanced" and advanced["text"] == advanced["levels"]["advanced"]
    misconception = quick_answer("What misconception do players have about a pin?", knowledge, glossary)
    assert misconception["detail"] == "common_misconception"
    assert misconception["text"] == misconception["common_misconception"]
    key_idea = quick_answer("What is the key idea behind a pin?", knowledge, glossary)
    assert key_idea["detail"] == "key_idea" and key_idea["text"] == key_idea["key_idea"]
    plural = quick_answer("What are the key ideas behind a pin?", knowledge, glossary)
    assert plural["detail"] == "key_idea" and plural["text"] == plural["key_idea"]


def test_contextual_comparative_and_personalized_questions_are_not_answered_as_definitions(sources):
    _, knowledge, glossary = sources
    questions = [
        "Compare absolute and relative pins",
        "Explain why a pin is good here",
        "What is a pin in this position?",
        "How can I use pins in my games?",
        "What mistake did I make with my fork?",
        "What did I blunder last game?",
        "Tell me more about pins in depth",
    ]
    for question in questions:
        assert not is_definition_question(question), question
        assert quick_answer(question, knowledge, glossary) is None, question


def test_basic_explanation_can_be_used_by_existing_text_lesson_builder(tmp_path, sources):
    basic, knowledge, glossary = sources
    resolved = resolve("teach me touch-move rule", knowledge, glossary, use_qwen=False, level="intermediate")
    assert resolved is not None
    assert resolved.text_source == "basic_explanations" and resolved.category == "rules"
    expected = basic.get("touch_move").level_text("intermediate", knowledge, glossary)
    assert resolved.text == expected

    record = fallback_plan("teach me touch-move rule", library=knowledge, engine=None, level="advanced",
                           use_qwen=False, usage=UsageTracker(tmp_path / "usage.json"))
    assert record["plan"]["fallback"]["text_source"] == "basic_explanations"
    assert record["plan"]["units"][0]["verified_by"] == TEXT_VERIFIED_BY
    assert expected not in record["lessons"][0]["steps"][0]["text"]
    assert basic.get("touch_move").level_text("advanced", knowledge, glossary) in \
        record["lessons"][0]["steps"][0]["text"]
    assert record["lessons"][0]["difficulty"] == "beginner"

    # The normal planner can reach the same source without changing lesson schema or content format.
    planned = plan_for_goal("teach me touch-move rule", use_qwen=False, level="advanced")
    assert planned["plan"]["planner"] == "fallback"
    assert planned["plan"]["fallback"]["text_source"] == "basic_explanations"
    assert basic.get("touch_move").level_text("advanced", knowledge, glossary) in \
        planned["lessons"][0]["steps"][0]["text"]
