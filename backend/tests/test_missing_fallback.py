"""A lesson request the verified library can't serve is never a dead end, and never a guess.

library → catalog → generated + engine-checked positions → a broader idea's verified
examples → a hand-written definition labelled as not engine-checked. Only a request that
names nothing we recognise ends in "I don't have that" (with suggestions).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.engine import EngineUnavailable, get_engine, set_engine
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge
from app.knowledge.usage import UsageTracker
from app.lessons import set_library
from app.lessons.schema import parse_lesson
from app.planner import PlanError, plan_for_goal
from app.planner import missing
from app.planner.catalog import _normalize
from app.session import SessionManager, set_manager
from tests.knowledge_helpers import make_library
from tests.test_api import FakeEngine


class FakeTeacher:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def complete(self, messages, max_tokens=None):
        self.calls += 1
        return self.reply


@pytest.fixture()
def usage(tmp_path):
    return UsageTracker(tmp_path / "usage.json")


def _units(record):
    return [(u["title"], u["verified_by"]) for u in record["plan"]["units"]]


# ------------------------------------------------------------------ the glossary
def test_glossary_points_only_at_real_concepts():
    library = get_knowledge()
    for term in get_glossary().terms.values():
        assert term.broader is None or term.broader in library.concepts, term.id
        assert all(c in library.concepts for c in term.related), term.id
        assert term.definition.endswith("."), term.id


def test_glossary_never_shadows_a_concept():
    """A glossary term is only for ideas the concept graph doesn't have."""
    library = get_knowledge()
    concept_names = {" ".join(_normalize(a)) for c in library.concepts.values() for a in [c.name, *c.aliases]}
    for term in get_glossary().terms.values():
        for alias in (term.term, *term.aliases):
            assert " ".join(_normalize(alias)) not in concept_names, (term.id, alias)


def test_glossary_matching_prefers_the_longest_alias():
    glossary = get_glossary()
    # "isolated queen pawn" (3 words) beats "pawn structures" (2 words)
    assert glossary.match("isolated queen pawn structures").id == "isolated_pawn"
    assert glossary.match("doubled pawns please").id == "doubled_pawns"
    assert glossary.match("I want to learn forks") is None


# ------------------------------------------------------------------ resolving the request
def test_resolve_glossary_term_with_a_broader_concept():
    res = missing.resolve("teach me calculation", get_knowledge(), get_glossary(), use_qwen=False)
    assert res.term == "Calculation" and res.text_source == "glossary" and res.broader == "tactics"
    assert res.concept is None and res.via == "text"


def test_resolve_concept_without_examples_finds_the_nearest_broader_one():
    res = missing.resolve("teach me relative pins", get_knowledge(), get_glossary(), use_qwen=False)
    assert res.concept == "relative_pin" and res.broader == "pin" and res.text_source == "concept"


def test_resolve_nonsense_is_none_without_qwen():
    assert missing.resolve("zorblax gambit", get_knowledge(), get_glossary(), use_qwen=False) is None


def test_qwen_may_only_map_to_an_existing_id():
    library, glossary = get_knowledge(), get_glossary()
    good = FakeTeacher(json.dumps({"id": "outpost", "match": "same"}))
    res = missing.resolve("l'avant-poste", library, glossary, use_qwen=True, teacher=good)
    assert res.term == "Outpost" and res.via == "qwen" and good.calls == 1
    invented = FakeTeacher(json.dumps({"id": "zorblax_gambit", "match": "same"}))
    assert missing.resolve("zorblax gambit", library, glossary, teacher=invented) is None
    merely_related = FakeTeacher(json.dumps({"id": "openings", "match": "related"}))
    assert missing.resolve("zorblax gambit", library, glossary, teacher=merely_related) is None
    chatter = FakeTeacher("I believe this is about openings!")
    assert missing.resolve("zorblax gambit", library, glossary, teacher=chatter) is None


def test_qwen_is_not_asked_when_the_words_are_known():
    teacher = FakeTeacher(json.dumps({"id": "fork", "match": "same"}))
    res = missing.resolve("teach me calculation", get_knowledge(), get_glossary(), teacher=teacher)
    assert res.term == "Calculation" and teacher.calls == 0


# ------------------------------------------------------------------ the fallback plan
def test_broader_concept_plan_is_labelled(usage):
    record = missing.fallback_plan("teach me relative pins", library=get_knowledge(), engine=None,
                                   use_qwen=False, usage=usage)
    units = _units(record)
    assert units[0] == ("Relative pin: what it is", missing.TEXT_VERIFIED_BY)
    assert units[1][0] == "Pin: the broader idea"
    assert "only covered by the broader idea “Pin”" in record["plan"]["summary"]
    assert record["plan"]["planner"] == "fallback" and record["plan"]["fallback"]["broader"] == "pin"
    assert record["plan"]["fallback"]["verified_examples"] == 0
    text = record["lessons"][0]["steps"][0]["text"]
    assert "hasn't been checked by the chess engine" in text
    broad = record["lessons"][1]
    assert "broader" in broad["steps"][0]["text"] and broad["examples"]
    for lesson in record["lessons"]:
        parse_lesson(lesson, course_id="t")  # every lesson is playable


def test_text_only_plan_says_so(usage):
    record = missing.fallback_plan("pawn structure", library=get_knowledge(), engine=None, use_qwen=False,
                                   usage=usage)
    assert _units(record) == [("Pawn structure: what it is", missing.TEXT_VERIFIED_BY)]
    assert "nothing here is engine-checked" in record["plan"]["summary"]
    assert "Passed pawn" in record["plan"]["related"]


def test_no_engine_explains_instead_of_generating(tmp_path, usage):
    record = missing.fallback_plan("teach me spotting threats", library=make_library(tmp_path), engine=None,
                                   use_qwen=False, usage=usage)
    assert [u["verified_by"] for u in record["plan"]["units"]] == [missing.TEXT_VERIFIED_BY]
    assert "need the Stockfish engine" in record["plan"]["summary"]


def test_generation_failure_is_a_partial_result_not_a_crash(tmp_path, usage, monkeypatch):
    import app.knowledge.generation as generation

    def boom(*a, **k):
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(generation, "generate", boom)
    record = missing.fallback_plan("teach me spotting threats", library=make_library(tmp_path),
                                   engine=object(), use_qwen=False, usage=usage)
    assert len(record["plan"]["units"]) == 1 and "failed this time" in record["plan"]["summary"]


def test_nothing_passing_verification_shows_nothing_generated(tmp_path, usage, monkeypatch):
    import app.knowledge.generation as generation
    from app.knowledge.generation import GenerationResult

    monkeypatch.setattr(generation, "generate", lambda *a, **k: GenerationResult("spotting_threats", attempts=9))
    record = missing.fallback_plan("teach me spotting threats", library=make_library(tmp_path),
                                   engine=object(), use_qwen=False, usage=usage)
    assert len(record["plan"]["units"]) == 1
    assert "none passed every check" in record["plan"]["summary"]
    assert record["plan"]["fallback"]["generated"] == []


# ------------------------------------------------------------------ through the planner
def test_nonsense_still_gets_the_honest_answer():
    with pytest.raises(PlanError) as err:
        plan_for_goal("zorblax gambit", use_qwen=False)
    assert err.value.suggestions


@pytest.mark.parametrize("goal, term", [
    ("teach me isolated pawns", "Isolated pawn"),
    ("I want to learn about outposts", "Outpost"),
    ("backward pawns", "Backward pawn"),
    ("teach me doubled pawns", "Doubled pawns"),
])
def test_named_ideas_without_examples_get_a_fallback_plan(goal, term):
    record = plan_for_goal(goal, use_qwen=False)
    assert record["plan"]["planner"] == "fallback" and record["plan"]["fallback"]["term"] == term


def test_stalemate_tricks_is_not_a_king_and_queen_lesson():
    """Audit B11: the K+Q mate topic lists "stalemate" (avoid it); "stalemate tricks" (use it
    to save a lost game) matched that topic. Stalemate tricks is a verified concept now, so the
    knowledge planner teaches it, still never as a king-and-queen lesson."""
    record = plan_for_goal("teach me stalemate tricks", use_qwen=False)
    assert record["plan"]["planner"] == "knowledge"
    units = record["plan"]["units"]
    assert units[0]["title"] == "Stalemate tricks: the idea"
    assert all(u["concepts"] == ["stalemate_tricks"] for u in units)
    assert all("king and queen" not in u["title"].lower() for u in units)
    lib = get_knowledge()
    examples = [lib.get(ex) for u in units for ex in u["example_ids"]]
    assert examples and all(e.concept == "stalemate_tricks" for e in examples)


def test_catalog_topics_that_are_the_named_idea_still_win():
    assert plan_for_goal("teach me zugzwang", use_qwen=False)["plan"]["planner"] != "fallback"
    assert plan_for_goal("teach me knight forks", use_qwen=False)["plan"]["planner"] == "knowledge"


def test_api_returns_fallback_plans_with_playable_lessons():
    set_library(None)
    set_engine(FakeEngine())
    set_manager(SessionManager())
    from app.main import app as fastapi_app
    try:
        with TestClient(fastapi_app) as client:
            res = client.post("/api/plans", json={"goal": "teach me outposts", "library": True})
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["source"] == "fallback"
            started = client.post(f"/api/lessons/{data['first_lesson_id']}/start")
            assert started.status_code == 200, started.text
            assert "checked by the chess engine" in json.dumps(started.json())
            client.delete(f"/api/plans/{data['plan']['id']}")
    finally:
        set_engine(None)
        set_manager(None)
        set_library(None)


# ------------------------------------------------------------------ with the real engine
@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def test_missing_concept_gets_new_verified_positions(tmp_path, usage, engine):
    library = make_library(tmp_path)
    record = missing.fallback_plan("teach me spotting threats", library=library, engine=engine,
                                   use_qwen=False, usage=usage, generate_budget=40)
    units = record["plan"]["units"]
    assert units[0]["verified_by"] == missing.TEXT_VERIFIED_BY
    gen_unit = units[1]
    assert gen_unit["verified_by"] == missing.GENERATED_VERIFIED_BY and gen_unit["generated"] is True
    ids = gen_unit["example_ids"]
    assert ids and ids == record["plan"]["fallback"]["generated"]
    for eid in ids:
        ex = library.get(eid)
        assert ex.status == "verified" and ex.tier == "generated" and ex.source["source_type"] == "procedural"
    lesson = record["lessons"][1]
    assert "built" in lesson["steps"][0]["text"] and "Stockfish" in lesson["steps"][0]["text"]
    assert any("Generated position" in s.get("text", "") for s in lesson["steps"])  # provenance on the example
    assert set(usage.seen_counts()) >= set(ids)  # counted as seen: the next request varies
