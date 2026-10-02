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
