"""Knowledge Library: Stockfish profiles (with a deterministic fake engine), the source
importers and data files, and the shipped seed library itself."""
from __future__ import annotations

import json
import threading

import chess
import pytest

from app.knowledge import engine_check
from app.knowledge.library import KnowledgeLibrary, Query
from app.knowledge.pipeline import verify_candidate
from app.knowledge.positions import replay
from app.knowledge.schema import CATEGORIES
from app.knowledge.sources import curated, lichess_openings, lichess_puzzles
from tests.knowledge_helpers import KNOWLEDGE_DATA, FakeEngine, make_library


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    return make_library(tmp_path_factory.mktemp("klib"))


def curated_record(record_id: str) -> dict:
    return next(c for c in curated.candidates() if c["id"] == record_id)


def opening_record(record_id: str) -> dict:
    return next(c for c in lichess_openings.candidates() if c["id"] == record_id)


def boards_of(rec: dict) -> list[chess.Board]:
    fen = rec.get("start_fen") or chess.STARTING_FEN
    return replay(chess.STARTING_FEN if fen == "startpos" else fen, rec["moves"]).boards


def engine_stage(rep):
    return next(s for s in rep.stages if s.name == "engine")


# ------------------------------------------------------------------ mistake profile
def test_mistake_must_really_lose(lib):
    rec = curated_record("mistake_queen_to_g5")
    after_mistake = boards_of(rec)[6]
    engine = FakeEngine(positions={after_mistake.epd(): 0})  # "Stockfish": Qg5 costs nothing
    rep = verify_candidate(rec, lib, engine=engine, check_duplicates=False)
    assert rep.status == "rejected" and "not a real mistake" in engine_stage(rep).detail


def test_real_mistake_with_best_punishment_passes(lib):
    rec = curated_record("mistake_queen_to_g5")
    before_mistake = boards_of(rec)[5]
    engine = FakeEngine({before_mistake.epd(): {"Qg5": 900}})  # White's view: Black just lost the queen
    rep = verify_candidate(rec, lib, engine=engine, check_duplicates=False)
    assert rep.status == "verified", rep.reasons


def test_mistake_with_a_bad_punishment_is_rejected(lib):
    """The punishment is the learner's solution: if Stockfish calls it a blunder, the example goes."""
    rec = curated_record("mistake_queen_to_g5")
    boards = boards_of(rec)
    engine = FakeEngine({boards[5].epd(): {"Qg5": 900}, boards[6].epd(): {"Nxg5": 100, "d4": 900}})
    rep = verify_candidate(rec, lib, engine=engine, check_duplicates=False)
    assert rep.status == "rejected" and "punishment" in engine_stage(rep).detail
    assert "blunder" in next(s for s in rep.stages if s.name == "solution").detail


# ------------------------------------------------------------------ principle profile
@pytest.mark.parametrize("final_white_cp, status", [(0, "rejected"), (60, "needs_review"), (160, "verified")])
def test_principle_lessons_must_be_borne_out(lib, final_white_cp, status):
    rec = curated_record("mistake_early_queen_f6")  # Black's early queen
    boards = boards_of(rec)
    engine = FakeEngine(positions={boards[-1].epd(): final_white_cp, boards[3].epd(): 0})
    rep = verify_candidate(rec, lib, engine=engine, check_duplicates=False)
    assert rep.status == status, rep.reasons


# ------------------------------------------------------------------ opening profile
@pytest.mark.parametrize("white_cp_after, status", [(0, "verified"), (-150, "needs_review"), (-300, "rejected")])
def test_opening_moves_are_checked(lib, white_cp_after, status):
    rec = opening_record("opening_italian_center_attack")
    boards = boards_of(rec)
    level = {b.epd(): 0 for b in boards}  # every position equal...
    level[boards[3].epd()] = white_cp_after  # ...except the one after 2.Nf3
    engine = FakeEngine(positions=level)
    rep = verify_candidate(rec, lib, engine=engine, check_duplicates=False)
    assert rep.status == status, rep.reasons


def test_opening_lines_must_come_from_the_database(lib):
    rec = opening_record("opening_italian_center_attack")
    invented = dict(rec, moves=["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"])
    rep = verify_candidate(invented, lib, engine=FakeEngine(), check_duplicates=False)
    assert rep.status == "rejected"
    too_long = dict(rec, moves=rec["moves"] + ["Kf1", "h6", "h3", "a6", "a3", "Ba5", "g3", "g6"])
    rep = verify_candidate(too_long, lib, engine=FakeEngine(), check_duplicates=False)
    assert rep.status == "rejected" and "beyond the database line" in rep.reasons[0]


# ------------------------------------------------------------------ endgame profile
def test_endgame_win_passes_and_thrown_win_fails(lib):
    rec = curated_record("endgame_kr_opposition_mate")
    rep = verify_candidate(rec, lib, engine=FakeEngine(), check_duplicates=False)
    assert rep.status == "verified", rep.reasons
    boards = boards_of(rec)
    thrown = FakeEngine(positions={boards[1].epd(): 0})  # after White's first move: a draw
    rep = verify_candidate(rec, lib, engine=thrown, check_duplicates=False)
    assert rep.status == "rejected" and "throws away the win" in engine_stage(rep).detail


# ------------------------------------------------------------------ engine failures
class BrokenEngine(FakeEngine):
    def analyse_lines(self, *args, **kwargs):
        raise RuntimeError("engine crashed")


def test_engine_errors_never_pass_silently(lib):
    rep = verify_candidate(curated_record("mistake_queen_to_g5"), lib, engine=BrokenEngine(), check_duplicates=False)
    assert rep.status == "needs_review" and "engine error" in engine_stage(rep).detail


def test_verification_asks_for_a_fresh_engine_state():
    """Library verification clears the engine hash (python-chess `game=`), so results are reproducible."""
    from app.engine.service import UciEngine

    class Stub:
        def __init__(self):
            self.kwargs = []

        def analyse(self, board, limit, multipv=1, game=None):
            self.kwargs.append(game)
            return [{"pv": [next(iter(board.legal_moves))], "score": chess.engine.PovScore(chess.engine.Cp(0), True)}]

    eng = UciEngine.__new__(UciEngine)
    eng._engine, eng._lock = Stub(), threading.Lock()
    eng.settings = type("S", (), {"engine_depth": 10})()
    eng.analyse_lines(chess.Board(), fresh=True)
    eng.analyse_lines(chess.Board(), fresh=True)
    eng.analyse_lines(chess.Board())
    a, b, c = eng._engine.kwargs
    assert a is not None and b is not None and a is not b and c is None
    engine_check.judge_move(eng, chess.Board(), chess.Move.from_uci("e2e4"), depth=10)
    assert eng._engine.kwargs[-1] is not None


# ------------------------------------------------------------------ source data files
def test_curated_and_opening_records_pass_rules_and_concepts(lib):
    """Every hand-curated record must at least be legal and demonstrate its concepts."""
    for rec in curated.candidates() + lichess_openings.candidates():
        rep = verify_candidate(rec, lib, engine=None, check_duplicates=False)
        assert rep.status != "rejected", (rec["id"], rep.reasons)


def test_every_source_record_has_provenance():
    for rec in curated.candidates() + lichess_openings.candidates():
        src = rec["source"]
        for key in ("source_type", "source_id", "source_license", "import_date"):
            assert src.get(key), (rec["id"], key)
        assert src["source_type"] != "qwen"


def test_opening_records_use_database_moves_only():
    index = lichess_openings.get_opening_index()
    for rec in lichess_openings.candidates():
        match = index.longest_prefix(rec["moves"])
        assert match is not None and len(rec["moves"]) - len(match.moves) <= 6, rec["id"]
        assert rec["source"]["source_license"] == "CC0-1.0"


def test_puzzle_mistakes_skip_positions_used_as_tactics(lib):
    first = lichess_puzzles.mistake_candidates(lib, per_concept=2)
    used = {c["source"]["source_id"] for c in first}
    again = lichess_puzzles.mistake_candidates(lib, per_concept=2, exclude=used)
    assert used and not used & {c["source"]["source_id"] for c in again}
    for c in first:
        assert c["mistake_move"] and c["key_move"] and c["source"]["source_license"] == "CC0-1.0"


# ------------------------------------------------------------------ the shipped seed library
@pytest.fixture(scope="module")
def seed():
    return KnowledgeLibrary(load_runtime=False)


REQUIRED = [
    # rules
    "check", "stalemate", "castling", "cannot_castle", "promotion", "en_passant", "captures",
    # tactics
    "fork", "knight_fork", "absolute_pin", "skewer", "discovered_check", "double_check", "deflection",
    "removing_defender", "overloaded_piece", "zwischenzug", "sacrifice", "hanging_piece",
    # checkmates
    "back_rank_mate", "smothered_mate", "ladder_mate", "queen_mate", "rook_mate", "mate_in_one",
    # openings
    "italian_game", "caro_kann", "two_knights_defense",
    # endgames
    "opposition", "passed_pawn", "lucena_position",
    # mistakes
    "hanging_queen", "hung_piece", "early_queen", "repeated_moves", "ignoring_development",
    "king_safety_mistake", "missed_threat", "walked_into_fork",
]


def test_seed_library_loads_cleanly(seed):
    stats = seed.stats()
    assert stats["load_errors"] == 0
    # The library was enlarged on request (a bigger Lichess pool, ~10 per tactic); the upper
    # bound still catches a runaway build that stops filtering.
    assert 200 <= stats["verified"] <= 600
    assert set(stats["by_category"]) == set(CATEGORIES)


def test_seed_library_is_balanced(seed):
    """Larger, not lopsided: no concept dominates, and the Lichess tactics have real depth."""
    from collections import Counter
    per = Counter(ex.concept for ex in seed.verified())
    assert max(per.values()) <= 15, per.most_common(3)
    for concept in ("fork", "knight_fork", "absolute_pin", "skewer", "discovered_attack", "back_rank_mate",
                    "smothered_mate", "mate_in_one", "hanging_piece", "removing_defender"):
        assert per[concept] >= 8, (concept, per[concept])


def test_every_tactic_has_room_for_a_stronger_learner(seed):
    """The harder tier: the common tactics span a real range, so a 1600 player isn't handed the
    same 1000-rated positions as a beginner. Every harder entry is a verified real-game puzzle."""
    from app.knowledge.difficulty import puzzle_rating
    for concept in ("fork", "knight_fork", "pin", "skewer", "deflection", "zwischenzug", "sacrifice",
                    "trapped_piece", "removing_defender"):
        ratings = [puzzle_rating(e) for e in seed.examples_for(concept) if e.key_ply is not None]
        assert max(ratings) >= 1450 and max(ratings) - min(ratings) >= 350, (concept, min(ratings), max(ratings))
    harder = [e for e in seed.verified() if "harder" in e.tags]
    assert len(harder) >= 40
    for e in harder:
        assert e.status == "verified" and e.tier == "global"
        assert e.source["source_type"] == "lichess_puzzle" and e.source["source_license"] == "CC0-1.0"
        assert e.path.endswith("lichess_harder.json")


def test_beginners_get_simple_positions_for_the_common_tactics(seed):
    """The starter tier: real-game forks start around 1000, too hard for a 600 player; the app's
    generator built simpler positions, each through the same pipeline (never Qwen)."""
    from app.knowledge.difficulty import puzzle_rating
    for concept in ("knight_fork", "queen_fork", "pawn_fork", "absolute_pin", "skewer"):
        ratings = [puzzle_rating(e) for e in seed.examples_for(concept) if e.key_ply is not None]
        assert min(ratings) <= 950, (concept, min(ratings))
    starter = [e for e in seed.verified() if "starter" in e.tags]
    assert len(starter) >= 8
    for e in starter:
        assert e.status == "verified" and e.tier == "global" and e.path.endswith("procedural.json")
        assert e.source["source_type"] == "procedural" and "qwen" not in e.source["reference"].lower()
        assert puzzle_rating(e) <= 950 and e.hints


@pytest.mark.parametrize("concept", REQUIRED)
def test_seed_covers_required_concepts(seed, concept):
    assert seed.examples_for(concept), f"no verified example for {concept}"


def test_seed_entries_carry_provenance_and_verification(seed):
    for ex in seed.verified():
        assert ex.status == "verified" and ex.tier == "global"
        for key in ("source_type", "source_id", "source_license", "import_date"):
            assert ex.source.get(key), (ex.id, key)
        assert ex.verification["status"] == "verified" and ex.verification["method"] == "seed_builder"
        assert "concept" in ex.verification["checked"], ex.id


def test_seed_report_matches_library(seed):
    report = json.loads((KNOWLEDGE_DATA / "seed_report.json").read_text(encoding="utf-8"))
    assert report["totals"]["verified"] == len(seed.verified())
    for item in report["rejected"] + report["needs_review"]:
        assert seed.get(item["id"]) is None  # never retrievable


def test_teach_me_checkmates_retrieves_verified_mates(seed):
    q = seed.parse_query("Teach me checkmates")
    assert q.concepts == ["checkmate"]
    picks = seed.select(Query(concepts=q.concepts, count=5))
    assert len(picks) == 5 and len({p.concept for p in picks}) == 5
    mates = set(seed.descendants("checkmate")) | {"checkmate"}
    assert all(p.status == "verified" and (set(p.concepts) & mates) for p in picks)
