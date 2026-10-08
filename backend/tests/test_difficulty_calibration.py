"""Difficulty calibration: *how hard* is separate from *what to train* (learner.difficulty).

Regression tests from the calibration brief:
   1 different skill profiles -> different difficulty for the same weakness
   2 rating alone doesn't decide          3 game evidence moves the profile
   4 several games are needed / used      5 puzzle results update the profile
   6 concept skill can differ from overall
   7 instant solves raise difficulty      8 failures/hints lower it
   9 a specific weakness doesn't mean kindergarten puzzles
  10 no trivial puzzles after mastery     11 deterministic
  12 every target carries inspectable evidence
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import chess
import pytest

from app.analysis.skill_evidence import game_evidence, summarize
from app.knowledge.library import get_knowledge
from app.learner import LearnerProfile
from app.learner.difficulty import (FAST_BONUS, MIN_GAMES, SESSION_STEP, WEAKNESS_SHIFT, ZONE_OFFSET, build,
                                    from_error_rate, performance, puzzle_scale, session_shift)
from app.puzzles import PuzzleLibrary
from app.puzzles.dashboard import candidates, theme_pool
from app.puzzles.select import select

LIB = get_knowledge()
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")
POOL = theme_pool(candidates(PuzzleLibrary(LIB).all()), LIB, "fork", 5)   # verified fork puzzles
FORKS = sorted((p for p in POOL if p.concept == "knight_fork"), key=lambda p: (p.rating, p.id))


def learner(rating: int = 800, game_skill: dict | None = None, weaknesses: list[str] = ()) -> LearnerProfile:
    p = LearnerProfile()
    p.game_observations = {"rating": rating}
    p.skill = {"rating": rating, "source": "games", "evidence": 0}
    p.game_skill = game_skill or {}
    p.weaknesses = [{"key": w, "concept": w, "title": w, "tier": "recurring"} for w in weaknesses]
    return p


def skill(games: int, errors: int, allowed: int, found: int, missed: int, rating: int = 1000) -> dict:
    chances = [{"rating": rating, "found": True, "kind": "pattern"} for _ in range(found)] + \
              [{"rating": rating, "found": False, "kind": "pattern"} for _ in range(missed)]
    return {"games": games, "moves": 30 * games, "errors": errors, "allowed": allowed,
            "phases": {"opening": {"moves": 10 * games, "errors": errors // 3},
                       "middlegame": {"moves": 15 * games, "errors": errors // 3},
                       "endgame": {"moves": 5 * games, "errors": errors // 3}},
            "chances": {"found": found, "missed": missed, "items": chances}}


STRONG = skill(12, errors=8, allowed=2, found=9, missed=1, rating=1100)
WEAK = skill(12, errors=60, allowed=25, found=2, missed=8, rating=800)


class Usage:
    def __init__(self, stats: dict[str, dict]):
        self.stats = stats

    def puzzle_stats(self, pid):
        return self.stats.get(pid, {})

    def all_puzzle_stats(self):
        return dict(self.stats)


def result(solved=True, first=True, seconds=10, hints=0) -> dict:
    return {"attempts": 1, "success_rate": 1.0 if solved else 0.0, "first_try_rate": 1.0 if solved and first else 0.0,
            "hints_used": hints, "average_seconds": seconds, "last_resolved": NOW, "last_result":
            "solved_first_try" if solved and first else "solved" if solved else "failed", "seen": True}


def lookup(pid):
    return next((p for p in POOL if p.id == pid), None)


def mean_rating(sel) -> float:
    return sum(c.puzzle.rating for c in sel.chosen) / len(sel.chosen)


# ---------------------------------------------------------------------- 1, 2, 3
def test_same_weakness_different_skill_profiles_get_different_difficulty():
    strong = build(learner(800, STRONG, ["knight_fork"])).target("knight_fork", LIB)
    weak = build(learner(800, WEAK, ["knight_fork"])).target("knight_fork", LIB)
    assert strong["target"] - weak["target"] >= 250
    a = select(POOL, LIB, "fork", 5, calibration=strong)
    b = select(POOL, LIB, "fork", 5, calibration=weak)
    assert mean_rating(a) > mean_rating(b) + 150


def test_rating_alone_does_not_decide():
    # a 400-rated player whose games show sharp tactics vs a 1100 player who keeps missing them
    low_but_sharp = build(learner(400, STRONG)).target("knight_fork", LIB)
    high_but_blunt = build(learner(1100, WEAK)).target("knight_fork", LIB)
    assert low_but_sharp["target"] > high_but_blunt["target"]
    # same rating, no other evidence: the same target (rating is still an input)
    assert build(learner(900)).target("pin", LIB)["target"] == build(learner(900)).target("pin", LIB)["target"]


def test_game_evidence_moves_the_profile():
    plain = build(learner(800))
    with_games = build(learner(800, STRONG))
    assert with_games.skills["tactics"].estimate > plain.skills["tactics"].estimate + 150
    sources = {e.source for e in with_games.skills["tactics"].evidence}
    assert {"rating", "game chances"} <= sources
    assert any(e.source == "games" for e in with_games.skills["defense"].evidence)
    assert build(learner(800, WEAK)).skills["overall"].estimate < plain.skills["overall"].estimate


# ---------------------------------------------------------------------- 4
def test_one_or_two_games_are_not_enough():
    two = {**STRONG, "games": MIN_GAMES - 1}
    assert [e.source for e in build(learner(800, two)).skills["tactics"].evidence] == ["rating"]
    assert len(build(learner(800, {**STRONG, "games": MIN_GAMES})).skills["tactics"].evidence) > 1


def _doc(gid: str, last: str, loss: int, category: str) -> dict:
    """White wins the queen after 3...Qh4?? with 4.Nxh4 (found) — or plays `last` instead (missed)."""
    moves = ["e2e4", "e7e5", "g1f3", "d8h4", last]
    evals = [{"kind": "cp", "value": v} for v in (30, 30, 30, 30, 900, 900 if loss == 0 else 0)]
    learner_ply = {0: ("excellent", 0), 2: ("good", 10), 4: (category, loss)}
    plies = []
    board = chess.Board()
    for i, u in enumerate(moves):
        e = {"ply": i, "san": board.san(chess.Move.from_uci(u)), "uci": u}
        if i in learner_ply:
            e.update(category=learner_ply[i][0], loss_cp=learner_ply[i][1])
        plies.append(e)
        board.push_uci(u)
    moments = [] if loss == 0 else [{"ply": 4, "best_move_uci": "f3h4", "findings": [{"family": "missed"}]}]
    return {"game": {"id": gid, "player_color": "white", "start_fen": chess.STARTING_FEN, "moves_uci": moves,
                     "result": "1-0" if loss == 0 else "0-1"},
            "analysis": {"plies": plies, "evals": evals, "moments": moments}}


def test_skill_evidence_counts_found_and_missed_chances_across_games():
    found = game_evidence(_doc("a", "f3h4", 0, "excellent")["game"], _doc("a", "f3h4", 0, "excellent")["analysis"])
    assert found["moves"] == 3 and found["errors"] == 0 and found["chances"][0]["found"] is True
    assert found["chances"][0]["capture"] and found["chances"][0]["rating"] >= 400
    docs = [_doc("a", "f3h4", 0, "excellent"), _doc("b", "a2a3", 900, "blunder"), _doc("c", "f3h4", 0, "excellent")]
    s = summarize(docs)
    assert s["games"] == 3 and s["errors"] == 1 and s["chances"] == {**s["chances"], "found": 2, "missed": 1}
    assert {c["game_id"] for c in s["chances"]["items"]} == {"a", "b", "c"}
    # the missed chance is rated from the engine's best move (the queen capture), not the move played
    assert next(c for c in s["chances"]["items"] if not c["found"])["capture"] is True


# ---------------------------------------------------------------------- 5, 6, 7, 8
def test_puzzle_results_update_the_profile_and_instant_solves_raise_difficulty():
    p = learner(600, weaknesses=["knight_fork"])
    before = build(p, Usage({}), lookup).target("knight_fork", LIB)
    easy = FORKS[:5]
    after = build(p, Usage({x.id: result(seconds=8) for x in easy}), lookup).target("knight_fork", LIB)
    assert after["target"] >= before["target"] + 150                  # 7: five instant solves -> harder
    assert after["concept"]["puzzles"] == 5 and after["concept"]["solved_cleanly"] == 5
    assert session_shift([{"solved": True, "first_try": True, "hints": 0, "seconds": 6}] * 2)[0] == SESSION_STEP


def test_failures_and_hints_lower_difficulty():
    p = learner(1200)
    before = build(p, Usage({}), lookup).target("knight_fork", LIB)
    tough = FORKS[len(FORKS) // 2:len(FORKS) // 2 + 5]
    stats = {x.id: result(solved=i == 0, first=False, seconds=200, hints=2) for i, x in enumerate(tough)}
    after = build(p, Usage(stats), lookup).target("knight_fork", LIB)
    assert after["target"] < before["target"] - 50
    assert session_shift([{"solved": False, "hints": 1}, {"solved": True, "hints": 2}])[0] == -SESSION_STEP
    assert session_shift([{"solved": True, "first_try": True, "seconds": 5}, {"solved": False}])[0] == 0


def test_concept_skill_can_differ_from_overall():
    p = learner(900)
    stats = {x.id: result(solved=False, first=False, seconds=90) for x in FORKS[:6]}
    dp = build(p, Usage(stats), lookup)
    forks, _ = dp.concept_estimate("knight_fork", LIB)
    pins, info = dp.concept_estimate("pin", LIB)
    assert forks < pins - 100 and info["puzzles"] == 0
    assert dp.target("knight_fork", LIB)["concept"]["puzzles"] == 6


# ---------------------------------------------------------------------- 9, 10
def test_a_specific_weakness_is_not_kindergarten_level():
    sharp = learner(800, STRONG, weaknesses=["knight_fork"])
    dp = build(sharp)
    t = dp.target("knight_fork", LIB)
    assert t["concept"]["weakness"] and t["concept"]["weakness_shift"] == -WEAKNESS_SHIFT
    assert t["target"] == dp.skills["tactics"].estimate - WEAKNESS_SHIFT - ZONE_OFFSET
    sel = select(POOL, LIB, "knight_fork", 5, calibration=t)
    assert min(c.puzzle.rating for c in sel.chosen) >= t["floor"]
    assert min(c.puzzle.rating for c in sel.chosen) > FORKS[0].rating   # not the easiest forks


def test_mastered_level_is_not_served_trivial_puzzles_again():
    p = learner(600)
    easy = FORKS[:8]
    dp = build(p, Usage({x.id: result(seconds=7) for x in easy}), lookup)
    cal = dp.target("knight_fork", LIB)
    sel = select(POOL, LIB, "fork", 5, calibration=cal, usage=Usage({}))
    assert sel.trivial_skipped > 0 and all(c.puzzle.rating >= cal["floor"] for c in sel.chosen)
    assert max(x.rating for x in easy) < max(c.puzzle.rating for c in sel.chosen)


# ---------------------------------------------------------------------- 11, 12
def test_deterministic_and_explained():
    p = learner(700, STRONG, ["knight_fork"])
    stats = {x.id: result(seconds=30) for x in FORKS[:3]}
    one = build(p, Usage(stats), lookup).target("knight_fork", LIB)
    two = build(p, Usage(stats), lookup).target("knight_fork", LIB)
    assert one == two
    assert [c.puzzle.id for c in select(POOL, LIB, "fork", 5, calibration=one).chosen] == \
           [c.puzzle.id for c in select(POOL, LIB, "fork", 5, calibration=two).chosen]
    # 12: every number has its evidence
    assert one["rule"].startswith(f"target = concept estimate {one['estimate']} - {ZONE_OFFSET}")
    ev = one["skill_evidence"]["evidence"]
    assert {e["source"] for e in ev} >= {"rating", "game chances", "puzzles"}
    assert all(e["detail"] and e["weight"] > 0 for e in ev)
    assert one["concept"]["detail"] and one["zone"][0] < one["target"] < one["zone"][1]
    assert "Aimed at your tactics level" in one["summary"]


def test_formulas_are_the_documented_ones():
    assert puzzle_scale(400) == 760 and puzzle_scale(1500) == 1420
    assert from_error_rate(1, 200) > from_error_rate(10, 100) > from_error_rate(30, 100)
    assert performance([(1000, 0.5, 1.0)], 1000) == 1000
    assert performance([(900, 1.0, 1.0)] * 5, 800) > 1100
    assert FAST_BONUS == 100


# ---------------------------------------------------------------------- the tab
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.knowledge.library import set_knowledge
    from app.knowledge.usage import UsageTracker, set_usage
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_knowledge(LIB)
    set_usage(UsageTracker(tmp_path / "usage.json"))
    from app.main import app
    with TestClient(app) as c:
        yield c
    set_usage(None)


def test_sets_carry_their_difficulty_and_running_sets_adapt(client):
    body = client.post("/api/puzzles/set", json={"mode": "practice", "concept": "fork", "count": 5}).json()
    d = body["difficulty"]
    assert d["summary"].startswith("Aimed at your tactics level") and d["zone"][0] < d["target"] < d["zone"][1]
    full = client.get("/api/puzzles/difficulty?concept=knight_fork").json()
    assert set(full["skills"]) >= {"overall", "tactics", "calculation", "defense", "endgame", "opening",
                                   "pattern_recognition"} and full["target"]["rule"]
    ids = [p["id"] for p in body["puzzles"]]
    done = [{"id": i, "solved": True, "first_try": True, "hints": 0, "seconds": 5} for i in ids[:2]]
    for i in ids[:2]:
        client.post(f"/api/puzzles/{i}/result", json={"solved": True, "first_try": True, "critical_first_try": True,
                                                       "seconds": 5})
    r = client.post("/api/puzzles/adapt", json={"mode": "practice", "concept": "fork", "done": done,
                                                "remaining": ids[2:], "set_ids": ids}).json()
    assert r["shift"] == SESSION_STEP and r["reason"].startswith("Stepping up")
    low = r["difficulty"]["zone"][0]
    for old, new in r["replace"].items():
        assert old in ids[2:] and new["id"] not in ids and new["rating"] >= low
    by_id = {p["id"]: p for p in body["puzzles"]}
    assert all(by_id[pid]["rating"] < low for pid in r["replace"])
    calm = client.post("/api/puzzles/adapt", json={"mode": "practice", "concept": "fork", "done": done[:1],
                                                   "remaining": ids[1:], "set_ids": ids}).json()
    assert calm == {"shift": 0, "reason": None, "replace": {}}
