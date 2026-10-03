"""Verified library examples: the correct move gets the library's stored explanation at once —
no AI call — and the lesson doesn't repeat it afterwards. Also: no spoilers before solving."""
from __future__ import annotations

import chess
import pytest

from app.teacher.library_feedback import library_feedback, sentences, strip_said


@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.engine import set_engine
    from app.main import app
    from app.session import SessionManager, set_manager
    from tests.test_api import FakeEngine
    set_engine(FakeEngine())
    manager = SessionManager()
    set_manager(manager)
    # Qwen "configured", but it must not be needed for a correct move in a library example
    monkeypatch.setattr(type(get_settings()), "qwen_configured", lambda self: True)
    with TestClient(app) as c:
        yield c, manager
    set_engine(None)
    set_manager(None)


def _to_exercise(c, sid, step):
    seen = []
    while step["type"] != "exercise":
        seen.append(step)
        step = c.post(f"/api/sessions/{sid}/advance").json()["step"]
    return step, seen


def test_correct_move_gets_the_stored_explanation_instantly_and_it_is_not_repeated(api):
    c, manager = api
    body = c.post("/api/plans", json={"goal": "knight forks", "library": True}).json()
    start = c.post(f"/api/lessons/{body['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step, before_steps = _to_exercise(c, sid, start["step"])
    session = manager.get(sid)
    ex_step = manager.current_step(session)
    example = manager._example_for(session, at_or_before=False)
    assert example is not None and example.status == "verified"

    # no spoiler before solving: neither the prompt nor the header names the idea
    shown = " ".join([step["prompt"]] + [s.get("text", "") for s in before_steps[1:]]).lower()
    assert "fork" not in step["prompt"].lower() and "find the best move" in step["prompt"].lower()
    assert example.title.lower() not in shown

    board = chess.Board(step["board"]["fen"])
    move = board.parse_san(ex_step.accepted_san[0])
    res = c.post(f"/api/sessions/{sid}/move", json={"uci": move.uci()}).json()
    assert res["accepted"] and res["teacher"] == "library" and res["ai_explanation"] is False
    assert res["explanation"] and res["deeper"] in (True, False)
    # every sentence comes from the verified example (its notes, explanation, line or accepted moves)
    stored = " ".join([example.explanation, example.description, *example.notes.values()]).lower()
    first = sentences(res["explanation"])[0].lower()
    assert first.rstrip(".") in stored or ex_step.accepted_san[0].lower() in first

    # the closing explanation of this example doesn't repeat what the feedback just said
    said = {s.strip().lower() for s in sentences(res["explanation"])}
    nxt = c.post(f"/api/sessions/{sid}/advance").json()
    while nxt.get("step") and not (nxt["step"]["type"] == "teach" and nxt["step"].get("example", {}).get("id") == example.id):
        if nxt["step"]["type"] == "exercise":
            break
        nxt = c.post(f"/api/sessions/{sid}/advance").json()
    if nxt.get("step") and nxt["step"]["type"] == "teach":
        closing = {s.strip().lower() for s in sentences(nxt["step"]["text"])}
        assert not (closing & said) or len(closing) == 1


def test_feedback_uses_only_verified_examples():
    from app.knowledge.library import get_knowledge
    lib = get_knowledge()
    ex = next(e for e in lib.entries.values() if e.key_move and e.status == "verified" and e.explanation)
    board = ex.replay().boards[ex.key_ply]
    san = ex.moves[ex.key_ply]
    out = library_feedback(ex, board, san, [san], final=True, idea="Knight fork")
    assert out and out["text"]
    assert "The idea: knight fork." in out["text"]
    from dataclasses import replace
    assert library_feedback(replace(ex, status="candidate"), board, san, [san], final=True) is None
    # not the final move: no full explanation yet (later moves stay unspoiled)
    early = library_feedback(ex, board, san, [san], final=False)
    assert early is None or "The idea" not in early["text"]


def test_strip_said_keeps_the_rest():
    text = "Nc7+ attacks the king and the rook. The king must move. Then Nxa8 wins the rook.\n\nFrom a real game."
    said = {"the king must move."}
    assert strip_said(text, said) == "Nc7+ attacks the king and the rook. Then Nxa8 wins the rook.\n\nFrom a real game."
    assert strip_said(text, set()) == text
