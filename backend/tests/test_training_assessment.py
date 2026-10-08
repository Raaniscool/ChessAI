"""Training (Puzzles tab): adaptive skill assessment from short games against a bot (app.assessment).

Covers: position generation, phase classification, bot difficulty, the Stockfish analysis of a
segment, concept detection (validators only), skill-profile updates, confidence/sample size,
adaptive Practice weighting, Practice and Personalized integration, Mixed Games, the hidden concept,
repeat prevention, and the hidden idea as unprompted recognition evidence for the learner model
(the found-set it uses, the moment cap it survives, and the transfer/recognition gap it can raise).
"""
import random
from collections import Counter
from types import SimpleNamespace

import chess
import pytest

from app.analysis import GameAnalyzer
from app.analysis.analyzer import phase_of
from app.assessment import bot, evaluate, needs, positions, record
from app.assessment.positions import PositionPool, TrainingPosition, choose, next_phase
from app.assessment.segment import (MAX_MOVES, MIN_MOVES, Segment, SegmentStore, end_reason, get_segments,
                                    set_segments)
from app.engine import set_engine
from app.engine.classification import Score
from app.engine.service import Line
from app.knowledge.library import get_knowledge
from app.knowledge.opening_trees import get_opening_trees
from app.knowledge.usage import get_usage
from app.learner import LearnerProfile, get_profile, get_store
from app.learner.difficulty import build, recognition_trials
from app.learner.recommend import suggestions
from app.learner.views import combined, concept_view, gaps_of, status_of
from app.puzzles import get_puzzles, select
from app.puzzles.dashboard import candidates, personalized, theme_pool
from app.puzzles.select import FOCUS_REASON
from tests.games_helpers import FORK_FEN, ScriptedEngine, epd, fork_engine
from tests.test_api import _fresh_state, client  # noqa: F401  (fixtures)

LIB = get_knowledge()


@pytest.fixture(autouse=True)
def _segments():
    set_segments(SegmentStore())
    yield
    set_segments(None)


def fork_position(**over) -> TrainingPosition:
    """FORK_FEN as a verified idea position: 1.Nc7+ forks king and queen (hidden: knight_fork)."""
    pos = TrainingPosition(id="puzzle:test_fork", fen=FORK_FEN, phase=phase_of(chess.Board(FORK_FEN)), side="white",
                           source={"type": "curated", "label": "Verified library"}, rating=900,
                           hidden={"concept": "knight_fork", "accepted": ["Nc7+"], "rating": 900,
                                   "puzzle_id": "test_fork", "key_move": "Nc7+"})
    for k, v in over.items():
        setattr(pos, k, v)
    return pos


def segment(pos=None, moves=(), rating=1000, seed=1) -> Segment:
    seg = Segment(id="s1", mode="middlegame", position=pos or fork_position(), bot={"rating": rating}, seed=seed)
    board = chess.Board(seg.position.fen)
    for san in moves:
        move = board.parse_san(san)
        seg.moves_san.append(san)
        seg.moves_uci.append(move.uci())
        board.push(move)
    return seg


def analyse(seg, engine):
    return evaluate.analyze(seg, engine, LIB, analyzer=GameAnalyzer(engine, depth=8, confirm_depth=10))


# ------------------------------------------------------------------ position generation / phases
def test_opening_positions_come_from_verified_opening_trees():
    ops = positions.opening_positions(get_opening_trees())
    assert len(ops) > 500
    lo, hi = positions.OPENING_PLIES
    for pos in ops[:: max(1, len(ops) // 200)]:
        board = chess.Board(pos.fen)
        assert board.is_valid() and not board.is_game_over()
        assert lo <= len(pos.source["line"]) <= hi
        assert pos.phase == phase_of(board) == "opening"
        assert pos.side == ("white" if board.turn else "black")
        assert pos.hidden is None and pos.source["type"] == "opening_tree"
    assert len({chess.Board(p.fen).epd() for p in ops}) == len(ops)   # no duplicate positions


def test_opening_ideas_are_verified_rare_and_never_revealed_early(client):
    """Opening training stays theory-first: the trees supply thousands of idea-less positions, and
    the idea-bearing ones are verified puzzles whose key position is in the opening. The local
    verified sources can only support a handful of those (the audit's finding) — the deepening
    proved it: both remaining candidates failed the engine stage. Theory positions keep no idea."""
    pool = positions.get_pool(LIB)
    theory = [p for p in pool.by_phase["opening"] if not p.hidden]
    ideas = [p for p in pool.by_phase["opening"] if p.hidden]
    assert len(theory) > 4000 and all(p.source["type"] == "opening_tree" for p in theory)
    assert 10 <= len(ideas) <= 40
    for pos in ideas:
        assert LIB.validator_for(pos.hidden["concept"]) and pos.hidden["concept"] in LIB.concepts
        assert pos.hidden["accepted"] and pos.hidden["key_move"] in pos.hidden["accepted"]
        assert pos.phase == phase_of(chess.Board(pos.fen)) == "opening"
        assert pos.public() == {"fen": pos.fen, "side": pos.side, "phase": pos.phase}
    for seed in range(12):                                 # whatever the draw, nothing leaks
        seg = _seed_start(client, "opening", seed)
        pos = get_segments().get(seg["id"]).position
        assert pos.phase == "opening"
        _no_spoilers(seg, {pos.hidden["concept"]} if pos.hidden else set())


def test_puzzle_positions_are_verified_non_personal_and_phase_classified():
    pp = positions.puzzle_positions(get_puzzles(LIB).all())
    assert len(pp) > 100
    index = get_puzzles(LIB)
    for pos in pp:
        p = index.get(pos.hidden["puzzle_id"])
        assert p.tier != "personal" and (p.source or {}).get("type") != "user_game"
        assert p.verification_state == "verified"          # only verified entries are ever served
        assert pos.hidden["concept"] == p.concept in LIB.concepts
        assert LIB.validator_for(p.concept)                # every idea has a validator behind it
        assert pos.phase == phase_of(chess.Board(pos.fen))
        assert pos.hidden["accepted"] and pos.hidden["key_move"] in pos.hidden["accepted"]
    phases = Counter(p.phase for p in pp)
    assert phases["middlegame"] and phases["endgame"]


def test_the_pool_is_deep_in_every_phase_and_middlegame_is_no_longer_the_thin_one():
    """The pool deepening: middlegame used to be ~140 positions against ~290 endgame ones because
    the importers spent their budget on the shortest (mostly endgame) solutions. The local verified
    sources supply ~190 more middlegame positions; they are now in the library, so the two are
    comparable. If this fails low, the deepening data is missing, not the code."""
    counts = positions.get_pool(LIB).counts()
    assert counts["opening"] > 4000                        # verified opening theory (no hidden idea)
    assert counts["middlegame"] >= 250
    assert counts["endgame"] >= 250
    # comparable: not "dramatically smaller" (before the deepening: 138 middlegame / 292 endgame)
    assert counts["middlegame"] >= 0.85 * counts["endgame"], counts


def test_phase_comes_from_the_position_not_from_the_source():
    """The phase is the analyzer's rule — piece material plus the position's own move number — so a
    position that starts late is never called an opening because of the source it came from."""
    full = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 {}"
    assert phase_of(chess.Board(full.format(1))) == "opening"
    assert phase_of(chess.Board(full.format(30))) == "middlegame"      # same material, later move
    assert phase_of(chess.Board("8/8/8/4k3/8/8/4P3/4K3 w - - 0 1")) == "endgame"   # material decides
    index = get_puzzles(LIB)
    middlegame = positions.get_pool(LIB).by_phase["middlegame"]
    kinds = Counter(index.get(p.hidden["puzzle_id"]).type for p in middlegame)
    assert len(kinds) >= 3 and kinds["tactic"] and kinds["checkmate"]   # not just one kind of idea
    assert len({p.hidden["concept"] for p in middlegame}) >= 25         # and not just a few ideas
    for pos in middlegame[::20]:
        assert pos.phase == phase_of(chess.Board(pos.fen)) == "middlegame"


def test_every_position_phase_is_the_analyzers_phase_and_the_ideas_are_sound():
    pool = positions.get_pool(LIB)
    for phase, spots in pool.by_phase.items():
        for pos in spots[:: max(1, len(spots) // 120)]:      # a spread across every phase
            board = chess.Board(pos.fen)
            assert pos.phase == phase_of(board) and not board.is_game_over()
            assert pos.side == ("white" if board.turn else "black")
            assert pos.public() == {"fen": pos.fen, "side": pos.side, "phase": pos.phase}
            if pos.hidden:
                assert pos.hidden["concept"] in LIB.concepts
                assert LIB.validator_for(pos.hidden["concept"])
                assert pos.hidden["accepted"] and pos.hidden["key_move"] in pos.hidden["accepted"]
                assert pos.rating is not None
            else:
                assert phase == "opening" and pos.source["type"] == "opening_tree"


def _seed_start(client, mode: str, seed: int) -> dict:
    res = client.post("/api/puzzles/training/start", json={"mode": mode, "seed": seed})
    assert res.status_code == 200, res.text
    return res.json()["segment"]


def test_mixed_games_and_the_idea_pools_draw_widely(client):
    """Mixed Games continues indefinitely *and* stops cycling through a handful of positions."""
    mixed = [_seed_start(client, "mixed", seed) for seed in range(6)]
    assert len({s["phase"] for s in mixed}) >= 2           # Mixed rotates through the phases
    middlegame = [_seed_start(client, "middlegame", seed) for seed in range(20)]
    assert len({s["id"] for s in middlegame}) == 20          # 20 starts, 20 distinct segments
    assert len({s["start_fen"] for s in middlegame}) >= 15   # ... drawing from a wide pool
    for seg in middlegame:                                   # and still nothing is revealed early
        pos = get_segments().get(seg["id"]).position
        _no_spoilers(seg, {pos.hidden["concept"]} if pos.hidden else set())


def test_the_pool_has_every_phase_and_public_data_has_no_idea():
    pool = positions.get_pool(LIB)
    assert pool.phases() == ["opening", "middlegame", "endgame"]
    pos = next(p for p in pool.by_phase["middlegame"] if p.hidden)
    assert set(pos.public()) == {"fen", "side", "phase"}


def test_mixed_games_rotates_through_all_three_phases():
    rng = random.Random(3)
    history, seen = [], []
    for _ in range(9):
        phase = next_phase("mixed", history, ["opening", "middlegame", "endgame"], rng)
        seen.append(phase)
        history.append({"phase": phase})
    for i in range(0, 9, 3):
        assert sorted(seen[i:i + 3]) == ["endgame", "middlegame", "opening"]
    assert next_phase("endgame", history, ["opening", "middlegame", "endgame"], rng) == "endgame"
    assert next_phase("mixed", [], ["endgame"], rng) == "endgame"   # only what exists
    with pytest.raises(LookupError):
        next_phase("middlegame", [], ["opening"], rng)


# ------------------------------------------------------------------ repeat prevention
def _pool(n=6, phase="middlegame"):
    out = []
    for i in range(n):
        out.append(TrainingPosition(id=f"puzzle:p{i}", fen=FORK_FEN, phase=phase, side="white", source={},
                                    rating=900 + 50 * i, hidden={"concept": "fork" if i % 2 else "pin",
                                                                 "accepted": ["Nc7+"], "puzzle_id": f"p{i}",
                                                                 "rating": 900 + 50 * i, "key_move": "Nc7+"}))
    return PositionPool(out)


def test_no_repeat_while_unseen_positions_exist_and_oldest_come_back_after():
    pool = _pool()
    seen: list[str] = []
    for i in range(6):
        pos = choose(pool, "middlegame", seen=seen, used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                     rng=random.Random(i))
        assert pos.id not in seen
        seen.append(pos.id)
    assert len(set(seen)) == 6
    again = choose(pool, "middlegame", seen=seen, used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                   rng=random.Random(9))
    assert again.id == seen[0]   # everything met: the one seen longest ago returns (effectively infinite)


def test_unseen_positions_always_come_before_recently_seen_ones():
    """A small phase pool must not send the learner back into a position just played."""
    pool = _pool(n=20)
    ids = [p.id for p in pool.by_phase["middlegame"]]
    seen = ids[:16]                       # 16 met, 4 never
    for seed in range(8):
        pos = choose(pool, "middlegame", seen=seen, used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                     rng=random.Random(seed))
        assert pos.id in ids[16:]         # only ever the unseen ones, however the seeds fall
    # every position met: the least recently seen come back first, never the ones just played
    seen = ids[:]
    for seed in range(6):
        pos = choose(pool, "middlegame", seen=seen, used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                     rng=random.Random(seed))
        assert pos.id in ids[:2], (pos.id, ids[:2])       # the oldest tenth (20 // 10)


def test_a_position_in_play_is_not_drawn_while_anything_else_exists():
    """`active` (open segments) is avoided like a seen position, and is the very last resort."""
    pool = _pool()
    ids = [p.id for p in pool.by_phase["middlegame"]]
    for seed in range(6):
        pos = choose(pool, "middlegame", seen=[], used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                     rng=random.Random(seed), active={ids[0], ids[1]})
        assert pos.id not in ids[:2]
    # everything seen, one of them still in play: the re-serve takes a finished one, not the open one
    pos = choose(pool, "middlegame", seen=ids[:], used_puzzles=set(), recent_concepts=[], target=1000, needs={},
                 rng=random.Random(3), active={ids[-1]})
    assert pos.id != ids[-1]


def test_the_same_seed_serves_the_same_sequence_of_positions(client):
    """Deterministic under the same seed and the same state: the deepened pool changes nothing here."""
    def run() -> list[str]:
        get_store().reset("local")
        set_segments(SegmentStore())
        return [_seed_start(client, "mixed", seed)["start_fen"] for seed in range(6)]
    assert run() == run()


def test_puzzles_already_met_in_the_puzzles_tab_are_not_used_as_hidden_tests():
    pool = _pool()
    used = {f"p{i}" for i in range(5)}
    pos = choose(pool, "middlegame", seen=[], used_puzzles=used, recent_concepts=[], target=1000, needs={},
                 rng=random.Random(1))
    assert pos.hidden["puzzle_id"] == "p5"


def test_the_same_hidden_idea_is_not_tested_again_right_away():
    pool = _pool()
    picks = Counter()
    for seed in range(40):
        pos = choose(pool, "middlegame", seen=[], used_puzzles=set(), recent_concepts=["pin"], target=1000,
                     needs={}, rng=random.Random(seed))
        picks[pos.hidden["concept"]] += 1
    assert picks["fork"] > picks["pin"]


def test_start_twice_gives_different_positions_without_burning_either(client):
    """Two open segments never share a position, but starting one spends nothing yet: only a
    segment that produced a result marks its position seen (see the pool section below)."""
    set_engine(ScriptedEngine())
    a = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 1}).json()["segment"]
    b = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 1}).json()["segment"]
    assert a["start_fen"] != b["start_fen"]
    assert record.ensure(get_profile().training)["seen"] == []
    assert [pid for pid, st in get_usage().all_puzzle_stats().items() if st["seen"]] == []
    board = chess.Board(a["fen"])
    client.post(f"/api/puzzles/training/{a['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
    client.post(f"/api/puzzles/training/{a['id']}/finish", json={})
    training = record.ensure(get_profile().training)
    assert a["start_fen"] in {p.fen for p in positions.get_pool(LIB).by_phase["middlegame"]}
    assert len(training["seen"]) == 1 and len(training["history"]) == 1
    used = [pid for pid, st in get_usage().all_puzzle_stats().items() if st["seen"]]
    assert len(used) == 1    # Practice won't serve the finished one right away


# ------------------------------------------------------------------ bot difficulty
class LinesEngine:
    """Four candidates 0 / 60 / 160 / 400 cp worse than the best (for the side to move)."""

    def analyse_lines(self, board, depth=None, multipv=3, fresh=False):
        moves = list(board.legal_moves)[:4]
        sign = 1 if board.turn == chess.WHITE else -1
        return [Line(move=m, san=board.san(m), score=Score("cp", sign * (100 - loss)), pv_san=[board.san(m)])
                for m, loss in zip(moves, (0, 60, 160, 400))]


def test_bot_strength_follows_the_learner_and_its_phase_skill():
    weak = build(LearnerProfile.from_dict({"onboarding": {"done": True, "rating": 600, "platform": "chesscom"}}))
    strong = build(LearnerProfile.from_dict({"onboarding": {"done": True, "rating": 1900, "platform": "chesscom"}}))
    assert bot.strength_for(weak, "middlegame")["rating"] < bot.strength_for(strong, "middlegame")["rating"]
    assert bot.strength_for(strong, "endgame")["skill"] == "endgame"
    assert bot.strength_for(strong, "opening")["skill"] == "opening"
    assert bot.tolerance(400) > bot.tolerance(1200) > bot.tolerance(2000)


def test_stronger_bots_lose_less_and_the_bot_is_reproducible_and_legal():
    board = chess.Board()
    engine = LinesEngine()

    def mean_loss(rating):
        rng = random.Random(5)
        return sum(bot.choose_move(engine, board, rating, rng)["loss_cp"] for _ in range(400)) / 400
    assert mean_loss(500) > mean_loss(1300) > mean_loss(2200)
    a = [bot.choose_move(engine, board, 900, random.Random(7))["uci"] for _ in range(3)]
    assert len(set(a)) == 1
    move = chess.Move.from_uci(bot.choose_move(engine, board, 900, random.Random(1))["uci"])
    assert move in board.legal_moves


def test_bot_with_one_legal_move_or_without_multipv():
    only = chess.Board("7k/8/8/8/8/8/6q1/7K w - - 0 1")   # Kxg2 is the only legal move
    legal = list(only.legal_moves)
    out = bot.choose_move(LinesEngine(), only, 1000, random.Random(1))
    assert len(legal) == 1 and out["uci"] == legal[0].uci()
    out = bot.choose_move(ScriptedEngine.__mro__[1](), chess.Board(), 1000, random.Random(1))  # FakeEngine: lines
    assert chess.Move.from_uci(out["uci"]) in chess.Board().legal_moves

    class BestOnly:
        def analyse(self, board, depth=None):
            return ScriptedEngine().analyse(board, depth)
    out = bot.choose_move(BestOnly(), chess.Board(), 1000, random.Random(1))
    assert chess.Move.from_uci(out["uci"]) in chess.Board().legal_moves


# ------------------------------------------------------------------ segment length
def test_segment_ends_at_a_quiet_moment_between_min_and_max_and_at_once_when_the_game_ends():
    seg = segment(pos=fork_position(fen=chess.STARTING_FEN, hidden=None, phase="opening"),
                  moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6"])
    assert seg.learner_moves() == 4 and end_reason(seg, seg.board()) is None      # fewer than MIN_MOVES
    seg = segment(pos=seg.position, moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4"])
    assert seg.learner_moves() == MIN_MOVES and end_reason(seg, seg.board()) is None   # an exchange is going on
    seg = segment(pos=seg.position, moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d3", "d6"])
    assert end_reason(seg, seg.board()) == "quiet"
    seg = segment(pos=seg.position, moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d3"])
    assert end_reason(seg, seg.board()) is None    # never on the bot's turn
    mate = segment(pos=seg.position, moves=["f3", "e5", "g4", "Qh4#"])
    assert end_reason(mate, mate.board()) == "checkmate"
    assert MAX_MOVES == 10


# ------------------------------------------------------------------ analysis + concept detection
def test_missed_hidden_fork_is_found_by_the_analysis_and_counted_once():
    seg = segment(moves=["Kd2", "Qa5+"])
    facts = analyse(seg, fork_engine())
    assert facts["learner_moves"] == 1 and facts["blunders"] == 1
    assert facts["tested"]["result"] == "missed" and facts["tested"]["concept"] == "knight_fork"
    assert [c["concept"] for c in facts["concepts"]] == ["knight_fork"]   # the motif repeat isn't double-counted
    assert facts["missed_opportunities"] and facts["missed_opportunities"][0]["best_move"] == "Nc7+"
    assert facts["accuracy"] is not None and facts["accuracy"] < 50


def test_found_hidden_idea_is_positive_evidence():
    board = chess.Board(FORK_FEN)
    board.push_san("Nc7+")
    engine = fork_engine(analyses={board.epd(): (850, ["Kd7", "Nxa8"])})   # still winning after the fork
    facts = analyse(segment(moves=["Nc7+", "Kd7"]), engine)
    assert facts["tested"]["result"] == "found"
    assert facts["mistakes"] == facts["blunders"] == 0 and facts["best_moves"] == 1
    assert facts["accuracy"] == pytest.approx(100, abs=1)


def test_a_different_good_move_is_no_evidence_either_way():
    engine = fork_engine(analyses={epd("q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"): (850, ["Nc7+", "Kd7", "Nxa8"])})
    seg = segment(moves=["Nc3", "Qa5"])
    board = chess.Board(FORK_FEN)
    board.push_san("Nc3")
    engine.analyses[board.epd()] = (840, ["Qa5"])     # Stockfish: just as good
    facts = analyse(seg, engine)
    assert facts["tested"] is None and facts["concepts"] == []


def test_a_centipawn_loss_alone_never_names_a_concept_or_a_missed_tactic():
    fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    board = chess.Board(fen)
    after = board.copy()
    after.push_san("Kd1")
    engine = ScriptedEngine(analyses={board.epd(): (350, ["Kd2"]), after.epd(): (0, ["Kd7"])})
    pos = TrainingPosition(id="x", fen=fen, phase="endgame", side="white", source={})
    facts = analyse(segment(pos=pos, moves=["Kd1", "Kd7"]), engine)
    assert facts["blunders"] + facts["mistakes"] == 1
    assert facts["concepts"] == [] and facts["missed_opportunities"] == []


def test_a_key_move_that_simply_mates_says_nothing_about_a_non_mate_concept():
    fen = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"    # Rd8# (the position's label: "fork", wrongly)
    pos = TrainingPosition(id="y", fen=fen, phase="endgame", side="white", source={}, rating=800,
                           hidden={"concept": "pawn_fork", "accepted": ["Rd8#"], "rating": 800,
                                   "puzzle_id": "y", "key_move": "Rd8#"})
    facts = analyse(segment(pos=pos, moves=["Rd8#"]), ScriptedEngine())
    assert facts["tested"] is None


def test_generic_findings_are_not_concept_evidence():
    assert {"tactics", "endgames", "check", None} <= evaluate.GENERIC_CONCEPTS


# ------------------------------------------------------------------ skill profile
def _facts(concepts=(), moves=6, errors=0, accuracy=90.0, phase="middlegame"):
    return {"learner_moves": moves, "accuracy": accuracy, "mistakes": errors, "blunders": 0,
            "phases": {phase: {"moves": moves, "errors": errors, "inaccuracies": 0, "best": 3, "loss_sum": 40 * moves}},
            "concepts": list(concepts)}


def _ev(concept, result, kind=None, rating=1000):
    return {"concept": concept, "result": result, "kind": kind or ("tested" if result in ("found", "missed") else
                                                                  result), "rating": rating, "phase": "middlegame"}


def test_profile_records_phases_concepts_tests_history_and_seen():
    seg = segment(pos=fork_position(phase="middlegame"))
    t = record.apply({}, seg, _facts([_ev("knight_fork", "missed"), _ev("pin", "allowed")], errors=1))
    assert t["segments"] == 1 and t["moves"] == 6
    assert t["phases"]["middlegame"]["errors"] == 1 and t["phases"]["middlegame"]["accuracy"] == [90.0]
    assert t["concepts"]["knight_fork"]["missed"] == 1 and t["concepts"]["pin"]["allowed"] == 1
    assert t["tested"][-1]["concept"] == "knight_fork" and t["tested"][-1]["found"] is False
    assert t["seen"] == ["puzzle:test_fork"] and t["history"][-1]["concept"] == "knight_fork"
    # older data is completed, never thrown away
    old = {"segments": 3, "phases": {"endgame": {"moves": 9}}}
    assert record.ensure(old)["segments"] == 3 and record.ensure(old)["phases"]["endgame"]["moves"] == 9


def test_profile_round_trips_through_the_learner_store():
    seg = segment()
    get_store().update("local", lambda p: setattr(p, "training", record.apply(p.training, seg,
                                                                               _facts([_ev("pin", "missed")]))))
    assert get_profile().training["concepts"]["pin"]["missed"] == 1


def _profile_with(events: list[tuple[str, str]]):
    p = LearnerProfile()
    for i, (concept, result) in enumerate(events):
        seg = segment(pos=fork_position(phase="middlegame"), seed=i)
        seg.id = f"s{i}"
        p.training = record.apply(p.training, seg, _facts([_ev(concept, result)]))
    return p


def test_one_bad_move_is_not_a_weakness():
    n = needs.concept_needs(_profile_with([("pin", "missed")]), LIB)["pin"]
    assert n["status"] == "insufficient" and n["priority"] < 0.05 and n["confidence"] == 0.25


def test_evidence_accumulates_with_confidence_from_the_sample_size():
    few = needs.concept_needs(_profile_with([("pin", "missed")] * 2), LIB)["pin"]
    many = needs.concept_needs(_profile_with([("pin", "missed")] * 6), LIB)["pin"]
    assert many["confidence"] > few["confidence"] and many["priority"] > few["priority"]
    assert many["status"] == "needs_work"
    assert any("Training" in r for r in many["reasons"]) and "pin" in many["why"].lower()


def test_improvement_lowers_the_priority_gradually():
    base = [("skewer", "missed")] * 5
    prios = []
    for wins in range(0, 7):
        n = needs.concept_needs(_profile_with(base + [("skewer", "found")] * wins), LIB)["skewer"]
        prios.append(n["priority"])
    assert all(a >= b for a, b in zip(prios, prios[1:])) and prios[0] > 0.1 and prios[-1] < prios[0] / 3
    n = needs.concept_needs(_profile_with(base + [("skewer", "found")] * 4), LIB)["skewer"]
    assert n["status"] in ("improving", "ok") and n["trend"] == "improving"


def test_needs_combine_training_puzzles_and_games():
    p = _profile_with([("fork", "missed")] * 2)
    for _ in range(4):
        p.record_attempt("fork", 1000, solved=False, first_try=False)
    p.weaknesses = [{"key": "w1", "concept": "fork", "game_count": 3, "total_games": 5}]
    n = needs.concept_needs(p, LIB)["fork"]
    assert set(n["sources"]) == {"training", "puzzles", "games"} and len(n["reasons"]) == 3
    assert n["status"] == "needs_work"


def test_phase_needs_and_the_summary_are_explainable():
    p = _profile_with([("pin", "missed")] * 3)
    s = needs.summary(p, LIB)
    assert s["segments"] == 3 and s["phases"]["middlegame"]["moves"] == 18
    assert s["phases"]["middlegame"]["reasons"][0].startswith("Training:")
    assert s["needs"] and s["needs"][0]["concept"] == "pin" and s["method"]


def test_training_feeds_the_difficulty_profile():
    p = LearnerProfile()
    plain = build(p).skills["overall"].estimate
    for i in range(6):
        seg = segment(seed=i)
        seg.id = f"s{i}"
        p.training = record.apply(p.training, seg, _facts([_ev("fork", "found", rating=1500)], moves=8, errors=0))
    better = build(p).skills["overall"]
    assert better.estimate > plain
    assert {e.source for e in better.evidence} >= {"training", "training chances"}


# ------------------------------------------------------------------ Practice weighting
def _pin_needs():
    return needs.concept_needs(_profile_with([("pin", "missed")] * 6), LIB)


def test_focus_covers_the_needed_concept_and_its_children_with_an_evidence_based_share():
    focus = needs.practice_focus(_pin_needs(), LIB)
    pin, other = SimpleNamespace(concept="pin"), SimpleNamespace(concept="skewer")
    rel = SimpleNamespace(concept="relative_pin")
    assert focus.key(pin) == focus.key(rel) == "pin" and focus.key(other) is None
    assert focus.weight(other) == 1.0
    b = focus.boost(pin)
    assert 0 < b <= needs.FOCUS_MAX
    assert focus.weight(pin) == pytest.approx(1 + b)                    # nothing served yet: favoured
    t = focus.target["pin"]
    assert focus.weight(pin, {"pin": 1}, 1) == pytest.approx(1 - b)      # 100% pins already: held back
    at_target = needs.practice_focus(_pin_needs(), LIB, served={"pin": round(t * 100), "fork": 100 - round(t * 100)})
    assert at_target.weight(pin) == pytest.approx(1.0, abs=0.02)        # at its share: neutral
    family = needs.practice_focus(_pin_needs(), LIB, served={"absolute_pin": 1, "relative_pin": 1, "fork": 2})
    assert family.served["pin"] == 2                                    # the whole family counts
    # a weaker need gets a smaller boost and a smaller target share
    weaker = needs.practice_focus(needs.concept_needs(_profile_with([("pin", "missed")] * 3), LIB), LIB)
    assert weaker is None or (weaker.boost(pin) < b and weaker.target["pin"] < t)
    assert needs.practice_focus(needs.concept_needs(_profile_with([("pin", "missed")]), LIB), LIB) is None


def test_mixed_tactics_selection_leans_to_the_weak_concept_but_stays_varied():
    index = get_puzzles(LIB)
    pool = theme_pool(candidates(index.all()), LIB, "tactics", 5)
    focus = needs.practice_focus(_pin_needs(), LIB)
    pins = set(LIB.descendants("pin"))
    tally_plain, tally_focus = Counter(), Counter()
    concepts_focus = set()
    for target in range(700, 1900, 100):
        cal = {"target": target, "zone": [target - 120, target + 150], "floor": target - 250, "summary": ""}
        plain = select(pool, LIB, "tactics", count=5, calibration=cal)
        weighted = select(pool, LIB, "tactics", count=5, calibration=cal, focus=focus)
        tally_plain["pin"] += sum(1 for c in plain.chosen if c.puzzle.concept in pins)
        tally_focus["pin"] += sum(1 for c in weighted.chosen if c.puzzle.concept in pins)
        concepts_focus |= {c.puzzle.concept for c in weighted.chosen}
        assert sum(1 for c in weighted.chosen if c.puzzle.concept in pins) < len(weighted.chosen)  # variety
        for c in weighted.chosen:
            if c.puzzle.concept in pins:
                assert FOCUS_REASON in c.reasons
    assert tally_focus["pin"] > tally_plain["pin"]
    assert len(concepts_focus) >= 5


def test_without_needs_selection_is_unchanged():
    index = get_puzzles(LIB)
    pool = theme_pool(candidates(index.all()), LIB, "tactics", 5)
    cal = {"target": 1100, "zone": [980, 1250], "floor": 850, "summary": ""}
    a = [c.puzzle.id for c in select(pool, LIB, "tactics", count=5, calibration=cal).chosen]
    b = [c.puzzle.id for c in select(pool, LIB, "tactics", count=5, calibration=cal, focus=None).chosen]
    assert a == b


def test_practice_api_uses_the_profile(client):
    get_store().update("local", lambda p: setattr(p, "training", _profile_with([("pin", "missed")] * 6).training))
    res = client.post("/api/puzzles/set", json={"mode": "practice", "concept": "tactics", "count": 5}).json()
    assert res["mixed"] is True
    pins = set(LIB.descendants("pin"))
    boosted = [p for p in res["puzzles"] if FOCUS_REASON in p["reasons"]]
    assert boosted and all(p["concept"] in pins for p in boosted)
    assert len({p["concept"] for p in res["puzzles"]}) >= 2          # still a mixed set
    assert all("pin" not in r.lower() for p in res["puzzles"] for r in p["reasons"] if r == FOCUS_REASON)


# ------------------------------------------------------------------ Personalized
def test_training_needs_become_personalized_cards_with_reasons(client):
    profile = _profile_with([("skewer", "missed")] * 6)
    pool = candidates(get_puzzles(LIB).all())
    out = personalized(profile, LIB, pool)
    card = next(c for c in out["weaknesses"] if c["key"] == "training:skewer")
    assert card["source"] == "training" and card["reasons"] and card["confidence"] > 0.5
    assert out["main"]["key"] == "training:skewer"
    # one miss: no card
    assert not any(c["source"] == "training" for c in personalized(_profile_with([("skewer", "missed")]),
                                                                   LIB, pool)["weaknesses"])
    get_store().update("local", lambda p: setattr(p, "training", profile.training))
    res = client.post("/api/puzzles/set", json={"mode": "personalized", "weakness": "training:skewer", "count": 3,
                                                "generate": False})
    assert res.status_code == 200, res.text
    assert res.json()["weakness"] == "training:skewer" and res.json()["concept"] == "skewer"
    assert res.json()["puzzles"]


def test_training_success_lowers_a_game_weakness_card_and_confirmation_raises_it():
    pool = candidates(get_puzzles(LIB).all())
    weakness = [{"key": "games:fork", "concept": "fork", "game_count": 2, "total_games": 5, "tier": "recurring"}]

    def prio(events):
        p = _profile_with(events)
        p.weaknesses = weakness
        return next(c for c in personalized(p, LIB, pool)["weaknesses"] if c["key"] == "games:fork")["priority"]
    neutral = prio([])
    assert prio([("fork", "found")] * 6) < neutral < prio([("fork", "missed")] * 6)


# ------------------------------------------------------------------ API: hidden concept, flow, Mixed
def _no_spoilers(payload: dict, pos_concepts: set[str]):
    text = str(payload)
    for key in ("'concept'", "'hidden'", "'source'", "'solution'", "'accepted'", "'key_move'", "'idea'"):
        assert key not in text, key
    names = {LIB.concepts[c].name.lower() for c in pos_concepts if c in LIB.concepts}
    assert not any(n in text.lower() for n in names)


def test_training_moves_confirm_the_expected_board_and_can_be_recovered(client):
    set_engine(ScriptedEngine(flat=True))
    segment = _seed_start(client, "opening", 42)
    board = chess.Board(segment["start_fen"])
    moves = list(board.legal_moves)
    move = moves[0]
    stale = board.copy()
    stale.push(moves[1])

    rejected = client.post(f"/api/puzzles/training/{segment['id']}/move", json={
        "uci": move.uci(), "expected_fen": stale.fen(en_passant="fen"),
    })
    assert rejected.status_code == 409
    unchanged = client.get(f"/api/puzzles/training/{segment['id']}").json()
    assert unchanged["moves_uci"] == []
    assert unchanged["current_fen"] == segment["current_fen"]

    accepted = client.post(f"/api/puzzles/training/{segment['id']}/move", json={
        "uci": move.uci(), "expected_fen": segment["current_fen"],
    })
    assert accepted.status_code == 200
    current_segment = accepted.json()["segment"]
    current = current_segment["current_fen"]
    assert current_segment["moves_uci"][0] == move.uci()
    assert current != segment["current_fen"]
    assert client.get(f"/api/puzzles/training/{segment['id']}").json()["current_fen"] == current


def test_api_flow_hides_the_idea_until_the_segment_is_over(client):
    set_engine(ScriptedEngine())
    res = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 4})
    assert res.status_code == 200
    seg = res.json()["segment"]
    # the position the API served itself (looking it up in the position pool assumes the pool has
    # not changed since the request, which stops being true when another test's generated examples
    # enter the library mid-run)
    pos = get_segments().get(seg["id"]).position
    assert pos.phase == "middlegame"
    concepts = {pos.hidden["concept"]} if pos.hidden else set()
    _no_spoilers(res.json(), concepts)
    for _ in range(12):
        board = chess.Board(seg["fen"])
        move = next(iter(board.legal_moves))
        r = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": move.uci()})
        assert r.status_code == 200, r.text
        _no_spoilers(r.json(), concepts)
        seg = r.json()["segment"]
        if r.json()["ended"]:
            break
    assert seg["end_reason"]
    assert client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": "a2a3"}).status_code == 409
    done = client.post(f"/api/puzzles/training/{seg['id']}/finish").json()
    fb = done["feedback"]
    assert fb["went_well"] and fb["work_on"] and done["analysis"]["learner_moves"] == seg["learner_moves"]
    if pos.hidden:
        assert fb["reveal"]["idea"] == LIB.concepts[pos.hidden["concept"]].name   # now it may be shown
    assert client.post(f"/api/puzzles/training/{seg['id']}/finish").json() == done    # idempotent
    prof = client.get("/api/puzzles/training/profile").json()
    assert prof["segments"] == 1


def test_an_abandoned_segment_does_not_burn_its_position(client):
    """Starting (or half-playing) a segment must not spend the position for good: only a segment
    that produced a result marks it seen. Otherwise a learner who opens and leaves would drain the
    pool without ever seeing those positions again."""
    set_engine(ScriptedEngine(flat=True))
    first = _seed_start(client, "middlegame", 4)
    for _ in range(3):                                     # play a few moves, then walk away
        board = chess.Board(first["fen"])
        r = client.post(f"/api/puzzles/training/{first['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
        assert r.status_code == 200 and not r.json()["ended"]
        first = r.json()["segment"]
    assert record.ensure(get_profile().training)["seen"] == []            # nothing spent yet
    assert get_usage().all_puzzle_stats() == {}                           # not used in Practice either
    # ... and it stays out of the next draw only while it is open
    other = _seed_start(client, "middlegame", 4)
    assert other["start_fen"] != first["start_fen"]
    assert other["id"] != first["id"]
    set_segments(SegmentStore())                            # the open segment is gone (restart/expiry)
    again = _seed_start(client, "middlegame", 4)
    assert again["start_fen"] == first["start_fen"]         # the pooled position is served again
    # finishing a segment *does* spend its position: the same seed now picks something else
    board = chess.Board(again["fen"])
    client.post(f"/api/puzzles/training/{again['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
    assert client.post(f"/api/puzzles/training/{again['id']}/finish", json={}).status_code == 200
    training = record.ensure(get_profile().training)
    assert training["seen"] == [get_segments().get(again["id"]).position.id]
    assert len([pid for pid, st in get_usage().all_puzzle_stats().items() if st["seen"]]) == 1
    assert _seed_start(client, "middlegame", 4)["start_fen"] != again["start_fen"]


def test_illegal_moves_and_wrong_turns_are_rejected(client):
    set_engine(ScriptedEngine())
    seg = client.post("/api/puzzles/training/start", json={"mode": "opening", "seed": 2}).json()["segment"]
    assert client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": "a1a8"}).status_code == 400
    assert client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": "zz"}).status_code == 400
    assert client.post(f"/api/puzzles/training/{seg['id']}/finish").status_code == 422   # nothing played yet
    assert client.post("/api/puzzles/training/start", json={"mode": "blitz"}).status_code == 404
    assert client.post("/api/puzzles/training/nope/move", json={"uci": "e2e4"}).status_code == 404


def test_api_mixed_games_covers_all_phases(client):
    set_engine(ScriptedEngine(flat=True))
    phases = []
    for seed in range(3):
        seg = client.post("/api/puzzles/training/start", json={"mode": "mixed", "seed": seed}).json()["segment"]
        phases.append(seg["phase"])
        board = chess.Board(seg["fen"])
        r = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
        assert r.status_code == 200
        assert client.post(f"/api/puzzles/training/{seg['id']}/finish").status_code == 200
    assert sorted(phases) == ["endgame", "middlegame", "opening"]


def test_home_lists_the_four_modes(client):
    home = client.get("/api/puzzles/training").json()
    assert [m["id"] for m in home["modes"]] == ["opening", "middlegame", "endgame", "mixed"]
    assert [m["label"] for m in home["modes"]] == ["Beginning Game", "Middlegame", "Endgame", "Mixed Games"]
    assert all(m["count"] > 0 for m in home["modes"])
    assert home["profile"]["segments"] == 0


def test_without_the_engine_training_says_so(client, monkeypatch):
    import app.training_api as api
    monkeypatch.setattr(api, "_engine", lambda: None)
    seg = client.post("/api/puzzles/training/start", json={"mode": "opening", "seed": 2}).json()["segment"]
    board = chess.Board(seg["fen"])
    r = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
    assert r.status_code == 503 and "Stockfish" in r.json()["error"]
    again = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
    assert again.status_code == 503   # the move wasn't kept: the segment is unchanged


# ------------------------------------------------------------------ recognition in play
# The hidden idea of a Training position is evidence of a second kind: the learner met it without
# being told anything (assessment.evaluate -> assessment.record.note_recognition). It sits beside
# the prompted puzzle results, feeds the concept estimate, and can raise a transfer/recognition gap.
def _tested_fact(concept, result, rating=1000):
    """What assessment.evaluate._tested hands back (the fields record.note_recognition reads)."""
    return {"concept": concept, "kind": "tested", "ply": 0, "rating": rating, "result": result,
            "key_move": None, "phase": "middlegame"}


def _with_recognition(profile, concept, results, rating=1000):
    """Write one recognition observation per result, the way a finished segment does: the segment
    record (assessment.record.apply, which keeps the rating) and the concept-state mirror
    (assessment.record.note_recognition) come from the same `tested` object."""
    for i, result in enumerate(results):
        seg = segment(seed=i)
        seg.id = f"s{i}"
        facts = _facts([_ev(concept, result, rating=rating)])
        facts["tested"] = _tested_fact(concept, result, rating)
        profile.training = record.apply(profile.training, seg, facts)
        record.note_recognition(profile, facts)
    return profile


def _strong_puzzles(profile, concept, n=4, rating=1000):
    for _ in range(n):
        profile.record_attempt(concept, rating, solved=True, first_try=True)
    return profile


def test_recognition_evidence_reaches_the_concept_estimate_and_stays_unprompted():
    from tests.test_difficulty_calibration import FORKS, Usage, lookup, result   # real puzzle trials
    usage = Usage({p.id: result(seconds=8) for p in FORKS[:5]})                  # five clean solves
    found = _with_recognition(LearnerProfile(), "knight_fork", ["found"] * 4)
    missed = _with_recognition(LearnerProfile(), "knight_fork", ["missed"] * 4)
    for p, hits in ((found, 4), (missed, 0)):
        st = p.concepts["knight_fork"]
        assert (st.training_tested, st.training_found, st.training_missed) == (4, hits, 4 - hits)
        assert st.training_recent == [1.0 if hits else 0.0] * 4 and st.training_last
        assert st.attempts == 0 and st.recent == [] and st.rating is None    # nothing prompted happened
    est_found, info_found = build(found, usage, lookup).concept_estimate("knight_fork", LIB)
    est_missed, info_missed = build(missed, usage, lookup).concept_estimate("knight_fork", LIB)
    assert info_missed["recognition"] == {"tested": 4, "found": 0, "missed": 4, "weight": 4.0,
                                          "unprompted": True, "detail": "found 0 of 4 hidden ideas in Training"}
    assert info_found["recognition"]["found"] == 4 and info_found["recognition"]["missed"] == 0
    assert info_missed["puzzles"] == 5 and info_missed["solved_cleanly"] == 5   # prompted side unchanged
    assert est_found > est_missed                # spotting the idea unprompted is stronger evidence
    trials = recognition_trials(missed)["knight_fork"]
    assert len(trials) == 4 and all(w == 1.0 for _r, _s, w in trials)   # its own weight, not a puzzle's
    assert trials[0][0] == 1000.0                                       # the position's own rating


def test_a_segment_that_proves_nothing_writes_no_recognition_evidence():
    p = LearnerProfile()
    record.note_recognition(p, {"tested": None})       # a different good move: no evidence either way
    assert p.concepts == {}


def test_one_segment_writes_exactly_one_recognition_observation():
    seg = segment(moves=["Kd2", "Qa5+"])
    facts = analyse(seg, fork_engine())
    assert facts["tested"]["result"] == "missed" and facts["tested"]["concept"] == "knight_fork"
    assert [c["concept"] for c in facts["concepts"]] == ["knight_fork"]   # the motif repeat is the same one
    p = LearnerProfile()
    p.training = record.apply(p.training, seg, facts)
    record.note_recognition(p, facts)
    st = p.concepts["knight_fork"]
    assert (st.training_tested, st.training_missed, st.training_found) == (1, 1, 0)
    assert len(p.training["tested"]) == 1 and p.training["tested"][0]["concept"] == "knight_fork"
    assert st.attempts == 0 and st.recent == [] and st.rating is None     # nothing prompted happened


def test_the_recognition_test_uses_the_same_accepted_moves_as_the_solver():
    """Puzzle.accepted_first is what the solver judges with, over the whole served library."""
    index = get_puzzles(LIB)
    served = [p for p in index.all(include_personal=False) if p.clear_start and p.steps]
    assert len(served) > 100 and any(len(p.accepted_first) > 1 for p in served)
    for p in served:
        solver = {p.steps[0]["uci"]} | set(p.steps[0].get("accepted") or [])
        board = chess.Board(p.fen)
        accepted = {board.parse_san(san).uci() for san in p.accepted_first}
        assert solver <= accepted, f"{p.id}: the solver accepts a move Training would call a miss"
    # and an accepted alternative really counts as found (no hard-coded puzzle ids)
    puzzle = next(p for p in served if p.steps[0].get("accepted"))
    board = chess.Board(puzzle.fen)
    alternative = board.san(chess.Move.from_uci(puzzle.steps[0]["accepted"][0]))
    assert alternative != puzzle.solution[0]
    pos = TrainingPosition(id=f"puzzle:{puzzle.id}", fen=puzzle.fen, phase=phase_of(board),
                           side=puzzle.side_to_move, source={}, rating=puzzle.rating,
                           hidden={"concept": puzzle.concept, "accepted": list(puzzle.accepted_first),
                                   "rating": puzzle.rating, "puzzle_id": puzzle.id,
                                   "key_move": puzzle.solution[0]})
    facts = analyse(segment(pos=pos, moves=[alternative]), ScriptedEngine(flat=True))
    assert facts["tested"] and facts["tested"]["result"] == "found"
    p = LearnerProfile()
    record.note_recognition(p, facts)                    # the found branch, through the real writer
    assert (p.concepts[puzzle.concept].training_found, p.concepts[puzzle.concept].training_missed) == (1, 0)


def test_an_equivalent_mate_solves_a_mating_idea_and_is_never_a_miss_for_another():
    fen = "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"       # Qg7# and Qf8# both mate
    mates = TrainingPosition(id="m", fen=fen, phase="endgame", side="white", source={}, rating=800,
                             hidden={"concept": "queen_mate", "accepted": ["Qg7#"], "rating": 800,
                                     "puzzle_id": "m", "key_move": "Qg7#"})
    facts = analyse(segment(pos=mates, moves=["Qf8#"]), ScriptedEngine(flat=True))
    assert facts["tested"]["result"] == "found"           # any mate solves a mating puzzle
    other = TrainingPosition(id="n", fen=fen, phase="endgame", side="white", source={}, rating=800,
                             hidden={"concept": "hanging_piece", "accepted": ["Qf5"], "rating": 800,
                                     "puzzle_id": "n", "key_move": "Qf5"})
    facts = analyse(segment(pos=other, moves=["Qf8#"]), ScriptedEngine(flat=True))
    assert facts["tested"] is None                        # and proves nothing about a non-mating idea


def test_a_first_move_miss_survives_the_moment_cap(monkeypatch):
    """MAX_MOMENTS caps what the report shows; the recognition test must not lose its verdict."""
    from app.analysis.analyzer import MAX_MOMENTS
    from app.engine.classification import Classification

    board = chess.Board(FORK_FEN)
    move = next(m for m in board.legal_moves if board.san(m) not in fork_position().hidden["accepted"])
    sans = [board.san(move)]
    board.push(move)
    while len(sans) < 18:                                 # nine learner moves, none of them the idea
        move = next(iter(board.legal_moves))
        sans.append(board.san(move))
        board.push(move)
    seg = segment(moves=sans)
    monkeypatch.setattr("app.analysis.analyzer.classify_move",
                        lambda *a, **k: (Classification.BLUNDER, 900, []))
    def fake_moment(self, game, boards, moves, i, side):
        if i == 0:                                        # the mildest: pushed out of the eight shown
            return {"ply": 0, "category": Classification.MISTAKE.value, "loss_cp": 90, "severity_weight": 2,
                    "phase": "middlegame", "findings": [], "san": game.moves_san[0], "best_move": None,
                    "label": "?", "motif": None}
        return {"ply": i, "category": Classification.BLUNDER.value, "loss_cp": 900, "severity_weight": 3,
                "phase": "middlegame", "findings": [], "san": game.moves_san[i], "best_move": None,
                "label": "??", "motif": None}
    monkeypatch.setattr(GameAnalyzer, "_moment", fake_moment)
    analysis = GameAnalyzer(ScriptedEngine(), depth=4, confirm_depth=6).analyze(evaluate.game_record(seg))
    assert len(analysis["moments"]) == MAX_MOMENTS and analysis["more_moments"] >= 1
    assert 0 not in [m["ply"] for m in analysis["moments"]]          # not displayed ...
    assert 0 in analysis["confirmed_plies"] and len(analysis["confirmed_plies"]) > MAX_MOMENTS

    class _Frozen:                                                   # the finished analysis, as evaluate gets it
        def analyze(self, game):
            return analysis
    facts = evaluate.analyze(seg, ScriptedEngine(), LIB, analyzer=_Frozen())
    assert facts["tested"]["result"] == "missed" and facts["tested"]["ply"] == 0
    assert facts["missed_opportunities"][0]["motif"] == "hidden_idea"


def test_strong_puzzles_with_repeated_unprompted_misses_are_a_transfer_gap():
    p = _with_recognition(_strong_puzzles(LearnerProfile(), "knight_fork", 4), "knight_fork", ["missed"] * 3)
    st = p.concepts["knight_fork"]
    assert status_of(st) == "practicing"                  # knows the idea: not weak, not mastered yet
    assert gaps_of(st) == ["transfer"]
    view = concept_view(p, "knight_fork", LIB)
    assert view["gaps"] == ["transfer"] and view["status"] == "practicing"
    assert view["recognition"] == {"tested": 3, "found": 0, "missed": 3, "last": st.training_last,
                                   "unprompted": True}
    clean = _strong_puzzles(LearnerProfile(), "knight_fork", 4)
    assert status_of(clean.concepts["knight_fork"]) == "mastered"
    assert gaps_of(clean.concepts["knight_fork"]) == []
    once = _with_recognition(_strong_puzzles(LearnerProfile(), "knight_fork", 4), "knight_fork", ["missed"])
    assert "transfer" not in gaps_of(once.concepts["knight_fork"])    # one miss is not a pattern
    even = _with_recognition(_strong_puzzles(LearnerProfile(), "knight_fork", 4), "knight_fork",
                             ["missed", "found", "found"])
    assert status_of(even.concepts["knight_fork"]) == "mastered"
    assert "transfer" not in gaps_of(even.concepts["knight_fork"])
    weak = _with_recognition(LearnerProfile(), "knight_fork", ["missed"] * 3)
    for _ in range(4):
        weak.record_attempt("knight_fork", 1000, solved=False, first_try=False)
    assert status_of(weak.concepts["knight_fork"]) == "weak"          # failing both ways is a weakness
    assert "transfer" not in gaps_of(weak.concepts["knight_fork"])


def test_recognition_of_a_sub_concept_reaches_the_family_view_and_the_suggestions():
    p = _with_recognition(_strong_puzzles(LearnerProfile(), "knight_fork", 4), "knight_fork", ["missed"] * 3)
    fam = combined(p, "fork", LIB)
    assert (fam.training_tested, fam.training_missed) == (3, 3)
    assert fam.training_last == p.concepts["knight_fork"].training_last
    assert "transfer" in gaps_of(fam)
    rec = next(s for s in suggestions(p, LIB) if s["kind"] == "recognition")
    assert rec["concept"] == "knight_fork" and "spot" in rec["title"].lower()
    assert rec["goal"] == "knight fork in real game positions" and "Training" in rec["reason"]


def test_practice_and_personalized_are_unchanged_by_the_recognition_mirror():
    """The existing Training -> needs -> Practice/Personalized loop keeps working, and the new
    concept-state mirror is not a second observation in it."""
    index = get_puzzles(LIB)
    pool = candidates(index.all())
    cal = {"target": 1100, "zone": [980, 1250], "floor": 850, "summary": ""}
    clean = _with_recognition(LearnerProfile(), "knight_fork", ["missed"] * 6)
    plain = _profile_with([("knight_fork", "missed")] * 6)             # the same segments, old writer only
    need = needs.concept_needs(clean, LIB)["knight_fork"]
    assert need == needs.concept_needs(plain, LIB)["knight_fork"]      # exactly once, not twice
    assert need["sources"]["training"]["trials"] == 6 and need["status"] == "needs_work"
    focus = needs.practice_focus(needs.concept_needs(clean, LIB), LIB)
    assert focus is not None
    chosen = select(theme_pool(pool, LIB, "tactics", 5), LIB, "tactics", count=5, calibration=cal, focus=focus).chosen
    assert any(c.puzzle.concept == "knight_fork" and FOCUS_REASON in c.reasons for c in chosen)
    assert any(c.puzzle.concept != "knight_fork" for c in chosen)      # and the set stays mixed
    cards = personalized(clean, LIB, pool)["weaknesses"]
    assert any(c["source"] == "training" and c["concept"] == "knight_fork" for c in cards)


def test_finishing_a_segment_records_the_hidden_idea_as_recognition_evidence(client):
    set_engine(ScriptedEngine())
    seg = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 4}).json()["segment"]
    pos = get_segments().get(seg["id"]).position           # the served position, as the API chose it
    assert pos.hidden                                     # a middlegame position always carries an idea
    start = chess.Board(seg["fen"])
    move = next(iter(start.legal_moves))
    best = next(m for m in start.legal_moves if m != move)
    after = start.copy(stack=False)
    after.push(move)
    sign = -1 if pos.side == "white" else 1               # the engine scores from White's point of view
    set_engine(ScriptedEngine(analyses={
        epd(seg["fen"]): (0, [start.san(best)]),
        epd(after.fen()): (sign * 900, [after.san(next(iter(after.legal_moves)))]),
    }))
    expected = "found" if start.san(move) in pos.hidden["accepted"] else "missed"
    r = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": move.uci()})
    assert r.status_code == 200, r.text
    seg = r.json()["segment"]
    for _ in range(12):                                   # play the segment out
        if r.json()["ended"]:
            break
        board = chess.Board(seg["fen"])
        r = client.post(f"/api/puzzles/training/{seg['id']}/move", json={"uci": next(iter(board.legal_moves)).uci()})
        assert r.status_code == 200, r.text
        seg = r.json()["segment"]
    done = client.post(f"/api/puzzles/training/{seg['id']}/finish", json={})
    assert done.status_code == 200 and done.json()["analysis"]["learner_moves"] >= 1
    prof = get_profile()
    st = prof.concepts[pos.hidden["concept"]]
    assert (st.training_tested, st.training_found, st.training_missed) == \
        (1, int(expected == "found"), int(expected == "missed"))
    assert len(prof.training["tested"]) == 1              # one segment, one observation
    assert st.attempts == 0 and st.recent == [] and st.rating is None
