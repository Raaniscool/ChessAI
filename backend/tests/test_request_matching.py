"""Regression tests from the system audit: what a learner's request is matched to.

Found bugs: fuzzy matching turned "position" into "opposition" / "positional", so "the
Philidor position" (a rook ending) became an opposition lesson; and a named variety the
library doesn't have ("Greek gift sacrifice", "relative pins") was silently answered with
the generic concept.
"""
import pytest

from app.knowledge.library import get_knowledge
from app.knowledge.retrieval import lesson_request, resolve_concepts
from app.planner.catalog import _token_match, get_catalog


@pytest.mark.parametrize("a, b, same", [
    ("position", "opposition", False),
    ("position", "positional", False),
    ("positions", "position", True),    # a plural is the same word...
    ("pins", "pin", True),              # ...also for short words (audit: "relative pins")
    ("mates", "mate", True),
    ("pinned", "pin", False),           # but not other word forms
    ("oppositions", "position", False),
    ("sicillian", "sicilian", True),    # real typos still work
    ("najdorff", "najdorf", True),
    ("forks", "forks", True),
])
def test_fuzzy_token_matching(a, b, same):
    assert _token_match(a, b) is same


@pytest.mark.parametrize("goal", ["teach me the philidor position", "show me positional play",
                                  "I want to learn the lucena position"])
def test_position_never_means_opposition(goal):
    assert "opposition" not in get_knowledge().match_concepts(goal)


@pytest.mark.parametrize("goal, generic", [
    ("teach me the greek gift sacrifice", "sacrifice"),
    ("teach me epaulette mate", "checkmate"),
    ("teach me mate in two", "checkmate"),
])
def test_a_named_variety_is_not_answered_with_the_generic_concept(goal, generic):
    library = get_knowledge()
    concepts, confident = resolve_concepts(library, goal)
    assert not (confident and generic in concepts), (goal, concepts)
    assert lesson_request(library, goal) == []


def test_a_named_variety_with_verified_examples_is_answered_with_itself():
    """Relative pins had no verified examples at first (so the request wasn't answered with generic
    pins); the harder tier added some — now it is answered precisely, still never as plain "pin"."""
    library = get_knowledge()
    concepts, confident = resolve_concepts(library, "teach me relative pins")
    assert confident and concepts == ["relative_pin"]
    assert lesson_request(library, "teach me relative pins") == ["relative_pin"]


@pytest.mark.parametrize("goal, concept", [
    ("teach me queen sacrifices", "sacrifice"),     # a piece is not a named variety
    ("teach me knight forks", "knight_fork"),
    ("show me some pins", "pin"),
    ("teach me the lucena position", "lucena_position"),
])
def test_ordinary_requests_still_match(goal, concept):
    concepts, confident = resolve_concepts(get_knowledge(), goal)
    assert confident and concept in concepts


def test_catalog_offers_a_qualified_match_as_related_not_as_the_answer():
    catalog = get_catalog()
    assert catalog.search("teach me the greek gift sacrifice") == []
    assert [t.id for t in catalog.near_matches("teach me the greek gift sacrifice")] == ["sacrifice"]
    assert [t.id for t in catalog.search("teach me clearance sacrifices")] == ["clearance"]
    assert [t.title for t in catalog.search("teach me mate in two")] == ["Mate in two"]
    assert catalog.search("teach me the philidor position") == []  # not "strategy" via "positional"


@pytest.mark.parametrize("goal, concept", [
    ("teach me forks with the queen", "queen_fork"),
    ("forks using a knight", "knight_fork"),
    ("show me forks with pawns", "pawn_fork"),
])
def test_a_piece_named_elsewhere_selects_that_pieces_version(goal, concept):
    """Audit: "forks with the queen" became a generic fork lesson. A piece named anywhere in the
    request picks the concept's child for that piece when the library has one."""
    concepts, confident = resolve_concepts(get_knowledge(), goal)
    assert confident and concepts == [concept]


@pytest.mark.parametrize("goal, concept", [("teach me queen sacrifices", "sacrifice"),
                                           ("pins with a bishop", "pin"), ("teach me forks", "fork")])
def test_no_piece_version_is_invented(goal, concept):
    concepts, _ = resolve_concepts(get_knowledge(), goal)
    assert concepts == [concept]


@pytest.mark.parametrize("goal, side", [("openings for black", "black"), ("black openings", "black"),
                                        ("openings for white", "white")])
def test_openings_for_a_side_are_that_sides_openings(goal, side):
    """Audit: "openings for black" asked about the Italian Game (a White opening)."""
    found = get_catalog().search(goal)
    assert found and all(t.side == side for t in found)


def test_openings_for_black_is_not_a_side_conflict():
    from app.planner.intent import IntentMemory, understand
    intent = understand("openings for black", memory=IntentMemory())  # no ClarificationNeeded
    assert not intent.clarified


def test_an_opening_only_the_database_names_is_suggested_for_a_custom_plan():
    """Audit: "I want to learn the Dutch Defense" was a dead end: the exact name matched but only
    partial names reached the planner's opening-database route."""
    from app.planner.intent import IntentMemory, understand
    intent = understand("I want to learn the Dutch Defense", memory=IntentMemory())
    assert [c.kind for c in intent.suggested][:1] == ["opening_db"]
    assert intent.suggested[0].id == "Dutch Defense"


@pytest.mark.parametrize("goal, title", [("I keep losing in the endgame", "Plan: Endgames"),
                                         ("help me get better at tactics", "Plan: Tactics"),
                                         ("openings for black", "Plan: Openings for Black")])
def test_plan_titles_name_the_subject(goal, title):
    from app.planner.planner import create_plan
    assert create_plan(goal, use_qwen=False)["plan"]["title"] == title
