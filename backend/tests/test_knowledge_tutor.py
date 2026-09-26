"""C3: the verified Knowledge Library wired into the tutor, planner, lessons and chat.

Uses the real seed library (package data). Every test gets a fresh library instance
and a throwaway usage file, so status changes and "seen" history never leak.
"""
from __future__ import annotations

import json

import chess
import pytest
from fastapi.testclient import TestClient

from app.engine import set_engine
from app.knowledge.library import KnowledgeLibrary, set_knowledge
from app.knowledge.retrieval import (
    RetrievalRequest, lesson_request, resolve_concepts, retrieve, teaching_facts,
)
from app.knowledge.usage import UsageTracker, set_usage
from app.lessons import set_library
from app.lessons.schema import DemonstrateStep, ExerciseStep, TeachStep, parse_lesson
from app.planner import plan_for_goal
from app.planner.knowledge_lessons import create_knowledge_plan, example_steps, knowledge_lesson
from app.session import SessionManager, set_manager
from app.teacher import qwen as qwen_mod
from app.teacher.prompts import build_chat_messages, build_move_feedback_messages

from tests.test_api import FakeEngine
from tests.test_streaming import FakeStream, qwen_on, sse  # noqa: F401  (qwen_on is a fixture)

UNTRUSTED = ("candidate", "verifying", "needs_review", "rejected", "deprecated")


class CountingEngine(FakeEngine):
    """The API FakeEngine, counting how often the tutor asks it to judge a move."""

    def __init__(self):
        self.calls = 0

    def evaluate_move(self, board, move, depth=None):
        self.calls += 1
        return super().evaluate_move(board, move, depth)


@pytest.fixture()
def lib():
    library = KnowledgeLibrary(load_runtime=False)  # the shipped seed, fresh per test
    set_knowledge(library)
    yield library
    set_knowledge(None)


@pytest.fixture()
def usage(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.json")
    set_usage(tracker)
    yield tracker
    set_usage(None)


@pytest.fixture()
def engine():
    eng = CountingEngine()
    set_engine(eng)
    yield eng
    set_engine(None)


@pytest.fixture()
def client(lib, usage, engine):
    set_library(None)
    set_manager(SessionManager())
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        yield c
    set_manager(None)
    set_library(None)


def _all_ids(result) -> list[str]:
    return [e.id for e in result.examples]


# ---- retrieval: trust -------------------------------------------------------

def test_only_verified_entries_are_retrieved(lib, usage):
    forks = lib.examples_for("fork")
    assert len(forks) >= len(UNTRUSTED) + 1
    for example, status in zip(forks, UNTRUSTED):
        example.status = status  # e.g. an entry demoted by a reviewer
    demoted = {e.id for e in forks[:len(UNTRUSTED)]}
    result = retrieve(lib, RetrievalRequest(text="Teach me forks", count=5, practice_count=3), usage)
    assert result.found
    assert not demoted & set(_all_ids(result))
    assert all(e.status == "verified" for e in result.examples)
    for eid in demoted:
        assert lib.get(eid) is None  # the teacher can't be handed a demoted entry either


def test_untrusted_example_gives_the_teacher_no_facts(lib):
    example = lib.examples_for("back_rank_mate")[0]
    assert teaching_facts(example, lib)
    example.status = "needs_review"
    assert teaching_facts(example, lib) == []


def test_mates_are_reported_as_mates_not_pawns(lib):
    example = lib.get("mate_back_rank_basic")
    facts = "\n".join(teaching_facts(example, lib, board=example.replay().boards[0]))
    assert "forced mate" in facts and "+100.00" not in facts
    assert "Legal moves for White here:" in facts and "Rd8#" in facts.split("Legal moves")[1]


def test_personal_tier_is_never_handed_out(lib, usage):
    example = lib.examples_for("fork")[0]
    example.tier = "personal"
    ids = _all_ids(retrieve(lib, RetrievalRequest(text="forks", count=5, practice_count=3), usage))
    assert example.id not in ids


# ---- retrieval: concepts, aliases, filters -----------------------------------

@pytest.mark.parametrize("goal, concept", [
    ("Teach me forks", "fork"),
    ("I want to learn pins", "pin"),
    ("Teach me the Italian Game", "italian_game"),
    ("Show me checkmates", "checkmate"),
    ("Give me an endgame lesson", "endgames"),
    ("teach me the smothered mate", "smothered_mate"),
])
def test_concept_retrieval(lib, usage, goal, concept):
    result = retrieve(lib, RetrievalRequest(text=goal), usage)
    assert result.concepts[0] == concept
    tree = set(lib.descendants(concept))
    assert result.found
    assert all(tree & set(e.concepts) for e in result.examples)


@pytest.mark.parametrize("text, concept", [
    ("checkmates", "checkmate"),          # plural alias
    ("knight forks", "knight_fork"),      # the most specific concept wins
    ("an endgame lesson", "endgames"),
    ("mate in one", "mate_in_one"),
    ("the absolute pin", "absolute_pin"),
])
def test_aliases_resolve_through_the_concept_graph(lib, text, concept):
    concepts, confident = resolve_concepts(lib, text)
    assert concepts and concepts[0] == concept and confident


def test_every_alias_of_a_concept_with_examples_resolves(lib):
    for concept in lib.concepts.values():
        if not lib.count_for(concept.id):
            continue
        for alias in concept.aliases:
            found, _ = resolve_concepts(lib, alias)
            related = set(lib.descendants(concept.id)) | {concept.id}
            assert found, alias
            assert any(f in related or concept.id in lib.descendants(f) for f in found), (alias, found)


def test_broad_root_concept_with_extra_words_is_not_a_match(lib, usage):
    """'opening principles' is not a request for any opening: leave it to the catalog."""
    concepts, confident = resolve_concepts(lib, "teach me opening principles")
    assert concepts == ["openings"] and not confident
    assert not retrieve(lib, RetrievalRequest(text="teach me opening principles"), usage).found


def test_level_filter_prefers_the_learners_band(lib, usage):
    easy = retrieve(lib, RetrievalRequest(text="checkmates", level="beginner",
                                          include_prerequisites=False), usage)
    assert easy.level == "beginner" and easy.level_source == "request"
    assert all(e.difficulty <= 2 for e in easy.examples[:3])
    hard = retrieve(lib, RetrievalRequest(text="show me hard checkmates"), usage)
    assert hard.level == "advanced" and hard.level_source == "text"
    assert min(e.difficulty for e, _ in hard.sequence) >= 3


def test_difficulty_band_and_category_filters(lib, usage):
    band = retrieve(lib, RetrievalRequest(text="checkmates", difficulty=(3, 5), count=5), usage)
    assert band.found and all(3 <= e.difficulty <= 5 for e in band.examples)
    cat = retrieve(lib, RetrievalRequest(text="checkmates", category="endgames", count=5), usage)
    assert cat.found and all(e.category == "endgames" for e in cat.examples)
    none = retrieve(lib, RetrievalRequest(text="forks", difficulty=(5, 5)), usage)
    assert not none.found


def test_mode_filter_quiz_me_gives_only_interactive_examples(lib, usage):
    result = retrieve(lib, RetrievalRequest(text="quiz me on forks"), usage)
    assert result.found
    assert all(e.key_move and "interactive" in e.presentation_modes for e in result.examples)
    assert [role for _, role in result.sequence][0] == "guided"


# ---- retrieval: sequences, seen history, personalization --------------------

def test_selects_a_small_varied_sequence(lib, usage):
    result = retrieve(lib, RetrievalRequest(text="Show me checkmates", include_prerequisites=False), usage)
    ids = [e.id for e, _ in result.sequence]
    assert len(ids) == 3 == len(set(ids))  # not the whole library
    assert [role for _, role in result.sequence] == ["demonstration", "guided", "practice"]
    assert len({e.concept for e, _ in result.sequence}) >= 2  # variety of mating patterns
    difficulties = [e.difficulty for e, _ in result.sequence]
    assert difficulties == sorted(difficulties)  # easiest first


def test_requested_count_is_respected(lib, usage):
    assert len(retrieve(lib, RetrievalRequest(text="show me 2 checkmates"), usage).sequence) == 2
    assert len(retrieve(lib, RetrievalRequest(text="show me three forks"), usage).sequence) == 3
    assert len(retrieve(lib, RetrievalRequest(text="forks", count=1), usage).sequence) == 1


def test_seen_examples_are_excluded_until_nothing_else_fits(lib, usage):
    first = retrieve(lib, RetrievalRequest(text="Teach me forks"), usage)
    usage.record_used(first.examples)
    second = retrieve(lib, RetrievalRequest(text="Teach me forks"), usage)
    assert not set(_all_ids(first)) & set(_all_ids(second))


def test_explicit_exclusions(lib, usage):
    first = retrieve(lib, RetrievalRequest(text="checkmates"), usage)
    again = retrieve(lib, RetrievalRequest(text="checkmates", exclude=set(_all_ids(first))), usage)
    assert again.found and not set(_all_ids(first)) & set(_all_ids(again))


def test_prerequisites_are_shown_first_to_beginners(lib, usage):
    result = retrieve(lib, RetrievalRequest(text="teach me really simple checkmates"), usage)
    assert result.prerequisites == ["check"]
    first, role = result.sequence[0]
    assert "check" in lib.descendants("check") and first.concept in lib.descendants("check")
    assert role == "demonstration"
    usage.record_used([first])  # met once: not repeated
    assert retrieve(lib, RetrievalRequest(text="teach me really simple checkmates"), usage).prerequisites == []


def test_history_sets_the_level_and_weak_spots(lib, usage):
    mates = lib.examples_for("smothered_mate")
    for _ in range(3):
        usage.record_attempt(mates[0].id, success=False)
    result = retrieve(lib, RetrievalRequest(text="checkmates"), usage)
    assert result.level == "beginner" and result.level_source == "history"
    assert any(e.concept == "smothered_mate" for e in result.examples)


def test_related_concepts_are_offered(lib, usage):
    assert "skewer" in retrieve(lib, RetrievalRequest(text="pins"), usage).related


# ---- intent detection (chat) -------------------------------------------------

@pytest.mark.parametrize("message, expected", [
    ("Teach me forks", ["fork"]),
    ("Show me checkmates", ["checkmate"]),
    ("I want to learn pins", ["pin"]),
    ("Teach me the Italian Game", ["italian_game"]),
    ("Give me an endgame lesson", ["endgames"]),
    ("quiz me on forks", ["fork"]),
    ("show me why this move is bad", []),
    ("why is Nf3 good here?", []),
    ("Teach me the London System", []),  # not in the library -> planner handles it
])
def test_lesson_request_detection(lib, message, expected):
    assert lesson_request(lib, message) == expected


# ---- lessons built from retrieved examples -----------------------------------

def test_every_verified_example_builds_a_valid_lesson_in_every_role(lib):
    for example in lib.verified():
        for role in ("demonstration", "guided", "practice"):
            lesson = knowledge_lesson("t", "t", "intro", [(example, role)], "done", ["c"])
            assert parse_lesson(lesson, course_id="x").steps


def test_board_lesson_from_a_retrieved_example(lib):
    example = lib.get("mate_back_rank_basic")
    rep = example.replay()
    demo = example_steps(example, "demonstration", 1, 1, ["Checkmate"])
    assert demo[0]["type"] == "demonstrate" and demo[0]["moves"] == example.uci
    assert demo[0]["fen"] == example.start_fen and demo[0]["comments"][0] == example.notes["1.Rd8#"]
    assert demo[-1]["type"] == "teach" and demo[-1]["board"]["fen"] == rep.final.fen()

    guided = example_steps(example, "guided", 1, 1, ["Checkmate"])
    exercise = next(s for s in guided if s["type"] == "exercise")
    assert exercise["fen"] == rep.boards[example.key_ply].fen()
    assert exercise["accepted"][0] == "Rd8#" and exercise["side"] == "white"
    assert exercise["hints"][:2] == example.hints and exercise["example"] == example.id
    assert all(s["example"] == example.id for s in guided)


def test_guided_example_stops_before_the_key_move(lib):
    example = next(e for e in lib.examples_for("tactics") if e.key_ply and e.key_ply > 0
                   and len(e.moves) > e.key_ply + 1)
    steps = example_steps(example, "guided", 1, 1, ["x"])
    assert steps[0]["type"] == "demonstrate" and steps[0]["moves"] == example.uci[:example.key_ply]
    assert steps[1]["type"] == "exercise"
    assert steps[2]["type"] == "demonstrate" and steps[2]["moves"] == example.uci[example.key_ply + 1:]


def test_practice_example_asks_for_every_learner_move(lib):
    example = next(e for e in lib.verified() if e.key_move and len(e.moves) - e.key_ply >= 3)
    steps = example_steps(example, "practice", 1, 1, ["x"])
    exercises = [s for s in steps if s["type"] == "exercise"]
    assert len(exercises) == (len(example.moves) - example.key_ply + 1) // 2
    assert exercises[1]["accepted"][0] == example.moves[example.key_ply + 2]


def test_knowledge_plan_record(lib, usage):
    record = create_knowledge_plan("Show me checkmates", usage=usage)
    plan = record["plan"]
    assert plan["planner"] == "knowledge"
    unit = plan["units"][0]
    assert unit["verified_by"].startswith("Knowledge Library") and unit["concepts"] == ["checkmate"]
    first = record["lessons"][0]
    assert first["examples"] == plan["knowledge"]["example_ids"][:len(first["examples"])]
    types = [s["type"] for s in first["steps"]]
    assert types[0] == "teach" and "demonstrate" in types and "exercise" in types
    # more practice from the catalog, linked through the concept graph (checkmate -> mate_in_1 ...)
    assert [u["topic_id"] for u in plan["units"][1:]] == ["mate_in_1", "back_rank_mate"]
    # the examples now count as seen
    assert set(usage.seen_counts()) == set(plan["knowledge"]["example_ids"])


def test_no_suitable_example_falls_back_to_the_existing_planner(lib, usage):
    assert create_knowledge_plan("I want to learn the London System", usage=usage) is None
    record = plan_for_goal("I want to learn the London System", use_qwen=False)
    assert record["plan"]["planner"] == "catalog"
    assert [u["topic_id"] for u in record["plan"]["units"]][-1] == "london_system"
    record = plan_for_goal("teach me opening principles", use_qwen=False)
    assert record["plan"]["planner"] == "catalog"
    assert record["plan"]["units"][0]["topic_id"] == "opening_principles"


def test_library_failure_falls_back_to_the_existing_planner(lib, usage, monkeypatch):
    from app.planner import knowledge_lessons

    def broken(*a, **k):
        raise RuntimeError("library exploded")
    monkeypatch.setattr(knowledge_lessons, "create_knowledge_plan", broken)
    assert plan_for_goal("I want to learn forks", use_qwen=False)["plan"]["planner"] == "catalog"


def test_catalog_planner_is_unchanged_without_library_flag(lib, usage):
    record = plan_for_goal("I want to learn forks", library_first=False, use_qwen=False)
    assert record["plan"]["planner"] == "catalog" and record["plan"]["units"][0]["topic_id"] == "forks"


# ---- through the HTTP API and the session engine ----------------------------

def _knowledge_plan(client, goal="Teach me the back rank mate", **extra):
    res = client.post("/api/plans", json={"goal": goal, "library": True, **extra})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["source"] == "knowledge"
    return data


def _advance_to_exercise(client, sid, step):
    while step["type"] != "exercise":
        step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    return step


def test_api_plan_prefers_the_library_and_plays_through(client, engine):
    data = _knowledge_plan(client)
    assert data["plan"]["planner"] == "knowledge"
    courses = client.get("/api/courses").json()["courses"]
    assert courses[0]["id"] == data["course_id"]

    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _advance_to_exercise(client, sid, start["step"])
    assert step["example"]["id"] and step["example"]["explainable"] is False

    from app.knowledge.library import get_knowledge
    example = get_knowledge().get(step["example"]["id"])
    board = chess.Board(step["board"]["fen"])
    key = board.parse_san(example.moves[example.key_ply])
    wrong = next(m for m in board.legal_moves if board.san(m) not in (example.moves[example.key_ply],
                                                                     *example.accepted))
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": wrong.uci()}).json()
    assert res["accepted"] is False and res["reset_fen"] == board.fen()
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": key.uci()}).json()
    assert res["accepted"] is True
    assert engine.calls == 2  # Stockfish judged both moves

    stats = UsageTracker(get_knowledge_usage_path()).stats(example.id)
    assert stats["attempts"] == 2 and stats["successes"] == 1


def get_knowledge_usage_path():
    from app.knowledge.usage import get_usage
    return get_usage().path


def test_illegal_learner_moves_are_still_rejected_by_python_chess(client, engine):
    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _advance_to_exercise(client, sid, start["step"])
    board = chess.Board(step["board"]["fen"])
    illegal = next(u for u in ("a1a8", "e1e5", "h8a1", "b1b8")
                   if chess.Move.from_uci(u) not in board.legal_moves)
    res = client.post(f"/api/sessions/{sid}/move", json={"uci": illegal})
    assert res.status_code == 400
    assert client.post(f"/api/sessions/{sid}/move", json={"uci": "zz99"}).status_code == 400
    assert client.post(f"/api/sessions/{sid}/move", json={"uci": "a1a1"}).status_code == 400
    assert engine.calls == 0  # rejected by the rules before the engine is asked
    assert client.get(f"/api/sessions/{sid}").json()["step"]["board"]["fen"] == board.fen()


def test_verified_facts_reach_the_teacher_on_a_move(client):
    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _advance_to_exercise(client, sid, start["step"])
    from app.knowledge.library import get_knowledge
    from app.session import get_manager
    example = get_knowledge().get(step["example"]["id"])
    board = chess.Board(step["board"]["fen"])
    key = board.parse_san(example.moves[example.key_ply])
    client.post(f"/api/sessions/{sid}/move", json={"uci": key.uci()})

    session = get_manager().get(sid)
    context = session.last_context
    assert context.example_id == example.id
    facts = "\n".join(context.facts)
    assert example.start_fen in facts and example.key_move in facts          # FEN + moves
    assert "Legal moves for" in facts and board.fen() in facts                # legal moves + position
    assert "Stockfish" in facts and "Verified explanation" in facts           # engine + verified text
    assert f"Concept: {get_knowledge().concepts[example.concept].name}" in facts
    assert "Learner level" in facts
    prompt = build_move_feedback_messages(session.last_feedback, context)[1]["content"]
    assert "Verified example from the lesson library" in prompt and example.start_fen in prompt


def test_chat_during_an_exercise_gets_no_spoilers(client):
    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _advance_to_exercise(client, sid, start["step"])
    from app.knowledge.library import get_knowledge
    from app.session import get_manager
    example = get_knowledge().get(step["example"]["id"])
    manager = get_manager()
    context = manager._chat_context(manager.get(sid))
    facts = "\n".join(context.facts)
    assert "do NOT name" in facts and "Legal moves for" in facts
    assert example.key_move not in facts and "Verified explanation" not in facts
    assert example.moves[example.key_ply] not in facts.split("Legal moves")[0]
    chat = build_chat_messages("help?", context, [])
    assert "do NOT name" in chat[1]["content"]
    # explaining the example now would give the answer away
    assert client.post(f"/api/sessions/{sid}/example/explain").status_code == 409


def _to_explanation_step(client, sid, step):
    """Solve exercises (via reveal) until a teach step that may be explained."""
    while not (step["type"] == "teach" and step.get("example", {}).get("explainable")):
        if step["type"] == "exercise":
            client.post(f"/api/sessions/{sid}/reveal")
        step = client.post(f"/api/sessions/{sid}/advance").json()["step"]
    return step


def test_explain_example_streams_qwen_with_verified_facts(client, monkeypatch, qwen_on):
    captured = {}

    def fake_stream(method, url, json, headers, timeout):
        captured["messages"] = json["messages"]
        return FakeStream(sse("The rook checks on the back rank ", "and the pawns block the king."))
    monkeypatch.setattr(qwen_mod.httpx, "stream", fake_stream)

    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _to_explanation_step(client, sid, start["step"])
    from app.knowledge.library import get_knowledge
    example = get_knowledge().get(step["example"]["id"])
    res = client.post(f"/api/sessions/{sid}/example/explain")
    events = [json.loads(line) for line in res.text.splitlines()]
    assert events[-1]["type"] == "done" and events[-1]["teacher"] == "qwen"
    prompt = captured["messages"][-1]["content"]
    assert example.start_fen in prompt and "Verified explanation" in prompt
    assert "do not judge whether moves are correct" in prompt


def test_contradicting_qwen_explanation_is_replaced_by_the_verified_one(client, monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream",
                        lambda *a, **k: FakeStream(sse("The queen on h5 delivers mate with Qxh7#.")))
    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _to_explanation_step(client, sid, start["step"])
    from app.knowledge.library import get_knowledge
    example = get_knowledge().get(step["example"]["id"])
    events = [json.loads(line) for line in
              client.post(f"/api/sessions/{sid}/example/explain").text.splitlines()]
    assert events[-1]["teacher"] == "fallback" and events[-1].get("corrected") is True
    assert events[-1]["text"] == example.explanation


def test_explain_example_offline_uses_the_verified_explanation(client):
    data = _knowledge_plan(client)
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid = start["session_id"]
    step = _to_explanation_step(client, sid, start["step"])
    from app.knowledge.library import get_knowledge
    example = get_knowledge().get(step["example"]["id"])
    events = [json.loads(line) for line in
              client.post(f"/api/sessions/{sid}/example/explain").text.splitlines()]
    assert events[-1] == {"type": "done", "teacher": "fallback", "text": example.explanation}


def test_ordinary_lessons_have_no_example_facts(client):
    start = client.post("/api/lessons/italian_01/start").json()
    assert "example" not in start["step"]
    assert client.post(f"/api/sessions/{start['session_id']}/example/explain").status_code == 409


def test_completing_a_knowledge_lesson_records_usage(client):
    data = _knowledge_plan(client, goal="show me 1 back rank mate")
    start = client.post(f"/api/lessons/{data['first_lesson_id']}/start").json()
    sid, step = start["session_id"], start["step"]
    while True:
        if step["type"] == "exercise":
            client.post(f"/api/sessions/{sid}/reveal")
        res = client.post(f"/api/sessions/{sid}/advance").json()
        if res["completed"]:
            break
        step = res["step"]
    from app.knowledge.usage import get_usage
    for eid in data["plan"]["units"][0]["example_ids"][:1]:
        assert get_usage().stats(eid)["times_completed"] == 1


def test_api_falls_back_when_the_library_has_nothing(client):
    res = client.post("/api/plans", json={"goal": "I want to learn the London System", "library": True})
    assert res.status_code == 200 and res.json()["source"] == "planner"
    res = client.post("/api/plans", json={"goal": "underwater basket weaving", "library": True})
    assert res.status_code == 422 and res.json()["suggestions"]


def test_intent_endpoint(client):
    assert client.post("/api/knowledge/intent", json={"message": "Show me checkmates"}).json() == {
        "lesson_request": True, "concepts": ["checkmate"]}
    assert client.post("/api/knowledge/intent",
                       json={"message": "show me why this move is bad"}).json()["lesson_request"] is False


def test_knowledge_plans_survive_restart(client):
    data = _knowledge_plan(client)
    set_library(None)  # rebuilt from disk: the example links survive with the lessons
    from app.lessons import get_library
    lesson = get_library().lesson(data["first_lesson_id"])
    assert any(getattr(s, "example", None) for s in lesson.steps)
    assert all(isinstance(s, (TeachStep, DemonstrateStep, ExerciseStep)) for s in lesson.steps)


def test_health_reports_the_library(client):
    assert client.get("/api/health").json()["knowledge_examples"] >= 100


def test_plan_lesson_titles_are_distinct():
    """Sidebar clutter: a library plan listed 'Back-rank mate: practice' twice."""
    from app.planner.knowledge_lessons import _distinct_titles
    existing = [{"title": "Back-rank mate: learn from examples"}, {"title": "Back-rank mate: practice"}]
    new = [{"title": "Back-rank mate"}, {"title": "Back-rank mate: practice"}]
    _distinct_titles(new, existing, "Back-rank mate")
    assert [lesson["title"] for lesson in new] == ["Back-rank mate: more examples", "Back-rank mate: more practice"]
    other = [{"title": "Forks"}, {"title": "Forks: practice"}]
    _distinct_titles(other, existing, "Forks")
    assert [lesson["title"] for lesson in other] == ["Forks: more examples", "Forks: practice"]
