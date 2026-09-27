"""Generated puzzle positions: constructors, Qwen proposals, verification, promotion,
rejection, duplicates, variety tracking, learner separation. Engine tests use the real
Stockfish and are skipped when it isn't available."""
from __future__ import annotations

import json
import random
from types import SimpleNamespace

import chess
import pytest

from app.engine import EngineUnavailable, get_engine, set_engine
from app.knowledge import validators
from app.knowledge.generation import generator as gen
from app.knowledge.generation.constructors import CONSTRUCTORS, Proposal
from app.knowledge.generation.describe import describe
from app.knowledge.generation.log import GenerationLog
from app.knowledge.generation.qwen_proposer import QwenProposalError, parse_proposal, propose
from app.knowledge.positions import replay
from app.planner.knowledge_lessons import _credit
from tests.knowledge_helpers import FORK_FEN, make_library

VALIDATOR = {"knight_fork": ("fork", {"piece": "knight"}), "queen_fork": ("fork", {"piece": "queen"}),
             "pawn_fork": ("fork", {"piece": "pawn"}), "hanging_piece": ("hanging_piece", {}),
             "back_rank_mate": ("back_rank_mate", {}), "support_mate": ("checkmate", {}),
             "bare_queen_mate": ("queen_mate", {}), "skewer": ("skewer", {}),
             "absolute_pin": ("pin", {"kind": "absolute"})}


class FakeTeacher:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, messages, max_tokens=None):
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


# ------------------------------------------------------------------ no engine needed
@pytest.mark.parametrize("name", sorted(CONSTRUCTORS))
def test_constructors_make_legal_positions_for_the_requested_side(name):
    made = 0
    for seed in range(120):
        rng = random.Random(seed)
        side = "white" if seed % 2 else "black"
        p = CONSTRUCTORS[name](rng, side)
        if p is None:
            continue
        made += 1
        board = chess.Board(p.fen)
        assert board.is_valid() and not board.is_check(), (name, p.fen)
        assert board.turn == (side == "white"), (name, p.fen)
        if p.key is not None:
            assert p.key in board.legal_moves, (name, p.fen, p.key)
    assert made >= 20, f"{name} almost never produces a position"


@pytest.mark.parametrize("name", sorted(VALIDATOR))
def test_constructed_key_moves_show_the_idea(name):
    """The intended move must pass the concept validator (the engine is checked later)."""
    kind, params = VALIDATOR[name]
    ok = total = 0
    for seed in range(150):
        p = CONSTRUCTORS[name](random.Random(seed), "white" if seed % 2 else "black")
        if p is None:
            continue
        total += 1
        board = chess.Board(p.fen)
        try:
            validators.run(kind, validators.Ctx(replay(p.fen, [board.san(p.key)]), key_ply=0), params)
            ok += 1
        except validators.Fail:
            pass
    assert total and ok / total >= 0.9, f"{name}: only {ok}/{total} show the idea"


def test_no_relative_pin_constructor():
    # relative pins rarely win by force; the generator doesn't claim to make them
    assert "relative_pin" not in CONSTRUCTORS
    assert not gen.supported("relative_pin")


def test_describe_uses_only_the_facts_it_is_given():
    facts = {"attacker": {"piece": "knight", "color": "white", "square": "c7"}, "gives_check": True,
             "targets": [{"piece": "queen", "color": "black", "square": "a8"},
                         {"piece": "king", "color": "black", "square": "e8"}]}
    words = describe("knight_fork", facts, "1.Nc7+", "white")
    assert "king on e8" in words["explanation"] and "queen on a8" in words["explanation"]
    assert words["explanation"].index("king") < words["explanation"].index("queen")  # the check first
    for hint in words["hints"]:  # hints never give the answer away
        assert "Nc7" not in hint and "c7" not in hint


def test_qwen_proposal_parsing_rejects_bad_answers():
    with pytest.raises(QwenProposalError, match="invalid FEN"):
        parse_proposal({"fen": "not a fen", "solution": "e4"}, "knight_fork")
    with pytest.raises(QwenProposalError, match="illegal position"):
        parse_proposal({"fen": "8/8/8/8/8/8/8/8 w - - 0 1", "solution": "e4"}, "knight_fork")
    with pytest.raises(QwenProposalError, match="not a legal move"):
        parse_proposal({"fen": FORK_FEN, "solution": "Nc8"}, "knight_fork")
    with pytest.raises(QwenProposalError, match="no FEN"):
        parse_proposal({"solution": "Nc7+"}, "knight_fork")
    p = parse_proposal({"fen": FORK_FEN, "solution": "Nc7+"}, "knight_fork")
    assert p.key == chess.Move.from_uci("b5c7") and p.variant == "qwen"


def test_qwen_answer_without_json_is_a_rejection_not_a_crash():
    with pytest.raises(QwenProposalError, match="no JSON"):
        propose("Knight fork", "x", "white", "knight_fork", "hint", teacher=FakeTeacher("I think Nc7 is nice"))


def test_generation_log_tracks_variants_and_shown(tmp_path):
    log = GenerationLog(tmp_path / "log.json")
    for sig in ("knight_fork:king+rook:w", "knight_fork:king+rook:w", "knight_fork:queen+rook:b"):
        log.record({"concept": "knight_fork", "outcome": "verified", "signature": sig})
    log.record({"concept": "knight_fork", "outcome": "rejected", "signature": "knight_fork:x:w"})
    assert log.recent_variants("knight_fork")["knight_fork:king+rook:w"] == 2
    assert "knight_fork:x:w" not in log.recent_variants("knight_fork")  # rejected ones don't count
    log.mark_shown(["gen_a"])
    assert "gen_a" in GenerationLog(tmp_path / "log.json").shown()


def test_generation_log_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "log.json"
    path.write_text("{not json", encoding="utf-8")
    log = GenerationLog(path)
    assert log.entries() == []
    log.record({"concept": "c", "outcome": "verified"})
    assert len(log.entries()) == 1


def test_no_engine_means_nothing_is_generated(tmp_path):
    lib = make_library(tmp_path)
    result = gen.generate("knight_fork", lib, None, gen_log=GenerationLog(tmp_path / "l.json"))
    assert not result.accepted and "Stockfish" in result.stopped


def test_unsupported_concept_is_reported(tmp_path):
    lib = make_library(tmp_path)
    result = gen.generate("italian_game", lib, object(), gen_log=GenerationLog(tmp_path / "l.json"))
    assert not result.accepted and "no generator" in result.stopped


def test_credit_line_labels_generated_positions():
    ex = SimpleNamespace(source={"source_type": "procedural"})
    assert "Generated position" in _credit(ex) and "Stockfish" in _credit(ex)
    ex = SimpleNamespace(source={"source_type": "qwen_generated"})
    assert "proposed by Qwen" in _credit(ex) and "not from a real game" in _credit(ex)


def test_needs_review_is_kept_only_for_qwen(tmp_path, monkeypatch):
    """A candidate the checks couldn't settle is never used; Qwen ones are kept for a human."""
    lib = make_library(tmp_path)
    fake_report = lambda status: SimpleNamespace(  # noqa: E731
        status=status, stages=[SimpleNamespace(name="engine", outcome="uncertain", detail="unsure")], example=None)

    def fake_evaluate(proposal, concept, library, engine, **kw):
        from app.knowledge.schema import parse_example
        raw = gen._raw(proposal, concept, "tactics", ["Nc7+", "Kd7", "Nxa8"], [],
                       {"attacker": {"piece": "knight", "color": "white", "square": "c7"},
                        "targets": [{"piece": "king", "color": "black", "square": "e8"},
                                    {"piece": "queen", "color": "black", "square": "a8"}]},
                       kw["source"], kw["tier"])
        raw["status"] = "needs_review"
        report = fake_report("needs_review")
        report.example = parse_example(raw, library.concepts, "generated/x.json", tier=kw["tier"])
        return report

    monkeypatch.setattr(gen, "evaluate", fake_evaluate)
    good = json.dumps({"fen": FORK_FEN, "solution": "Nc7+"})
    result = gen.generate("knight_fork", lib, object(), count=1, max_attempts=2, use_qwen=True,
                          teacher=FakeTeacher(good), gen_log=GenerationLog(tmp_path / "l.json"), seed=1)
    assert not result.accepted
    assert len(result.needs_review) == 1  # the Qwen one, saved for review...
    saved = result.needs_review[0]
    assert saved.source["source_type"] == "qwen_generated"
    assert lib.get(saved.id) is None  # ...but never served as trusted material
    assert saved.id not in {e.id for e in lib.examples_for("knight_fork")}
    assert any(r["stage"] == "engine" for r in result.rejected)  # the procedural one: dropped


# ------------------------------------------------------------------ real engine
@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def _log(tmp_path):
    return GenerationLog(tmp_path / "log.json")


def test_generated_puzzle_is_verified_saved_and_labelled(tmp_path, engine):
    lib = make_library(tmp_path)
    result = gen.generate("knight_fork", lib, engine, count=1, seed=5, use_qwen=False, gen_log=_log(tmp_path))
    assert len(result.accepted) == 1, result.rejected
    ex = result.accepted[0]
    assert ex.status == "verified" and ex.tier == "generated" and ex.id.startswith("gen_knight_fork_")
    assert ex.source["source_type"] == "procedural" and ex.source["generated"] is True
    assert ex.verification["status"] == "verified"
    assert [s["name"] for s in ex.verification["stages"]][-1] == "duplicate"
    board = chess.Board(ex.start_fen)
    assert board.piece_at(board.parse_san(ex.moves[0]).from_square).piece_type == chess.KNIGHT
    assert (tmp_path / "runtime" / "generated" / f"{ex.id}.json").exists()  # promoted to the runtime tier
    assert lib.get(ex.id) is not None and ex.id in {e.id for e in lib.examples_for("knight_fork")}
    assert ex.facts.get("fork", {}).get("attacker", {}).get("piece") == "knight"
    rows = _log(tmp_path).entries("knight_fork", "verified")
    assert rows and rows[-1]["id"] == ex.id


def test_personal_puzzles_stay_out_of_the_shared_library(tmp_path, engine):
    lib = make_library(tmp_path)
    personal = {"target_weakness": "walked_into_fork", "evidence": [{"game_id": "chesscom-1", "ply": 23}]}
    result = gen.generate("knight_fork", lib, engine, count=1, seed=9, tier="personal", personal=personal,
                          use_qwen=False, gen_log=_log(tmp_path))
    assert len(result.accepted) == 1, result.rejected
    ex = result.accepted[0]
    assert ex.tier == "personal" and ex.id.startswith("mygen_")
    assert ex.source["personal"]["target_weakness"] == "walked_into_fork"
    assert ex.source["personal"]["evidence"][0]["game_id"] == "chesscom-1"
    assert ex.id not in {e.id for e in lib.examples_for("knight_fork")}
    assert ex.id in {e.id for e in lib.examples_for("knight_fork", include_personal=True)}
    assert ex not in lib.verified() and ex not in lib.all_examples(include_unverified=True)
    assert (tmp_path / "runtime" / "personal" / f"{ex.id}.json").exists()


def test_same_position_is_not_generated_twice(tmp_path, engine):
    lib = make_library(tmp_path)
    first = gen.generate("hanging_piece", lib, engine, count=1, seed=21, use_qwen=False, gen_log=_log(tmp_path))
    assert len(first.accepted) == 1
    again = gen.generate("hanging_piece", lib, engine, count=1, seed=21, use_qwen=False, gen_log=_log(tmp_path))
    assert first.accepted[0].id not in {e.id for e in again.accepted}
    assert any(r["stage"] == "duplicate" for r in again.rejected)


def test_threat_puzzle_key_move_parries_the_threat(tmp_path, engine):
    lib = make_library(tmp_path)
    result = gen.generate("hung_piece", lib, engine, count=1, seed=4, use_qwen=False, gen_log=_log(tmp_path))
    ex = next((e for e in result.accepted if e.concept == "spotting_threats"), None)
    if ex is None:  # the plan mixes in hanging_piece; ask for the threat concept directly
        result = gen.generate("spotting_threats", lib, engine, count=1, seed=4, use_qwen=False,
                              gen_log=_log(tmp_path))
        ex = result.accepted[0]
    threat = ex.facts["parries_threat"]["threat"]
    board = chess.Board(ex.start_fen)
    target = chess.parse_square(threat["target"]["square"])
    assert board.piece_at(target).color == board.turn  # the learner's piece is the one attacked
    assert ex.verification["engine"]["profile"] == "defence"
    for san in [ex.moves[0], *ex.accepted]:  # every accepted answer deals with the threat
        b = board.copy()
        b.push_san(san)
        assert not validators._threats_against(b, board.turn)


def test_intended_tactic_that_does_not_exist_is_rejected(tmp_path, engine):
    lib = make_library(tmp_path)
    quiet = Proposal(FORK_FEN, chess.Move.from_uci("b5d4"), "knight_fork", "test")  # no fork at all
    with pytest.raises(gen.Rejected) as err:
        gen.evaluate(quiet, "knight_fork", lib, engine, source={"source_type": "procedural"})
    assert err.value.stage == "concept"


def test_fork_that_loses_to_a_better_move_is_rejected(tmp_path, engine):
    """1.Nc7+ forks king and rook, but 1.Bxh4 (taking the queen) is just as good: solving the
    puzzle wouldn't require seeing the fork, so it doesn't test forks."""
    lib = make_library(tmp_path)
    fen = "r3k3/8/8/3N4/7q/8/8/1K2B3 w - - 0 1"
    proposal = Proposal(fen, chess.Move.from_uci("d5c7"), "knight_fork", "test")
    with pytest.raises(gen.Rejected) as err:
        gen.evaluate(proposal, "knight_fork", lib, engine, source={"source_type": "procedural"})
    assert err.value.stage == "discrimination"


def test_equally_good_move_with_the_same_idea_becomes_an_accepted_answer(tmp_path, engine):
    lib = make_library(tmp_path)
    fen = "6k1/4Q3/6K1/8/8/8/8/8 w - - 0 1"  # Qg7#, Qe8# and Qd8# all mate
    report = gen.evaluate(Proposal(fen, chess.Move.from_uci("e7g7"), "support_mate", "bare"), "queen_mate",
                          lib, engine, source={"source_type": "procedural", "source_id": "t",
                                               "source_license": "x", "reference": "t", "import_date": "2026-09-26"})
    assert report.status == "verified", [(s.name, s.detail) for s in report.stages]
    assert set(report.example.accepted) == {"Qe8#", "Qd8#"}
    assert report.example.moves == ["Qg7#"]


def test_qwen_candidate_is_verified_like_any_other(tmp_path, engine):
    lib = make_library(tmp_path)
    # a real knight fork (king g8 + rook c8); White is behind before it and clearly ahead after
    good = json.dumps({"fen": "2r3k1/5ppp/8/3N4/8/8/5PPP/6K1 w - - 0 1", "solution": "Ne7+",
                       "explanation": "The knight wins the queen and mates next move"})  # prose is ignored
    teacher = FakeTeacher(good)
    result = gen.generate("knight_fork", lib, engine, count=1, seed=2, use_qwen=True, teacher=teacher,
                          max_attempts=1, gen_log=_log(tmp_path))
    assert teacher.calls == 1
    assert len(result.accepted) == 1, result.rejected
    ex = result.accepted[0]
    assert ex.source["source_type"] == "qwen_generated" and ex.moves[0] == "Ne7+"
    assert "mates" not in ex.explanation  # the text comes from verified facts, not from Qwen


def test_qwen_candidates_with_wrong_chess_are_rejected(tmp_path, engine):
    lib = make_library(tmp_path)
    replies = [json.dumps({"fen": "2r3k1/5ppp/8/3N4/8/8/5PPP/6K1 w - - 0 1", "solution": "Nf4"}),  # no fork
               json.dumps({"fen": "r3k3/8/8/3N4/7q/8/8/1K2B3 w - - 0 1", "solution": "Nc7+"}),  # fork, not needed
               "Sure! Here is a great puzzle.",                                                   # no JSON
               json.dumps({"fen": "8/8/8/8/8/8/8/8 w - - 0 1", "solution": "e4"})]                # no kings
    stages = []
    for reply in replies:  # one attempt each: the first attempt of a run goes to Qwen
        result = gen.generate("knight_fork", lib, engine, count=1, seed=2, use_qwen=True, max_attempts=1,
                              teacher=FakeTeacher(reply), gen_log=_log(tmp_path))
        # (an unparseable answer falls back to a constructed position in the same attempt)
        assert all(e.source["source_type"] == "procedural" for e in result.accepted)
        stages += [r["stage"] for r in result.rejected if r["source"] == "qwen_generated"]
    assert stages == ["concept", "discrimination", "rules", "rules"]
    assert not any(e.source.get("source_type") == "qwen_generated" for e in lib.entries.values())


def test_variety_prefers_less_used_motifs(tmp_path, engine):
    lib = make_library(tmp_path)
    log = _log(tmp_path)
    for _ in range(8):
        log.record({"concept": "knight_fork", "outcome": "verified", "signature": "knight_fork:king+rook:w"})
    result = gen.generate("fork", lib, engine, count=1, seed=3, use_qwen=False, gen_log=log)
    assert result.accepted and result.accepted[0].concept in ("queen_fork", "pawn_fork")


# ------------------------------------------------------------------ the parries_threat validator
def _parries(fen: str, san: str) -> dict:
    return validators.run("parries_threat", validators.Ctx(replay(fen, [san]), key_ply=0), {})


def test_parries_threat_validator():
    fen = "4k3/8/8/3p4/4N3/8/8/4K3 w - - 0 1"  # the pawn on d5 attacks the knight on e4
    facts = _parries(fen, "Nc3")
    assert facts["threat"]["attacker"]["square"] == "d5" and facts["threat"]["target"]["square"] == "e4"
    with pytest.raises(validators.Fail, match="can still be won"):
        _parries(fen, "Ke2")  # ignores the threat: the knight is still en prise
    with pytest.raises(validators.Fail):
        _parries("4k3/8/8/8/4N3/8/8/4K3 w - - 0 1", "Nc3")  # nothing was threatened
