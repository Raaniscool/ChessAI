"""Training (lesson) difficulty comes from the same skill profile as the Puzzles tab: real game
evidence, per concept, moving gradually with lesson results (learner.training_level)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.knowledge.library import get_knowledge
from app.knowledge.usage import UsageTracker
from app.learner.personalize import retrieval_settings
from app.learner.training_level import NAMED_SKILLS, PURPOSE_OFFSET, calibrated_target, rating_of, skill_profile
from app.planner.knowledge_lessons import create_knowledge_plan
from tests.test_difficulty_calibration import STRONG, WEAK, learner, skill

LIB = get_knowledge()
RATE = rating_of(LIB)


def usage() -> UsageTracker:
    return UsageTracker(Path(tempfile.mkdtemp()) / "usage.json")


def solved_ratings(record) -> list[int]:
    lesson = record["lessons"][0]
    ids = dict.fromkeys(s["example"] for s in lesson["steps"] if s.get("example") and s["type"] == "exercise")
    return [RATE(LIB.get(e)) for e in ids]


def test_same_rating_different_games_different_training_difficulty():
    strong, weak = learner(1000, STRONG), learner(1000, WEAK)
    a = calibrated_target(strong, "knight_fork", "learn", LIB, usage())
    b = calibrated_target(weak, "knight_fork", "learn", LIB, usage())
    assert a["target"] > b["target"] + 100, (a["target"], b["target"])
    # ... and the lessons really are built at different levels
    ra = solved_ratings(create_knowledge_plan("teach me knight forks", library=LIB, usage=usage(), profile=strong,
                                              record_usage=False))
    rb = solved_ratings(create_knowledge_plan("teach me knight forks", library=LIB, usage=usage(), profile=weak,
                                              record_usage=False))
    assert sum(ra) / len(ra) > sum(rb) / len(rb), (ra, rb)


def test_concept_sensitive_not_one_global_number():
    # sharp at tactics, but walks into tactics a lot: defensive training is pitched lower
    sharp_but_careless = skill(12, errors=8, allowed=40, found=9, missed=1, rating=1100)
    p = learner(1000, sharp_but_careless)
    tactics = calibrated_target(p, "knight_fork", "practice", LIB, usage())
    defence = calibrated_target(p, "spotting_threats", "practice", LIB, usage())
    assert tactics["skill"] == "tactics" and defence["skill"] == "defense"
    assert defence["target"] < tactics["target"] - 100, (defence["target"], tactics["target"])


def test_lesson_purpose_shifts_the_target_a_little():
    p = learner(1000, STRONG)
    learn = calibrated_target(p, "knight_fork", "learn", LIB, usage())["target"]
    challenge = calibrated_target(p, "knight_fork", "challenge", LIB, usage())["target"]
    assert challenge - learn == PURPOSE_OFFSET["challenge"] - PURPOSE_OFFSET["learn"]


def test_lesson_results_move_the_next_lesson_gradually_and_only_for_that_skill():
    p = learner(1000, STRONG)
    u = usage()
    forks = sorted(LIB.examples_for("knight_fork"), key=RATE)
    start = calibrated_target(p, "knight_fork", "practice", LIB, u)["target"]
    endgame = calibrated_target(p, "opposition", "practice", LIB, u)["target"] if "opposition" in LIB.concepts else None
    u.record_resolved(forks[-1].id, solved=False, first_try=False, revealed=True, concept="knight_fork")
    one = calibrated_target(p, "knight_fork", "practice", LIB, u)["target"]
    assert 0 < start - one <= 120, (start, one)  # one failure: a small step down, no wild jump
    for e in forks[-4:-1]:
        u.record_resolved(e.id, solved=False, first_try=False, revealed=True, concept="knight_fork")
    four = calibrated_target(p, "knight_fork", "practice", LIB, u)["target"]
    assert four < one, (one, four)
    if endgame is not None:  # an endgame lesson is not affected by failed forks
        assert calibrated_target(p, "opposition", "practice", LIB, u)["target"] == endgame
    # clean, quick solves raise it again
    for e in forks[:6]:
        u.record_resolved(e.id, solved=True, first_try=True, seconds=8, concept="knight_fork")
    assert calibrated_target(p, "knight_fork", "practice", LIB, u)["target"] > four


def test_plan_settings_carry_the_calibration_and_its_scale():
    p = learner(1000, STRONG)
    out = retrieval_settings(p, "knight_fork", ["knight_fork"], LIB, usage=usage())
    cal = out["shape"]["calibration"]
    assert out["settings"]["target_rating"] == cal["target"] and out["settings"]["rating_of"] is not None
    assert out["settings"]["practice_rating"] == cal["practice"] > cal["target"]
    assert cal["evidence"]["evidence"] and "target = concept estimate" in cal["rule"]


def test_skill_profile_has_the_named_fields():
    sp = skill_profile(learner(1000, STRONG), LIB, usage())
    assert set(sp["skills"]) == set(NAMED_SKILLS) == {
        "overall_skill_level", "tactical_skill", "calculation_skill", "defensive_skill", "opening_skill",
        "endgame_skill", "pattern_recognition"}
    assert sp["games"] == 12 and all(s["level"] and s["evidence"] for s in sp["skills"].values())
