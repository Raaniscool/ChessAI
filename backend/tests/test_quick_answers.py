"""Instant "what is …?" answers from the library: no AI call, no lesson plan."""
import pytest
from fastapi.testclient import TestClient

from app.knowledge.answers import is_definition_question, quick_answer
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge


@pytest.mark.parametrize("q", ["What is a fork?", "what's a zwischenzug", "What does zugzwang mean?",
                               "define smothered mate", "What are skewers", "ok, what is en passant?"])
def test_definition_questions(q):
    assert is_definition_question(q)


@pytest.mark.parametrize("q", ["What is the best move here?", "why is a pin good", "I want to learn forks",
                               "what should I play in this position?", "How do I checkmate with a rook?",
                               "what is " + "very " * 30 + "long"])
def test_not_definition_questions(q):
    assert not is_definition_question(q)


def test_library_concepts_answer_with_their_verified_summary():
    lib = get_knowledge()
    a = quick_answer("What is a fork?", lib, get_glossary())
    assert a["source"] == "library" and a["verified"] and a["concept"] == "fork"
    assert a["text"] == lib.concepts["fork"].summary and a["examples"] > 0
    assert a["lesson_goal"] == "I want to learn fork"
    # the most specific concept wins
    assert quick_answer("define smothered mate", lib, get_glossary())["concept"] == "smothered_mate"


def test_glossary_terms_are_labelled_unverified():
    a = quick_answer("What does zugzwang mean?", get_knowledge(), get_glossary())
    assert a["source"] == "glossary" and a["verified"] is False and a["examples"] == 0


def test_unknown_terms_get_no_made_up_answer():
    assert quick_answer("what is love", get_knowledge(), get_glossary()) is None


def test_answer_endpoint_is_instant_and_uses_no_teacher(monkeypatch):
    import time
    from app import teacher as teacher_mod
    from app.main import app
    monkeypatch.setattr(teacher_mod, "get_teacher", lambda: (_ for _ in ()).throw(AssertionError("no AI call")))
    with TestClient(app) as c:
        t0 = time.monotonic()
        body = c.post("/api/knowledge/answer", json={"message": "What's a pin?"}).json()
        assert time.monotonic() - t0 < 1.0
        assert body["answer"]["term"] == "Pin"
        assert c.post("/api/knowledge/answer", json={"message": "why did I lose?"}).json() == {"answer": None}
