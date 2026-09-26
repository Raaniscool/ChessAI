"""Knowledge Library: verification pipeline, validators, duplicates, explanation checks,
trusted retrieval, runtime tiers and usage metadata (no real engine needed)."""
from __future__ import annotations

import json

import chess
import pytest

from app.knowledge import dedupe, validators
from app.knowledge.facts import check_explanation
from app.knowledge.pipeline import verify_candidate
from app.knowledge.positions import replay
from app.knowledge.schema import KnowledgeError, parse_example
from app.knowledge.usage import UsageTracker
from tests.knowledge_helpers import SOURCE, FakeEngine, fork_record, make_library, winning_fork_engine


@pytest.fixture()
def lib(tmp_path):
    return make_library(tmp_path)


def stage(report, name):
    return next(s for s in report.stages if s.name == name)


def record(concept, category, fen, moves, key=None, **over):
    rec = {"id": f"t_{concept}", "title": concept.replace("_", " ").title(), "concept": concept,
           "category": category, "difficulty": 1, "description": "A test position.", "start_fen": fen,
           "moves": moves, "explanation": "A test example.", "tags": ["test"], "source": dict(SOURCE),
           "hints": ["Look for forcing moves."]}
    if key:
        rec["key_move"] = key
    rec.update(over)
    return rec


# ------------------------------------------------------------------ rules stages
def test_invalid_fen_rejected(lib):
    rep = verify_candidate(fork_record(start_fen="not a fen"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "fen").outcome == "fail"


def test_illegal_position_rejected(lib):
    two_kings = "q3k3/8/8/1N6/8/8/8/3KK3 w - - 0 1"
    rep = verify_candidate(fork_record(start_fen=two_kings), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "position").outcome == "fail"


def test_illegal_move_in_sequence_rejected(lib):
    rep = verify_candidate(fork_record(moves=["Nc7+", "Kd7", "Nxa7"]), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "moves").outcome == "fail"
    assert "Nxa7" in stage(rep, "moves").detail


def test_legal_sequence_passes_rules(lib):
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    assert [stage(rep, n).outcome for n in ("fen", "position", "moves", "structure")] == ["pass"] * 4


def test_key_move_must_be_in_the_line(lib):
    rep = verify_candidate(fork_record(key_move="1.Nd6+"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "structure").outcome == "fail"


# ------------------------------------------------------------------ concept validators
BACK_RANK = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"
LUFT = "6k1/5pp1/7p/8/8/8/5PPP/3R2K1 w - - 0 1"


def test_real_back_rank_mate_passes_concept(lib):
    rep = verify_candidate(record("back_rank_mate", "checkmates", BACK_RANK, ["Rd8#"], "1.Rd8#"),
                           lib, engine=FakeEngine())
    assert stage(rep, "concept").outcome == "pass"
    assert rep.status == "verified"


def test_fake_mate_rejected(lib):
    rep = verify_candidate(record("back_rank_mate", "checkmates", LUFT, ["Rd8+", "Kh7"], "1.Rd8+"),
                           lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


STALEMATE = "k7/8/1K6/8/8/8/8/2Q5 w - - 0 1"


def test_real_stalemate_passes(lib):
    rep = verify_candidate(record("stalemate", "basics", STALEMATE, ["Qc7"], "1.Qc7",
                                  explanation="Black is not in check and has no legal move: that is stalemate."),
                           lib, engine=FakeEngine())
    assert stage(rep, "concept").outcome == "pass", rep.reasons


def test_fake_stalemate_rejected(lib):
    rep = verify_candidate(record("stalemate", "basics", STALEMATE, ["Qd2"], "1.Qd2"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


def test_stalemate_in_check_is_not_stalemate(lib):
    rep = verify_candidate(record("stalemate", "basics", STALEMATE, ["Qc8+"], "1.Qc8+"), lib, engine=FakeEngine())
    assert rep.status == "rejected"


def test_real_fork_passes_and_facts_name_targets(lib):
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    assert rep.status == "verified", rep.reasons
    fork = rep.example.facts["fork"]
    assert fork["attacker"]["piece"] == "knight"
    assert {t["piece"] for t in fork["targets"]} == {"king", "queen"}


def test_fake_fork_rejected(lib):
    rep = verify_candidate(fork_record(moves=["Nd6+", "Kd7"], key_move="1.Nd6+", notes={}), lib,
                           engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


PIN = "4k3/4n3/8/8/8/8/8/R4K2 w - - 0 1"


def test_real_pin_passes(lib):
    rep = verify_candidate(record("absolute_pin", "tactics", PIN, ["Re1"], "1.Re1"), lib, engine=FakeEngine())
    assert stage(rep, "concept").outcome == "pass", rep.reasons
    assert rep.example.facts["pin"]["pinned"]["piece"] == "knight"


def test_fake_pin_rejected(lib):
    rep = verify_candidate(record("absolute_pin", "tactics", PIN, ["Rd1"], "1.Rd1"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


SKEWER = "q7/8/8/k7/8/8/8/1R4K1 w - - 0 1"


def test_real_skewer_passes(lib):
    rep = verify_candidate(record("skewer", "tactics", SKEWER, ["Ra1+", "Kb4", "Rxa8"], "1.Ra1+"),
                           lib, engine=FakeEngine())
    assert stage(rep, "concept").outcome == "pass", rep.reasons


def test_fake_skewer_rejected(lib):
    rep = verify_candidate(record("skewer", "tactics", SKEWER, ["Rb2", "Ka4"], "1.Rb2"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


def test_wrong_concept_rejected(lib):
    """A real fork labelled as a pin is still rejected: the claim must match the board."""
    rep = verify_candidate(fork_record(concept="absolute_pin"), lib, engine=winning_fork_engine())
    assert rep.status == "rejected" and stage(rep, "concept").outcome == "fail"


def test_every_claimed_concept_is_checked(lib):
    rep = verify_candidate(fork_record(concepts=["back_rank_mate"]), lib, engine=winning_fork_engine())
    assert rep.status == "rejected" and "Back" in stage(rep, "concept").detail


def test_unknown_concept_rejected(lib):
    rep = verify_candidate(fork_record(concept="flying_fork"), lib, engine=FakeEngine())
    assert rep.status == "rejected" and stage(rep, "structure").outcome == "fail"


# ------------------------------------------------------------------ engine stages
def test_losing_solution_rejected(lib):
    board = chess.Board(fork_record()["start_fen"])
    engine = FakeEngine({board.epd(): {"Nc7+": -400, "Kf2": 0}})
    rep = verify_candidate(fork_record(), lib, engine=engine)
    assert rep.status == "rejected"
    assert stage(rep, "engine").outcome == "fail" and "blunder" in stage(rep, "engine").detail


def test_multiple_good_solutions_are_recorded_not_rejected(lib):
    board = chess.Board(fork_record()["start_fen"])
    engine = FakeEngine({board.epd(): {"Nc7+": 850, "Nd6+": 900}})
    rep = verify_candidate(fork_record(), lib, engine=engine)
    assert rep.status == "verified", rep.reasons  # Nd6+ is 50cp "better": not a reason to reject
    assert "Nd6+" in rep.example.verification["alternatives"]


def test_no_engine_means_no_auto_verification(lib):
    rep = verify_candidate(fork_record(), lib, engine=None)
    assert rep.status == "needs_review"


def test_rules_concepts_need_no_engine(lib):
    rep = verify_candidate(record("stalemate", "basics", STALEMATE, ["Qc7"], "1.Qc7"), lib, engine=None)
    assert rep.status == "verified", rep.reasons


def test_verification_is_deterministic(lib):
    a = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    b = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    assert a.as_dict() == b.as_dict()
    assert a.example.to_record() == b.example.to_record()


# ------------------------------------------------------------------ explanation consistency
def _example(lib, rec):
    return parse_example(rec, lib.concepts)


def test_explanation_matching_board_passes(lib):
    ex = _example(lib, fork_record())
    assert check_explanation("The knight on c7 attacks the queen on a8. Nc7+ is check.", ex) == []


def test_explanation_wrong_square_caught(lib):
    ex = _example(lib, fork_record())
    problems = check_explanation("The bishop on c7 attacks the queen.", ex)
    assert any("bishop" in p and "c7" in p for p in problems)


def test_explanation_false_attack_caught(lib):
    ex = _example(lib, fork_record())
    assert check_explanation("The knight attacks the rook.", ex)
    assert check_explanation("The knight attacks h8.", ex)


def test_explanation_illegal_move_caught(lib):
    ex = _example(lib, fork_record())
    assert any("Qh5" in p for p in check_explanation("Then Qh5 wins.", ex))


def test_explanation_false_mate_claim_caught_but_advice_allowed(lib):
    ex = _example(lib, fork_record())
    assert check_explanation("Nc7+ and that is checkmate.", ex)
    assert check_explanation("Keep the king safe until you can give checkmate.", ex) == []
    assert check_explanation("Be careful: the danger is stalemate.", ex) == []


def test_explanation_wrong_tactic_piece_caught(lib):
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    assert check_explanation("The queen forks the king and the knight.", rep.example)
    assert check_explanation("The knight forks the king and the queen.", rep.example) == []


def test_inconsistent_explanation_rejects_candidate(lib):
    rep = verify_candidate(fork_record(explanation="The bishop on b5 forks everything."), lib,
                           engine=winning_fork_engine())
    assert rep.status == "rejected" and stage(rep, "explanation").outcome == "fail"


# ------------------------------------------------------------------ duplicates
def _verified(lib, rec, engine=None):
    rep = verify_candidate(rec, lib, engine=engine or winning_fork_engine())
    assert rep.status == "verified", rep.reasons
    lib.add(rep.example)
    return rep.example


def test_exact_duplicate_rejected(lib):
    _verified(lib, fork_record())
    rep = verify_candidate(fork_record(id="test_fork_copy", title="Same fork again"), lib,
                           engine=winning_fork_engine())
    assert rep.status == "rejected" and "same teaching position" in stage(rep, "duplicate").detail


# Fuller positions: near-duplicates differ by one pawn step among many pieces.
PAWNS_FEN = "q3k3/1ppp1ppp/8/1N6/8/8/PPPP1PPP/4K3 w - - 0 1"
PAWNS_NEAR = "q3k3/1ppp1ppp/8/1N6/8/7P/PPPP1PP1/4K3 w - - 0 1"


def pawns_fork(fen, **over):
    engine = FakeEngine({chess.Board(fen).epd(): {"Nxc7+": 850}})
    rec = fork_record(start_fen=fen, moves=["Nxc7+", "Kd8", "Nxa8"], key_move="1.Nxc7+",
                      notes={"1.Nxc7+": "The knight takes on c7 and attacks the king and the queen."}, **over)
    return rec, engine


def test_near_duplicate_rejected_without_teaching_reason(lib):
    _verified(lib, *pawns_fork(PAWNS_FEN))
    rec, engine = pawns_fork(PAWNS_NEAR, id="test_fork_near")
    rep = verify_candidate(rec, lib, engine=engine)
    assert stage(rep, "duplicate").outcome == "fail" and "nearly identical" in stage(rep, "duplicate").detail


def test_near_duplicate_allowed_with_teaching_reason(lib):
    _verified(lib, *pawns_fork(PAWNS_FEN))
    rec, engine = pawns_fork(PAWNS_NEAR, id="test_fork_near", difficulty=3,
                             teaching_purpose="practice: the same idea one level harder")
    rep = verify_candidate(rec, lib, engine=engine)
    assert rep.status == "verified", rep.reasons
    assert "different difficulty" in stage(rep, "duplicate").detail


def test_different_mistakes_from_the_same_position_are_not_duplicates(lib):
    base = {"category": "mistakes", "difficulty": 1, "description": "x", "explanation": "x", "tags": ["t"],
            "source": dict(SOURCE)}
    a = parse_example({**base, "id": "a", "title": "a", "concept": "early_queen",
                       "moves": ["e4", "e5", "Nf3", "Qf6"], "mistake_move": "2...Qf6"}, lib.concepts)
    b = parse_example({**base, "id": "b", "title": "b", "concept": "early_queen",
                       "moves": ["e4", "e5", "Nf3", "Qe7"], "mistake_move": "2...Qe7"}, lib.concepts)
    assert dedupe.check(b, [a]).kind == "none"


# ------------------------------------------------------------------ trusted retrieval & tiers
def test_only_verified_entries_are_retrieved(tmp_path):
    lib = make_library(tmp_path)
    good = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    bad = verify_candidate(fork_record(id="test_bad", concept="absolute_pin"), lib, engine=winning_fork_engine())
    unsure = verify_candidate(fork_record(id="test_unsure", start_fen="q3k3/8/8/1N6/8/8/6P1/4K3 w - - 0 1"),
                              lib, engine=None)
    for rep in (good, bad, unsure):
        rep.example.tier = "generated"
        lib.save_entry(rep.example)
    assert [e.id for e in lib.examples_for("knight_fork")] == ["test_knight_fork"]
    assert lib.get("test_bad") is None and lib.get("test_unsure") is None
    assert lib.get("test_bad", trusted_only=False).status == "rejected"
    assert lib.get("test_unsure", trusted_only=False).status == "needs_review"
    assert all(e.status == "verified" for e in lib.search(concept="fork"))


def test_verified_candidate_is_persisted_and_reloaded(tmp_path):
    lib = make_library(tmp_path)
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    rep.example.tier = "generated"
    lib.save_entry(rep.example)
    again = make_library(tmp_path)
    assert [e.id for e in again.examples_for("fork")] == ["test_knight_fork"]
    stored = json.loads((tmp_path / "runtime" / "generated" / "test_knight_fork.json").read_text())
    assert stored["verification"]["status"] == "verified" and stored["source"]["source_type"] == "curated"


def test_runtime_entry_that_fails_recheck_is_demoted(tmp_path):
    lib = make_library(tmp_path)
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine())
    record_ = rep.example.to_record()
    record_["concept"] = "absolute_pin"  # tampered on disk
    lib.store.save("generated", record_)
    again = make_library(tmp_path)
    assert again.get("test_knight_fork") is None
    assert again.get("test_knight_fork", trusted_only=False).status == "needs_review"


def test_global_library_refuses_unverified_entries(tmp_path):
    with pytest.raises(KnowledgeError, match="only holds verified"):
        make_library(tmp_path, {"tactics/x": [fork_record(status="needs_review")]})


def test_global_library_rechecks_entries(tmp_path):
    with pytest.raises(KnowledgeError):
        make_library(tmp_path, {"tactics/x": [fork_record(status="verified", concept="skewer")]})


def test_reviewer_cannot_approve_broken_chess(tmp_path):
    lib = make_library(tmp_path)
    rep = verify_candidate(fork_record(id="test_bad", concept="absolute_pin"), lib, engine=winning_fork_engine())
    rep.example.tier = "generated"
    lib.save_entry(rep.example)
    with pytest.raises(KnowledgeError):
        lib.set_status("test_bad", "verified")


def test_personal_examples_only_on_request(tmp_path):
    lib = make_library(tmp_path)
    rep = verify_candidate(fork_record(), lib, engine=winning_fork_engine(), tier="personal")
    lib.save_entry(rep.example)
    assert lib.examples_for("fork") == []
    assert [e.id for e in lib.examples_for("fork", include_personal=True)] == ["test_knight_fork"]


# ------------------------------------------------------------------ usage metadata
def test_usage_tracking_never_changes_status(tmp_path):
    lib = make_library(tmp_path)
    ex = _verified(lib, fork_record())
    usage = UsageTracker(tmp_path / "usage.json")
    usage.record_used([ex])
    for _ in range(4):
        usage.record_attempt(ex.id, success=False, hints=2)
    usage.record_attempt(ex.id, success=True)
    usage.record_feedback(ex.id, "too_hard")
    stats = UsageTracker(tmp_path / "usage.json").stats(ex.id)
    assert stats["times_used"] == 1 and stats["attempts"] == 5 and stats["average_success_rate"] == 0.2
    assert stats["difficulty_feedback"]["too_hard"] == 1 and stats["last_used"]
    assert lib.get(ex.id).status == "verified"  # failure is information, not a verdict
    with pytest.raises(ValueError):
        usage.record_feedback(ex.id, "boring")


def test_recently_seen_examples_are_not_repeated(tmp_path):
    from app.knowledge.library import Query
    lib = make_library(tmp_path)
    first = _verified(lib, fork_record())
    rook_fen = "r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"  # the same fork, winning a rook
    second = _verified(lib, fork_record(id="test_rook_fork", start_fen=rook_fen, title="Fork of king and rook",
                                        notes={"1.Nc7+": "The knight attacks the king and the rook."},
                                        explanation="The knight checks the king and attacks the rook."),
                       engine=FakeEngine({chess.Board(rook_fen).epd(): {"Nc7+": 450}}))
    q = Query(concepts=["fork"], count=1)
    recent = {first.id: "2999-01-01T00:00:00+00:00"}
    assert [e.id for e in lib.select(q, seen={first.id: 1}, last_used=recent)] == [second.id]
    assert {e.id for e in lib.select(Query(concepts=["fork"], count=2))} == {first.id, second.id}


# ------------------------------------------------------------------ positions helper
def test_replay_labels_and_positions():
    r = replay(chess.STARTING_FEN, ["e4", "e5", "Nf3"])
    assert r.labels == ["1.e4", "1...e5", "2.Nf3"]
    assert r.boards[0].fen() == chess.STARTING_FEN and r.final.fullmove_number == 2
    assert validators.REGISTRY  # the registry is populated
