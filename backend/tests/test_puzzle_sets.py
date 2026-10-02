"""Personalized sets (P3 of the puzzle redesign): the learner's own mistake, the same pattern easy ->
hard, the defensive side, a visible reason per item, and progression by recognition stage.

Covers the brief's source hierarchy (A: your game, B: library, C: generation) and adds:
  - a game position is only served after a fresh Stockfish check, trimmed like any puzzle
  - unclear positions from games are rejected, never served
  - game puzzles stay private (never in Practice, never in the Knowledge Library)
  - results on game puzzles feed the learner model like any other puzzle
  - progression moves to harder *recognition* problems, not just higher ratings
"""
from __future__ import annotations

from dataclasses import replace

import chess
import pytest
from fastapi.testclient import TestClient

from app.engine.service import Line, Score, set_engine
from app.knowledge.generation import GenerationResult
from app.knowledge.library import get_knowledge, set_knowledge
from app.knowledge.usage import UsageTracker, set_usage
from app.learner import get_profile
from app.puzzles import PuzzleLibrary
from app.puzzles import sets
from app.puzzles.from_game import GamePuzzleStore, pick, to_puzzle, verify
from app.puzzles.profile import EngineProfiles
from app.puzzles.progression import FOCUS_BONUS, ladder, stage_of
from app.puzzles.select import relevance, select, targets
from app.session import SessionManager, set_manager

from .games_helpers import FORK_FEN, epd, fork_engine
from .knowledge_helpers import FakeEngine, make_library
from .test_puzzle_library import _analyzed, fork_record, ndjson  # noqa: F401

# the learner (White) skipped Nc7+ here; the engine line goes on after the queen is won
FORK_LINES = [("Nc7+", 850, ["Nc7+", "Kd7", "Nxa8", "Kc8", "Kd2"]), ("Kd2", -900, ["Kd2"]), ("Ke2", -900, ["Ke2"]),
              ("Kf2", -900, ["Kf2"]), ("Nd4", -905, ["Nd4"])]
FORK_EVIDENCE = {"game_id": "g1", "moment_id": "g1:20", "ply": 20, "move_number": 11, "side": "white", "san": "Kd2",
                 "best_move": "Nc7+", "severity": "blunder", "motif": "missed_fork", "family": "missed",
                 "opponent": "alice", "fen": FORK_FEN, "concept": "knight_fork"}
FORK_WEAKNESS = {"key": "knight_fork", "concept": "knight_fork", "title": "Missed knight fork", "game_count": 3,
                 "occurrences": 4, "evidence": [FORK_EVIDENCE]}


class LinesEngine(FakeEngine):
    """Scripted multipv lines for given positions; the greedy material search elsewhere."""

    def __init__(self, lines: dict[str, list[tuple[str, int, list[str]]]]):
        super().__init__()
        self.lines = lines

    def analyse_lines(self, board, depth=None, multipv=3, fresh=False):
        rows = self.lines.get(epd(board.fen()))
        if rows is None:
            return super().analyse_lines(board, depth, multipv, fresh)
        sign = 1 if board.turn == chess.WHITE else -1
        return [Line(move=board.parse_san(san), san=san, score=Score("cp", sign * cp), pv_san=list(pv))
                for san, cp, pv in rows[:multipv]]


def fork_lines_engine():
    return LinesEngine({epd(FORK_FEN): FORK_LINES})


# ------------------------------------------------------------------ A: the learner's own mistake
def test_a_game_mistake_becomes_a_trimmed_verified_puzzle():
    rec, why = verify(FORK_EVIDENCE, fork_lines_engine())
    assert why == "ok" and rec["played_cp"] == -900 and rec["best_cp"] == 850
    p = to_puzzle({**rec, "id": "game:g1:20"}, "knight_fork", "knight_fork")
    # fork -> reply -> take the queen -> done (the engine's "Kc8 Kd2" is not part of the puzzle)
    assert p.solution == ("Nc7+", "Kd7", "Nxa8") and p.trimmed == 2 and p.objective == "material"
    assert p.critical_moves == ("Nc7+",) and p.forced_moves == ("Nxa8",) and p.decision_points == (1,)
    assert p.accepted_first == ("Nc7+",) and p.uniqueness == "unique"
    assert p.tier == "personal" and p.source["type"] == "user_game" and p.weakness == "knight_fork"
    assert "e1d2" not in [p.steps[0]["uci"], *p.steps[0]["accepted"]]   # what they played is never "correct"


def test_unclear_or_harmless_game_moments_are_rejected():
    several = LinesEngine({epd(FORK_FEN): [("Nc7+", 850, ["Nc7+"]), ("Nd6+", 840, ["Nd6+"]), ("Na7", 820, ["Na7"]),
                                           ("Nc3", 800, ["Nc3"]), ("Kd2", -900, ["Kd2"])]})
    assert verify(FORK_EVIDENCE, several) == (None, "several moves are about as good: no single idea to find")
    harmless = LinesEngine({epd(FORK_FEN): [("Nc7+", 100, ["Nc7+"]), ("Kd2", 20, ["Kd2"]), ("Ke2", 0, ["Ke2"])]})
    assert verify(FORK_EVIDENCE, harmless)[1] == "the move played was not clearly worse at full depth"
    assert verify({**FORK_EVIDENCE, "side": "black"}, fork_lines_engine())[1] == "not the learner's move"


def test_two_equally_good_answers_are_both_accepted():
    two = LinesEngine({epd(FORK_FEN): [("Nc7+", 850, ["Nc7+", "Kd7", "Nxa8"]), ("Nd6+", 800, ["Nd6+"]),
                                       ("Kd2", -900, ["Kd2"]), ("Ke2", -900, ["Ke2"])]})
    rec, why = verify(FORK_EVIDENCE, two)
    p = to_puzzle({**rec, "id": "x"}, "knight_fork", "knight_fork")
    assert why == "ok" and p.accepted_first == ("Nc7+", "Nd6+") and p.uniqueness == "multiple"


def test_pick_verifies_once_and_respects_novelty(tmp_path):
    store, usage = GamePuzzleStore(tmp_path / "g.json"), UsageTracker(tmp_path / "u.json")
    engine = fork_lines_engine()
    p, trail = pick(FORK_WEAKNESS, engine, usage, store=store)
    assert p is not None and p.id == "game:g1:20" and trail[-1]["status"] == "used"
    calls = engine.calls
    usage.record_used([p])                        # shown today ...
    again, trail = pick(FORK_WEAKNESS, engine, usage, store=store)
    assert again is None and trail[0]["status"] == "not due" and engine.calls == calls   # ... not repeated
    # the same position from another game isn't "new" either
    twin = {**FORK_WEAKNESS, "evidence": [{**FORK_EVIDENCE, "game_id": "g2", "moment_id": "g2:20"}]}
    assert pick(twin, engine, usage, store=store)[0] is None
    # a rejected moment is remembered (no second engine run)
    bad = {**FORK_WEAKNESS, "evidence": [{**FORK_EVIDENCE, "game_id": "g3", "san": "Nc7+"}]}
    store = GamePuzzleStore(tmp_path / "g2.json")
    assert pick(bad, engine, usage, store=store)[1][0]["status"] == "rejected"
    calls = engine.calls
    assert pick(bad, engine, usage, store=store)[1][0]["status"] == "rejected before" and engine.calls == calls


# ------------------------------------------------------------------ the set, end to end
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_knowledge(make_library(tmp_path, {"tactics/t": [
        fork_record(), fork_record(id="test_knight_fork_2", start_fen="q3k3/8/8/1N6/8/8/8/5K2 w - - 0 1"),
        fork_record(id="test_knight_fork_3", start_fen="q3k3/8/8/1N6/8/8/8/6K1 w - - 0 1")]}))
    set_usage(UsageTracker(tmp_path / "usage.json"))
    engine = fork_engine()
    engine.analyse_lines = fork_lines_engine().analyse_lines   # scripted multipv for the game position
    set_engine(engine)
    set_manager(SessionManager())
    from app.main import app
    with TestClient(app) as c:
        yield c
    set_engine(None)
    set_manager(None)
    set_usage(None)


def test_personalized_set_opens_with_your_game_then_the_same_pattern(client, monkeypatch):
    import app.analysis.personal_puzzles as pp
    monkeypatch.setattr(pp, "generate_for", lambda *a, **k: GenerationResult("knight_fork", attempts=1))
    _analyzed(client)
    body = client.post("/api/puzzles/set", json={"mode": "personalized", "count": 5}).json()
    first, rest = body["puzzles"][0], body["puzzles"][1:]
    assert first["role"] == "your_game" and first["role_label"] == "Your game" and first["origin"] == "your_game"
    assert first["id"].startswith("game:chesscom-60000000") and first["fen"] == FORK_FEN
    assert first["solution"] == ["Nc7+", "Kd7", "Nxa8"] and first["critical_moves"] == ["Nc7+"]
    assert "Your own game vs" in first["why"] and "Kd2" in first["why"]
    assert "You played" in first["explanation"].replace("you played", "You played") and "Nc7+" in first["explanation"]
    assert first["source"].startswith("Your game")
    # the other items: the exact skill, each with a short reason from the evidence
    assert [p["role"] for p in rest] and all(p["role"] in ("same", "easier", "harder") for p in rest)
    assert all(p["concept"] == "knight_fork" and "2 recent games" in p["why"] for p in rest)
    assert "test_knight_fork" not in [p["id"] for p in rest]   # library copies of their position: never
    assert body["debug"]["your_game"]["used"] == first["id"] and body["debug"]["roles"][first["id"]] == "your_game"
    # private: not in Practice, not in the Knowledge Library
    practice = client.post("/api/puzzles/set", json={"mode": "practice", "concept": "fork", "count": 5}).json()
    assert not any(p["id"].startswith("game:") for p in practice["puzzles"])
    assert get_knowledge().get(first["id"]) is None
    # solving it is recorded like any puzzle and feeds the learner model
    r = client.post(f"/api/puzzles/{first['id']}/result", json={"solved": True, "first_try": True,
                                                               "critical_first_try": True, "seconds": 12}).json()
    assert r["recorded"] and r["concept"] == "knight_fork" and r["stats"]["attempts"] == 1
    assert get_profile().concepts["knight_fork"].solved >= 1
    assert client.get(f"/api/puzzles/{first['id']}").json()["source"]["type"] == "user_game"


# ------------------------------------------------------------------ roles, reasons, defensive side
def test_roles_order_and_reasons():
    lib = get_knowledge()
    p = next(iter(PuzzleLibrary(lib).all()))
    assert sets.role_for(replace(p, rating=700), 900) == "easier"
    assert sets.role_for(replace(p, rating=950), 900) == "same"
    assert sets.role_for(replace(p, rating=1100), 900) == "harder"
    items = [{"puzzle": replace(p, id=i, rating=r), "role": role} for i, r, role in
             [("d", 800, "defend"), ("h", 1100, "harder"), ("e", 700, "easier"), ("s2", 950, "same"),
              ("s1", 900, "same"), ("g", 1500, "your_game")]]
    assert [it["puzzle"].id for it in sets.order(items)] == ["g", "s1", "s2", "e", "h", "d"]
    assert sets.why(FORK_WEAKNESS, "harder") == "Missed knight fork — you missed this 4 times in 3 recent games " \
                                                "(a step harder)."
    walked = {"key": "walked_into_fork", "title": "Walked into a fork", "game_count": 3, "occurrences": 3,
              "evidence": [{"family": "allowed"}]}
    assert sets.walked_into(walked) and not sets.walked_into(FORK_WEAKNESS)
    assert "cost you material in 3 recent games" in sets.why(walked, "same")
    assert sets.why(walked, "defend").startswith("Walked into a fork: the defensive side")


def test_defensive_item_only_for_walked_into_weaknesses(tmp_path):
    lib = get_knowledge()
    index = PuzzleLibrary(lib)
    item, debug = sets.defend_item(FORK_WEAKNESS, lib, index, None, {}, 900, set())
    assert item is None and debug["applies"] is False
    walked = {"key": "walked_into_fork", "concept": "knight_fork", "title": "Walked into a fork",
              "evidence": [{"family": "allowed"}]}
    item, debug = sets.defend_item(walked, lib, index, None, {}, 900, set(), engine=None)
    assert debug["applies"] is True
    if item is not None:   # the library has defence puzzles: one of them, a real "stop the threat" item
        assert item["role"] == "defend" and item["puzzle"].type == "defense" and item["puzzle"].clear_start
    else:                  # none and no engine: nothing unverified is invented
        assert debug["source"].startswith("none")


def test_a_defend_puzzle_made_for_a_weakness_does_not_fill_its_skill_slots():
    lib = get_knowledge()
    p = next(iter(PuzzleLibrary(lib).all()))
    defend = replace(p, id="d", tier="personal", weakness="knight_fork", concept="spotting_threats",
                     concepts=("spotting_threats",), facts={})
    fork = replace(p, id="f", tier="personal", weakness="knight_fork", concept="knight_fork",
                   concepts=("knight_fork",), facts={})
    wanted = targets("knight_fork", lib)
    assert relevance(fork, "knight_fork", wanted) == (1.0, "personal")
    assert relevance(defend, "knight_fork", wanted)[0] < 0.95


# ------------------------------------------------------------------ progression (stats -> what's next)
def _staged(p, pid, plausible, quiet=False, rating=900):
    return replace(p, id=pid, rating=rating, meaningful_moves=1, clear_start=True,
                   difficulty_basis={**p.difficulty_basis, "plausible": plausible, "quiet": quiet})


def test_progression_moves_to_harder_recognition_not_just_higher_ratings(tmp_path):
    lib = make_library(tmp_path, {"tactics/t": [fork_record(), fork_record(
        id="b", start_fen="q3k3/8/8/1N6/8/8/8/5K2 w - - 0 1")]})
    base = PuzzleLibrary(lib, EngineProfiles(bundled=tmp_path / "none.json")).all()[0]
    spots = [_staged(base, f"s{i}", plausible=2) for i in range(4)]
    choose = _staged(base, "c", plausible=5)
    deep = _staged(base, "q", plausible=3, quiet=True)
    assert [stage_of(p) for p in (spots[0], choose, deep)] == ["spot", "choose", "deep"]
    assert stage_of(replace(spots[0], meaningful_moves=2)) == "deep"   # two real decisions

    fresh = ladder(spots + [choose, deep], lambda _id: {})
    assert fresh.focus == "spot" and fresh.note is None
    # three easy forks solved first try -> the next thing to train is choosing among candidates
    clean = {f"s{i}": {"attempts": 1, "first_try_rate": 1.0} for i in range(3)}
    lad = ladder(spots + [choose, deep], lambda pid: clean.get(pid, {}))
    assert lad.focus == "choose" and "next: several moves look reasonable" in lad.note
    assert lad.bonus(choose) == FOCUS_BONUS and lad.bonus(spots[3]) < 0 < lad.bonus(deep)
    # with ratings equal, selection now prefers the "choose" puzzle over another easy one
    sel = select([spots[3], choose], lib, "knight_fork", count=1, bonus=lad.bonus)
    assert sel.chosen[0].puzzle.id == "c"
    assert select([spots[3], choose], lib, "knight_fork", count=1).chosen[0].puzzle.id == "s3"  # tie -> id order
    # missing "choose" puzzles keeps the focus there (no blind difficulty increase)
    stats = {**clean, "c": {"attempts": 3, "first_try_rate": 0.0}}
    assert ladder(spots + [choose, deep], lambda pid: stats.get(pid, {})).focus == "choose"


def test_dashboard_cards_show_the_next_stage(client, monkeypatch):
    _analyzed(client)
    card = client.get("/api/puzzles/dashboard").json()["personalized"]["main"]
    assert card["focus"]["focus"] in ("spot", "choose", "deep") and "bands" in card["focus"]
    assert "a position from your own games" in client.get("/api/puzzles/dashboard").json()[
        "personalized"]["profile"]["recommendation"]
