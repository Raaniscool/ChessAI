"""Plans contain only what the learner asked for, organized as stages.

The user's report: "when you ask to learn something it still makes plans on stuff the user
didn't request" — "play as black against e4" gave forks, pins, back-rank mate and K+Q mate;
"rook endgames" added K+Q and K+R mate units; openings added an "Opening principles" unit.
"""
from __future__ import annotations

import pytest

from app.planner import planner as planner_mod
from app.planner.catalog import get_catalog
from app.planner.planner import PlanError, QwenPlan, create_plan, plan_for_goal

CATALOG = get_catalog()


def unit_topics(record) -> list:
    return [u["topic_id"] for u in record["plan"]["units"]]


# ---------------------------------------------------------------- repertoire requests
@pytest.mark.parametrize("goal,side,move", [
    ("I want to learn how to play as black against e4", "black", "e4"),
    ("what should I play against 1.e4", "black", "e4"),
    ("how do I answer 1.d4 as black", "black", "d4"),
    ("teach me what to play vs d4", "black", "d4"),
    ("as white against e5", "white", "e5"),
])
def test_repertoire_requests_are_understood(goal, side, move):
    rep = CATALOG.repertoire(goal)
    assert rep and rep.side == side and rep.move == move
    assert rep.openings and all(t.category == "opening" and t.side == side for t in rep.openings)
    for t in rep.openings:
        assert (t.line[0] if side == "black" else t.line[1]) == move


def test_black_against_e4_is_an_opening_plan_not_the_general_curriculum():
    record = create_plan("I want to learn how to play as black against e4", use_qwen=False)
    plan = record["plan"]
    rep = CATALOG.repertoire("as black against e4")
    assert unit_topics(record) == [rep.openings[0].id]          # one main answer, learned properly
    assert CATALOG.topics[unit_topics(record)[0]].line[0] == "e4"
    assert plan["title"] == "Plan: play as Black against 1.e4"
    assert set(plan["related"]) <= {t.title for t in rep.openings[1:]} and plan["related"]  # alternatives offered
    general = set(CATALOG.general["topics"])
    assert not general & set(unit_topics(record))
    assert all(lesson["title"].startswith(rep.openings[0].title) for lesson in record["lessons"])


def test_beginner_friendly_answer_comes_first():
    rep = CATALOG.repertoire("as black against e4")
    levels = [t.level for t in rep.openings]
    assert levels == sorted(levels, key=["beginner", "intermediate", "advanced"].index)


def test_a_named_opening_wins_over_the_move():
    record = create_plan("I want to learn the Sicilian against e4", use_qwen=False)
    assert unit_topics(record) == ["sicilian_defense"]
    assert not record["plan"]["title"].startswith("Plan: play as")


def test_unknown_answer_is_not_replaced_by_something_else():
    with pytest.raises(PlanError):  # no catalog opening answers 1.c4 — say so, don't teach forks
        create_plan("what should I play against c4", use_qwen=False)


# ---------------------------------------------------------------- general curriculum only when general
@pytest.mark.parametrize("goal", ["I want to learn chess", "I want to get better at chess", "teach me the basics",
                                  "I want to stop losing", "how to play"])
def test_general_goals_still_get_the_general_curriculum(goal):
    assert [t.id for t in CATALOG.search(goal)] == CATALOG.general["topics"]


@pytest.mark.parametrize("goal", ["how to play the Stonewall", "how to play the hippo", "I want to get better at bughouse"])
def test_specific_unknown_subjects_do_not_fall_back_to_the_general_curriculum(goal):
    assert CATALOG.search(goal) == []
    with pytest.raises(PlanError):
        create_plan(goal, use_qwen=False)


# ---------------------------------------------------------------- prerequisites are suggestions
@pytest.mark.parametrize("goal,topic,prereqs", [
    ("I want to learn rook endgames", "rook_endgames", {"kq_vs_k", "kr_vs_k"}),
    ("teach me the London System", "london_system", {"opening_principles"}),
    ("I want to learn the queen's gambit", "queens_gambit", {"opening_principles"}),
])
def test_prerequisites_are_suggested_not_added(goal, topic, prereqs):
    record = plan_for_goal(goal, use_qwen=False)
    assert unit_topics(record) == [topic]
    assert {p["topic_id"] for p in record["plan"]["prerequisites"]} == prereqs


# ---------------------------------------------------------------- staged library plans
def test_single_concept_plan_is_staged_idea_practice_review():
    record = plan_for_goal("I want to learn the smothered mate", use_qwen=False)
    plan = record["plan"]
    assert plan["planner"] == "knowledge"
    assert [lesson["title"] for lesson in record["lessons"]] == [
        "Smothered mate: understand the idea", "Smothered mate: learn the pattern",
        "Smothered mate: practice", "Smothered mate: review"]
    assert [u["topic_id"] for u in plan["units"]] == [None, "smothered_mate", None]
    assert [u["reason"].split(":")[0] for u in plan["units"]] == ["Step 1", "Step 2", "Step 3"]
    # course order follows the stages, and the first lesson is the one that explains the idea
    assert [lesson["id"] for lesson in record["course"]["lessons"]] == [
        lid for u in plan["units"] for lid in u["lesson_ids"]]
    first = record["lessons"][0]["steps"]
    assert first[0]["type"] == "teach" and any(s["type"] == "demonstrate" for s in first)


def test_opening_plan_goes_ideas_then_lines_then_whole_line():
    record = plan_for_goal("I want to learn the Italian Game", use_qwen=False)
    assert [lesson["title"] for lesson in record["lessons"]] == [
        "Italian Game: moves and ideas", "Italian Game: see it in real lines", "Italian Game: play the whole line"]


def test_narrow_request_does_not_pull_in_neighbouring_topics():
    record = plan_for_goal("I keep losing to scholar's mate", use_qwen=False)
    assert "attacking_f7" not in unit_topics(record)
    assert all("Scholar" in lesson["title"] for lesson in record["lessons"])


def test_broad_request_covers_its_sub_patterns():
    record = plan_for_goal("I want to learn how to checkmate", use_qwen=False)
    assert {"mate_in_1", "back_rank_mate"} <= set(unit_topics(record))


@pytest.mark.parametrize("goal", ["I want to learn forks", "teach me pins", "I want to learn about skewers",
                                  "back rank mate", "I want to learn discovered attacks", "I want to stop hanging pieces"])
def test_every_lesson_is_about_the_request(goal):
    record = plan_for_goal(goal, use_qwen=False)
    subject = {"forks": "fork", "pins": "pin", "skewers": "skewer", "back rank": "back-rank",
               "discovered": "discovered", "hanging": "hanging"}
    word = next(v for k, v in subject.items() if k in goal.lower())
    for lesson in record["lessons"]:
        assert word in lesson["title"].lower(), (goal, lesson["title"])


# ---------------------------------------------------------------- Qwen's extra openings
def test_qwen_openings_are_not_added_to_a_non_opening_request(monkeypatch):
    qwen = QwenPlan(title="Mates", summary="x", topic_ids=[], reasons={},
                    new_openings=[{"name": "Vienna Game", "side": "white", "moves": "1.e4 e5 2.Nc3 Nf6", "reason": "fun"}])
    monkeypatch.setattr(planner_mod, "ask_qwen", lambda goal, catalog: qwen)
    with pytest.raises(PlanError):  # the request is about a mate we don't have: no opening sneaks in
        create_plan("I want to learn the epaulette mate", engine=None)
