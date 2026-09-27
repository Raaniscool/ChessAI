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
    ("teach me relative pins", "pin"),  # a real concept, but with no verified examples yet
])
def test_a_named_variety_is_not_answered_with_the_generic_concept(goal, generic):
    library = get_knowledge()
    concepts, confident = resolve_concepts(library, goal)
    assert not (confident and generic in concepts), (goal, concepts)
    assert lesson_request(library, goal) == []


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
