"""Instant factual answers from local libraries: no AI call or lesson plan."""
import pytest
from fastapi.testclient import TestClient

from app.knowledge.answers import classify_question, is_definition_question, quick_answer
from app.knowledge.glossary import get_glossary
from app.knowledge.library import get_knowledge


@pytest.mark.parametrize("q", [
    "What is a fork?", "what's a zwischenzug", "What does zugzwang mean?",
    "define smothered mate", "What are skewers", "ok, what is en passant?",
    "Explain pins", "Can you explain a relative pin?", "What is the key idea behind a fork?",
    "Explain a pin to me.", "Can you explain pins?", "What's a pin again?", "I forgot what a pin is.",
    "What is a fork?", "Explain forks.",
    "What misconception do players have about a pin?", "What do beginners get wrong about castling?",
])
def test_definition_questions(q):
    assert is_definition_question(q)


@pytest.mark.parametrize("q", [
    "What is the best move here?", "why is a pin good", "I want to learn forks",
    "what should I play in this position?", "How do I checkmate with a rook?",
    "what is " + "very " * 30 + "long", "Compare absolute and relative pins",
    "How can I use pins in my games?", "Explain why a pin is good here",
    "What is a pin in this position?", "What did I blunder last game?",
    "Tell me more about pins in depth",
])
def test_not_definition_questions(q):
    assert not is_definition_question(q)


def test_basic_explanation_answer_includes_structured_levels_and_related_examples():
    lib = get_knowledge()
    answer = quick_answer("What is a fork?", lib, get_glossary())
    assert answer["source"] == "basic_explanations" and answer["curated"]
    assert answer["verified"] is True and answer["id"] == "fork" and answer["concept"] == "fork"
    assert answer["text"] == answer["levels"]["beginner"]
    assert set(answer["levels"]) == {"beginner", "intermediate", "advanced"}
    assert answer["key_idea"] and answer["common_misconception"]
    assert answer["examples"] == lib.count_for("fork") and answer["examples"] > 0
    assert answer["lesson_goal"] == "I want to learn fork"
    assert answer["related_verified_examples"]
    assert all(lib.get(eid) is not None and lib.get(eid).status == "verified"
               for eid in answer["related_verified_examples"])
    # The most specific term wins.
    assert quick_answer("define smothered mate", lib, get_glossary())["concept"] == "smothered_mate"


@pytest.mark.parametrize(("question", "explanation_id"), [
    ("What is a pin?", "pin"),
    ("Explain a pin to me.", "pin"),
    ("Can you explain pins?", "pin"),
    ("What's a pin again?", "pin"),
    ("I forgot what a pin is.", "pin"),
    ("What is a fork?", "fork"),
    ("Explain forks.", "fork"),
    ("Remind me what castling is.", "castling"),
    ("Can you go over skewers again?", "skewer"),
    ("What does stalemate mean?", "stalemate"),
])
def test_natural_definition_variations_resolve_the_named_basic_concept(question, explanation_id):
    library, glossary = get_knowledge(), get_glossary()
    intent = classify_question(question, library, glossary)
    answer = quick_answer(question, library, glossary)
    assert intent.kind == "basic_explanation"
    assert intent.source == "basic_explanations"
    assert intent.confidence >= 0.95
    assert answer["source"] == "basic_explanations"
    assert answer["id"] == explanation_id


def test_request_type_beats_a_related_keyword_match():
    library, glossary = get_knowledge(), get_glossary()
    routes = [
        ("Show me an example of a pin.", "example_request", "knowledge_library"),
        ("Show me a pin.", "example_request", "knowledge_library"),
        ("Show me a position where I can use a pin.", "example_request", "knowledge_library"),
        ("Why is this pin good?", "position_question", "position_tutor"),
        ("How do I use a pin against this knight?", "position_question", "position_tutor"),
        ("How do I play the Sicilian?", "opening_question", "opening_knowledge"),
        ("How do I respond to this Sicilian line?", "opening_question", "opening_knowledge"),
        ("What is the pin variation of the Sicilian?", "opening_question", "opening_knowledge"),
        ("What should I practice based on my games?", "personalized_coaching", "personalized_tutor"),
        ("Why do I keep missing pins?", "personalized_coaching", "personalized_tutor"),
        ("I understand pins but I keep blundering them.", "personalized_coaching", "personalized_tutor"),
    ]
    for question, kind, source in routes:
        intent = classify_question(question, library, glossary)
        assert (intent.kind, intent.source) == (kind, source), (question, intent)
        assert quick_answer(question, library, glossary) is None, question


def test_explain_a_pin_never_routes_to_the_sicilian_pin_variation():
    library, glossary = get_knowledge(), get_glossary()
    answer = quick_answer("Explain a pin.", library, glossary)
    assert answer["source"] == "basic_explanations"
    assert answer["id"] == "pin"
    assert answer["term"] == "Pin"


def test_genuinely_ambiguous_multi_concept_definition_stays_with_tutor():
    library, glossary = get_knowledge(), get_glossary()
    question = "What is a pin and a fork?"
    intent = classify_question(question, library, glossary)
    assert intent.kind == "ambiguous" and intent.source == "tutor"
    assert quick_answer(question, library, glossary) is None


def test_basic_explanation_accepts_an_explicit_level_or_detail():
    library, glossary = get_knowledge(), get_glossary()
    advanced = quick_answer("Could you explain a pin at an advanced level?", library, glossary)
    assert advanced["id"] == "pin" and advanced["level"] == "advanced"
    assert advanced["text"] == advanced["levels"]["advanced"]
    misconception = quick_answer("What misconception do players have about a pin?", library, glossary)
    assert misconception["detail"] == "common_misconception"
    assert misconception["text"] == misconception["common_misconception"]
    key_idea = quick_answer("What is the key idea behind a pin?", library, glossary)
    assert key_idea["detail"] == "key_idea" and key_idea["text"] == key_idea["key_idea"]


def test_legacy_glossary_definition_is_reused_but_remains_unengine_checked():
    answer = quick_answer("What does fianchetto mean?", get_knowledge(), get_glossary())
    assert answer["source"] == "basic_explanations" and answer["curated"] is True
    assert answer["verified"] is False and answer["examples"] == 0
    assert answer["text"] == get_glossary().terms["fianchetto"].definition


def test_a_glossary_idea_that_became_a_concept_keeps_verified_examples():
    """Zugzwang was a glossary term; it now has verified examples and a structured entry."""
    answer = quick_answer("What does zugzwang mean?", get_knowledge(), get_glossary())
    assert answer["source"] == "basic_explanations" and answer["verified"] is True
    assert answer["examples"] >= 3


def test_existing_knowledge_summary_fallback_remains_available():
    """Concepts not yet curated in the Basic Explanation Library retain the old answer path."""
    answer = quick_answer("What is mate in one?", get_knowledge(), get_glossary())
    assert answer["source"] == "library" and answer["verified"] is True
    assert answer["text"] == get_knowledge().concepts["mate_in_one"].summary


def test_unknown_terms_get_no_made_up_answer():
    assert quick_answer("what is love", get_knowledge(), get_glossary()) is None


def test_basic_answer_during_an_active_lesson_does_not_advance_or_replace_it():
    from app.main import app
    from app.session import SessionManager, set_manager

    manager = SessionManager()
    set_manager(manager)
    try:
        with TestClient(app) as client:
            start = client.post("/api/lessons/italian_01/start")
            assert start.status_code == 200
            sid = start.json()["session_id"]
            before = client.get(f"/api/sessions/{sid}").json()
            before_index = manager.get(sid).step_index
            answer = client.post("/api/knowledge/answer", json={"message": "Wait, what is a pin again?"})
            after = client.get(f"/api/sessions/{sid}").json()
            assert answer.json()["answer"]["id"] == "pin"
            assert manager.get(sid).step_index == before_index
            assert after["lesson_id"] == before["lesson_id"] == "italian_01"
            assert after["status"] == before["status"] == "active"
            assert after["step"] == before["step"]
    finally:
        set_manager(None)


def test_answer_endpoint_is_instant_and_uses_no_teacher(monkeypatch):
    import time
    from app import teacher as teacher_mod
    from app.main import app
    monkeypatch.setattr(teacher_mod, "get_teacher", lambda: (_ for _ in ()).throw(AssertionError("no AI call")))
    with TestClient(app) as client:
        t0 = time.monotonic()
        response = client.post("/api/knowledge/answer", json={"message": "What's a pin?"})
        assert time.monotonic() - t0 < 1.0
        answer = response.json()["answer"]
        assert answer["term"] == "Pin" and answer["source"] == "basic_explanations"
        assert answer["levels"]["advanced"]
        assert client.post("/api/knowledge/answer", json={"message": "why did I lose?"}).json() == {"answer": None}
