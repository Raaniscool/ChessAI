"""The engine verdict, the lesson result and the explanation must never contradict.

Reported bug: the tutor said "Blunder" and then Qwen said it was the right move.
"""
import pytest
from fastapi.testclient import TestClient

from app.engine import Classification, MoveFeedback, Score, set_engine
from app.session import SessionManager, agree_with_lesson, set_manager
from app.teacher import FallbackTeacher
from app.teacher.base import LessonContext
from app.teacher.prompts import build_move_feedback_messages

from tests.test_api import FakeEngine
from tests.session_helpers import confirm_advance

ITALIAN_W = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"


def feedback(san="Bc4", uci="f1c4", best_san="d4", best_uci="d2d4",
             category=Classification.BLUNDER, before=900, after=450, notes=None):
    return MoveFeedback(
        fen_before=ITALIAN_W, fen_after=ITALIAN_W, user_move_uci=uci, user_move_san=san,
        category=category, loss_cp=max(0, before - after), best_move_uci=best_uci, best_move_san=best_san,
        eval_before=Score("cp", before), eval_after=Score("cp", after),
        best_pv_san=["d4", "exd4"], reply_pv_san=["Nf6"], notes=list(notes or []), depth=14)


def ctx(accepted_moves=("Bc4",), move_accepted=None):
    return LessonContext("Italian Game", "First moves", ["development"],
                         "Develop your bishop.", list(accepted_moves), move_accepted=move_accepted)


def prompt(fb, context):
    return build_move_feedback_messages(fb, context)[1]["content"]


# --- a move the lesson accepts is never shown as Mistake/Blunder ---

@pytest.mark.parametrize("category", [Classification.INACCURATE, Classification.MISTAKE, Classification.BLUNDER])
def test_accepted_move_is_capped_at_good_with_the_engine_preference_noted(category):
    fb = agree_with_lesson(feedback(category=category, notes=["missed_mate", "decided_position"]))
    assert fb.category == Classification.GOOD
    assert fb.notes == ["engine_prefers"]  # a verified move did not "miss" the win
    assert fb.loss_cp == 450 and fb.best_move_san == "d4"  # engine facts untouched


@pytest.mark.parametrize("category", [Classification.EXCELLENT, Classification.GOOD])
def test_good_verdicts_on_accepted_moves_are_unchanged(category):
    fb = feedback(category=category, notes=["x"])
    assert agree_with_lesson(fb) is fb


def test_fallback_says_correct_and_names_the_stronger_engine_move():
    fb = agree_with_lesson(feedback())
    text = FallbackTeacher().explain_move(fb, ctx(move_accepted=True))
    assert text.startswith("Correct — Bc4 solves the exercise")
    assert "d4" in text and "blunder" not in text.lower() and "mistake" not in text.lower()


def test_prompt_states_the_lesson_result_explicitly():
    solved = prompt(agree_with_lesson(feedback()), ctx(move_accepted=True))
    assert "Lesson result: CORRECT" in solved
    assert "even stronger" in solved and "Do not call it a mistake" in solved
    assert "engine_prefers" not in solved  # internal flag, not a fact for the model

    wrong = prompt(feedback(san="h3", uci="h2h3"), ctx(move_accepted=False))
    assert "Lesson result: NOT SOLVED" in wrong
    assert "Never call the student's move good, right, correct or best" in wrong

    playable = prompt(feedback(san="d4", uci="d2d4", category=Classification.EXCELLENT, before=30, after=30),
                      ctx(move_accepted=False))
    assert "Do not say the student solved the exercise" in playable


def test_prompt_without_lesson_result_has_no_lesson_line():
    assert "Lesson result" not in prompt(feedback(category=Classification.GOOD), ctx())


# --- over HTTP: what the learner actually sees ---

class HarshEngine(FakeEngine):
    """Grades every move a blunder that 'missed a mate' — the live grader disagreeing
    with the lesson's verified solution."""

    def evaluate_move(self, board, move, depth=None):
        fb = super().evaluate_move(board, move, depth)
        fb.category, fb.loss_cp, fb.notes = Classification.BLUNDER, 900, ["missed_mate"]
        fb.best_move_uci, fb.best_move_san = "d2d4", "d4"
        return fb


@pytest.fixture()
def client():
    from app.main import app as fastapi_app
    set_engine(HarshEngine())
    set_manager(SessionManager())
    with TestClient(fastapi_app) as c:
        yield c
    set_engine(None)
    set_manager(None)


def _to_first_exercise(client):
    sid = client.post("/api/lessons/italian_01/start").json()["session_id"]
    for _ in range(2):  # teach → demonstrate → exercise (White to play Bc4)
        confirm_advance(client, sid)
    return sid


def test_accepted_move_never_shows_blunder_next_to_correct(client):
    sid = _to_first_exercise(client)
    body = client.post(f"/api/sessions/{sid}/move", json={"uci": "f1c4"}).json()
    assert body["accepted"] is True and body["continue_text"]
    assert body["feedback"]["category"] == "good"
    assert "missed_mate" not in body["feedback"]["notes"]
    assert body["explanation"].startswith("Correct")
    events = [e for e in client.post(f"/api/sessions/{sid}/explain").iter_lines() if e]
    assert "slip away" not in "".join(events)  # no "you let the mate slip" on the lesson move


def test_rejected_move_keeps_the_engine_verdict(client):
    sid = _to_first_exercise(client)
    body = client.post(f"/api/sessions/{sid}/move", json={"uci": "h2h3"}).json()
    assert body["accepted"] is False
    assert body["feedback"]["category"] == "blunder"
    assert "blunder" in body["explanation"].lower()


# --- the deterministic backstop on Qwen's move explanation ---

from app.teacher.consistency import verdict_conflicts  # noqa: E402

BLUNDER_H3 = feedback(san="h3", uci="h2h3", category=Classification.BLUNDER)
GOOD_BC4 = feedback(category=Classification.EXCELLENT, best_san="Bc4", best_uci="f1c4", before=30, after=30)
PLAYABLE_D4 = feedback(san="d4", uci="d2d4", category=Classification.GOOD, before=30, after=10)


@pytest.mark.parametrize("text", [
    "That's the right move! h3 stops any pin on your knight.",
    "Great move! You keep your king safe.",
    "Well done. This keeps the position under control.",
    "h3 is a solid choice that prevents Bg4.",
    "Your move is the best way to fight for the center.",
    "You found it: h3 is exactly the idea of the lesson.",
])
def test_praise_for_a_blunder_is_caught(text):
    assert verdict_conflicts(text, BLUNDER_H3, move_accepted=False)


@pytest.mark.parametrize("text", [
    "h3 is too slow: it allows Ng5 and the attack on f7. The best move was d4, fighting for the center.",
    "Your move is not the right idea here, because it ignores development.",
    "You missed the best move, d4, which opens the center while you are ahead in development.",
    "Instead of h3, the engine prefers d4. Development and the center come first.",
    "This is a blunder: after h3 White loses the initiative. d4 was stronger.",
])
def test_honest_criticism_of_a_blunder_passes(text):
    assert verdict_conflicts(text, BLUNDER_H3, move_accepted=False) == []


@pytest.mark.parametrize("text", [
    "That's a mistake: Bc4 leaves the e4 pawn loose.",
    "Your move is a blunder because Black wins a piece.",
    "You made an error by moving the bishop twice.",
])
def test_calling_the_accepted_best_move_bad_is_caught(text):
    assert verdict_conflicts(text, GOOD_BC4, move_accepted=True)


@pytest.mark.parametrize("text", [
    "Excellent! Bc4 aims at f7, the weakest square near Black's king.",
    "Bc4 is the right move: it develops a piece and eyes f7. You avoid the common mistake of moving the queen early.",
    "You punished Black's blunder by developing with tempo.",
    "Good job, this is exactly what strong players do in the Italian.",
])
def test_praise_for_the_accepted_move_passes(text):
    assert verdict_conflicts(text, GOOD_BC4, move_accepted=True) == []


def test_playable_but_not_the_lesson_move_may_be_good_but_not_correct():
    assert verdict_conflicts("d4 is a good move that grabs the center, but the lesson wants Bc4.",
                             PLAYABLE_D4, move_accepted=False) == []
    assert verdict_conflicts("d4 is the correct move here!", PLAYABLE_D4, move_accepted=False)
    assert verdict_conflicts("You found it! d4 takes the center.", PLAYABLE_D4, move_accepted=False)


def test_checkmate_explained_as_right_is_consistent():
    """The original report: a mating move was graded Blunder and Qwen (rightly) praised it.
    Now the verdict is Excellent, so the same praise is consistent."""
    mate = feedback(san="Rd8#", uci="d1d8", category=Classification.EXCELLENT, best_san="Rd8#",
                    best_uci="d1d8", before=0, after=0)
    assert verdict_conflicts("Rd8# is the right move: it is checkmate on the back rank!",
                             mate, move_accepted=True) == []


class _Stream:
    """Stand-in Qwen teacher streaming a fixed reply."""

    def __init__(self, reply):
        self.reply = reply

    def stream(self, messages, **_):
        yield from self.reply


def test_contradicting_qwen_reply_is_replaced_with_the_fallback(client, monkeypatch):
    import json
    import app.teacher as teacher_mod
    from app.teacher.qwen import QwenTeacher

    fake = QwenTeacher.__new__(QwenTeacher)
    fake.stream = _Stream(["That's the right ", "move! h3 is great."]).stream
    monkeypatch.setattr(teacher_mod, "get_teacher", lambda: fake)
    sid = _to_first_exercise(client)
    assert client.post(f"/api/sessions/{sid}/move", json={"uci": "h2h3"}).json()["accepted"] is False
    events = [json.loads(e) for e in client.post(f"/api/sessions/{sid}/explain").iter_lines() if e]
    done = events[-1]
    assert done["type"] == "done" and done["teacher"] == "fallback" and done.get("corrected") is True
    assert "right move" not in done["text"] and "blunder" in done["text"].lower()
    assert any(e["type"] == "replace" for e in events)


def test_consistent_qwen_reply_is_kept(client, monkeypatch):
    import json
    import app.teacher as teacher_mod
    from app.teacher.qwen import QwenTeacher

    reply = "h3 is too slow: it lets Black develop freely. The engine prefers d4 to open the center."
    fake = QwenTeacher.__new__(QwenTeacher)
    fake.stream = _Stream([reply]).stream
    monkeypatch.setattr(teacher_mod, "get_teacher", lambda: fake)
    sid = _to_first_exercise(client)
    client.post(f"/api/sessions/{sid}/move", json={"uci": "h2h3"})
    events = [json.loads(e) for e in client.post(f"/api/sessions/{sid}/explain").iter_lines() if e]
    assert events[-1] == {"type": "done", "teacher": "qwen", "text": reply}
