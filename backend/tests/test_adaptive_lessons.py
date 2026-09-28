"""Personalized plans (learner model → lesson shape) and in-lesson adaptation."""
import tempfile
from pathlib import Path

import pytest

from app.knowledge.difficulty import puzzle_rating
from app.knowledge.library import get_knowledge
from app.knowledge.usage import UsageTracker
from app.learner import LearnerProfile, get_profile, get_store
from app.learner.adapt import MAX_CHANGES, Outcome, decide, help_offer
from app.learner.personalize import piece_reminder, roles_for
from app.learner.recommend import suggestions
from app.planner.knowledge_lessons import create_knowledge_plan
from tests.test_api import _fresh_state, client  # noqa: F401  (fixtures)

LIB = get_knowledge()


def usage():
    return UsageTracker(Path(tempfile.mkdtemp()) / "usage.json")


def plan(goal, profile=None, **kw):
    return create_knowledge_plan(goal, library=LIB, usage=usage(), profile=profile, record_usage=False, **kw)


def learner(**onboarding):
    p = LearnerProfile()
    p.set_onboarding(**onboarding)
    return p


def ratings(record):
    return [puzzle_rating(LIB.get(e)) for e in record["plan"]["knowledge"]["example_ids"]]


def mean(xs):
    return sum(xs) / len(xs)


# ------------------------------------------------------------------ plans
def test_a_blank_profile_keeps_the_default_lesson():
    default, blank = plan("checkmate patterns"), plan("checkmate patterns", LearnerProfile())
    assert blank["plan"]["knowledge"]["roles"] == default["plan"]["knowledge"]["roles"] == \
        ["demonstration", "guided", "practice"]
    assert "personalization" not in blank["plan"]


def test_same_request_different_learners_different_lessons():
    beginner = plan("checkmate patterns", learner(experience="new"))
    strong = plan("checkmate patterns", learner(rating=2000, platform="chesscom"))
    b, s = beginner["plan"], strong["plan"]
    assert b["knowledge"]["roles"].count("demonstration") > s["knowledge"]["roles"].count("demonstration")
    assert "guided" not in s["knowledge"]["roles"]
    assert mean(ratings(strong)) > mean(ratings(beginner)) + 300
    assert b["personalization"]["level"] == "beginner" and s["personalization"]["level"] == "advanced"
    assert b["personalization"]["reason"] != s["personalization"]["reason"]
    intro_b = beginner["lessons"][0]["steps"][0]["text"]
    assert b["personalization"]["reason"] in intro_b and intro_b.count("verified example") == 1
    n_demos = b["knowledge"]["roles"].count("demonstration")
    assert f"{['', 'one', 'two', 'three'][n_demos]} to watch" in intro_b  # the outline counts what's coming
    intro_s = strong["lessons"][0]["steps"][0]["text"]
    assert "to watch" not in intro_s or s["knowledge"]["roles"].count("demonstration")


def test_beginners_get_a_piece_reminder_and_strong_players_dont():
    kb = plan("knight forks", learner(experience="new"))["lessons"][0]["steps"][0]["text"]
    ks = plan("knight forks", learner(rating=1800))["lessons"][0]["steps"][0]["text"]
    assert "knight moves in an L" in kb and "knight moves in an L" not in ks
    assert piece_reminder(LIB, "fork") is None  # no single piece: no reminder


def test_mastered_concept_gets_a_challenge_without_demonstrations():
    p = learner(rating=1100)
    for _ in range(5):
        p.record_attempt("checkmate", 900, solved=True, first_try=True)
    record = plan("checkmate patterns", p)
    assert record["plan"]["personalization"]["status"] == "mastered"
    assert set(record["plan"]["knowledge"]["roles"]) == {"practice"}
    assert "harder positions" in record["plan"]["personalization"]["reason"]


def test_weak_concept_gets_easier_positions_than_a_practicing_one():
    weak, fine = learner(rating=1300), learner(rating=1300)
    for _ in range(4):
        weak.record_attempt("checkmate", 1200, solved=False, first_try=False, revealed=True)
        fine.record_attempt("checkmate", 1200, solved=True, first_try=False)
    rw, rf = plan("checkmate patterns", weak), plan("checkmate patterns", fine)
    assert rw["plan"]["personalization"]["status"] == "weak"
    assert "trouble" in rw["plan"]["personalization"]["reason"]
    assert mean(ratings(rw)) < mean(ratings(rf))


def test_puzzles_but_not_games_prefers_real_game_positions():
    p = learner(rating=1200)
    for _ in range(4):
        p.record_attempt("checkmate", 1100, solved=True, first_try=True)
    p.update_from_history({"patterns": [{"key": "missed_mate", "concept": "checkmate", "title": "Missed mates",
                                         "tier": "recurring", "game_count": 3, "total_games": 10,
                                         "occurrences": 4}]})
    record = plan("checkmate patterns", p)
    assert record["plan"]["personalization"]["gaps"] == ["transfer"]
    for eid in record["plan"]["knowledge"]["example_ids"]:
        assert LIB.get(eid).source["source_type"] in ("lichess_puzzle", "historical_game")


def test_words_in_the_request_beat_the_learner_model():
    strong = learner(rating=2000)
    easy = plan("easy checkmates", strong)
    assert mean(ratings(easy)) < mean(ratings(plan("checkmate patterns", strong)))


def test_role_patterns():
    base = {"reminders": False, "prerequisites": False}
    assert roles_for({**base, "demos": 0, "purpose": "challenge", "level": "advanced"}) == ["practice"] * 3
    assert roles_for({**base, "demos": 2, "purpose": "learn", "level": "beginner"}) == \
        ["demonstration", "demonstration", "guided", "practice"]
    assert roles_for({**base, "demos": 1, "purpose": "learn", "level": "advanced"}) == \
        ["demonstration", "practice", "practice"]


# ------------------------------------------------------------------ adaptation rules
def outcome(eid, rating, score, **kw):
    return Outcome(eid, kw.pop("concept", "checkmate"), rating, score, **kw)


def test_two_clean_solves_swap_the_next_position_for_a_harder_one():
    exs = sorted(LIB.examples_for("checkmate"), key=puzzle_rating)
    a, b, c = exs[0], exs[1], exs[2]
    upcoming = [(c.id, puzzle_rating(c), "practice")]
    d = decide([outcome(a.id, puzzle_rating(a), 1.0), outcome(b.id, puzzle_rating(b), 1.0)], upcoming,
               {a.id, b.id, c.id}, 0, LIB)
    assert d.kind == "harder" and d.replace == c.id
    assert puzzle_rating(LIB.get(d.example_id)) >= max(puzzle_rating(a), puzzle_rating(b)) + 100
    assert d.note


def test_no_swap_when_the_next_one_is_already_harder_or_only_one_clean():
    exs = sorted(LIB.examples_for("checkmate"), key=puzzle_rating)
    hard = exs[-1]
    solved = [outcome(exs[0].id, puzzle_rating(exs[0]), 1.0), outcome(exs[1].id, puzzle_rating(exs[1]), 1.0)]
    assert decide(solved, [(hard.id, puzzle_rating(hard), "practice")], set(), 0, LIB) is None
    assert decide(solved[:1], [(hard.id, 600, "practice")], set(), 0, LIB) is None


def test_a_revealed_solution_inserts_an_easier_position():
    exs = sorted(LIB.examples_for("checkmate"), key=puzzle_rating)
    hard = exs[-1]
    d = decide([outcome(hard.id, puzzle_rating(hard), 0.0, revealed=True)], [], {hard.id}, 0, LIB,
               level="intermediate")
    assert d.kind == "easier" and d.replace is None and d.role == "guided"
    assert puzzle_rating(LIB.get(d.example_id)) <= puzzle_rating(hard) - 80


def test_a_beginner_who_fails_is_shown_a_missing_prerequisite_first():
    ex = LIB.examples_for("smothered_mate")[0]
    fail = [outcome(ex.id, puzzle_rating(ex), 0.0, concept="smothered_mate", revealed=True)]
    d = decide(fail, [], {ex.id}, 0, LIB, level="beginner", known=set())
    assert d.kind == "prerequisite" and d.role == "demonstration"
    assert LIB.get(d.example_id).concept in LIB.descendants("check") and "check" in d.note
    # already knows checks, or isn't a beginner: an easier position of the idea instead
    d = decide(fail, [], {ex.id}, 0, LIB, level="beginner", known={"check"})
    assert d is None or d.kind == "easier"
    assert decide(fail, [], {ex.id}, 0, LIB, level="intermediate").kind != "prerequisite"


def test_easier_falls_back_to_the_wider_idea():
    from app.learner.adapt import prerequisites
    assert prerequisites(LIB, "smothered_mate") == ["check"]
    hard = max(LIB.examples_for("knight_fork"), key=puzzle_rating)
    d = decide([outcome(hard.id, 1600, 0.0, concept="knight_fork", revealed=True)], [], {hard.id}, 0, LIB,
               level="intermediate")
    assert d.kind == "easier" and puzzle_rating(LIB.get(d.example_id)) <= 1520


def test_found_the_idea_but_lost_the_line_practises_calculation():
    longer = [e for e in LIB.examples_for("checkmate") if e.key_ply is not None and len(e.moves) - e.key_ply >= 3]
    hard = max(longer, key=puzzle_rating)
    d = decide([outcome(hard.id, puzzle_rating(hard), 0.0, revealed=True, key_found=True, learner_moves=2)],
               [], {hard.id}, 0, LIB, level="intermediate")
    assert d.kind == "calculation"
    new = LIB.get(d.example_id)
    assert len(new.moves) - new.key_ply >= 3 and puzzle_rating(new) <= puzzle_rating(hard)


def test_adaptation_is_capped_per_lesson():
    ex = LIB.examples_for("checkmate")[-1]
    assert decide([outcome(ex.id, 1500, 0.0, revealed=True)], [], set(), MAX_CHANGES, LIB) is None


def test_help_offers_escalate():
    assert help_offer(1, 0, 2) is None
    assert help_offer(2, 0, 2)["offer"] == "hint"
    assert help_offer(3, 2, 2)["offer"] == "solution"


# ------------------------------------------------------------------ suggestions
def test_suggestions_for_new_learners_follow_goals_or_level():
    p = learner(experience="casual", goals=["endgames"])
    s = suggestions(p, LIB)
    assert s[0]["kind"] == "start" and s[0]["concept"] == "opposition" and s[0]["goal"]
    assert suggestions(learner(rating=1900), LIB)[0]["concept"] == "deflection"


def test_suggestions_put_game_weaknesses_first_and_say_why():
    p = learner(rating=1200)
    p.update_from_history({"patterns": [{"key": "hanging_piece", "concept": "hanging_piece",
                                         "title": "Hanging pieces", "tier": "recurring", "game_count": 5,
                                         "total_games": 10, "occurrences": 6}]})
    for _ in range(3):
        p.record_attempt("pin", 1100, solved=False, first_try=False, revealed=True)
    s = suggestions(p, LIB)
    assert s[0]["kind"] == "weakness" and s[0]["weakness"] == "hanging_piece" and "5 of your last 10" in s[0]["reason"]
    assert any(x["kind"] == "revisit" and x["concept"] == "pin" for x in s)


def test_after_mastering_a_concept_suggest_a_related_new_one():
    p = learner(rating=1200)
    for _ in range(5):
        p.record_attempt("pin", 1000, solved=True, first_try=True)
    s = suggestions(p, LIB, just_studied=["pin"])
    assert s[0]["kind"] == "next" and s[0]["concept"] != "pin"


# ------------------------------------------------------------------ over HTTP
def _walk_to_exercise(client, sid, step):  # noqa: F811
    while step["type"] != "exercise":
        r = client.post(f"/api/sessions/{sid}/advance").json()
        assert not r["completed"]
        step = r["step"]
    return step


def test_onboarding_personalizes_plans_over_the_api(client):  # noqa: F811
    r = client.put("/api/profile/onboarding", json={"rating": 1900, "platform": "chesscom", "goals": ["tactics"]})
    assert r.status_code == 200 and r.json()["profile"]["level"] == "advanced"
    body = client.post("/api/plans", json={"goal": "checkmate patterns", "library": True}).json()
    assert body["plan"]["personalization"]["level"] == "advanced"
    assert client.put("/api/profile/onboarding", json={"platform": "icc"}).status_code == 422


def test_profile_api_preferences_reset_and_username_optional(client):  # noqa: F811
    s = client.get("/api/profile").json()
    assert s["profile"]["new"] and s["suggestions"]
    r = client.put("/api/profile/onboarding", json={"experience": "rules"}).json()
    assert r["profile"]["onboarding"].get("chesscom_username") is None
    r = client.put("/api/profile/preferences", json={"explanation": "brief", "tts": {
        "enabled": True, "speed": "fast", "voice": "af_heart", "read": {"hints": False, "bogus": True},
        "evil": "x"}}).json()
    prefs = r["profile"]["preferences"]
    assert prefs["explanation"] == "brief" and prefs["tts"] == {"enabled": True, "speed": "fast",
                                                                 "voice": "af_heart", "read": {"hints": False}}
    assert client.put("/api/profile/preferences", json={"explanation": "loud"}).status_code == 422
    assert client.post("/api/profile/reset").json()["profile"]["new"]


def test_a_lesson_records_results_adapts_and_ends_with_next_steps(client):  # noqa: F811
    client.put("/api/profile/onboarding", json={"rating": 1300, "platform": "chesscom"})
    body = client.post("/api/plans", json={"goal": "checkmate patterns", "library": True}).json()
    start = client.post(f"/api/lessons/{body['first_lesson_id']}/start").json()
    sid, step = start["session_id"], start["step"]
    total_before = step["total_steps"]
    step = _walk_to_exercise(client, sid, step)
    # two wrong tries → offer a hint; then the learner looks at the answer
    fen_moves = __import__("chess").Board(step["board"]["fen"])
    wrong = next(m for m in fen_moves.legal_moves if fen_moves.san(m) not in step.get("accepted_moves", []))
    first = client.post(f"/api/sessions/{sid}/move", json={"uci": wrong.uci()}).json()
    if first["accepted"]:
        pytest.skip("the chosen move happened to be accepted")
    second = client.post(f"/api/sessions/{sid}/move", json={"uci": wrong.uci()}).json()
    assert second["tries"] == 2 and second["help"]["offer"] == "hint"
    revealed = client.post(f"/api/sessions/{sid}/reveal").json()
    assert revealed["uci"]  # the board can play the answer
    profile = get_profile()
    assert profile.stats["reveals"] >= 1
    assert revealed["adapted"]["kind"] in ("easier", "prerequisite", "calculation")
    assert revealed["adapted"]["total_steps"] > total_before
    # finish the lesson
    notes = []
    while True:
        r = client.post(f"/api/sessions/{sid}/advance").json()
        if r["completed"]:
            break
        step = r["step"]
        if step.get("coach_note"):
            notes.append(step["coach_note"])
        if step["type"] == "exercise":
            client.post(f"/api/sessions/{sid}/reveal")
    assert revealed["adapted"]["note"] in notes
    assert r["summary"]["positions"] >= 3 and r["summary"]["revealed"] == r["summary"]["positions"]
    assert r["summary"]["adapted"] >= 1 and "hard" in r["summary"]["text"]
    assert r["next_steps"][0]["kind"] == "revisit" and r["next_steps"][0]["concept"] == "checkmate"
    assert get_profile().concepts["checkmate"].lessons_completed == 1
    progress = client.get("/api/progress").json()["completed_lessons"]
    assert body["first_lesson_id"] in progress
    assert body["first_lesson_id"] in get_store().get().completed_lessons  # survives a restart


# ------------------------------------------------------------------ games → learner model
def test_game_history_updates_the_learner_model():
    from app.game_api import learn_from_history
    report = {"games_analyzed": 10, "patterns": [
        {"key": "knight_fork", "concept": "knight_fork", "title": "Missed knight forks", "tier": "recurring",
         "game_count": 4, "total_games": 10, "occurrences": 5}], "mistakes": {"per_game": 1.5}}
    games = [{"game": {"player_color": "white", "white_elo": 1400, "black_elo": 1500}},
             {"game": {"player_color": "black", "white_elo": 1300, "black_elo": 1420}},
             {"game": {"player_color": "black", "white_elo": 1300, "black_elo": None}},
             {"game": {"player_color": "white", "white_elo": 1380}}]
    learn_from_history(report, games, "someone")
    p = get_profile()
    assert p.rating == 1400 and p.skill["source"] == "games"
    assert p.weaknesses[0]["key"] == "knight_fork" and p.game_observations["username"] == "someone"
    s = suggestions(p, LIB)
    assert s[0]["kind"] == "weakness" and s[0]["weakness"] == "knight_fork"
    # the lesson for it now mentions the games
    record = plan("knight forks", p)
    assert "4 of your last 10 games" in record["plan"]["personalization"]["reason"]


def test_skipping_onboarding_keeps_the_default_lesson_shape():
    from app.learner import LearnerProfile
    from app.learner.personalize import personalized
    p = LearnerProfile()
    p.set_onboarding(skipped=True)
    assert not p.is_new and not personalized(p)  # not offered again, but nothing to personalize from
    p.set_onboarding(experience="casual")
    assert personalized(p)


# --- the same request, three learners (difficulty tiers of the library) -----------------------
def _solved(record):
    """Puzzle ratings of the positions the learner solves in the main lesson (not demonstrations)."""
    lesson = record["lessons"][0]
    ids = [s["example"] for s in lesson["steps"] if s.get("example") and s.get("type") == "exercise"]
    return [puzzle_rating(LIB.get(e)) for e in dict.fromkeys(ids)]


@pytest.mark.parametrize("goal", ["teach me knight forks", "teach me pins", "teach me skewers"])
def test_same_request_three_learners_three_difficulties(goal):
    beginner = plan(goal, learner(rating=600, experience="casual"))
    club = plan(goal, learner(rating=1200, experience="casual"))
    strong = plan(goal, learner(rating=1800, experience="strong"))
    b, c, s = mean(_solved(beginner)), mean(_solved(club)), mean(_solved(strong))
    assert b < s and c < s, (goal, b, c, s)
    assert b <= c + 60, (goal, b, c)          # (the club player may land on the same band)
    assert max(_solved(beginner)) < min(_solved(strong)), goal  # no overlap between the extremes
    assert set(beginner["plan"]["knowledge"]["example_ids"]) != set(strong["plan"]["knowledge"]["example_ids"])


def test_a_beginners_first_positions_are_simple():
    record = plan("teach me knight forks", learner(rating=600, experience="casual"))
    first = [puzzle_rating(LIB.get(e)) for e in record["plan"]["knowledge"]["example_ids"][:2]]
    assert max(first) <= 950, first


def test_review_says_harder_only_when_it_is_and_is_dropped_when_far_too_easy():
    for rating, exp in ((600, "casual"), (1200, "casual"), (1800, "strong")):
        record = plan("teach me knight forks", learner(rating=rating, experience=exp))
        review = next((l for l in record["lessons"] if l["title"].endswith(": review")), None)
        if review is None:
            continue
        intro = review["steps"][0]["text"]
        practice = [puzzle_rating(LIB.get(s["example"])) for s in review["steps"] if s.get("example")]
        solved = _solved(record)
        if "a little harder" in intro:
            assert mean(practice) > mean(solved), (rating, practice, solved)
    strong = plan("teach me knight forks", learner(rating=1800, experience="strong"))
    target = strong["plan"]["personalization"]["target_rating"]
    for lesson in strong["lessons"]:
        if lesson["title"].endswith(": review"):
            ratings_ = [puzzle_rating(LIB.get(s["example"])) for s in lesson["steps"] if s.get("example")]
            assert max(ratings_) >= target - 350, (ratings_, target)
