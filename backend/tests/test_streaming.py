"""Streaming AI text: think-tag filtering, SSE parsing, fallbacks, endpoints, planner modes."""
from tests.session_helpers import confirm_advance
import json
import random

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.engine import set_engine
from app.planner import create_plan
from app.planner import planner as planner_mod
from app.session import SessionManager, set_manager
from app.teacher import qwen as qwen_mod
from app.teacher import stream_events
from app.teacher.qwen import QwenTeacher, TeacherUnavailable, ThinkFilter, clean_reply

from tests.test_api import FakeEngine

SAMPLES = [
    "<think>\n\n</think>\n\nGreat move! Bc4 eyes f7.",
    "<think>Let me see... e4 < d4?</think>Develop the knight.",
    "No thinking here, just a < b comparison and x<y.",
    "reasoning leaked</think>\nThe answer.",
    "Answer first. <think>hidden</think> And more.",
    "<think>never closed",
]


def run_filter(text, sizes):
    f = ThinkFilter()
    out = []
    for piece in [text[sum(sizes[:k]):sum(sizes[:k + 1])] for k in range(len(sizes))] + [text[sum(sizes):]]:
        visible = f.feed(piece)
        if f.consume_reset():
            out.clear()
        out.append(visible)
    out.append(f.flush())
    return "".join(out)


@pytest.mark.parametrize("text", SAMPLES)
def test_think_filter_matches_clean_reply_for_any_chunking(text):
    expected = clean_reply(text)
    rng = random.Random(7)
    assert run_filter(text, [len(text)]).strip() == expected
    assert run_filter(text, [1] * len(text)).strip() == expected  # char by char
    for _ in range(50):
        sizes = [rng.randint(1, 6) for _ in range(len(text))]
        assert run_filter(text, sizes).strip() == expected, sizes


class FakeStream:
    def __init__(self, lines, status=200, fail_after=None):
        self.lines, self.status, self.fail_after = lines, status, fail_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(self.status, request=req))

    def iter_lines(self):
        for i, line in enumerate(self.lines):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.ReadTimeout("stalled")
            yield line


def sse(*pieces):
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}" for p in pieces]
    lines.insert(1, ": keep-alive comment")
    lines.insert(2, f"data: {json.dumps({'choices': [{'delta': {'reasoning': 'secret'}}]})}")
    return lines + ["data: [DONE]"]


@pytest.fixture()
def qwen_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "qwen_model", "qwen3:4b")
    return s


def test_stream_parses_sse_and_hides_reasoning(monkeypatch, qwen_on):
    captured = {}

    def fake_stream(method, url, json, headers, timeout):
        captured["payload"] = json
        return FakeStream(sse("<thi", "nk>\n\n</th", "ink>\n\nDevelop ", "your ", "knight."))

    monkeypatch.setattr(qwen_mod.httpx, "stream", fake_stream)
    text = "".join(QwenTeacher(qwen_on).stream([{"role": "user", "content": "q"}]))
    assert text == "Develop your knight."
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["max_tokens"] == qwen_on.qwen_max_tokens
    assert captured["payload"]["messages"][-1]["content"].endswith("/no_think")


def test_stream_retracts_leaked_reasoning(monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream",
                        lambda *a, **k: FakeStream(sse("hmm, reasoning ", "here</thi", "nk>\n\nReal answer.")))
    events, _ = collect(stream_events(lambda: [{"role": "user", "content": "q"}], lambda: "fb"))
    shown = ""
    for e in events:
        if e["type"] == "delta":
            shown += e["text"]
        elif e["type"] == "replace":
            shown = e["text"]
    assert shown == "Real answer."
    assert events[-1] == {"type": "done", "teacher": "qwen", "text": "Real answer."}


def test_stream_http_error_raises_teacher_unavailable(monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream([], status=500))
    with pytest.raises(TeacherUnavailable):
        list(QwenTeacher(qwen_on).stream([{"role": "user", "content": "q"}]))


def collect(events):
    events = list(events)
    return events, "".join(e.get("text", "") for e in events if e["type"] == "delta")


def test_stream_events_fallback_when_qwen_off():
    events, text = collect(stream_events(lambda: [], lambda: "Quick answer."))
    assert events[0] == {"type": "start", "teacher": "fallback"}
    assert events[-1] == {"type": "done", "teacher": "fallback", "text": "Quick answer."}
    assert text == "Quick answer."


def test_stream_events_qwen_success(monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream(sse("Hello ", "there.")))
    events, text = collect(stream_events(lambda: [{"role": "user", "content": "q"}], lambda: "fb"))
    assert text == "Hello there."
    assert events[-1] == {"type": "done", "teacher": "qwen", "text": "Hello there."}


def test_closed_local_replies_can_skip_model_stream_even_when_qwen_is_configured(monkeypatch, qwen_on):
    def should_not_stream(*args, **kwargs):
        raise AssertionError("a closed greeting/discovery reply must stay local")

    monkeypatch.setattr(qwen_mod.httpx, "stream", should_not_stream)
    events, text = collect(stream_events(should_not_stream, lambda: "Hi!", force_fallback=True))
    assert text == "Hi!"
    assert events[0] == {"type": "start", "teacher": "fallback"}
    assert events[-1] == {"type": "done", "teacher": "fallback", "text": "Hi!"}


def test_stream_events_qwen_down_before_any_text_uses_fallback(monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream([], status=503))
    events, _ = collect(stream_events(lambda: [{"role": "user", "content": "q"}], lambda: "Fallback text."))
    assert {"type": "replace", "text": "Fallback text."} in events
    assert events[-1]["teacher"] == "fallback"


def test_stream_events_qwen_stalls_midway_keeps_partial(monkeypatch, qwen_on):
    monkeypatch.setattr(qwen_mod.httpx, "stream",
                        lambda *a, **k: FakeStream(sse("Knights ", "like ", "outposts."), fail_after=4))
    events, text = collect(stream_events(lambda: [{"role": "user", "content": "q"}], lambda: "fb"))
    assert text.startswith("Knights ") and "stopped responding" in text
    assert events[-1]["teacher"] == "qwen"


# ---- endpoints -----------------------------------------------------------------

@pytest.fixture()
def client():
    set_engine(FakeEngine())
    set_manager(SessionManager())
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        yield c
    set_engine(None)
    set_manager(None)


def to_exercise(client):
    sid = client.post("/api/lessons/italian_01/start").json()["session_id"]
    for _ in range(2):
        confirm_advance(client, sid)
    return sid


def ndjson(res):
    assert res.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


def test_move_answers_instantly_and_explain_streams(client):
    sid = to_exercise(client)
    assert client.post(f"/api/sessions/{sid}/explain").status_code == 409  # nothing to explain yet
    move = client.post(f"/api/sessions/{sid}/move", json={"uci": "f1c4"}).json()
    assert move["accepted"] and move["teacher"] == "fallback" and move["explanation"]
    assert move["ai_explanation"] is False  # no QWEN_MODEL in tests
    events = ndjson(client.post(f"/api/sessions/{sid}/explain"))
    assert [e["type"] for e in events] == ["start", "delta", "done"]
    assert events[-1]["text"] == move["explanation"]


def test_chat_stream_records_transcript_once(client):
    sid = to_exercise(client)
    events = ndjson(client.post(f"/api/sessions/{sid}/chat/stream", json={"message": "Why Bc4?"}))
    assert events[-1]["type"] == "done"
    from app.session import get_manager
    transcript = get_manager().get(sid).transcript
    assert [t["role"] for t in transcript] == ["user", "assistant"]
    assert transcript[0]["content"] == "Why Bc4?"


def test_chat_prompt_contains_new_message_once(monkeypatch, client, qwen_on):
    sid = to_exercise(client)
    sent = {}

    def fake_stream(method, url, json, headers, timeout):
        sent["messages"] = json["messages"]
        return FakeStream(sse("Because f7."))

    monkeypatch.setattr(qwen_mod.httpx, "stream", fake_stream)
    ndjson(client.post(f"/api/sessions/{sid}/chat/stream", json={"message": "Why Bc4?"}))
    ndjson(client.post(f"/api/sessions/{sid}/chat/stream", json={"message": "And then?"}))
    contents = [m["content"].replace("\n\n/no_think", "") for m in sent["messages"]]
    assert contents.count("Why Bc4?") == 1 and contents.count("And then?") == 1


# ---- planner modes --------------------------------------------------------------

@pytest.mark.parametrize("mode, goal, expect_call", [
    ("auto", "I want to learn the London System", False),  # catalog answers instantly
    ("auto", "I want to learn the Vienna Game", True),     # unknown to the catalog
    ("always", "I want to learn the London System", True),
    ("never", "I want to learn the Vienna Game", False),
])
def test_planner_qwen_modes(monkeypatch, qwen_on, mode, goal, expect_call):
    monkeypatch.setattr(qwen_on, "qwen_planner", mode)
    calls = []
    monkeypatch.setattr(planner_mod, "ask_qwen", lambda g, c: calls.append(g) or None)
    try:
        create_plan(goal)
    except planner_mod.PlanError:
        pass
    assert bool(calls) is expect_call
