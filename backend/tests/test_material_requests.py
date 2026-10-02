"""Specific material requests ("two rooks against a queen") keep their exact meaning:
counts per side, which side the learner has, and never a weaker neighbouring topic."""
import chess
import pytest

from app.planner.intent import MaterialSpec

TWO_R_V_Q = MaterialSpec(("R", "R"), "versus", against=("Q",), owner="learner")
R_V_Q = MaterialSpec(("R",), "versus", against=("Q",))


# ------------------------------------------------------------------ the model
@pytest.mark.parametrize("spec, slug", [
    (TWO_R_V_Q, "two_rooks_vs_queen"),
    (R_V_Q, "rook_vs_queen"),
    (MaterialSpec(("Q",), "versus", against=("R", "R")), "queen_vs_two_rooks"),
    (MaterialSpec(("R", "Q"), "versus", against=("Q",)), "queen_and_rook_vs_queen"),
    (MaterialSpec(("Q", "R"), "versus", against=("Q",)), "queen_and_rook_vs_queen"),  # word order is not meaning
    (MaterialSpec(("R", "R"), "versus", against=("Q", "Q")), "two_rooks_vs_two_queens"),
    (MaterialSpec(("R", "B"), "versus", against=("Q",)), "rook_and_bishop_vs_queen"),
    (MaterialSpec(("R", "R"), "versus", against=("Q", "R")), "two_rooks_vs_queen_and_rook"),
])
def test_every_count_combination_has_its_own_id(spec, slug):
    assert spec.slug() == slug


def test_the_learners_side_and_counts_are_explicit():
    assert TWO_R_V_Q.sides() == {
        "learner": {"queens": 0, "rooks": 2, "bishops": 0, "knights": 0, "pawns": 0},
        "opponent": {"queens": 1, "rooks": 0, "bishops": 0, "knights": 0, "pawns": 0}}
    assert TWO_R_V_Q.describe() == ["You have two rooks.", "Your opponent has a queen."]
    assert TWO_R_V_Q.label() == "Two rooks against a queen"
    assert TWO_R_V_Q.short() == "2R vs Q"


def test_round_trip_and_legacy_form():
    assert MaterialSpec.from_dict(TWO_R_V_Q.as_dict()) == TWO_R_V_Q
    assert MaterialSpec(("R", "Q"), "versus") == R_V_Q  # old one-against-one form
    assert MaterialSpec.from_dict({"pieces": ["R", "Q"], "relation": "versus"}) == R_V_Q
    assert R_V_Q.label() == "Rook against queen endgames"


TWO_R_V_Q_WHITE = "8/8/3q4/4k3/8/8/2R5/1R2K3 w - - 0 1"


@pytest.mark.parametrize("fen, learner, spec, ok", [
    (TWO_R_V_Q_WHITE, chess.WHITE, TWO_R_V_Q, True),
    (TWO_R_V_Q_WHITE, chess.BLACK, TWO_R_V_Q, False),            # the learner would hold the queen
    (TWO_R_V_Q_WHITE, None, R_V_Q, False),                       # 2R vs Q is not R vs Q
    ("8/8/3q4/4k3/8/8/2R5/4K3 w - - 0 1", chess.WHITE, TWO_R_V_Q, False),   # only one rook
    ("8/8/3q4/4k3/8/8/2R5/1R1QK3 w - - 0 1", chess.WHITE, TWO_R_V_Q, False),  # an extra queen
    ("8/8/2rq4/4k3/8/8/2R5/1R2K3 w - - 0 1", chess.WHITE, TWO_R_V_Q, False),  # the opponent has a rook too
    ("8/5pp1/3q4/4k3/8/5P2/2R5/1R2K3 w - - 0 1", chess.WHITE, TWO_R_V_Q, True),  # pawns are fine
    ("8/8/3q4/4k3/8/8/2R5/4K3 w - - 0 1", None, R_V_Q, True),
])
def test_matching_is_exact_per_side(fen, learner, spec, ok):
    assert spec.matches(chess.Board(fen), learner) is ok


def test_bad_specs_are_refused():
    with pytest.raises(ValueError):
        MaterialSpec(("R", "R", "Q"), "versus")  # legacy form is only one against one
    with pytest.raises(ValueError):
        MaterialSpec(("R",), "together", owner="learner")


# ------------------------------------------------------------------ reading the request
from app.planner.catalog import _normalize  # noqa: E402
from app.planner.intent import ClarificationNeeded, IntentMemory, understand  # noqa: E402
from app.planner.intent.material import extract, objectives_for  # noqa: E402
from app.planner.intent.semantic import Interpreted, SchemaError, validate  # noqa: E402


def _read(text):
    r = extract(_normalize(text.replace("+", " and ")))
    return r.spec if r else None


@pytest.mark.parametrize("text, slug", [
    ("2 rooks against a queen", "two_rooks_vs_queen"),
    ("two rooks vs one queen", "two_rooks_vs_queen"),
    ("2 rooks vs a queen", "two_rooks_vs_queen"),
    ("2R vs Q", "two_rooks_vs_queen"),
    ("I have two rooks and they have a queen", "two_rooks_vs_queen"),
    ("I have two rooks and they only have a queen", "two_rooks_vs_queen"),
    ("beat a queen with two rooks", "two_rooks_vs_queen"),
    ("practice positions where I have two rooks against their queen", "two_rooks_vs_queen"),
    ("Show me endgames where I'm up two rooks but they have a queen", "two_rooks_vs_queen"),
    ("I want to learn the 2 rooks and queen endgame, where I have 2 rooks and my opponent has 1 queen",
     "two_rooks_vs_queen"),
    ("rook against queen", "rook_vs_queen"),
    ("queen against two rooks", "queen_vs_two_rooks"),
    ("queen vs two rooks", "queen_vs_two_rooks"),
    ("the opponent has two rooks and I have a queen", "queen_vs_two_rooks"),
    ("rook and queen against a queen", "queen_and_rook_vs_queen"),
    ("two rooks vs two queens", "two_rooks_vs_two_queens"),
    ("rook and bishop vs queen", "rook_and_bishop_vs_queen"),
    ("two rooks vs queen and rook", "two_rooks_vs_queen_and_rook"),
])
def test_the_parser_reads_counts_and_sides(text, slug):
    assert _read(text).slug() == slug


@pytest.mark.parametrize("text", ["2 rooks against a queen", "I have two rooks and they have a queen",
                                  "beat a queen with two rooks", "queen against two rooks"])
def test_specific_readings_say_whose_pieces_are_whose(text):
    assert _read(text).owner == "learner"


@pytest.mark.parametrize("text", ["how to defend against a queen sacrifice with my rook", "is a queen better than two rooks",
                                  "Queen's Gambit with my knights", "I want to learn the 2 rooks and queen endgame"])
def test_no_position_is_invented(text):
    assert _read(text) is None


def test_a_bare_plural_is_a_guess_that_gets_confirmed():
    r = extract(_normalize("rooks vs queen"))
    assert r.guessed and r.spec.slug() == "two_rooks_vs_queen"
    with pytest.raises(ClarificationNeeded) as exc:
        understand("rooks vs queen")
    assert any("assumed two" in d for d in exc.value.question.details)


CONFIRM = "confirm:two_rooks_vs_queen:learner"


@pytest.mark.parametrize("goal", ["2 rooks against a queen", "two rooks vs one queen", "I have two rooks and they have a queen",
                                  "I want to learn the 2 rooks and queen endgame, where I have 2 rooks and my opponent "
                                  "has 1 queen"])
def test_specific_requests_are_confirmed_with_explicit_counts(goal):
    with pytest.raises(ClarificationNeeded) as exc:
        understand(goal)
    q = exc.value.question.as_dict()
    assert q["key"] == CONFIRM and q["kind"] == "confirm"
    assert q["question"] == "I understand your request as:"
    assert q["details"][:2] == ["You have two rooks.", "Your opponent has a queen."]
    ids = [o["id"] for o in q["options"]]
    assert ids[0] == "general" and ids[-1] == "other" and q["options"][-1]["label"] == "Change something"
    assert {"coordinate", "avoid_perpetual"} <= set(ids)  # the focuses this material makes meaningful


def test_simple_requests_are_not_confirmed():
    intent = understand("rook against queen")
    assert [c.material.slug() for c in intent.components] == ["rook_vs_queen"]


@pytest.mark.parametrize("goal, slug", [
    ("2 rooks against a queen", "two_rooks_vs_queen"), ("queen against two rooks", "queen_vs_two_rooks"),
    ("rook and queen against a queen", "queen_and_rook_vs_queen")])
def test_confirmed_intent_is_exactly_the_request(goal, slug):
    spec = _read(goal)
    intent = understand(goal, answers={f"confirm:{slug}:learner": {"choice": "general"}})
    assert [c.material for c in intent.components] == [spec]
    assert intent.components[0].material.slug() == slug and intent.objective == "general"


def test_the_confirmation_is_remembered():
    memory = IntentMemory()
    understand("2 rooks against a queen", answers={CONFIRM: {"choice": "coordinate"}}, memory=memory)
    again = understand("I have two rooks and they have a queen", memory=memory)  # same meaning, other words
    assert again.components[0].material.slug() == "two_rooks_vs_queen" and again.objective == "coordinate"


def test_a_stated_focus_is_the_objective_not_another_subject():
    intent = understand("two rooks vs a queen and avoid perpetual checks", answers={CONFIRM: {"choice": "avoid_perpetual"}})
    assert [c.kind for c in intent.components] == ["material"] and intent.objective == "avoid_perpetual"


def test_change_something_rereads_the_correction():
    intent = understand("2 rooks against a queen",
                        answers={CONFIRM: {"choice": "other", "text": "I have a rook and a queen, they have a queen"},
                                 "confirm:queen_and_rook_vs_queen:learner": {"choice": "general"}})
    assert intent.components[0].material.slug() == "queen_and_rook_vs_queen"


def test_counts_in_an_ambiguous_phrase_are_kept():
    with pytest.raises(ClarificationNeeded) as exc:
        understand("I want to learn the 2 rooks and queen endgame")
    labels = [o.label for o in exc.value.question.options]
    assert "Two rooks against a queen (you have two rooks)" in labels
    assert "Queen against two rooks (you have a queen)" in labels
    assert not any("a rook and a rook" in label for label in labels)
    intent = understand("I want to learn the 2 rooks and queen endgame",
                        answers={exc.value.question.key: {"choice": "versus:two_rooks_vs_queen"}})
    assert intent.components[0].material == TWO_R_V_Q


def test_objectives_follow_the_material():
    assert objectives_for(TWO_R_V_Q) == ["general", "coordinate", "avoid_perpetual"]
    assert "hold" in objectives_for(MaterialSpec(("R",), "versus", against=("Q",), owner="learner"))
    assert "convert" in objectives_for(MaterialSpec(("Q", "R"), "versus", against=("Q",), owner="learner"))


# ------------------------------------------------------------------ the semantic interpreter (Qwen)
QWEN_2RQ = {"topic_type": "endgame", "user_pieces": {"queens": 0, "rooks": 2, "bishops": 0, "knights": 0},
            "opponent_pieces": {"queens": 1, "rooks": 0, "bishops": 0, "knights": 0}, "material_relation": "versus",
            "side_to_train": "user", "requested_focus": "general", "needs_custom_generation": True}


def _qwen(data):
    return lambda goal: Interpreted("ok", validate(data))


@pytest.mark.parametrize("bad", [
    {**QWEN_2RQ, "user_pieces": {"rooks": "two"}}, {**QWEN_2RQ, "user_pieces": {"rooks": 9}},
    {**QWEN_2RQ, "user_pieces": {"rooks": True}}, {**QWEN_2RQ, "topic_type": "rook endgame"},
    {**QWEN_2RQ, "material_relation": "vs"}, {**QWEN_2RQ, "opponent_pieces": {"dragons": 1}},
    {**QWEN_2RQ, "needs_custom_generation": "yes"}, ["not", "an", "object"]])
def test_the_interpreter_schema_is_strict(bad):
    with pytest.raises(SchemaError):
        validate(bad)


def test_the_interpreter_never_produces_positions():
    from app.planner.intent.semantic import SYSTEM
    assert validate(QWEN_2RQ).spec == TWO_R_V_Q
    assert "never give moves, positions" in SYSTEM


def test_qwen_and_parser_agreeing_is_confirmed():
    with pytest.raises(ClarificationNeeded) as exc:
        understand("I have two rooks and they have a queen", interpreter=_qwen(QWEN_2RQ))
    assert exc.value.question.key == CONFIRM


def test_qwen_and_parser_disagreeing_is_asked_never_chosen():
    flipped = {**QWEN_2RQ, "user_pieces": QWEN_2RQ["opponent_pieces"], "opponent_pieces": QWEN_2RQ["user_pieces"]}
    with pytest.raises(ClarificationNeeded) as exc:
        understand("I have two rooks and they have a queen", interpreter=_qwen(flipped))
    q = exc.value.question
    assert q.kind == "material_reading"
    assert [o.label for o in q.options] == ["You have two rooks. Your opponent has a queen.",
                                            "You have a queen. Your opponent has two rooks."]


def test_a_reading_only_qwen_found_is_confirmed_and_marked():
    with pytest.raises(ClarificationNeeded) as exc:
        understand("an ending with two rooks for me and a queen for the other guy", interpreter=_qwen(QWEN_2RQ))
    assert exc.value.question.key == CONFIRM
    assert any("language model" in d for d in exc.value.question.details)


@pytest.mark.parametrize("status", ["invalid", "timeout", "unavailable"])
def test_a_failed_interpreter_falls_back_to_the_parser(status):
    with pytest.raises(ClarificationNeeded) as exc:
        understand("I have two rooks and they have a queen", interpreter=lambda g: Interpreted(status, error="x"))
    assert exc.value.question.key == CONFIRM


def test_the_interpreter_is_not_called_for_unrelated_requests():
    calls = []
    understand("knight fork puzzles", interpreter=lambda g: calls.append(g))
    assert not calls
