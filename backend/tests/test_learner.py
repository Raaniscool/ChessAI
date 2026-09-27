"""The learner model: onboarding, skill estimates, concept statuses, gaps, lesson shapes."""
from datetime import datetime, timedelta, timezone

import pytest

from app.knowledge.difficulty import LABEL_RATING, features, puzzle_rating
from app.knowledge.library import get_knowledge
from app.knowledge.schema import parse_example
from app.learner import LearnerProfile, LearnerStore, lesson_shape, prompt_context, summary, target_rating
from app.learner import rating as R
from app.learner.views import concept_view, gaps_of, status_of

SOURCE = {"source_type": "curated", "source_id": "test", "source_license": "CC0-1.0",
          "reference": "unit test", "import_date": "2026-09-27"}


def example(**over):
    raw = {"id": "t", "status": "verified", "title": "t", "concept": "hanging_piece", "category": "tactics",
           "difficulty": 1, "description": "d", "start_fen": "4k3/8/8/3q4/8/8/8/3RK3 w - - 0 1",
           "moves": ["Rxd5"], "key_move": "1.Rxd5", "prompt": "p", "hints": ["h"], "explanation": "e",
           "source": dict(SOURCE)}
    raw.update(over)
    return parse_example(raw, get_knowledge().concepts)


# ------------------------------------------------------------------ difficulty
def test_free_capture_is_easier_than_a_quiet_two_move_mate():
    easy = example()
    hard = example(id="q", concept="checkmate", difficulty=2, start_fen="k7/8/1K6/8/8/8/8/7R w - - 0 1",
                   moves=["Kc7", "Ka7", "Ra1#"], key_move="1.Kc7")
    fe, fh = features(easy), features(hard)
    assert fe["free_capture"] and fe["learner_moves"] == 1
    assert fh["quiet"] and fh["learner_moves"] == 2 and fh["mate"]
    assert puzzle_rating(easy) + 300 < puzzle_rating(hard)


def test_demonstrations_use_the_curators_label_and_ratings_are_clamped():
    demo = example(id="demo", key_move=None, difficulty=3)
    assert puzzle_rating(demo) == LABEL_RATING[3]
    for e in get_knowledge().verified()[:60]:
        assert 400 <= puzzle_rating(e) <= 2400


def test_library_ratings_follow_the_labels():
    by = {}
    for e in get_knowledge().verified():
        by.setdefault(e.difficulty, []).append(puzzle_rating(e))
    means = [sum(v) / len(v) for _, v in sorted(by.items())]
    assert means == sorted(means)


def test_select_prefers_examples_near_the_target_rating():
    from app.knowledge.library import Query
    lib = get_knowledge()
    low = lib.select(Query(concepts=["checkmate"], count=3, target_rating=600, not_seen_recently=False))
    high = lib.select(Query(concepts=["checkmate"], count=3, target_rating=1600, not_seen_recently=False))
    avg = lambda xs: sum(puzzle_rating(e) for e in xs) / len(xs)  # noqa: E731
    assert avg(low) + 150 < avg(high)
    assert [puzzle_rating(e) for e in high] == sorted(puzzle_rating(e) for e in high)


# ------------------------------------------------------------------ ratings
def test_rating_helpers():
    assert R.level_for(650) == "beginner" and R.level_for(1200) == "intermediate" and R.level_for(1900) == "advanced"
    assert R.normalize(1800, "lichess") < R.normalize(1800, "chesscom")
    assert R.from_experience("new") < R.from_experience("casual") < R.from_experience("strong")
    assert R.expected(1000, 1000) == pytest.approx(0.5)
    assert R.score_for(True, True, 0, False) == 1.0
    assert R.score_for(True, False, 1, False) < R.score_for(True, False, 0, False) < 1.0
    assert R.score_for(True, True, 0, True) == 0.0 and R.score_for(False, False, 0, False) == 0.0


# ------------------------------------------------------------------ onboarding
def test_onboarding_with_a_rating_sets_the_skill():
    p = LearnerProfile()
    assert p.is_new and p.level == "beginner"
    p.set_onboarding(rating=1500, platform="chesscom", goals=["tactics", "nonsense"])
    assert p.rating == 1500 and p.level == "intermediate" and p.skill["source"] == "onboarding"
    assert p.onboarding["goals"] == ["tactics"] and p.onboarding["done"] and not p.is_new


def test_onboarding_without_a_rating_uses_experience_and_username_is_optional():
    p = LearnerProfile()
    p.set_onboarding(experience="new")
    assert p.rating == R.from_experience("new")
    assert "chesscom_username" not in p.onboarding
    p.set_onboarding(username="  ")
    assert p.onboarding["chesscom_username"] is None


@pytest.mark.parametrize("kwargs", [{"platform": "icc"}, {"experience": "grandmaster"}, {"rating": 5},
                                    {"username": "bad name!"}, {"explanation": "verbose"}])
def test_onboarding_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        LearnerProfile().set_onboarding(**kwargs)


def test_skipping_onboarding_is_allowed():
    p = LearnerProfile()
    p.set_onboarding(skipped=True)
    assert p.onboarding["done"] and p.onboarding["skipped"] and p.rating == R.DEFAULT_RATING


# ------------------------------------------------------------------ results
def solve(p, concept="knight_fork", n=1, puzzle=900, **kw):
    kw.setdefault("solved", True)
    kw.setdefault("first_try", True)
    for _ in range(n):
        p.record_attempt(concept, puzzle, **kw)


def test_successes_raise_and_failures_lower_the_estimates():
    p = LearnerProfile()
    p.set_onboarding(rating=1000)
    solve(p, n=3, puzzle=1100)
    up = p.concept_rating("knight_fork")
    assert up > 1000 and p.rating > 1000
    q = LearnerProfile()
    q.set_onboarding(rating=1000)
    solve(q, n=3, puzzle=900, solved=False, first_try=False, revealed=True)
    assert q.concept_rating("knight_fork") < 1000 and q.rating < 1000
    assert q.stats["reveals"] == 3 and q.stats["puzzles"]["solved"] == 0


def test_a_reveal_counts_as_a_failure_and_hints_as_partial_credit():
    p = LearnerProfile()
    r = p.record_attempt("pin", 800, solved=True, first_try=True, revealed=True)
    assert r["score"] == 0.0
    r = p.record_attempt("pin", 800, solved=True, first_try=True, hints=1)
    assert 0 < r["score"] < 1
    assert p.stats["hints_used"] == 1 and p.stats["puzzles"]["first_try"] == 0


def test_concept_statuses():
    p = LearnerProfile()
    assert status_of(None) == "new"
    solve(p, "knight_fork", n=4)
    solve(p, "pin", n=3, solved=False, first_try=False)
    solve(p, "skewer", n=1)
    p.record_lesson(["opposition"], completed=True, lesson_id="L1")
    s = summary(p, get_knowledge())
    assert s["mastered"] == ["knight_fork"] and s["weak"] == ["pin"]
    assert s["practicing"] == ["skewer"] and s["learned"] == ["opposition"]
    assert s["strengths"][0]["concept"] == "knight_fork" and s["completed_lessons"] == ["L1"]
    assert s["concepts"]["knight_fork"]["name"].lower().startswith("knight")


def test_calculation_gap_one_movers_fine_longer_lines_not():
    p = LearnerProfile()
    solve(p, "fork", n=3, learner_moves=1)
    solve(p, "fork", n=2, solved=False, first_try=False, learner_moves=3)
    assert "calculation" in gaps_of(p.concepts["fork"])
    assert lesson_shape(p, "fork")["calculation"]


def test_transfer_gap_solves_puzzles_but_misses_it_in_games():
    p = LearnerProfile()
    solve(p, "knight_fork", n=4)
    p.update_from_history({"patterns": [{"key": "missed_fork:knight_fork", "concept": "knight_fork",
                                         "title": "Missed knight forks", "tier": "recurring",
                                         "game_count": 4, "total_games": 10, "occurrences": 5}]})
    view = concept_view(p, "knight_fork")
    assert "transfer" in view["gaps"]
    assert lesson_shape(p, "knight_fork")["realistic"]


def test_sub_concepts_count_towards_the_parent():
    p = LearnerProfile()
    solve(p, "knight_fork", n=4)
    lib = get_knowledge()
    assert concept_view(p, "fork", lib)["status"] == "mastered"
    assert concept_view(p, "fork")["status"] == "new"  # without the graph there's no link


def test_review_is_due_after_time_passes():
    p = LearnerProfile()
    solve(p, "pin", n=4)
    assert not concept_view(p, "pin")["needs_review"]
    p.concepts["pin"].last_seen = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert concept_view(p, "pin")["needs_review"]
    assert summary(p)["needs_review"] == ["pin"]


# ------------------------------------------------------------------ personalization shape
def test_beginner_and_strong_player_get_different_lessons_for_the_same_concept():
    beginner, strong = LearnerProfile(), LearnerProfile()
    beginner.set_onboarding(experience="new")
    strong.set_onboarding(rating=1900, platform="chesscom")
    b, s = lesson_shape(beginner, "knight_fork"), lesson_shape(strong, "knight_fork")
    assert b["demos"] > s["demos"] and b["reminders"] and not s["reminders"]
    assert b["prerequisites"] and not s["prerequisites"]
    assert b["target_rating"] + 800 < s["target_rating"]


def test_mastered_concepts_get_challenge_and_weak_ones_simplify():
    p = LearnerProfile()
    p.set_onboarding(rating=1200)
    solve(p, "pin", n=5, puzzle=1100)
    solve(p, "skewer", n=4, puzzle=1100, solved=False, first_try=False)
    m, w = lesson_shape(p, "pin"), lesson_shape(p, "skewer")
    assert m["purpose"] == "challenge" and m["demos"] == 0
    assert w["purpose"] == "simplify" and w["target_rating"] < m["target_rating"] - 300
    assert target_rating(p, None, "challenge") > target_rating(p, None, "learn")


def test_history_updates_weaknesses_and_rating_from_games():
    p = LearnerProfile()
    p.set_onboarding(experience="casual")
    p.update_from_history({"games_analyzed": 10, "patterns": [
        {"key": "hanging_piece", "concept": "hanging_piece", "title": "Hanging pieces", "tier": "recurring",
         "game_count": 5, "total_games": 10, "occurrences": 7},
        {"key": "x", "concept": None, "title": "once", "tier": "one_time", "game_count": 1}]},
        ratings=[1200, 1210, 1650, 1190, 1205], username="someone")
    assert [w["key"] for w in p.weaknesses] == ["hanging_piece"]
    assert p.rating == 1205 and p.skill["source"] == "games"
    assert concept_view(p, "hanging_piece")["status"] == "weak"
    assert "Hanging pieces" in prompt_context(p)


def test_prompt_context_is_short():
    p = LearnerProfile()
    p.set_onboarding(rating=900, goals=["tactics", "endgames"])
    solve(p, "knight_fork", n=2)
    text = prompt_context(p, ["knight_fork", "pin", "skewer"])
    assert f"about {p.rating} rating" in text and "knight fork" in text and "skewer" not in text
    assert len(text) < 300


# ------------------------------------------------------------------ store
def test_store_persists_resets_and_survives_a_corrupt_file(tmp_path):
    store = LearnerStore(tmp_path)
    store.update("local", lambda p: p.set_onboarding(rating=1300))
    again = LearnerStore(tmp_path).get("local")
    assert again.rating == 1300 and again.onboarding["done"]
    assert store.reset("local").rating == R.DEFAULT_RATING
    store.path("broken").write_text("{not json", encoding="utf-8")
    assert LearnerStore(tmp_path).get("broken").is_new
    assert store.path("../../etc").parent == tmp_path
