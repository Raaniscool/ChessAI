"""Library expansion (final pre-V3 fixes, item 9): decoys, clearance, interference,
underpromotion, mate in two, hook and dovetail mates, pawn-ending technique (rule of the square,
key squares, triangulation, outside passed pawn, breakthrough), the wrong bishop, the Philidor
position, and the mistakes "ignoring the back rank", "missing a threat" and "avoiding stalemate".

Every validator is checked both ways: it finds the idea in a position that shows it, and refuses
a position that only looks similar. The data tests check that what entered the library went
through the whole pipeline and carries its provenance."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import chess
import pytest

from app.engine import EngineUnavailable, get_engine, set_engine
from app.knowledge import teaching, validators
from app.knowledge.engine_check import verify as engine_verify
from app.knowledge.library import get_knowledge
from app.knowledge.positions import replay
from app.knowledge.sources import curated, lichess_puzzles

KNOWLEDGE = Path(__file__).resolve().parents[1] / "app" / "knowledge" / "data"
NEW = ["attraction", "clearance", "interference", "underpromotion", "mate_in_two", "hook_mate", "dovetail_mate",
       "rule_of_the_square", "key_squares", "philidor_position", "triangulation", "wrong_bishop",
       "outside_passed_pawn", "pawn_breakthrough", "back_rank_weakness", "stalemate_trap"]


def run(kind: str, fen: str, sans: list[str], key_ply: int | None = 0, mistake_ply: int | None = None) -> dict:
    return validators.run(kind, validators.Ctx(replay(fen, sans), key_ply=key_ply, mistake_ply=mistake_ply), {})


def fails(kind: str, fen: str, sans: list[str], key_ply: int | None = 0, mistake_ply: int | None = None) -> str:
    with pytest.raises(validators.Fail) as err:
        run(kind, fen, sans, key_ply, mistake_ply)
    return str(err.value)


# ------------------------------------------------------------------ the library
def test_every_new_concept_has_verified_examples():
    lib = get_knowledge()
    for cid in NEW + ["missed_threat"]:
        assert cid in lib.concepts, cid
        assert lib.count_for(cid) >= 1, f"{cid} has no verified example"


def test_expansion_examples_were_verified_and_carry_provenance():
    lib = get_knowledge()
    records = [r for path in (KNOWLEDGE / "examples").rglob("*_expansion.json")
               for r in json.loads(path.read_text(encoding="utf-8"))]
    assert len(records) >= 50
    ids = {e.id for e in lib.verified()}
    for rec in records:
        assert rec["id"] in ids  # loaded (the loader re-runs the concept validators)
        stages = {st["name"]: st["outcome"] for st in rec["verification"]["stages"]}
        assert set(stages) >= {"fen", "position", "moves", "concept", "engine", "solution", "explanation",
                               "duplicate"}, rec["id"]
        assert set(stages.values()) == {"pass"}, (rec["id"], stages)
        src = rec["source"]
        assert src["source_license"] and src["import_date"] and src["source_type"], rec["id"]
        if src["source_type"] == "lichess_puzzle":
            assert src["source_url"].startswith("https://lichess.org/training/")


def test_no_position_enters_the_library_twice():
    sources = Counter((e.source or {}).get("source_id") for e in get_knowledge().verified()
                      if (e.source or {}).get("source_type") == "lichess_puzzle")
    assert [s for s, n in sources.items() if n > 1] == []


def test_seed_report_lists_what_was_not_verified():
    report = json.loads((KNOWLEDGE / "seed_report.json").read_text(encoding="utf-8"))
    exp = report["expansion"]
    assert exp["totals"]["verified"] == sum(exp["by_concept"].values())
    for item in exp["not_verified"]:
        assert item["reasons"]  # nothing is dropped silently


def test_glossary_has_no_term_that_is_now_a_verified_concept():
    lib = get_knowledge()
    glossary = json.loads((KNOWLEDGE / "glossary.json").read_text(encoding="utf-8"))["terms"]
    assert [t["id"] for t in glossary if t["id"] in lib.concepts and lib.count_for(t["id"])] == []


def test_curated_expansion_files_are_separate_from_the_seed_files():
    assert not set(curated.EXPANSION_FILES) & set(curated.FILES)
    recs = curated.candidates(files=curated.EXPANSION_FILES)
    assert recs and all(r["category"] in ("endgames", "mistakes") for r in recs)


# ------------------------------------------------------------------ tactics
def test_decoy_needs_the_offer_to_be_taken():
    lib = get_knowledge()
    for ex in lib.examples_for("attraction"):
        facts = validators.run("attraction", validators.Ctx(ex.replay(), key_ply=ex.key_ply), {})
        assert facts["offered"]["square"] == facts["square"] == facts["lured"]["square"]
    # checks that nobody captures are not a decoy
    assert "no decoy" in fails("attraction", "8/8/3k4/8/2B5/8/8/R3K3 w - - 0 1", ["Ra6+", "Kc5", "Ra5+"])


def test_clearance_needs_a_move_that_was_impossible_before():
    fen = "8/8/8/8/8/1R4K1/8/6k1 w - - 0 1"
    facts = run("clearance", fen, ["Kf4", "Kf2", "Rg3"])  # the king clears g3 for the rook
    assert facts["cleared_square"] == "g3" and facts["used_by"]["piece"] == "rook"
    # the rook could always reach c3: nothing was cleared
    assert "no clearance" in fails("clearance", fen, ["Kf4", "Kf2", "Rc3"])


def test_interference_follow_up_must_come_from_another_piece():
    # Lichess GTv4H: Bg6 cuts e8-h5, but it is the same bishop that then takes on h5
    fen = "4b3/7B/5p2/5P1p/5K1P/3k4/8/8 b - - 5 55"
    assert "no interference" in fails("interference", fen, ["Ke2", "Bg6", "Bc6", "Bxh5+"], key_ply=1)
    lib = get_knowledge()
    for ex in lib.examples_for("interference"):
        facts = validators.run("interference", validators.Ctx(ex.replay(), key_ply=ex.key_ply), {})
        assert facts["cut_square"] != facts["guarded_square"]


def test_underpromotion_refuses_a_promotion_where_a_queen_mates_too():
    assert "queen would mate as well" in fails("underpromotion", "k7/2P5/1K6/8/8/8/8/8 w - - 0 1", ["c8=R#"])
    facts = run("underpromotion", "8/8/8/8/8/1k6/p7/2K5 b - - 0 1", ["a1=N"], key_ply=None)
    assert facts["promoted_to"] == "knight" and facts["square"] == "a1"


# ------------------------------------------------------------------ mates
def test_mate_in_two_counts_the_learners_moves():
    assert "not two" in fails("mate_in_two", "k7/8/2K5/8/8/8/8/1Q6 w - - 0 1", ["Qb7#"])
    facts = run("mate_in_two", "7k/8/5K2/8/8/8/8/R7 w - - 0 1", ["Kg6", "Kg8", "Ra8#"])
    assert facts["pattern"] == "mate in two"


def test_hook_and_dovetail_mates_need_their_shape():
    back_rank = ("6k1/5ppp/8/8/8/8/8/3R2K1 w - - 0 1", ["Rd8#"])
    assert "rook checks from right next to the king" in fails("hook_mate", *back_rank)
    assert "delivered by the queen" in fails("dovetail_mate", *back_rank)
    lib = get_knowledge()
    for cid in ("hook_mate", "dovetail_mate"):
        for ex in lib.examples_for(cid):
            facts = validators.run(cid, validators.Ctx(ex.replay(), key_ply=ex.key_ply), {})
            assert facts["pattern"] == cid.replace("_", " ")


# ------------------------------------------------------------------ endgames
def test_rule_of_the_square():
    outside = "8/8/8/6k1/1P6/8/8/6K1 w - - 0 1"
    facts = run("rule_of_the_square", outside, ["b5", "Kf6", "b6", "Ke7", "b7", "Kd7", "b8=Q"])
    assert facts["queening_square"] == "b8" and facts["pawn_moves"] == 4
    inside = "8/8/8/4k3/1P6/8/8/6K1 w - - 0 1"  # Ke5 is inside the square, even if it then walks away
    assert "inside the square" in fails("rule_of_the_square", inside,
                                        ["b5", "Kf4", "b6", "Ke3", "b7", "Kd2", "b8=Q"])


def test_rule_of_the_square_counts_the_defenders_tempo():
    # g5 needs four moves; the king on b4 is five away, so with Black to move it is outside
    facts = run("rule_of_the_square", "k7/8/8/K5p1/8/8/8/8 w - - 0 1",
                ["Kb4", "g4", "Kc3", "g3", "Kd2", "g2", "Ke2", "g1=Q"], key_ply=1)
    assert facts["queening_square"] == "g1"


def test_key_squares():
    facts = run("key_squares", "8/8/4k3/8/8/4K3/4P3/8 w - - 0 1", ["Kd4", "Kd6"])
    assert set(facts["key_squares"]) == {"d4", "e4", "f4"} and facts["king_reaches"] == "d4"
    assert "already stands" in fails("key_squares", "8/8/4k3/8/4K3/8/4P3/8 w - - 0 1", ["Kd4"])
    assert validators._key_squares(chess.A5, chess.WHITE) == [chess.B7, chess.B8]  # rook pawn
    assert set(validators._key_squares(chess.E5, chess.WHITE)) == {
        chess.D6, chess.E6, chess.F6, chess.D7, chess.E7, chess.F7}


def test_triangulation_needs_the_same_position_with_the_other_side_to_move():
    fen = "8/1p1k4/1P6/2PK4/8/8/8/8 w - - 0 1"
    facts = run("triangulation", fen, ["Ke5", "Kc6", "Kd4", "Kd7", "Kd5"])
    assert facts["king_moves"] == 3 and facts["now_to_move"] == "black"
    assert "no triangulation" in fails("triangulation", fen, ["Ke5", "Kc6", "Kd4", "Kd7"])


def test_wrong_bishop_refuses_the_right_bishop():
    sans = ["Kg6", "Kd2", "Kh6", "Ke3", "Kh7", "Kf4", "Kh8"]
    facts = run("wrong_bishop", "8/8/8/5k2/7P/8/4B3/3K4 b - - 0 1", sans)
    assert facts["corner"] == "h8" and facts["bishop_squares"] == "light"
    assert "right bishop" in fails("wrong_bishop", "8/8/8/5k2/7P/B7/8/3K4 b - - 0 1", sans)


def test_philidor_needs_the_check_from_behind():
    fen = "4k3/7R/1r6/3KP3/8/8/8/8 b - - 0 1"
    facts = run("philidor_position", fen, ["Ra6", "e6", "Ra1", "Kd6", "Rd1+"])
    assert facts["third_rank"] == 6
    assert "check from behind" in fails("philidor_position", fen, ["Ra6", "e6", "Ra1", "Kd6", "Ra8"])


def test_breakthrough_and_outside_passer_are_pawn_ending_ideas():
    fen = "7k/ppp5/8/PPP5/8/8/8/7K w - - 0 1"
    facts = run("pawn_breakthrough", fen, ["b6", "axb6", "c6", "bxc6", "a6", "Kg7", "a7", "Kf7", "a8=Q"])
    assert facts["promotes"] == "5.a8=Q" and len(facts["sacrifices"]) == 2
    assert "no pawn is given up" in fails("pawn_breakthrough", fen, ["Kg2", "Kg7"])
    assert "pawn ending" in fails("outside_passed_pawn", "7k/ppp5/8/PPP5/8/8/8/R6K w - - 0 1", ["Kg2"])


# ------------------------------------------------------------------ mistakes
def test_stalemate_trap():
    facts = run("stalemate_trap", "k7/8/2K5/8/8/8/8/1Q6 w - - 0 1", ["Qb7#"])
    assert facts["stalemating_moves"] == ["Qb6"]
    assert "itself stalemates" in fails("stalemate_trap", "k7/8/2K5/8/8/8/8/1Q6 w - - 0 1", ["Qb6"])
    assert "no move would stalemate" in fails("stalemate_trap", "7k/8/8/8/8/8/8/K5R1 w - - 0 1", ["Ka2"])


def test_back_rank_weakness_needs_a_back_rank_mate():
    fen = "r5k1/5ppp/8/3p1q2/3P4/1Q2P2P/R5P1/6K1 b - - 0 38"
    facts = run("back_rank_weakness", fen, ["Rxa2", "Qb8+", "Qc8", "Qxc8#"], key_ply=1, mistake_ply=0)
    assert facts["mate"]["king"] == "g8"


def test_imported_missed_threats_were_real_threats():
    # 8.dxe4 opened the d-file: ...Qxd1 was impossible before, so it's a new threat, not a missed one
    assert not lichess_puzzles._threat_was_real({
        "fen": "r1b1k2r/ppp2ppp/2pq4/2b3B1/4n3/2PP4/PP3PPP/RN1QKB1R w KQkq - 0 8",
        "moves": ["d3e4", "c5f2", "e1f2", "d6d1"]})
    # ...c6 ignored Bxf7+ followed by Qxd8 — both were already on
    assert lichess_puzzles._threat_was_real({
        "fen": "rnbqkb1r/ppp2ppp/8/3Bp3/8/1PP5/P1P2PPP/R1BQK1NR b KQkq - 0 6",
        "moves": ["c7c6", "d5f7", "e8f7", "d1d8"]})


# ------------------------------------------------------------------ teaching text
def test_mate_sentence_keeps_its_lists_apart():
    text = teaching.mate_sentence({"mated_side": "white", "king": "h3", "checkers": [
        {"piece": "rook", "square": "h1"}], "escape_squares": [{"square": "g2", "covered_by": []},
                                                                {"square": "g4", "covered_by": []}]})
    assert "rook on h1; g2 and g4 are covered;" in text


def test_a_mate_in_two_is_not_called_checkmate_at_its_first_move():
    lib = get_knowledge()
    for ex in lib.examples_for("mate_in_two"):
        first = ex.notes.get(ex.labels[ex.key_ply], "")
        assert not first.startswith("Checkmate"), ex.id
        assert ex.notes.get(ex.labels[-1], "").startswith("Checkmate")


# ------------------------------------------------------------------ where learners meet them
def test_requests_reach_the_new_concepts():
    lib = get_knowledge()
    for goal, cid in [("teach me decoys", "attraction"), ("the philidor position", "philidor_position"),
                      ("triangulation", "triangulation"), ("rule of the square", "rule_of_the_square"),
                      ("back rank weakness", "back_rank_weakness"), ("how to avoid stalemate", "stalemate_trap"),
                      ("missing threats", "missed_threat"), ("underpromotion", "underpromotion")]:
        assert cid in lib.match_concepts(goal), goal
    # the more specific concept owns the phrase now
    assert "passed_pawn" not in lib.match_concepts("rule of the square")


def test_practice_offers_the_new_tactics():
    from app.puzzles import get_puzzles
    from app.puzzles.dashboard import candidates, themes
    lib = get_knowledge()
    labels = {t["concept"]: t["label"] for t in themes(candidates(get_puzzles(lib).all()), lib)}
    assert labels.get("attraction") == "Decoys"
    assert "clearance" in labels and "interference" in labels


# ------------------------------------------------------------------ engine profile
@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def test_underpromotion_profile_passes_library_examples(engine):
    ex = get_knowledge().examples_for("underpromotion")[0]
    report = engine_verify(ex, engine, "underpromotion", depth=10)
    assert report.outcome == "pass", report.reasons
