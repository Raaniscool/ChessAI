"""Puzzle profile (Lichess-style puzzles): decisions vs forced moves, where a puzzle ends, difficulty.

Regression tests from the puzzle redesign brief:
  1 no unnecessary continuation moves      2 forced moves don't inflate difficulty
  3 a 1-critical-move puzzle is possible   4 several critical moves are possible
  5 critical points are identified         9 a puzzle ends once its objective is met
  10 difficulty comes from decisions, not move count
"""
from __future__ import annotations

import json
from pathlib import Path

import chess

from app.puzzles.model import from_example
from app.puzzles.profile import EngineProfiles, build, decision_rating, remember, signature

from .knowledge_helpers import FORK_FEN, FakeEngine, fork_record, make_library

# 1.Nc7+ forks king and rook; 2.Nxa8 cashes in. Then two filler moves nobody needs to play.
ROOK_FORK = "r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"
FORK_LINE = ["Nc7+", "Kd7", "Nxa8", "Kc6", "Kd2"]
FORK_DATA = {"0": [["b5c7", 480], ["b5d6", 0], ["e1d2", 0], ["b5a7", -20], ["e1e2", 0]],
             "2": [["c7a8", 500], ["c7e6", 0], ["c7b5", 0]],
             "4": [["e1d2", 500], ["e1e2", 500], ["e1f2", 495], ["e1d1", 490], ["e1f1", 490]]}
# 1.Ra7 (quiet) ... 2.Rb8# : two real decisions
LADDER = "7k/8/8/8/8/8/R7/1R4K1 w - - 0 1"
LADDER_LINE = ["Ra7", "Kg8", "Rb8#"]
LADDER_DATA = {"0": [["a2a7", 9980], ["b1b7", 600], ["a2a3", 500], ["g1f2", 450], ["b1c1", 400]],
               "2": [["b1b8", 9990], ["a7a8", 600], ["a7a6", 500]]}
BACK_RANK = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
TWO_MATES = "6k1/5ppp/8/8/8/8/8/RR4K1 w - - 0 1"   # Ra8# and Rb8# both mate


def kinds(profile):
    return [(s.san, s.kind) for s in profile.steps]


# ---------------------------------------------------------------- 1 / 9 where the puzzle ends
def test_fork_puzzle_ends_once_the_target_is_won():
    p = build(ROOK_FORK, FORK_LINE, 0, data=FORK_DATA)
    assert p.solution == ["Nc7+", "Kd7", "Nxa8"]      # fork -> reply -> win the target -> done
    assert p.objective == "material" and p.trimmed == 2
    assert p.steps[-1].reply_san is None               # the puzzle ends on the learner's move


def test_no_continuation_without_engine_data_either():
    p = build(ROOK_FORK, FORK_LINE, 0)                  # heuristics only
    assert p.solution == ["Nc7+", "Kd7", "Nxa8"]
    assert kinds(p) == [("Nc7+", "critical"), ("Nxa8", "forced")]
    assert p.steps[1].reason == "takes the target"


def test_mate_puzzle_runs_to_mate_and_defense_stops_when_the_threat_is_met():
    mate = build(LADDER, LADDER_LINE, 0, family="mate", data=LADDER_DATA)
    assert mate.objective == "mate" and mate.solution == LADDER_LINE
    # the same fork line as a "defense" family puzzle still wins material -> material objective
    assert build(ROOK_FORK, FORK_LINE, 0, family="defense", data=FORK_DATA).objective == "material"
    # a defensive move with only forced follow-ups ends right after the defence
    quiet = build("4k3/8/8/8/8/8/3q4/R3K3 w - - 0 1", ["Kxd2", "Ke7", "Ra7+", "Kd6", "Rxh7"], 0,
                  family="defense", data={"0": [["e1d2", 900], ["a1a8", -900]],
                                          "2": [["a1a7", 0], ["d2e3", 0]],
                                          "4": [["a7a8", 0], ["d2d3", 0]]})
    assert quiet.solution == ["Kxd2"]


# ---------------------------------------------------------------- 2 / 10 difficulty
def test_forced_moves_add_no_difficulty():
    p = build(ROOK_FORK, FORK_LINE, 0, data=FORK_DATA)
    assert [s.rating for s in p.steps if s.kind == "forced"] == [None]
    assert p.rating == p.steps[0].rating == decision_rating(p.steps[0].features)


def test_difficulty_comes_from_decisions_not_move_count():
    short = build(ROOK_FORK, ["Nc7+", "Kd7", "Nxa8"], 0, data=FORK_DATA)
    padded = build(ROOK_FORK, FORK_LINE, 0, data=FORK_DATA)
    assert padded.rating == short.rating               # filler moves change nothing
    one_move_mate = build(BACK_RANK, ["Ra8#"], 0, family="mate", data={"0": [["a1a8", 9990], ["g1f2", 0]]})
    two_decisions = build(LADDER, LADDER_LINE, 0, family="mate", data=LADDER_DATA)
    assert two_decisions.rating > one_move_mate.rating  # a quiet first move is a harder decision


def test_quiet_and_sacrificial_decisions_rate_higher_than_checks_and_free_captures():
    base = dict(quiet=False, check=False, capture=False, sacrifice=False, backward=False, free_capture=False,
                legal_moves=20, plausible=1, payoff_plies=1)
    assert decision_rating({**base, "quiet": True}) > decision_rating({**base, "check": True})
    assert decision_rating({**base, "sacrifice": True}) > decision_rating(base)
    assert decision_rating({**base, "capture": True, "free_capture": True}) < decision_rating(base)
    assert decision_rating({**base, "plausible": 6}) > decision_rating(base)


# ---------------------------------------------------------------- 3 / 4 / 5 critical decisions
def test_a_one_critical_move_puzzle():
    p = build(BACK_RANK, ["Ra8#"], 0, family="mate", data={"0": [["a1a8", 9990], ["g1f2", 0]]})
    assert kinds(p) == [("Ra8#", "critical")] and p.solution == ["Ra8#"]


def test_several_critical_moves():
    p = build(LADDER, LADDER_LINE, 0, family="mate", data=LADDER_DATA)
    assert kinds(p) == [("Ra7", "critical"), ("Rb8#", "critical")]
    assert p.steps[1].reason == "checkmate"


def test_any_checkmate_solves_a_mate_in_one():
    p = build(TWO_MATES, ["Ra8#"], 0, family="mate", data={"0": [["a1a8", 9990], ["b1b8", 9990], ["g1f2", 0]]})
    assert kinds(p) == [("Ra8#", "critical")] and p.steps[0].accepted == ["b1b8"] and p.clear_start


def test_a_slower_win_is_good_but_not_the_solution():
    near = {**FORK_DATA, "0": [["b5c7", 480], ["b5d6", 340], ["e1d2", 0]]}
    step = build(ROOK_FORK, FORK_LINE, 0, data=near).steps[0]
    assert step.kind == "critical" and step.good == ["b5d6"]   # "good, but there's a stronger move"


def test_critical_points_identified_from_the_engine_margin():
    p = build(ROOK_FORK, FORK_LINE, 0, data=FORK_DATA)
    assert p.steps[0].kind == "critical" and p.steps[0].features["margin_cp"] == 480
    # if Stockfish says another move is just as good, the step is not a unique decision
    soft = {**FORK_DATA, "0": [["b5c7", 480], ["b5d6", 450], ["e1d2", 0]]}
    open_start = build(ROOK_FORK, FORK_LINE, 0, data=soft)
    assert open_start.steps[0].kind == "open" and open_start.steps[0].good == ["b5d6"]
    assert not open_start.clear_start
    # ... unless that move was verified as an accepted alternative
    accepted = build(ROOK_FORK, FORK_LINE, 0, data=soft, accepted={0: ["Nd6+"]})
    assert accepted.steps[0].kind == "critical" and accepted.steps[0].accepted == ["b5d6"]


# ---------------------------------------------------------------- engine data store / puzzle model
def test_engine_profiles_match_by_signature(tmp_path):
    store = EngineProfiles(bundled=tmp_path / "none.json", runtime=tmp_path / "rt.json")
    sig = signature(ROOK_FORK, FORK_LINE, 0)
    store.put("x", sig, FORK_DATA)
    assert store.get("x", sig) == FORK_DATA and store.get("x", "other") is None
    again = EngineProfiles(bundled=tmp_path / "none.json", runtime=tmp_path / "rt.json")
    assert again.get("x", sig) == FORK_DATA            # persisted for the next run
    assert store.version == 1


def test_remember_stores_engine_data_for_a_new_puzzle(tmp_path):
    from app.puzzles import profile as P
    store = EngineProfiles(bundled=tmp_path / "none.json", runtime=tmp_path / "rt.json")
    P.set_engine_profiles(store)
    try:
        lib = make_library(tmp_path, {"tactics/test": [fork_record(status="verified")]})
        ex = lib.get("test_knight_fork")
        engine = FakeEngine({chess.Board(FORK_FEN).epd(): {"Nc7+": 850}})
        remember(ex, engine)
        data = store.get(ex.id, signature(ex.start_fen, ex.moves, ex.key_ply))
        assert data and data["0"][0][0] == "b5c7"
    finally:
        P.set_engine_profiles(None)


def test_puzzle_model_exposes_the_profile(tmp_path):
    lib = make_library(tmp_path, {"tactics/test": [fork_record(status="verified")]})
    store = EngineProfiles(bundled=tmp_path / "none.json")
    puzzle = from_example(lib.get("test_knight_fork"), lib, store)
    d = puzzle.as_dict()
    assert d["critical_moves"] == ["Nc7+"] and d["forced_moves"] == ["Nxa8"]
    assert d["decision_points"] == [1] and d["meaningful_moves"] == 1
    assert d["expected_solution_length"] == 2 and d["primary_concept"] == "knight_fork"
    assert d["objective"] == "material" and d["solution"] == ["Nc7+", "Kd7", "Nxa8"]
    assert d["steps"][0]["reply_san"] == "Kd7" and 1 <= d["difficulty"] <= 5
    json.dumps(d)  # serialisable for the API


def test_rule_drills_are_not_puzzles(tmp_path):
    lib = make_library(tmp_path, {"basics/test": [fork_record(status="verified", category="basics")]})
    assert from_example(lib.get("test_knight_fork"), lib, EngineProfiles(bundled=Path("/none"))) is None
