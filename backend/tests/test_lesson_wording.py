"""Lessons read like a coach, not a template: nothing the learner was already told is repeated
by later examples, provenance notes appear once, headers don't stack labels, and the
personalised intro doesn't describe the lesson twice."""
import re
import tempfile
from pathlib import Path

import pytest

from app.knowledge.usage import UsageTracker
from app.learner import LearnerProfile
from app.planner.knowledge_lessons import Said, create_knowledge_plan

GOALS = ["teach me knight forks", "teach me pins", "skewers", "back rank mate", "discovered attacks"]


def _plan(goal, rating=None, experience="casual"):
    profile = None
    if rating:
        profile = LearnerProfile()
        profile.set_onboarding(rating=rating, experience=experience)
    return create_knowledge_plan(goal, profile=profile, record_usage=False,
                                 usage=UsageTracker(Path(tempfile.mkdtemp()) / "u.json"))


def _sentences(lesson):
    for step in lesson["steps"]:
        for para in (step.get("text") or "").split("\n\n"):
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", para.strip()):
                if s:
                    yield re.sub(r"\b(white|black)('s)?\b", "side", s.lower())


def test_said_drops_repeats_but_keeps_concrete_and_side_to_move():
    said = Said("Knight fork: Knights are the best forking pieces: they jump, and their attack can't be blocked.")
    assert said.fresh("Knights are the best forking pieces: they jump, and their attack can't be blocked. "
                      "Fork! The knight on c6 attacks the rook.") == "Fork! The knight on c6 attacks the rook."
    assert said.fresh("Black to move. White can only save one of them.") == \
        "Black to move. White can only save one of them."
    assert said.fresh("White to move. Black can only save one of them.") == "White to move."  # colors ignored
    assert said.fresh("Nf5+ attacks the king on h6 and the rook on h4.") != ""  # different squares survive
    assert said.fresh("White can only save one of them.") == "White can only save one of them."  # keep one
    assert said.fresh("White can only save one of them.", keep_one=False) == ""


@pytest.mark.parametrize("goal", GOALS)
@pytest.mark.parametrize("rating", [None, 600, 1600])
def test_lessons_do_not_repeat_themselves(goal, rating):
    plan = _plan(goal, rating, "casual" if (rating or 0) < 1000 else "club")
    assert plan, goal
    for lesson in plan["lessons"]:
        seen = {}
        for s in _sentences(lesson):
            if "to move" in s or len(s.split()) < 4:
                continue  # "Black to move." / "Correct!" are fine to repeat
            assert s not in seen, f"{lesson['title']}: repeated {s!r}"
            seen[s] = True
        texts = " ".join(step.get("text") or "" for step in lesson["steps"])
        assert texts.count("Generated position") <= 1
        for step in lesson["steps"]:
            head = (step.get("text") or "").split("\n")[0]
            if head.startswith("Example ") and " — " not in head:
                # a position to solve: the header names nothing (the title comes afterwards)
                assert re.fullmatch(r"Example \d+ of \d+\.", head), head
            elif head.startswith("Example "):
                label = head.split(" — ", 1)[1].split(".")[0]
                assert not any(label.lower().startswith(n.lower() + ":") for n in lesson["concepts"]), head


def test_personal_intro_describes_the_lesson_once():
    for rating, exp in [(500, "new"), (900, "casual"), (1400, "club"), (2000, "strong")]:
        intro = _plan("teach me knight forks", rating, exp)["lessons"][0]["steps"][0]["text"]
        assert intro.count("example") <= 2, intro  # "I picked N verified examples ..." only
        assert "before you try" not in intro and "One quick example" not in intro
        assert intro.count("I picked") == 1
