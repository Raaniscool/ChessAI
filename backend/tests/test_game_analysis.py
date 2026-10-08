"""Game analysis: engine use, classification, motif detection, evidence and edge cases."""
from __future__ import annotations

import chess
import pytest

from app.analysis import analyzer as analyzer_mod
from app.analysis import motifs
from app.engine.classification import Score
from tests.games_helpers import (BACK_RANK_DEFENCE_FEN, BACK_RANK_FEN, FOOLS_MATE, FORK_AND_BISHOP_FEN, FORK_FEN,
                                 FRIED_LIVER_GAME, STALEMATE_FEN, ScriptedEngine, analyze, chesscom_pgn, epd,
                                 fork_engine, fork_game, import_one)

HABIT_MOVES = "e4 e5 Qh5 Nf6 Qh4 Be7 Qg3 d6 Qd3 Nc6 Qf3 Bg4 Qg3 O-O Qd3 d5"
HABIT_END = "r2q1rk1/ppp1bppp/2n2n2/3pp3/4P1b1/3Q4/PPPP1PPP/RNB1KBNR w KQ - 0 9"


def only_moment(analysis):
    assert len(analysis["moments"]) == 1, [(m["san"], m["category"]) for m in analysis["moments"]]
    return analysis["moments"][0]


# --- the engine is the authority ------------------------------------------------------------
def test_every_position_is_analysed_by_the_engine():
    game = import_one(FRIED_LIVER_GAME)
    engine = ScriptedEngine()
    analysis = analyze(game, engine)
    quick = {e for e, d in engine.analyse_calls if d == 8}
    boards = [b for b in game.boards() if not b.is_game_over()]
    assert quick == {b.epd() for b in boards}
    assert len(analysis["evals"]) == len(game.boards())
    assert analysis["stats"]["learner_moves"] == 13  # Black's moves in a 27-ply game


def test_candidates_get_a_deeper_second_look_and_false_alarms_are_dropped():
    game = import_one(fork_game())
    # Quick pass: Nc7+ wins the queen, so Kd2 looks like a blunder. Deep pass: it doesn't.
    engine = fork_engine(deep={epd(FORK_FEN): (-600, ["Kd2", "Qa5+"])}, deep_from=10)
    analysis = analyze(game, engine, depth=8, confirm_depth=10)
    assert analysis["moments"] == []
    assert (epd(FORK_FEN), 10) in engine.analyse_calls


def test_progress_events_are_streamed():
    from app.analysis import GameAnalyzer
    game = import_one(fork_game())
    events = list(GameAnalyzer(fork_engine(), depth=8, confirm_depth=10).iter_analysis(game))
    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["done"] == progress[-1]["total"] == 2
    assert events[-1]["type"] == "analysis"


# --- motifs mapped to existing concept ids ------------------------------------------------------
def test_missed_knight_fork_maps_to_the_knight_fork_concept():
    m = only_moment(analyze(import_one(fork_game()), fork_engine()))
    assert (m["san"], m["category"], m["symbol"]) == ("Kd2", "blunder", "??")
    assert (m["best_move"], m["best_move_uci"]) == ("Nc7+", "b5c7")
    assert (m["motif"], m["concept"]) == ("missed_fork", "knight_fork")
    assert m["findings"][0]["facts"]["attacker"]["piece"] == "knight"
    assert m["best_line"][:3] == ["Nc7+", "Kd7", "Nxa8"]


def test_several_mistakes_in_one_move_are_all_reported_most_telling_first():
    game = import_one(chesscom_pgn(["Ba5"], fen=FORK_AND_BISHOP_FEN, game_no=5))
    m = only_moment(analyze(game, fork_engine()))
    motifs_found = [f["motif"] for f in m["findings"]]
    assert motifs_found[0] == "hung_piece" and "missed_fork" in motifs_found
    assert "walked_into_fork" in motifs_found
    # Taking the bishop that just moved is not an "ignored threat": it wasn't there before.
    assert "missed_threat" not in motifs_found
    assert m["material_change"] < 0


def test_missed_back_rank_mate():
    game = import_one(chesscom_pgn(["h3"], fen=BACK_RANK_FEN, game_no=6))
    m = only_moment(analyze(game, ScriptedEngine()))
    assert "missed_mate" in m["notes"] and m["category"] == "blunder"
    assert (m["motif"], m["concept"], m["best_move"]) == ("missed_checkmate", "back_rank_mate", "Rd8#")


def test_allowed_mate_and_the_ignored_threat_as_black():
    game = import_one(chesscom_pgn(["Ra4", "Rd8#"], fen=BACK_RANK_DEFENCE_FEN, white="opp", black="RaanTest",
                                   result="1-0", game_no=7))
    m = only_moment(analyze(game, ScriptedEngine()))
    assert m["side"] == "black" and m["label"].startswith("1...")
    assert [f["motif"] for f in m["findings"]] == ["allowed_checkmate", "missed_threat"]
    assert m["concept"] == "back_rank_mate"
    assert m["findings"][1]["facts"]["ignored_threat"] == "Rd8#"


def test_checkmate_findings_drop_unrelated_noise():
    m = only_moment(analyze(import_one(FOOLS_MATE), ScriptedEngine()))
    assert m["san"] == "g4"
    assert {f["motif"] for f in m["findings"]} <= motifs.MATE_COMPANIONS
    assert m["motif"] == "allowed_checkmate"


def test_real_threat_rules():
    board = chess.Board(BACK_RANK_DEFENCE_FEN)
    assert motifs._real_threat(board, board.parse_san("Ra4"), chess.Move.from_uci("d1d8"))  # mate threat
    board = chess.Board(FORK_AND_BISHOP_FEN)
    ba5 = board.parse_san("Ba5")
    assert not motifs._real_threat(board, ba5, chess.Move.from_uci("a8a5"))  # captures the moved piece


# --- edge cases ------------------------------------------------------------------------------
def test_game_without_mistakes_has_no_moments():
    game = import_one(chesscom_pgn("e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d3 d6 O-O O-O", game_no=9))
    analysis = analyze(game, ScriptedEngine(flat=True))
    assert analysis["moments"] == [] and analysis["habits"] == []
    assert analysis["stats"]["by_category"]["excellent"] == 6
    assert analysis["stats"]["avg_loss_cp"] == 0


def test_equally_good_moves_are_not_mistakes_and_are_listed_as_alternatives():
    # Two moves win the queen equally; the learner finds neither -> the other shows as an alternative.
    engine = fork_engine(table={epd(FORK_FEN): {"Nc7+": 850, "Nd6+": 850, "Kd2": -600}})
    m = only_moment(analyze(import_one(fork_game()), engine))
    assert m["best_move"] == "Nc7+" and m["alternatives"] == ["Nd6+"]
    # ...and playing the other good move is not a mistake at all.
    engine = fork_engine(analyses={epd(FORK_FEN, "Nd6+"): (850, ["Kd7", "Nb7"])},
                         table={epd(FORK_FEN): {"Nc7+": 850, "Nd6+": 850}})
    assert analyze(import_one(fork_game("Nd6+")), engine)["moments"] == []


def test_very_short_game_and_opponent_only_moves():
    # One ply played by the opponent: nothing of the learner's to judge.
    game = import_one(chesscom_pgn(["Kd2"], white="opp", black="RaanTest", fen=FORK_FEN, game_no=11))
    analysis = analyze(game, fork_engine())
    assert analysis["moments"] == [] and analysis["stats"]["learner_moves"] == 0


def test_the_winning_side_of_a_checkmate_game_is_clean():
    analysis = analyze(import_one(FOOLS_MATE, username="speedy"), ScriptedEngine())
    assert analysis["moments"] == []
    assert analysis["evals"][-1]["kind"] == "checkmate"


def test_stalemating_a_won_position():
    game = import_one(chesscom_pgn(["Qe6"], fen=STALEMATE_FEN, result="1/2-1/2", game_no=8))
    assert game.learner_result() == "draw"
    m = only_moment(analyze(game, ScriptedEngine()))
    assert m["motif"] == "missed_checkmate" and m["category"] == "blunder"
    from app.analysis.review import fact_lines
    assert any("stalemate" in line for line in fact_lines(m))


def test_already_decided_positions_are_not_nitpicked():
    # White is a queen up; the move costs 3 pawns but White is still completely winning.
    game = import_one(fork_game())
    engine = fork_engine(analyses={epd(FORK_FEN): (1500, ["Nc7+"]), epd(FORK_FEN, "Kd2"): (1200, ["Qa5+"])})
    assert analyze(game, engine)["moments"] == []


def test_moment_cap_keeps_the_worst_and_counts_the_rest(monkeypatch):
    game = import_one(FRIED_LIVER_GAME)
    full = analyze(game, ScriptedEngine())
    assert len(full["moments"]) >= 2
    monkeypatch.setattr(analyzer_mod, "MAX_MOMENTS", 1)
    capped = analyze(game, ScriptedEngine())
    assert len(capped["moments"]) == 1
    assert capped["more_moments"] == len(full["moments"]) - 1
    worst = max(full["moments"], key=lambda m: (analyzer_mod.SEVERITY_WEIGHT[m["category"]], m["loss_cp"]))
    assert capped["moments"][0]["ply"] == worst["ply"]


# --- evidence ------------------------------------------------------------------------------
def test_each_moment_carries_its_evidence():
    game = import_one(fork_game())
    m = only_moment(analyze(game, fork_engine()))
    assert m["id"] == f"{game.id}:0" and m["ply"] == 0 and m["move_number"] == 1
    assert (m["san"], m["uci"]) == ("Kd2", "e1d2")
    assert m["fen_before"] == FORK_FEN
    assert chess.Board(m["fen_after"]).piece_at(chess.D2).piece_type == chess.KING
    assert m["eval_before"] == {"kind": "cp", "value": 850}
    assert Score(**m["eval_after"]).to_cp() <= -500
    assert m["loss_cp"] > 300 and m["severity"] == "blunder"
    assert m["source"]["game_id"] == game.id and m["source"]["opponent"] == "opp"
    assert m["source"]["player_color"] == "white"
    assert m["phase"] in ("opening", "middlegame", "endgame")


# --- opening habits ------------------------------------------------------------------------
def test_opening_habits_need_the_engine_to_confirm_they_cost_something():
    game = import_one(chesscom_pgn(HABIT_MOVES, game_no=12))
    assert analyze(game, ScriptedEngine(flat=True))["habits"] == []  # no damage, no lecture
    engine = ScriptedEngine(flat=True, analyses={chess.Board(HABIT_END).epd(): (-200, ["Qe2"])})
    habits = analyze(game, engine)["habits"]
    found = {h["motif"] for h in habits}
    assert {"early_queen", "repeated_moves", "missed_castling"} <= found
    assert all(h["category"] == "habit" and h["id"].startswith(f"{game.id}:habit:") for h in habits)
    assert {h["concept"] for h in habits} & {"early_queen", "repeated_moves", "castling"}


def test_phase_uses_the_positions_own_move_number_not_the_index_in_the_move_list():
    """A game (or a Training segment) that starts at move 25 isn't an opening just because its
    first move is index 0; from the normal start the result is unchanged."""
    middlegame = chess.Board("r1bq1rk1/pp2bppp/2n1pn2/3p4/3P4/2NBPN2/PP3PPP/R2Q1RK1 w - - 0 25")
    assert analyzer_mod.game_ply(middlegame) == 48
    assert analyzer_mod.phase_of(middlegame) == "middlegame"
    assert analyzer_mod.phase_of(middlegame, 0) == "opening"   # an explicit ply still wins
    start = chess.Board()
    for i, san in enumerate(["e4", "e5", "Nf3", "Nc6"]):
        assert analyzer_mod.game_ply(start) == i
        assert analyzer_mod.phase_of(start) == analyzer_mod.phase_of(start, i) == "opening"
        start.push_san(san)
    endgame = chess.Board("8/5k2/8/3K4/8/8/5P2/8 b - - 0 60")
    assert analyzer_mod.phase_of(endgame) == "endgame"
