"""Instructional importance: explanation length, AI use and read-aloud follow what matters."""
import chess
import pytest

from app.engine import Classification, MoveFeedback, Score
from app.teacher import FallbackTeacher, LessonContext
from app.teacher.importance import (CRITICAL, IMPORTANT, OBVIOUS, SUPPORTING, budget, classify, read_aloud,
                                    wants_ai)
from app.teacher.prompts import build_move_feedback_messages

FORK = "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"  # Nc7+ forks king and queen


def fb(fen, uci, category=Classification.EXCELLENT, best=None, loss=0, notes=None, before=30, after=30):
    board = chess.Board(fen)
    move = chess.Move.from_uci(uci)
    san = board.san(move)
    after_board = board.copy()
    after_board.push(move)
    best_uci = best or uci
    best_san = board.san(chess.Move.from_uci(best_uci))
    return MoveFeedback(fen_before=fen, fen_after=after_board.fen(), user_move_uci=uci, user_move_san=san,
                        category=category, loss_cp=loss, best_move_uci=best_uci, best_move_san=best_san,
                        eval_before=Score("cp", before), eval_after=Score("cp", after),
                        best_pv_san=[best_san, "Kd7", "Nxa8", "Kc6", "Nb6"], reply_pv_san=[], depth=14,
                        notes=list(notes or []))


def ctx(**kw):
    base = dict(course_title="c", lesson_title="Forks", concepts=["Knight fork"])
    base.update(kw)
    return LessonContext(**base)


# ------------------------------------------------------------------ classification
def test_errors_and_missed_mates_are_critical():
    b = chess.Board(FORK)
    assert classify(fb(FORK, "b5d6", Classification.BLUNDER, best="b5c7", loss=900), accepted=False,
                    board=b)[0] == CRITICAL
    assert classify(fb(FORK, "b5d6", Classification.GOOD, best="b5c7", notes=["missed_mate"]), accepted=False,
                    board=b)[0] == CRITICAL
    assert classify(fb(FORK, "b5d6", Classification.INACCURATE, best="b5c7", loss=60), accepted=None,
                    board=b)[0] == IMPORTANT


def test_a_sound_move_that_isnt_the_exercise_is_important_not_critical():
    level, why = classify(fb(FORK, "b5d6", Classification.GOOD, best="b5c7"), accepted=False,
                          board=chess.Board(FORK))
    assert level == IMPORTANT and "not the exercise's move" in why


def test_the_key_tactical_move_is_critical_quiet_key_move_important():
    level, why = classify(fb(FORK, "b5c7"), accepted=True, key_move=True, board=chess.Board(FORK))
    assert level == CRITICAL and "check" in why
    quiet = "k7/8/1K6/8/8/8/8/7R w - - 0 1"
    assert classify(fb(quiet, "b6c7"), accepted=True, key_move=True, board=chess.Board(quiet))[0] == IMPORTANT


def test_follow_ups_forced_moves_and_finishing_mates_are_minor():
    only = "k7/8/1K6/8/8/8/8/1R6 b - - 0 1"  # Black's king has one legal move
    board = chess.Board(only)
    assert board.legal_moves.count() == 1
    move = next(iter(board.legal_moves)).uci()
    assert classify(fb(only, move), accepted=True, board=board)[0] == OBVIOUS
    mate = "k7/2K5/8/8/8/8/8/7R w - - 0 1"  # Ra1# finishes a mate that was set up
    assert classify(fb(mate, "h1a1"), accepted=True, board=chess.Board(mate))[0] == SUPPORTING
    assert classify(fb(FORK, "b5c7"), accepted=True, key_move=False, board=chess.Board(FORK))[0] == SUPPORTING


def test_budget_ai_and_read_aloud_follow_importance():
    assert budget(CRITICAL) > budget(IMPORTANT) > budget(SUPPORTING) > budget(OBVIOUS)
    assert budget(CRITICAL, "brief") < budget(CRITICAL) < budget(CRITICAL, "detailed")
    assert wants_ai(CRITICAL) and wants_ai(IMPORTANT) and not wants_ai(SUPPORTING) and not wants_ai(OBVIOUS)
    assert wants_ai(SUPPORTING, "detailed") and not wants_ai(OBVIOUS, "detailed")
    assert read_aloud(CRITICAL) and not read_aloud(SUPPORTING)


# ------------------------------------------------------------------ fallback wording
def test_obvious_and_supporting_moves_get_a_few_words():
    t = FallbackTeacher()
    obvious = t.explain_move(fb(FORK, "b5c7"), ctx(move_accepted=True, importance=OBVIOUS))
    supporting = t.explain_move(fb(FORK, "b5c7"), ctx(move_accepted=True, importance=SUPPORTING))
    critical = t.explain_move(fb(FORK, "b5c7"), ctx(move_accepted=True, importance=CRITICAL, concepts=["tactics"]))
    assert len(obvious.split()) <= 4 and len(supporting.split()) <= 7
    assert len(critical.split()) > len(supporting.split())
    assert "Engine" not in obvious + supporting


def test_beginners_hear_what_a_mistake_costs_not_engine_numbers():
    t = FallbackTeacher()
    feedback = fb(FORK, "b5d6", Classification.BLUNDER, best="b5c7", loss=600, before=850, after=250)
    beginner = t.explain_move(feedback, ctx(move_accepted=False, importance=CRITICAL, level="beginner"))
    advanced = t.explain_move(feedback, ctx(move_accepted=False, importance=CRITICAL, level="advanced"))
    brief = t.explain_move(feedback, ctx(move_accepted=False, importance=CRITICAL, level="advanced", style="brief"))
    assert "blunder" in beginner.lower() and "Nc7+" in beginner and "about a piece" in beginner
    assert "evaluation" not in beginner.lower() and "Main line" not in beginner
    assert "evaluation goes from" in advanced and "Main line: Nc7+ Kd7 Nxa8 Kc6." in advanced
    assert "Main line" not in brief and len(brief) < len(advanced)


def test_wording_varies_between_positions_but_is_stable_for_one():
    t = FallbackTeacher()
    texts = set()
    for n in range(1, 9):
        f = fb(FORK, "b5c7")
        f.fen_before = FORK.replace(" 0 1", f" 0 {n}")  # a different position (move number) each time
        texts.add(t.explain_move(f, ctx(move_accepted=True, importance=CRITICAL)))
    assert len(texts) >= 2
    one = fb(FORK, "b5c7")
    assert t.explain_move(one, ctx(move_accepted=True, importance=CRITICAL)) == \
        t.explain_move(one, ctx(move_accepted=True, importance=CRITICAL))


def test_offline_chat_doesnt_show_configuration_names():
    reply = FallbackTeacher().chat("hi", ctx(), [])
    assert "QWEN_" not in reply and "Qwen" in reply


# ------------------------------------------------------------------ prompts
def _user(feedback, **kw):
    return build_move_feedback_messages(feedback, ctx(**kw))[1]["content"]


def test_prompt_length_and_level_follow_importance_and_learner():
    f = fb(FORK, "b5c7")
    crit = _user(f, move_accepted=True, importance=CRITICAL, level="advanced")
    sup = _user(f, move_accepted=True, importance=SUPPORTING, level="advanced")
    assert "2-4 sentences" in crit and f"under {budget(CRITICAL)} words" in crit
    assert "1 short sentence" in sup and f"under {budget(SUPPORTING)} words" in sup
    assert "Level: advanced" in crit and "no evaluation numbers" not in crit
    beginner = _user(f, move_accepted=True, importance=CRITICAL, learner_note="Learner: about 600 rating.")
    assert "no evaluation numbers" in beginner and "Learner: about 600 rating." in beginner


# ------------------------------------------------------------------ over HTTP
@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.engine import set_engine
    from app.main import app
    from app.session import SessionManager, set_manager
    from tests.test_api import FakeEngine
    set_engine(FakeEngine())
    set_manager(SessionManager())
    monkeypatch.setattr(type(get_settings()), "qwen_configured", lambda self: True)
    with TestClient(app) as c:
        yield c
    set_engine(None)
    set_manager(None)


def test_move_results_carry_importance_and_skip_ai_for_minor_moves(api):
    body = api.post("/api/plans", json={"goal": "checkmate patterns", "library": True}).json()
    start = api.post(f"/api/lessons/{body['first_lesson_id']}/start").json()
    sid, step = start["session_id"], start["step"]
    while step["type"] != "exercise":
        step = api.post(f"/api/sessions/{sid}/advance").json()["step"]
    board = chess.Board(step["board"]["fen"])
    results = []
    for move in list(board.legal_moves)[:6]:
        res = api.post(f"/api/sessions/{sid}/move", json={"uci": move.uci()}).json()
        results.append(res)
        if res["accepted"]:
            break
    wrong = [r for r in results if not r["accepted"]]
    assert wrong, "expected at least one move that doesn't solve the exercise"
    # FakeEngine calls every move excellent: a sound move that isn't the exercise's
    assert wrong[0]["importance"] == IMPORTANT and wrong[0]["ai_explanation"] is True and wrong[0]["read_aloud"]
    for r in results:
        assert r["ai_explanation"] == (r["importance"] in (CRITICAL, IMPORTANT))
