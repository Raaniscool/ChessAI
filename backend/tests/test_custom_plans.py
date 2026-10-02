"""Custom plans: generation, validation, regeneration, fallback, promotion, separation.

Candidates are built by hand here to break one rule at a time; the validator must name
the broken rule. A table-driven stub engine gives exact "Stockfish" answers for claim
checks; one test runs the whole pipeline on the real engine.
"""
from __future__ import annotations

import json

import chess
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.engine import EngineUnavailable, get_engine, set_engine
from app.engine.classification import Classification, Score
from app.engine.service import Line
from app.knowledge.library import get_knowledge
from app.knowledge.plan_library import PlanLibrary, PromotionError, ReviewQueue
from app.lessons import set_library
from app.planner import plan_for_goal
from app.planner.catalog import get_catalog
from app.planner.custom import CandidatePlan, Item, Unit, build_custom_plan, validate
from app.planner.custom.composer import _item, compose
from app.planner.custom.content import get_content_index
from app.planner.custom.validate import Context
from app.planner.intent import ClarificationNeeded, Component, IntentMemory, LearningIntent, MaterialSpec, understand
from app.session import SessionManager, set_manager
from tests.test_api import FakeEngine

LIB = get_knowledge()
CAT = get_catalog()
_VAL = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


class TableEngine(FakeEngine):
    """analyse_lines from a table {"<board fen> w|b": [(san, kind, value White-POV)]}; other
    positions: mates found by python-chess, else material. `blunders`: SANs screen_line rejects."""

    def __init__(self, table=None, blunders=()):
        self.table = table or {}
        self.blunders = set(blunders)

    @staticmethod
    def key(board):
        return f"{board.board_fen()} {'w' if board.turn else 'b'}"

    def analyse_lines(self, board, depth=12, multipv=1, fresh=False):
        rows = self.table.get(self.key(board))
        if rows is None:
            rows = []
            for m in board.legal_moves:
                board.push(m)
                if board.is_checkmate():
                    rows.append((None, m, "mate", 1 if not board.turn else -1))
                else:
                    mat = sum((_VAL[p.piece_type] if p.color else -_VAL[p.piece_type]) for p in board.piece_map().values())
                    rows.append((None, m, "cp", mat))
                board.pop()
            sign = 1 if board.turn else -1
            rows.sort(key=lambda r: -(sign * (100000 if r[2] == "mate" and r[3] * sign > 0 else r[3])))
            return [Line(m, board.san(m), Score(k, v), []) for _, m, k, v in rows][:multipv]
        return [Line(board.parse_san(s), board.san(board.parse_san(s)), Score(k, v), []) for s, k, v in rows][:multipv]

    def evaluate_move(self, board, move, depth=None):
        fb = super().evaluate_move(board, move, depth)
        if board.san(move) in self.blunders:
            fb.category = Classification.BLUNDER
        return fb


def ctx_for(intent, engine=None, personal=None, level=None, plan_library=None, learner=None):
    return Context(LIB, CAT, get_content_index(LIB, CAT), intent, level, engine=engine, personal=personal,
                   plan_library=plan_library, learner=learner)


def material_intent(pieces=("B", "N"), relation="together", head="endgame"):
    # plan-structure tests need a subject with verified library coverage; knight-vs-bishop
    # (two-sided) has none under exact per-side matching, so it is generated on request instead
    spec = MaterialSpec(pieces, relation, head)
    return LearningIntent("test", [Component("material", spec.label(), spec.label(), spec)]), spec


def issues(report, family=None):
    return {i.check for i in report.issues if i.severity == "error" and (family is None or i.family == family)}


def proposed(fen, moves, role="practice", claims=None, concept=None, text="", title="New position", diff=3):
    return Item("proposed", f"p_{abs(hash((fen, tuple(moves)))) % 10**8}", role, "unverified", fen=fen, moves=moves,
                key_index=0, claims=claims or {}, concept=concept, title=title, text=text, difficulty=diff)


def one_unit_plan(intent, items, role="learn", title="Unit", objective="Learn it.", text=""):
    comp = intent.components[0]
    return CandidatePlan("test", intent.as_dict(), "Plan", "", [Unit(title, objective, comp.as_dict(), role, items, text)])


KNIGHT_BISHOP = "8/8/3b4/4k3/8/8/2N5/4K3 w - - 0 1"


# ---------------------------------------------------------------- missing library plans / generation
def test_missing_library_plan_becomes_a_verified_custom_plan(tmp_path):
    intent = understand("knight and bishop endgames", answers={"coordination:B+N:endgame": {"choice": "together"}})
    res = build_custom_plan(intent, use_qwen=False, engine=None, plan_library=PlanLibrary(tmp_path / "plans"),
                            review=ReviewQueue(tmp_path / "review"))
    assert res.status == "verified", res.attempts
    plan = res.record["plan"]
    assert plan["planner"] == "custom" and plan["custom"]["status"] == "verified"
    assert plan["intent"]["components"][0]["material"]["relation"] == "together"
    spec = MaterialSpec(("B", "N"), "together")
    for unit in plan["units"]:
        for eid in unit["example_ids"]:
            ex = LIB.get(eid) or next(i.example for i in get_content_index().items if i.example.id == eid)
            rep = ex.replay()
            assert spec.matches(rep.boards[ex.key_ply or 0])
    # every lesson is a normal, playable lesson
    from app.lessons.schema import parse_lesson
    for lesson in res.record["lessons"]:
        parse_lesson(lesson, course_id="_t")


def test_every_reading_of_an_ambiguous_request_gives_its_own_plan(tmp_path, engine):
    titles = {}
    for choice in ("separate", "together", "versus"):
        intent = understand("knight and bishop endgames", answers={"coordination:B+N:endgame": {"choice": choice}})
        # knight against bishop has no exact verified library positions: they're generated (needs Stockfish)
        res = build_custom_plan(intent, use_qwen=False, plan_library=PlanLibrary(tmp_path / choice),
                                review=ReviewQueue(tmp_path / "review"), engine=engine)
        assert res.status == "verified", (choice, res.attempts)
        titles[choice] = [u["title"] for u in res.record["plan"]["units"]]
    assert titles["separate"] == ["Knight endgames", "Bishop endgames"]
    assert all("both" in t for t in titles["together"]) and all("against" in t for t in titles["versus"])


def test_side_flip_and_database_openings_are_custom_plans(tmp_path):
    face = understand("Sicilian as White", answers={"side:sicilian_defense:white": {"choice": "face"}})
    res = build_custom_plan(face, use_qwen=False, plan_library=PlanLibrary(tmp_path / "a"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified"
    lesson = res.record["lessons"][0]
    assert "You play White" in json.dumps(lesson)
    dutch = LearningIntent("the Dutch", [Component("opening_db", "Dutch Defense", "Dutch Defense", side="black")])
    res = build_custom_plan(dutch, use_qwen=False, engine=TableEngine(), plan_library=PlanLibrary(tmp_path / "b"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified"
    assert res.record["plan"]["units"][0]["verified_by"].startswith("Lichess opening database")


def test_database_line_without_engine_is_never_presented(tmp_path):
    dutch = LearningIntent("the Dutch", [Component("opening_db", "Dutch Defense", "Dutch Defense", side="black")])
    res = build_custom_plan(dutch, use_qwen=False, engine=None, plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.status == "failed" and res.record is None
    assert res.report.status == "needs_review"
    assert ReviewQueue(tmp_path / "r").items()  # kept for a human, not shown


def test_database_line_with_a_bad_move_is_rejected(tmp_path):
    dutch = LearningIntent("the Dutch", [Component("opening_db", "Dutch Defense", "Dutch Defense", side="black")])
    ctx = ctx_for(dutch, engine=TableEngine(blunders={"f5"}))
    report = validate(compose(dutch, ctx), ctx)
    assert "opening_line_sound" in issues(report, "correctness")


@pytest.fixture(scope="module")
def engine():
    try:
        eng = get_engine()
    except EngineUnavailable:
        pytest.skip("no engine available")
    yield eng
    set_engine(None)
    eng.close()


def test_generation_fills_material_the_library_lacks_real_engine(tmp_path, engine):
    """No verified K+B+N mates exist: new ones are generated and verified by Stockfish."""
    intent = understand("bishop and knight checkmate")
    res = build_custom_plan(intent, use_qwen=False, engine=engine, plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"), generate_budget=40)
    assert res.status == "verified", res.attempts
    spec = MaterialSpec(("B", "N"), "together", "mate")
    ids = [e for u in res.record["plan"]["units"] for e in u["example_ids"]]
    assert len(ids) >= 3
    for eid in ids:
        ex = LIB.get(eid)
        assert ex.status == "verified" and ex.tier == "generated"
        rep = ex.replay()
        assert spec.matches(rep.boards[0]) and rep.final.is_checkmate()
    assert "generated and verified" in res.record["plan"]["summary"]


# ---------------------------------------------------------------- legality
@pytest.mark.parametrize("fen, moves, check", [
    ("not a fen", ["e4"], "fen_valid"),
    ("8/8/8/4k3/8/8/2N5/1BK1K3 w - - 0 1", ["Kd2"], "position_legal"),        # two white kings
    ("8/8/8/4k3/8/8/8/P3K3 w - - 0 1", ["Kd2"], "position_legal"),            # pawn on the first rank
    (KNIGHT_BISHOP, ["Nxd6"], "moves_legal"),                                  # the knight can't reach d6
    (KNIGHT_BISHOP, ["Qh5"], "moves_legal"),                                   # there is no queen
])
def test_invalid_positions_and_illegal_moves_are_rejected(fen, moves, check):
    intent, _ = material_intent()
    cand = one_unit_plan(intent, [proposed(fen, moves)])
    report = validate(cand, ctx_for(intent, engine=TableEngine()))
    assert report.status == "rejected" and check in issues(report, "legality")


def test_opening_line_with_illegal_move_is_rejected():
    intent = LearningIntent("t", [Component("opening_db", "X", "X", side="white")])
    item = Item("opening_line", "db:X@white", "lesson", "database", title="X", line=["e4", "e5", "Ke3"], side="white")
    cand = one_unit_plan(intent, [item], role="opening")
    assert "moves_legal" in issues(validate(cand, ctx_for(intent, engine=TableEngine())))


# ---------------------------------------------------------------- correctness (Stockfish claims)
def test_key_move_that_stockfish_refutes_is_rejected():
    intent, _ = material_intent()
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Ne3", "cp", 150), ("Kd2", "cp", 10), ("Na3", "cp", -300)]})
    cand = one_unit_plan(intent, [proposed(KNIGHT_BISHOP, ["Na3"])])
    report = validate(cand, ctx_for(intent, engine=eng))
    assert "key_move_sound" in issues(report, "correctness")


@pytest.mark.parametrize("claims, check", [
    ({"result": "win"}, "claim_matches_engine"),      # engine: +0.2 — not a win
    ({"mate_in": 2}, "claim_matches_engine"),         # engine: no mate
    ({"best": True}, "claim_matches_engine"),         # engine prefers Ne3 by 40 cp
])
def test_claims_that_stockfish_contradicts_are_rejected(claims, check):
    intent, _ = material_intent()
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Ne3", "cp", 60), ("Kd2", "cp", 20), ("Na3", "cp", -300)]})
    cand = one_unit_plan(intent, [proposed(KNIGHT_BISHOP, ["Kd2"], claims=claims)])
    report = validate(cand, ctx_for(intent, engine=eng))
    assert check in issues(report, "correctness")


def test_claim_that_matches_the_engine_passes():
    intent, _ = material_intent()
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Kd2", "cp", 350), ("Ne3", "cp", 20), ("Na3", "cp", -300)]})
    cand = one_unit_plan(intent, [proposed(KNIGHT_BISHOP, ["Kd2"], role="guided", claims={"result": "win", "best": True})])
    report = validate(cand, ctx_for(intent, engine=eng))
    assert not issues(report, "correctness")


def test_exercise_where_every_move_is_equal_is_rejected():
    intent, _ = material_intent()
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Kd2", "cp", 20), ("Ne3", "cp", 15), ("Kf2", "cp", 10)]})
    cand = one_unit_plan(intent, [proposed(KNIGHT_BISHOP, ["Kd2"])])
    assert "discriminates" in issues(validate(cand, ctx_for(intent, engine=eng)), "correctness")


def test_new_position_without_engine_needs_review_not_trust():
    intent, spec = material_intent(("N", "B"), "versus")
    cand = one_unit_plan(intent, [proposed(KNIGHT_BISHOP, ["Kd2"])], title=f"{spec.label()}: learn the idea")
    cand.title = f"Plan: {spec.label()}"  # a plan for exact material must name it (request_satisfied)
    report = validate(cand, ctx_for(intent, engine=None))
    assert report.status == "needs_review"


# ---------------------------------------------------------------- education
def _pool(spec, n=6):
    return get_content_index().material(spec)[:n]


def test_practice_before_learning_is_rejected():
    intent, spec = material_intent()
    pool = _pool(spec)
    comp = intent.components[0].as_dict()
    cand = CandidatePlan("t", intent.as_dict(), "P", "", [
        Unit("Practice", "Practise.", comp, "practice", [_item(c, "practice") for c in pool[3:5]]),
        Unit("Learn", "Learn.", comp, "learn", [_item(pool[0], "demonstration"), _item(pool[1], "guided")]),
    ])
    assert "role_order" in issues(validate(cand, ctx_for(intent)), "education")


def test_exercise_before_demonstration_in_a_unit_is_rejected():
    intent, spec = material_intent()
    pool = _pool(spec)
    cand = one_unit_plan(intent, [_item(pool[0], "practice"), _item(pool[1], "demonstration")])
    assert "role_order" in issues(validate(cand, ctx_for(intent)), "education")


def test_prerequisite_after_dependent_is_rejected_and_composer_orders_it():
    intent = LearningIntent("t", [Component("concept", "lucena_position", "Lucena position"),
                                  Component("concept", "passed_pawn", "Passed pawn")])
    ctx = ctx_for(intent)
    good = compose(intent, ctx)
    assert [u.component["id"] for u in good.units][0] == "passed_pawn"  # prerequisite first
    bad = CandidatePlan("t", intent.as_dict(), "P", "", list(reversed(good.units)))
    assert "prerequisites_first" in issues(validate(bad, ctx), "education")
    assert "prerequisites_first" not in issues(validate(good, ctx))


def test_difficulty_that_jumps_back_down_is_rejected():
    intent, spec = material_intent()
    hard = _item(_pool(spec)[0], "guided")
    hard.difficulty = 5
    easy = _item(_pool(spec)[1], "guided")
    easy.difficulty = 1
    cand = one_unit_plan(intent, [hard, easy])
    assert "difficulty_progression" in issues(validate(cand, ctx_for(intent)), "education")


def test_repeated_positions_redundant_units_and_no_practice_are_rejected():
    intent, spec = material_intent()
    p = _pool(spec)
    comp = intent.components[0].as_dict()
    repeated = one_unit_plan(intent, [_item(p[0], "demonstration"), _item(p[0], "guided")])
    assert "no_repeated_positions" in issues(validate(repeated, ctx_for(intent)))
    twice = CandidatePlan("t", intent.as_dict(), "P", "", [
        Unit("Same", "x", comp, "learn", [_item(p[0], "demonstration"), _item(p[1], "guided")]),
        Unit("Same", "x", comp, "practice", [_item(p[1], "practice"), _item(p[2], "practice")])])
    assert "no_redundant_units" in issues(validate(twice, ctx_for(intent)))
    watch_only = one_unit_plan(intent, [_item(p[0], "demonstration"), _item(p[1], "demonstration")])
    assert "has_practice" in issues(validate(watch_only, ctx_for(intent)))


def test_too_hard_for_the_level_is_rejected():
    intent, spec = material_intent()
    hard = _item(_pool(spec)[0], "guided")
    hard.difficulty = 5
    cand = one_unit_plan(intent, [hard])
    assert "level_fit" in issues(validate(cand, ctx_for(intent, level="beginner")), "education")
    assert "level_fit" not in issues(validate(cand, ctx_for(intent, level="advanced")))


def test_units_need_objectives():
    intent, spec = material_intent()
    cand = one_unit_plan(intent, [_item(_pool(spec)[0], "guided")], objective="")
    assert "objectives" in issues(validate(cand, ctx_for(intent)))


# ---------------------------------------------------------------- consistency (lesson/concept matching)
def test_item_without_the_requested_material_is_rejected():
    intent, spec = material_intent(("N", "B"), "versus")
    knight_vs_bishop = proposed(KNIGHT_BISHOP, ["Kd2"], role="demonstration")
    rook_vs_bishop = proposed("8/8/3b4/4k3/8/8/2R5/4K3 w - - 0 1", ["Kd2"], role="guided")
    report = validate(one_unit_plan(intent, [knight_vs_bishop, rook_vs_bishop]), ctx_for(intent))
    bad = [i for i in report.issues if i.check == "items_match_unit" and i.severity == "error"]
    assert [i.item for i in bad] == [rook_vs_bishop.ref]


def test_item_about_another_concept_is_rejected():
    intent = LearningIntent("t", [Component("concept", "fork", "Fork")])
    pins = get_content_index().concept("pin")
    forks = get_content_index().concept("fork")
    cand = one_unit_plan(intent, [_item(forks[0], "demonstration"), _item(pins[0], "guided")])
    assert "items_match_unit" in issues(validate(cand, ctx_for(intent)), "consistency")


def test_unrequested_unit_is_rejected():
    intent, spec = material_intent()
    forks = get_content_index().concept("fork")
    cand = one_unit_plan(intent, [_item(_pool(spec)[0], "guided")])
    cand.units.append(Unit("Forks", "Forks too.", Component("concept", "fork", "Fork").as_dict(), "learn",
                           [_item(forks[0], "guided")]))
    assert "units_in_scope" in issues(validate(cand, ctx_for(intent)), "consistency")


def test_excluded_subject_is_rejected_and_composer_respects_it():
    intent = understand("tactics without forks")
    ctx = ctx_for(intent)
    good = compose(intent, ctx)
    fork_family = {"fork", *LIB.descendants("fork")}
    assert not any(set(i.example.concepts) & fork_family for u in good.units for i in u.items)
    assert validate(good, ctx).status == "verified"
    good.units[0].items.append(_item(get_content_index().concept("knight_fork")[0], "guided"))
    assert "respects_exclusions" in issues(validate(good, ctx), "consistency")


def test_texts_must_match_verified_facts():
    intent, spec = material_intent()
    cand = one_unit_plan(intent, [_item(_pool(spec)[0], "guided")], text="The key idea is Qh7+ followed by mate.")
    assert "texts_match_facts" in issues(validate(cand, ctx_for(intent)), "consistency")
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Kd2", "cp", 350), ("Ne3", "cp", 20), ("Na3", "cp", -300)]})
    win = proposed(KNIGHT_BISHOP, ["Kd2"], role="guided", claims={"result": "win"})
    drawn_words = one_unit_plan(intent, [win], text="This ending is a draw with correct play.")
    assert "texts_match_facts" in issues(validate(drawn_words, ctx_for(intent, engine=eng)), "consistency")


def test_unknown_reference_is_rejected():
    intent, _ = material_intent()
    cand = one_unit_plan(intent, [Item("unknown", "r99", "practice", "unverified", title="r99")])
    assert "provenance_verified" in issues(validate(cand, ctx_for(intent)))


def test_fake_verified_claim_is_rejected():
    intent, spec = material_intent()
    fake = _item(_pool(spec)[0], "guided")
    fake.ref = "no_such_example"
    assert "provenance_verified" in issues(validate(one_unit_plan(intent, [fake]), ctx_for(intent)))


# ---------------------------------------------------------------- personalization
def _personal(evidence=True, fens=()):
    ev = [{"game_id": "g1", "fen": f} for f in fens] + ([{"game_id": "g1"}, {"game_id": "g2"}] if evidence else [])
    return {"learner": "alice", "weakness": "fork", "concept": "fork", "title": "Missed forks",
            "evidence": ev if evidence else [], "evidence_fens": list(fens), "level": "beginner"}


def test_weakness_plan_must_target_the_weakness():
    intent = LearningIntent("t", [Component("concept", "pin", "Pin")])
    ctx = ctx_for(intent, personal=_personal())
    cand = compose(intent, ctx)
    assert "targets_weakness" in issues(validate(cand, ctx), "personalization")


def test_weakness_plan_needs_evidence_and_mustnt_copy_games():
    intent = LearningIntent("t", [Component("concept", "fork", "Fork")])
    ctx = ctx_for(intent, personal=_personal(evidence=False))
    assert "uses_evidence" in issues(validate(compose(intent, ctx), ctx), "personalization")
    forks = get_content_index().concept("fork")
    own = forks[0].board.fen()
    ctx = ctx_for(intent, personal=_personal(fens=[own]))
    cand = compose(intent, ctx)
    assert "not_copying_games" in issues(validate(cand, ctx), "personalization")


def test_good_weakness_plan_passes():
    intent = LearningIntent("t", [Component("concept", "fork", "Fork")])
    ctx = ctx_for(intent, personal=_personal(), learner="alice")
    report = validate(compose(intent, ctx), ctx)
    assert report.status == "verified", report.issues


# ---------------------------------------------------------------- duplication
def test_new_position_already_in_library_is_a_duplicate():
    intent, spec = material_intent()
    known = _pool(spec)[0]
    copy = proposed(known.example.start_fen, list(known.example.moves), role="guided")
    copy.key_index = known.example.key_ply
    report = validate(one_unit_plan(intent, [copy]), ctx_for(intent, engine=TableEngine()))
    assert "positions_not_duplicated" in issues(report, "duplication")


def test_duplicate_plans_are_not_stored_twice(tmp_path):
    lib = PlanLibrary(tmp_path / "plans")
    intent, _ = material_intent()
    ctx = ctx_for(intent, plan_library=lib)
    cand = compose(intent, ctx)
    first = lib.promote(cand, validate(cand, ctx))
    again = compose(intent, ctx)
    assert lib.promote(again, validate(again, ctx))["id"] == first["id"]
    assert len(lib.templates("global")) == 1
    report = validate(again, ctx)
    assert any(i.check == "plan_not_duplicated" for i in report.issues)  # flagged, not blocking


# ---------------------------------------------------------------- failure, regeneration, fallback
def _bad(intent, ctx, fb, ex):
    c = compose(intent, ctx)
    c.units = list(reversed(c.units))  # practice before learn
    c.proposer = "bad"
    return c


def test_failed_validation_is_never_presented_and_goes_to_review(tmp_path):
    intent, _ = material_intent()
    review = ReviewQueue(tmp_path / "review")
    lib = PlanLibrary(tmp_path / "plans")
    intent.components = [Component("material", "x", "x", MaterialSpec(("N", "N"), "versus", against=("R", "R")))]  # nothing verified
    res = build_custom_plan(intent, use_qwen=False, proposers=[_bad], plan_library=lib, review=review)
    assert res.status in ("failed", "fallback")
    assert not lib.templates()
    if res.status == "fallback":  # a broader, verified plan — labelled as broader
        assert "broader idea" in res.record["plan"]["summary"]
        assert res.record["plan"]["custom"]["status"] == "fallback"
    assert review.items() and all(r["report"]["status"] != "verified" for r in review.items())


def test_regeneration_after_failure(tmp_path):
    intent, _ = material_intent()
    seen_feedback = []

    def good(i, c, fb, ex):
        seen_feedback.append(list(fb))
        return compose(i, c)

    res = build_custom_plan(intent, use_qwen=False, proposers=[_bad, good], plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified"
    assert [a["status"] for a in res.attempts] == ["rejected", "verified"]
    assert any("before" in m for m in seen_feedback[0])  # the second attempt saw why the first failed


def test_regeneration_drops_the_items_that_failed(tmp_path):
    intent, spec = material_intent()

    def with_bad_item(i, c, fb, ex):
        cand = compose(i, c)
        cand.units[0].items.append(Item("unknown", "r42", "guided", "unverified", title="r42"))
        return cand

    res = build_custom_plan(intent, use_qwen=False, plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"),
                            proposers=[with_bad_item, lambda i, c, fb, ex: compose(i, c, exclude_refs=ex)])
    assert res.status == "verified" and len(res.attempts) == 2


class FakeTeacher:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, messages, max_tokens=None):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else "{}"


def test_qwen_proposals_are_validated_and_retried_with_feedback(tmp_path):
    intent, _ = material_intent()
    bad = json.dumps({"title": "Plan", "summary": "s", "units": [
        {"part": "C1", "role": "learn", "title": "Learn", "objective": "Learn it", "items": ["r1", "r77"]}]})
    good = json.dumps({"title": "Knight vs bishop", "summary": "s", "units": [
        {"part": "C1", "role": "learn", "title": "See it", "objective": "Watch the knight fight the bishop",
         "items": ["r1", "r2"]},
        {"part": "C1", "role": "practice", "title": "Try it", "objective": "Play it yourself", "items": ["r3", "r4"]}]})
    teacher = FakeTeacher([bad, good])
    res = build_custom_plan(intent, use_qwen=True, teacher=teacher, plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified" and res.candidate.proposer == "qwen"
    assert [a["status"] for a in res.attempts] == ["rejected", "verified"]
    assert "rejected" in teacher.prompts[1] and "r77" in teacher.prompts[1]
    assert "r1" in teacher.prompts[0] and "fen" not in teacher.prompts[0].split("Answer JSON")[0].lower()


def test_qwen_proposed_position_with_false_claim_is_rejected(tmp_path):
    intent, _ = material_intent()
    board = chess.Board(KNIGHT_BISHOP)
    eng = TableEngine({TableEngine.key(board): [("Kd2", "cp", 20), ("Ne3", "cp", -100), ("Na3", "cp", -300)]})
    reply = json.dumps({"title": "P", "summary": "s", "units": [
        {"part": "C1", "role": "learn", "title": "L", "objective": "o", "items": ["r1", "r2"]},
        {"part": "C1", "role": "practice", "title": "Pr", "objective": "o", "items": ["r3"]}],
        "positions": [{"part": "C1", "fen": KNIGHT_BISHOP, "moves": ["Kd2"], "claim": "win", "text": "White wins."}]})
    res = build_custom_plan(intent, use_qwen=True, teacher=FakeTeacher([reply, reply]), engine=eng,
                            plan_library=PlanLibrary(tmp_path / "p"), review=ReviewQueue(tmp_path / "r"))
    qwen_attempts = [a for a in res.attempts if a["proposer"] == "qwen"]
    assert qwen_attempts and all(a["status"] == "rejected" for a in qwen_attempts)
    assert any("claims Kd2 wins" in m for a in qwen_attempts for m in a["issues"])
    assert res.status == "verified" and res.candidate.proposer == "composer"  # the deterministic fallback


# ---------------------------------------------------------------- verification + promotion
def test_successful_verification_runs_every_family():
    intent, _ = material_intent()
    ctx = ctx_for(intent)
    report = validate(compose(intent, ctx), ctx)
    assert report.status == "verified" and not report.errors
    from app.planner.custom.validate import CHECKS
    families = {f for _, f, _ in CHECKS}
    assert families == {"legality", "provenance", "correctness", "education", "consistency", "duplication",
                        "personalization", "request"}
    assert set(report.checks) == {n for n, _, _ in CHECKS}


def test_promotion_requires_a_matching_verified_report(tmp_path):
    lib = PlanLibrary(tmp_path / "plans")
    intent, _ = material_intent()
    ctx = ctx_for(intent)
    cand = compose(intent, ctx)
    report = validate(cand, ctx)
    cand.units[0].items.pop()  # changed after verification
    with pytest.raises(PromotionError):
        lib.promote(cand, report)
    bad = _bad(intent, ctx, [], set())
    with pytest.raises(PromotionError):
        lib.promote(bad, validate(bad, ctx))
    good = compose(intent, ctx)
    tpl = lib.promote(good, validate(good, ctx))
    assert tpl["tier"] == "global" and tpl["status"] == "verified"
    prov = tpl["provenance"]
    assert prov["generated_by"] == "ChessAI composer" and prov["verified_at"] and prov["checks"]
    assert (tmp_path / "plans" / "global" / f"{tpl['id']}.json").exists()


def test_ai_generated_plan_keeps_its_provenance(tmp_path):
    intent, _ = material_intent()
    good = json.dumps({"title": "T", "summary": "s", "units": [
        {"part": "C1", "role": "learn", "title": "See", "objective": "o", "items": ["r1", "r2"]},
        {"part": "C1", "role": "practice", "title": "Try", "objective": "o", "items": ["r3"]}]})
    res = build_custom_plan(intent, use_qwen=True, teacher=FakeTeacher([good]), plan_library=PlanLibrary(tmp_path / "p"),
                            review=ReviewQueue(tmp_path / "r"))
    assert res.template["provenance"]["generated_by"] == "ai (Qwen)"
    assert res.record["plan"]["custom"]["provenance"]["proposer"] == "qwen"


def test_learner_plans_stay_out_of_the_shared_library(tmp_path):
    lib = PlanLibrary(tmp_path / "plans")
    intent = LearningIntent("t", [Component("concept", "fork", "Fork")])
    ctx = ctx_for(intent, personal=_personal(), learner="alice")
    cand = compose(intent, ctx)
    tpl = lib.promote(cand, validate(cand, ctx))
    assert tpl["tier"] == "personal" and tpl["learner"] == "alice"
    assert (tmp_path / "plans" / "personal" / "alice" / f"{tpl['id']}.json").exists()
    assert lib.templates("global") == []
    assert lib.find(intent.signature(), "bob", "fork") is None       # another learner never gets it
    assert lib.find(intent.signature(), None) is None                  # nor does the shared tier
    assert lib.find(intent.signature(), "alice", "fork")["id"] == tpl["id"]
    with pytest.raises(PromotionError):
        PlanLibrary._assert_global_safe({"candidate": {**cand.as_dict(), "personal": None, "learner": None,
                                                        "units": [{"items": [{"ref": "mygen_x"}]}]}})


def test_equivalent_request_reuses_the_verified_plan(tmp_path, engine):
    lib = PlanLibrary(tmp_path / "plans")
    a = understand("knight vs bishop endgames")
    first = build_custom_plan(a, use_qwen=False, plan_library=lib, review=ReviewQueue(tmp_path / "r"), engine=engine)
    b = understand("knight against bishop endings")
    second = build_custom_plan(b, use_qwen=False, plan_library=lib, review=ReviewQueue(tmp_path / "r"), engine=engine)
    assert first.status == "verified" and second.status == "reused"
    assert second.record["plan"]["custom"]["template_id"] == first.template["id"]


def test_stored_plan_that_no_longer_validates_is_retired(tmp_path):
    lib = PlanLibrary(tmp_path / "plans")
    intent, _ = material_intent()
    first = build_custom_plan(intent, use_qwen=False, plan_library=lib, review=ReviewQueue(tmp_path / "r"))
    path = tmp_path / "plans" / "global" / f"{first.template['id']}.json"
    data = json.loads(path.read_text())
    data["candidate"]["units"][0]["items"][0]["ref"] = "vanished_example"
    path.write_text(json.dumps(data))
    retired = []
    real_retire = lib.retire
    lib.retire = lambda tid, reason, learner=None: retired.append((tid, reason)) or real_retire(tid, reason, learner)
    again = build_custom_plan(intent, use_qwen=False, plan_library=lib, review=ReviewQueue(tmp_path / "r"))
    assert retired and retired[0][0] == first.template["id"] and "vanished_example" in retired[0][1]
    assert again.status == "verified"  # rebuilt and re-verified, not served from the broken template
    assert "vanished_example" not in path.read_text()


# ---------------------------------------------------------------- planner + API integration
def test_plan_for_goal_asks_then_builds_the_chosen_reading(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    with pytest.raises(ClarificationNeeded):
        plan_for_goal("knight and bishop endgames", clarify=True, memory=mem, use_qwen=False)
    record = plan_for_goal("knight and bishop endgames", clarify=True, memory=mem, use_qwen=False,
                           answers={"coordination:B+N:endgame": {"choice": "versus"}})
    assert record["plan"]["planner"] == "custom"
    assert record["plan"]["intent"]["clarified"][0]["choice"] == "versus"
    again = plan_for_goal("bishop and knight endgames", clarify=True, memory=mem, use_qwen=False)  # remembered
    assert again["plan"]["intent"]["source"] == "remembered"


def test_clear_requests_keep_the_existing_planners(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    for goal, planner in (("knight forks", "knowledge"), ("teach me the windmill", "fallback")):
        record = plan_for_goal(goal, clarify=True, memory=mem, use_qwen=False)
        assert record["plan"]["planner"] == planner
    assert mem.all() == {}


def test_qwen_readings_become_a_question_never_a_choice(tmp_path):
    from app.planner import missing
    from app.knowledge.glossary import get_glossary
    teacher = FakeTeacher([json.dumps({"ids": ["knight_fork", "smothered_mate"], "match": "same"})] * 3)
    with pytest.raises(ClarificationNeeded) as exc:
        missing.resolve("that horsey trick", LIB, get_glossary(), use_qwen=True, teacher=teacher, clarify=True)
    q = exc.value.question
    assert {o.id for o in q.options} == {"concept:knight_fork", "concept:smothered_mate"}
    assert missing.resolve("that horsey trick", LIB, get_glossary(), use_qwen=True, teacher=teacher) is None
    intent = understand("that horsey trick", answers={q.key: {"choice": "concept:smothered_mate"}},
                        memory=IntentMemory(tmp_path / "m.json"))
    assert [c.key() for c in intent.components] == ["concept:smothered_mate"]


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_library(None)
    set_engine(FakeEngine())
    set_manager(SessionManager())
    from app.main import app as fastapi_app
    try:
        with TestClient(fastapi_app) as client:
            yield client
    finally:
        set_engine(None)
        set_manager(None)
        set_library(None)


def test_api_clarification_round_trip(api):
    res = api.post("/api/plans", json={"goal": "I want to learn knight and bishop endgames", "library": True})
    assert res.status_code == 200
    q = res.json()["clarify"]
    assert q["question"] == "What do you mean by “knight and bishop endgames”?"
    assert [o["id"] for o in q["options"]][-1] == "other"
    res = api.post("/api/plans", json={"goal": "I want to learn knight and bishop endgames", "library": True,
                                       "clarification": {"key": q["key"], "choice": "together"}})
    data = res.json()
    assert res.status_code == 200 and data["source"] == "custom"
    assert data["plan"]["intent"]["interpretation"].startswith("Endgames where one side has both")
    started = api.post(f"/api/lessons/{data['first_lesson_id']}/start")
    assert started.status_code == 200
    # remembered: asked in other words, no question
    again = api.post("/api/plans", json={"goal": "endgames with a bishop and a knight", "library": True}).json()
    assert "clarify" not in again and again["plan"]["intent"]["source"] == "remembered"
    # the learner can always be asked again
    asked = api.post("/api/plans", json={"goal": "endgames with a bishop and a knight", "library": True,
                                         "reclarify": True}).json()
    assert asked["clarify"]["key"] == q["key"]


def test_api_something_else(api):
    q = api.post("/api/plans", json={"goal": "the Philidor", "library": True}).json()["clarify"]
    empty = api.post("/api/plans", json={"goal": "the Philidor", "library": True,
                                         "clarification": {"key": q["key"], "choice": "other", "text": "  "}})
    assert empty.status_code == 422
    res = api.post("/api/plans", json={"goal": "the Philidor", "library": True,
                                       "clarification": {"key": q["key"], "choice": "other",
                                                         "text": "smothered mate"}})
    assert res.status_code == 200 and "smothered" in json.dumps(res.json()["plan"]).lower()


def test_api_without_library_flag_never_asks(api):
    res = api.post("/api/plans", json={"goal": "knight and bishop endgames"})
    assert res.status_code == 200 and "clarify" not in res.json()


# ---------------------------------------------------------------- latency
class SlowTeacher(FakeTeacher):
    def __init__(self, replies, delay):
        super().__init__(replies)
        self.delay = delay

    def complete(self, messages, max_tokens=None):
        import time
        time.sleep(self.delay)
        return super().complete(messages, max_tokens)


def _good_reply():
    return json.dumps({"title": "T", "summary": "s", "units": [
        {"part": "C1", "role": "learn", "title": "See", "objective": "o", "items": ["r1", "r2"]},
        {"part": "C1", "role": "practice", "title": "Try", "objective": "o", "items": ["r3"]}]})


def test_a_slow_qwen_doesnt_hold_up_a_verified_plan(tmp_path, monkeypatch):
    import time
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "qwen_plan_budget", 0.3)
    intent, _ = material_intent()
    t0 = time.monotonic()
    res = build_custom_plan(intent, use_qwen=True, teacher=SlowTeacher([_good_reply()], delay=3.0),
                            plan_library=PlanLibrary(tmp_path / "p"), review=ReviewQueue(tmp_path / "r"))
    took = time.monotonic() - t0
    assert res.status == "verified" and res.candidate.proposer == "composer"
    assert took < 2.5, took  # didn't wait the 3 s for Qwen
    assert res.record["plan"]["custom"]["status"] == "verified"


def test_a_quick_qwen_answer_is_still_preferred(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "qwen_plan_budget", 5.0)
    intent, _ = material_intent()
    res = build_custom_plan(intent, use_qwen=True, teacher=SlowTeacher([_good_reply()], delay=0.2),
                            plan_library=PlanLibrary(tmp_path / "p"), review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified" and res.candidate.proposer == "qwen"


def test_the_plan_is_composed_once_while_qwen_organises_it(tmp_path, monkeypatch):
    import app.planner.custom.pipeline as pipeline
    calls = []
    real = pipeline.compose
    monkeypatch.setattr(pipeline, "compose", lambda *a, **k: calls.append(1) or real(*a, **k))
    intent, _ = material_intent()
    res = build_custom_plan(intent, use_qwen=True, teacher=FakeTeacher([_good_reply()]),
                            plan_library=PlanLibrary(tmp_path / "p"), review=ReviewQueue(tmp_path / "r"))
    assert res.status == "verified" and len(calls) == 1


def test_a_repeated_request_reuses_the_stored_plan_without_asking_qwen(tmp_path):
    intent, _ = material_intent()
    lib = PlanLibrary(tmp_path / "p")
    build_custom_plan(intent, use_qwen=True, teacher=FakeTeacher([_good_reply()]), plan_library=lib,
                      review=ReviewQueue(tmp_path / "r"))
    again = FakeTeacher([_good_reply()])
    res = build_custom_plan(intent, use_qwen=True, teacher=again, plan_library=lib, review=ReviewQueue(tmp_path / "r"))
    assert res.status == "reused" and again.prompts == []
