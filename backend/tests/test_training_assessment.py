"""Training (Puzzles tab): adaptive skill assessment from short games against a bot (app.assessment).

Covers: position generation, phase classification, bot difficulty, the Stockfish analysis of a
segment, concept detection (validators only), skill-profile updates, confidence/sample size,
adaptive Practice weighting, Practice and Personalized integration, Mixed Games, the hidden concept,
and repeat prevention.
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
from app.assessment.segment import MAX_MOVES, MIN_MOVES, Segment, SegmentStore, end_reason, set_segments
from app.engine import set_engine
from app.engine.classification import Score
from app.engine.service import Line
from app.knowledge.library import get_knowledge
from app.knowledge.opening_trees import get_opening_trees
from app.knowledge.usage import get_usage
from app.learner import LearnerProfile, get_profile, get_store
from app.learner.difficulty import build
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


def test_puzzle_positions_are_verified_non_personal_and_phase_classified():
    pp = positions.puzzle_positions(get_puzzles(LIB).all())
    assert len(pp) > 100
    index = get_puzzles(LIB)
    for pos in pp:
        p = index.get(pos.hidden["puzzle_id"])
        assert p.tier != "personal" and (p.source or {}).get("type") != "user_game"
        assert pos.hidden["concept"] == p.concept in LIB.concepts
        assert pos.phase == phase_of(chess.Board(pos.fen))
        assert pos.hidden["accepted"] and pos.hidden["key_move"] in pos.hidden["accepted"]
    phases = Counter(p.phase for p in pp)
    assert phases["middlegame"] and phases["endgame"]


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


def test_start_twice_gives_different_positions_and_marks_the_puzzle_used(client):
    set_engine(ScriptedEngine())
    a = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 1}).json()["segment"]
    b = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 1}).json()["segment"]
    assert a["start_fen"] != b["start_fen"]
    seen = record.ensure(get_profile().training)["seen"]
    assert len(seen) == 2
    used = [pid for pid, st in get_usage().all_puzzle_stats().items() if st["seen"]]
    assert len(used) == 2    # Practice won't serve these right away


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


def test_api_flow_hides_the_idea_until_the_segment_is_over(client):
    set_engine(ScriptedEngine())
    res = client.post("/api/puzzles/training/start", json={"mode": "middlegame", "seed": 4})
    assert res.status_code == 200
    seg = res.json()["segment"]
    pool = positions.get_pool(LIB)
    pos = next(p for p in pool.by_phase["middlegame"] if p.fen == seg["start_fen"])
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
