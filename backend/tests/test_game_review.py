"""Review cards and the Qwen explanation of game moments: verified facts in, contradictions out."""
from __future__ import annotations

import pytest

from app.analysis import review
from app.knowledge.library import get_knowledge
from app.teacher import stream_events
from app.teacher import qwen as qwen_mod
from app.teacher.prompts import build_game_moment_messages
from tests.games_helpers import (BACK_RANK_FEN, FORK_AND_BISHOP_FEN, ScriptedEngine, analyze, chesscom_pgn,
                                 fork_engine, fork_game, import_one)
from tests.test_streaming import FakeStream, qwen_on, sse  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def fork_moment():
    return analyze(import_one(fork_game()), fork_engine())["moments"][0]


@pytest.fixture(scope="module")
def double_moment():
    game = import_one(chesscom_pgn(["Ba5"], fen=FORK_AND_BISHOP_FEN, game_no=5))
    return analyze(game, fork_engine())["moments"][0]


# --- what the review screen shows without any AI -----------------------------------------------
def test_review_card_is_short_and_beginner_friendly(fork_moment):
    card = review.review_card(fork_moment)
    assert card["title"] == "Move 1: Kd2??"
    assert card["headline"] == "You missed a knight fork."
    assert "Kd2 is a blunder according to Stockfish" in card["why"]
    assert "Nc7+" in card["why"] and card["why"].count("Next time:") == 1
    assert "missed_fork" not in card["why"] and "missed_fork" not in card["headline"]  # no internal ids leak into the text
    assert card["tip"]


def test_black_moves_are_numbered_the_chess_way():
    moment = {"move_number": 7, "side": "black", "san": "Qa2", "symbol": "?", "category": "mistake",
              "findings": []}
    assert review.title(moment) == "Move 7...Qa2?"


def test_material_is_described_by_value_not_by_a_piece_that_was_not_lost(double_moment):
    facts = review.moment_facts(double_moment, get_knowledge())
    material = next(line for line in facts if line.startswith("Material:"))
    assert "6 pawns' worth of material" in material and "rook" not in material


# --- what Qwen receives ------------------------------------------------------------------------
def test_qwen_gets_the_verified_facts(double_moment):
    facts = review.moment_facts(double_moment, get_knowledge(), level="beginner")
    text = "\n".join(facts)
    assert "Move 1: the student played Ba5 - Stockfish verdict: blunder" in text
    assert double_moment["fen_before"] in text and "reference only" in text
    assert "Stockfish's best move: Nc7+" in text
    assert "+8.50 with best play, -6.00 after Ba5" in text
    assert "Engine line after the best move: Nc7+ Kd7 Nxa8" in text
    assert "white bishop on a5 can be taken" in text           # the hung piece (validator fact)
    assert "white knight on c7 attacks black queen on a8" in text  # the missed fork (validator fact)
    assert "Lesson-library concept: Hanging a piece" in text
    assert facts[-1] == "Learner level: beginner"
    messages = build_game_moment_messages(facts, "beginner")
    user = messages[-1]["content"]
    assert all(line in user for line in facts)
    assert "do not add moves" in user and "under 110 words" in user


def test_alternatives_are_passed_on():
    moment = {"move_number": 3, "side": "white", "san": "h3", "category": "mistake", "phase": "opening",
              "fen_before": BACK_RANK_FEN, "best_move": "Rd8#", "alternatives": ["Rd7"], "findings": []}
    assert "Other moves Stockfish rates about as good: Rd7" in review.moment_facts(moment)


def test_a_learners_question_is_passed_on_with_the_same_rules(fork_moment):
    facts = review.moment_facts(fork_moment)
    user = build_game_moment_messages(facts, "beginner", "Why not Kd1?")[-1]["content"]
    assert "The student asks: Why not Kd1?" in user and "Answer using only the facts" in user


# --- checking what Qwen says --------------------------------------------------------------------
def test_praising_a_blunder_is_caught(fork_moment):
    assert review.conflicts("Kd2 is a great move that keeps everything safe.", fork_moment)


def test_invented_board_claims_are_caught(fork_moment):
    assert review.conflicts("After Kd2 the black rook on h8 wins the game.", fork_moment)


def test_a_faithful_explanation_passes(fork_moment):
    text = ("Kd2 was a blunder. Nc7+ was a knight fork: the knight on c7 attacks the king on e8 and the "
            "queen on a8, so after the king moves you win the queen. Next time, look for knight checks.")
    assert review.conflicts(text, fork_moment) == []


def test_stream_uses_qwen_when_it_agrees_with_the_facts(monkeypatch, qwen_on, fork_moment):  # noqa: F811
    good = "Kd2 was a blunder. Nc7+ forks the king and the queen. Next time, look for knight checks."
    sent = {}

    def fake_stream(method, url, json, headers, timeout):
        sent["messages"] = json["messages"]
        return FakeStream(sse(good))

    monkeypatch.setattr(qwen_mod.httpx, "stream", fake_stream)
    facts = review.moment_facts(fork_moment)
    events = list(stream_events(lambda: build_game_moment_messages(facts), lambda: review.fallback_why(fork_moment),
                                validate=lambda text: review.conflicts(text, fork_moment)))
    assert events[-1]["teacher"] == "qwen" and events[-1]["text"].strip() == good
    assert "Stockfish's best move: Nc7+" in sent["messages"][-1]["content"]


def test_stream_replaces_a_contradicting_reply_with_the_verified_fallback(monkeypatch, qwen_on,  # noqa: F811
                                                                         fork_moment):
    monkeypatch.setattr(qwen_mod.httpx, "stream",
                        lambda *a, **k: FakeStream(sse("Kd2 is an excellent move, well played!")))
    facts = review.moment_facts(fork_moment)
    events = list(stream_events(lambda: build_game_moment_messages(facts), lambda: review.fallback_why(fork_moment),
                                validate=lambda text: review.conflicts(text, fork_moment)))
    done = events[-1]
    assert done["text"] == review.fallback_why(fork_moment)
    assert "excellent" not in done["text"]


def test_without_qwen_the_template_explanation_is_used(fork_moment):
    events = list(stream_events(lambda: [], lambda: review.fallback_why(fork_moment)))
    assert events[-1] == {"type": "done", "teacher": "fallback", "text": review.fallback_why(fork_moment)}


def test_mate_moments_explain_the_mate():
    game = import_one(chesscom_pgn(["h3"], fen=BACK_RANK_FEN, game_no=6))
    moment = analyze(game, ScriptedEngine())["moments"][0]
    card = review.review_card(moment)
    assert "checkmate" in card["headline"].lower()
    assert "Rd8#" in card["why"]
