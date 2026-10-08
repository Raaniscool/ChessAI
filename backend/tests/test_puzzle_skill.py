"""Skill targeting: a specific weakness is only served by puzzles of exactly that skill.

Regression 7 from the puzzle redesign brief: a generic category can't satisfy a specific weakness.
"""
from __future__ import annotations

from dataclasses import replace

from app.puzzles import PuzzleLibrary
from app.puzzles.personal import library_selection
from app.puzzles.profile import EngineProfiles
from app.puzzles.skill import classify, skill_for, split

from .knowledge_helpers import fork_record

KNIGHT = {"fork": {"move": "1.Nc7+", "attacker": {"piece": "knight", "color": "white", "square": "c7"},
                   "targets": [{"piece": "king", "color": "black", "square": "e8"},
                               {"piece": "queen", "color": "black", "square": "a8"}], "gives_check": True}}
OTHER_PIECE = {"fork": {**KNIGHT["fork"], "attacker": {"piece": "queen", "color": "white", "square": "c7"}}}


def records():
    v = {"status": "verified"}
    return {"tactics/t": [
        fork_record(**v, id="exact", facts=KNIGHT),
        fork_record(**v, id="generic_proven", concept="fork", start_fen="q3k3/8/8/1N6/8/8/8/5K2 w - - 0 1",
                    facts=KNIGHT),
        # a real queen fork filed under the broad "fork" concept
        fork_record(**v, id="generic_queen", concept="fork", start_fen="4k3/8/8/8/7n/8/8/3QK3 w - - 0 1",
                    moves=["Qa4+", "Kf7", "Qxh4"], key_move="1.Qa4+", notes={}),
        fork_record(**v, id="secondary", concept="double_attack", concepts=["double_attack", "knight_fork"],
                    start_fen="q3k3/8/8/1N6/8/8/8/3K4 w - - 0 1"),
    ]}


def puzzles(tmp_path):
    from .knowledge_helpers import make_library
    lib = make_library(tmp_path, records())
    return lib, {p.id: p for p in PuzzleLibrary(lib, EngineProfiles(bundled=tmp_path / "none.json")).all()}


def test_the_skill_of_a_weakness():
    assert skill_for("knight_fork").attacker == "knight" and skill_for("knight_fork").family == "fork"
    assert skill_for("absolute_pin").attacker is None


def test_only_the_exact_skill_satisfies_a_weakness(tmp_path):
    lib, ps = puzzles(tmp_path)
    skill = skill_for("knight_fork")
    verdict = {pid: classify(p, skill, lib)[0] for pid, p in ps.items()}
    assert ps["generic_queen"].facts["fork"]["attacker"]["piece"] == "queen"   # verified facts, not labels
    assert verdict["exact"] == "satisfies"
    assert verdict["generic_proven"] == "satisfies"     # "fork", and the verified facts show a knight fork
    assert verdict["generic_queen"] == "none"           # a fork, but not the skill
    assert verdict["secondary"] == "partial"            # mainly another idea
    # labelled knight fork, but the facts show another piece: not trusted for the slot
    assert classify(replace(ps["exact"], facts=OTHER_PIECE), skill, lib)[0] == "partial"
    keep, overrides, partial = split(list(ps.values()), "knight_fork", lib)
    assert {p.id for p in keep} == {"exact", "generic_proven"} and set(overrides) == {"generic_proven"}
    assert partial == 1


def test_a_weakness_set_never_fills_slots_with_partial_matches(tmp_path):
    lib, _ps = puzzles(tmp_path)
    from app.knowledge.library import set_knowledge
    set_knowledge(lib)
    try:
        sel = library_selection({"key": "knight_fork", "concept": "knight_fork", "title": "Missed knight fork"},
                                lib, 5)
        assert {c.puzzle.id for c in sel.chosen} == {"exact", "generic_proven"}
        assert sel.shortfall == 3 and sel.partial == 1   # the shortfall goes to constrained generation
        # a broader request (practice "fork") may use all of them
        from app.puzzles import get_puzzles, select
        broad = select(get_puzzles(lib).all(), lib, "fork", count=5)
        assert len(broad.chosen) >= 4
    finally:
        set_knowledge(None)
