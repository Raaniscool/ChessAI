"""Prompts: engine facts only, from the student's side, and small (CPU prompt reading is the wait)."""
import pytest

from app.engine import MoveFeedback
from app.engine.classification import Classification, Score
from app.teacher.base import LessonContext
from app.teacher.prompts import SYSTEM_PROMPT, build_chat_messages, build_move_feedback_messages
from app.teacher.qwen import QwenTeacher

from tests.test_qwen import make_settings

ITALIAN_W = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
SICILIAN_B = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
CTX = LessonContext("Italian Game", "First moves", ["development", "center"],
                    "🎯 Exercise: Develop your bishop to its best square.", ["Bc4"])


def feedback(fen=ITALIAN_W, san="Bc4", uci="f1c4", best_san="d4", best_uci="d2d4",
             category=Classification.EXCELLENT, before=28, after=18, notes=None):
    return MoveFeedback(
        fen_before=fen, fen_after=fen, user_move_uci=uci, user_move_san=san, category=category,
        loss_cp=before - after, best_move_uci=best_uci, best_move_san=best_san,
        eval_before=Score("cp", before), eval_after=Score("cp", after),
        best_pv_san=["d4", "exd4", "Nxd4", "Nf6", "Nxc6", "dxc6"],
        reply_pv_san=["Nf6", "d3", "Be7", "Nc3", "d6", "a3"], notes=notes or [], depth=14)


def user_text(fb, ctx=CTX):
    msgs = build_move_feedback_messages(fb, ctx)
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    return msgs[1]["content"]


def test_move_prompt_is_compact_and_has_no_fen():
    text = user_text(feedback())
    assert ITALIAN_W.split()[0] not in text and "fen" not in text.lower()
    assert "d4 exd4 Nxd4 Nf6" in text and "Nxc6" not in text      # lines trimmed to 4 plies
    assert "depth" not in text and "{" not in text                 # no JSON / engine internals
    assert "..." not in text and ".." not in text                  # goal punctuation tidied
    # Regression guard: every prompt token delays the first word on a CPU (was ~1,500 chars).
    assert len(text) < 700, len(text)
    assert len(SYSTEM_PROMPT) < 800


def test_evaluations_are_from_the_students_side():
    # Black student; engine scores are White-POV: +150 for White = -1.50 for Black.
    text = user_text(feedback(fen=SICILIAN_B, san="a6", uci="a7a6", best_san="c5", best_uci="c7c5",
                              category=Classification.MISTAKE, before=30, after=150))
    assert "Student (Black) played: a6 - verdict: mistake" in text
    assert "-0.30 with best play, -1.50 after their move" in text


def test_good_move_is_not_told_it_missed_something():
    text = user_text(feedback(best_san="Bc4", best_uci="f1c4"))
    assert "Engine's best move: the same move" in text
    assert "misses" not in text and "Engine line after the best move" not in text
    assert "planned move" not in text  # student played the lesson move


def test_bad_move_asks_what_it_misses_and_mentions_the_plan():
    text = user_text(feedback(san="h3", uci="h2h3", category=Classification.INACCURATE, before=28, after=-30,
                              notes=["missed a tactic"]))
    assert "what the move allows or misses" in text
    assert "The lesson's planned move is Bc4" in text
    assert "Notes: missed a tactic" in text


def test_system_prompt_is_identical_across_requests_for_prompt_caching():
    a = build_move_feedback_messages(feedback(), CTX)[0]
    b = build_move_feedback_messages(feedback(fen=SICILIAN_B), CTX)[0]
    c = build_chat_messages("why?", CTX, [])[0]
    assert a == b == c


def test_chat_sends_only_recent_history():
    transcript = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    msgs = build_chat_messages("latest?", CTX, transcript)
    contents = [m["content"] for m in msgs]
    assert "m13" not in contents and "m14" in contents and "m19" in contents
    assert contents[-1] == "latest?"


@pytest.mark.parametrize("url, thinking, expected", [
    ("http://localhost:11434/v1", "auto", "none"),   # Ollama: reliable think-off switch
    ("http://localhost:11434/v1", "off", "none"),
    ("http://localhost:11434/v1", "on", None),
    ("http://my-ollama-box:8080/v1", "auto", "none"),
    ("http://localhost:1234/v1", "auto", None),      # LM Studio etc.: don't send unknown fields
])
def test_reasoning_effort_only_for_ollama(url, thinking, expected):
    s = make_settings("qwen3:4b-instruct", thinking)
    s.qwen_base_url = url
    payload = QwenTeacher(s).build_payload([{"role": "user", "content": "hi"}])
    assert payload.get("reasoning_effort") == expected
