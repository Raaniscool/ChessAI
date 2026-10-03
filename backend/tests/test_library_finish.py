"""Finishing the library expansion (final pre-V3 fixes, item 9): the remaining standard ideas
(x-ray, double-bishop and epaulette mates, the Greek gift, the windmill, the desperado, zugzwang,
perpetual check, stalemate tricks) and top-ups of thin concepts.

As in test_library_expansion.py, every validator is checked both ways — it finds the idea where it
is, and refuses a position that only looks similar — and the data tests check that what entered
the library went through the whole pipeline with its provenance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine import EngineUnavailable, get_engine, set_engine
from app.knowledge import teaching, validators
from app.knowledge.engine_check import verify as engine_verify
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge
from app.knowledge.positions import replay
from app.knowledge.sources import lichess_puzzles

KNOWLEDGE = Path(__file__).resolve().parents[1] / "app" / "knowledge" / "data"
FINISHED = ["x_ray", "double_bishop_mate", "epaulette_mate", "greek_gift", "windmill", "desperado", "zugzwang",
            "perpetual_check", "stalemate_tricks"]


def run(kind: str, fen: str, line: str, key_ply: int | None = 0, mistake_ply: int | None = None) -> dict:
    return validators.run(kind, validators.Ctx(replay(fen, line.split()), key_ply=key_ply, mistake_ply=mistake_ply), {})


def fails(kind: str, fen: str, line: str, key_ply: int | None = 0) -> str:
    with pytest.raises(validators.Fail) as err:
        run(kind, fen, line, key_ply)
    return str(err.value)


# ------------------------------------------------------------------ the library
def test_every_finished_concept_has_verified_examples():
    lib = get_knowledge()
    for cid in FINISHED:
        assert cid in lib.concepts, cid
        assert lib.count_for(cid) >= 1, f"{cid} has no verified example"


def test_thin_concepts_were_topped_up():
    lib = get_knowledge()
    for cid, at_least in [("double_attack", 3), ("spotting_threats", 3), ("relative_pin", 3), ("pawn_fork", 3),
                          ("hanging_queen", 3), ("poisoned_pawn", 3), ("queen_mate", 2), ("rook_mate", 2),
                          ("ladder_mate", 2), ("philidor_position", 2), ("lucena_position", 2), ("early_queen", 2),
                          ("repeated_moves", 2), ("ignoring_development", 2)]:
        assert lib.count_for(cid) >= at_least, cid


def test_glossary_terms_became_concepts():
    glossary = get_glossary()
    lib = get_knowledge()
    for cid in FINISHED:
        assert cid not in glossary.terms, cid
        assert lib.concepts[cid].summary


def test_new_examples_carry_provenance_and_passed_every_stage():
    lib = get_knowledge()
    ids = {e.id for e in lib.verified()}
    records = [r for path in (KNOWLEDGE / "examples").rglob("*_expansion.json")
               for r in json.loads(path.read_text(encoding="utf-8")) if r["concept"] in FINISHED]
    assert records
    for rec in records:
        assert rec["id"] in ids
        assert set(st["outcome"] for st in rec["verification"]["stages"]) == {"pass"}, rec["id"]
        src = rec["source"]
        assert src["source_license"] and src["reference"] and src.get("import_date"), rec["id"]


def test_spotting_threats_imports_are_defensive_moves():
    """A threat "parried" by giving check or mate is not the defensive lesson."""
    for ex in get_knowledge().examples_for("spotting_threats"):
        if "lichess" not in ex.tags:
            continue
        rep = ex.replay()
        assert not rep.boards[ex.key_ply + 1].is_check(), ex.id
        assert not rep.final.is_checkmate(), ex.id


def test_no_early_queen_from_mid_game_puzzles():
    """A Lichess puzzle starts mid-game: it can't show that a queen came out early."""
    assert "earlyQueen" not in lichess_puzzles.EXPANSION_MISTAKE_THEMES
    for ex in get_knowledge().examples_for("early_queen"):
        assert ex.source.get("source_type") != "lichess_puzzle", ex.id


# ------------------------------------------------------------------ validators, both ways
XRAY = "4q1k1/5ppp/8/4n3/8/8/5PPP/4R1K1 w - - 0 1"


def test_x_ray():
    facts = run("x_ray", XRAY, "f4 Nd3 Rxe8#")
    assert facts["slider"]["square"] == "e1" and facts["through"]["piece"] == "knight" and facts["square"] == "e8"
    # nothing in between: a plain capture, not an x-ray
    assert "x-ray" in fails("x_ray", "4q1k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "h3 h6 Rxe8#")


def test_double_bishop_mate():
    facts = run("double_bishop_mate", "7k/2B4p/8/8/2B5/8/8/6K1 w - - 0 1", "Be5#")
    assert facts["second_bishop"]["square"] == "c4"
    assert "bishop" in fails("double_bishop_mate", "3rkr2/8/8/8/8/8/Q7/4K3 w - - 0 1", "Qe6#")


def test_epaulette_mate():
    facts = run("epaulette_mate", "3rkr2/8/8/8/8/8/Q7/4K3 w - - 0 1", "Qe6#")
    assert [b["square"] for b in facts["blocked_by"]] == ["d8", "f8"]
    # a back-rank mate by a rook is not an epaulette mate
    assert "queen" in fails("epaulette_mate", "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "Ra8#")


GREEK = "r1bq1rk1/pppn1ppp/4p3/3pP3/1b1P4/2NB1N2/PPP2PPP/R2QK2R w KQ - 0 1"


def test_greek_gift():
    facts = run("greek_gift", GREEK, "Bxh7+ Kxh7 Ng5+ Kg8 Qh5")
    assert facts["square"] == "h7" and facts["knight_check"] == "2.Ng5+"
    # the king declines: no knight check follows the capture
    assert "Greek gift" in fails("greek_gift", GREEK, "Bxh7+ Kh8 Ng5 g6")


WINDMILL = "r2q3k/1b1n1pRp/8/8/8/8/1B6/6K1 w - - 0 1"


def test_windmill_follows_the_piece_as_it_swings_back():
    facts = run("windmill", WINDMILL, "Rxf7+ Kg8 Rg7+ Kh8 Rxd7+ Kg8")
    assert facts["discovered_checks"] == ["1.Rxf7+", "3.Rxd7+"] and facts["captures"] == 2
    assert "twice" in fails("windmill", WINDMILL, "Rxf7+ Kg8")


def test_desperado():
    facts = run("desperado", "r3k2r/ppp2ppp/3p4/2b1N3/8/8/PPP2PPP/R3K2R w KQkq - 0 1", "Nxf7 Kxf7")
    assert facts["desperado"]["square"] == "e5" and facts["captured"]["piece"] == "pawn"
    # the knight wasn't attacked: just a capture
    fails("desperado", "r3k2r/ppp2ppp/8/2b1N3/8/8/PPP2PPP/R3K2R w KQkq - 0 1", "Nxf7 Kxf7")
    # taking the checking piece is answering a check, not a desperado
    fails("desperado", "4k3/8/3p4/4N3/6p1/5n2/8/4K3 w - - 0 1", "Nxf3 gxf3")


def test_zugzwang_rules_part():
    facts = run("zugzwang", "8/8/8/4k3/8/4K3/4P3/8 w - - 0 1", "Kd3 Kd5")
    assert facts["move"] == "1.Kd3"
    assert "quiet" in fails("zugzwang", "8/8/8/3k4/8/5K2/4P3/8 w - - 0 1", "e4+ Ke5")
    # a king getting out of check doesn't hand the opponent the move
    assert "out of check" in fails("zugzwang", "8/8/8/3k4/8/5K2/4P3/8 w - - 0 1", "e4+ Kd6 Kf4 Ke6", key_ply=1)


PERPETUAL = "7k/6p1/7p/8/8/8/rr6/4Q1K1 w - - 0 1"


def test_perpetual_check():
    facts = run("perpetual_check", PERPETUAL, "Qe8+ Kh7 Qe4+ g6 Qe7+ Kg8 Qe8+ Kg7 Qe7+")
    assert facts["behind_by"] == 3 and len(facts["checks"]) == 5
    # not behind: nothing to save
    assert "behind" in fails("perpetual_check", "7k/6p1/7p/8/8/8/8/4Q1K1 w - - 0 1", "Qe8+ Kh7 Qe4+ Kh8 Qe8+")
    # a quiet move in between
    assert "check" in fails("perpetual_check", PERPETUAL, "Qe8+ Kh7 Qe2 Kg6")
    # the checks win the material back: the draw doesn't come from the checks
    assert "material back" in fails("perpetual_check", "7k/8/6pp/4r3/8/8/r7/4Q1K1 w - - 0 1",
                                    "Qxe5+ Kh7 Qe7+ Kh8 Qe8+")


def test_stalemate_trick():
    facts = run("stalemate_trick", "7k/7p/8/8/8/6Rp/5q1P/7K w - - 0 1", "Rg8+ Kxg8")
    assert facts["stalemated"] == "white" and facts["behind_by"] == 5
    assert "stalemate" in fails("stalemate_trick", "7k/7p/8/8/8/6Rp/5q1P/7K w - - 0 1", "Rg4 Qf1+")


# ------------------------------------------------------------------ teaching text
def test_teaching_text_names_sides_with_a_capital():
    note = teaching.key_note("perpetual_check", {"checks": ["1.Qe8+", "2.Qe4+"], "side": "white", "behind_by": 3})
    assert "White is 3 points behind" in note
    note = teaching.key_note("stalemate_trick", {"final_move": "1...Kxg8", "stalemated": "white", "side": "white",
                                                 "behind_by": 5})
    assert "White has no legal move" in note and "White was 5 points behind" in note


def test_double_attack_and_threat_notes_name_the_pieces():
    note = teaching.key_note("double_attack", {"move": "1.Qd4", "attacks": [
        {"target": {"piece": "rook", "square": "a7"}, "by": {"piece": "queen", "square": "d4"}},
        {"target": {"piece": "knight", "square": "h8"}, "by": {"piece": "queen", "square": "d4"}}]})
    assert note == "Double attack: after 1.Qd4 the queen on d4 attacks the rook on a7 and the knight on h8."
    note = teaching.key_note("parries_threat", {"move": "1.Nd2", "threat": {
        "attacker": {"piece": "bishop", "square": "b4"}, "target": {"piece": "knight", "square": "c3"}}})
    assert "bishop on b4" in note and "knight on c3" in note


# ------------------------------------------------------------------ where learners meet them
def test_requests_reach_the_finished_concepts():
    lib = get_knowledge()
    for goal, cid in [("teach me the greek gift", "greek_gift"), ("x-ray attacks", "x_ray"), ("zugzwang", "zugzwang"),
                      ("perpetual check", "perpetual_check"), ("the windmill", "windmill"),
                      ("desperado", "desperado"), ("epaulette mate", "epaulette_mate"),
                      ("stalemate tricks", "stalemate_tricks"), ("double bishop mate", "double_bishop_mate")]:
        assert cid in lib.match_concepts(goal), goal
    # saving with stalemate and avoiding stalemate stay apart
    assert "stalemate_trap" not in lib.match_concepts("stalemate tricks")


def test_practice_offers_x_rays():
    from app.puzzles import get_puzzles
    from app.puzzles.dashboard import candidates, themes
    lib = get_knowledge()
    labels = {t["concept"]: t["label"] for t in themes(candidates(get_puzzles(lib).all()), lib)}
    assert labels.get("x_ray") == "X-rays"


# ------------------------------------------------------------------ engine profiles
@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def test_zugzwang_profile_passes_library_examples(engine):
    ex = get_knowledge().examples_for("zugzwang")[0]
    report = engine_verify(ex, engine, "zugzwang", depth=12)
    assert report.outcome == "pass", report.reasons
    assert report.details["zugzwang_cp"] >= 200


def test_draw_profiles_pass_the_composed_saves(engine):
    lib = get_knowledge()
    for cid in ("stalemate_tricks", "perpetual_check"):
        ex = next(e for e in lib.examples_for(cid) if e.id.startswith("endgame_"))
        assert engine_verify(ex, engine, "endgame_draw", depth=12).outcome == "pass", ex.id
