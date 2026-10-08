"""Qwen teacher: reply cleaning, thinking switch, and HTTP handling (no real model needed)."""
import httpx
import pytest

from backend.app.config import Settings
from backend.app.teacher import qwen as qwen_mod
from backend.app.teacher.qwen import (
    NO_THINK_SWITCH,
    QwenTeacher,
    TeacherUnavailable,
    apply_no_think,
    clean_reply,
)


def make_settings(model="qwen3:4b", thinking="auto"):
    s = Settings()
    s.qwen_model = model
    s.qwen_thinking = thinking
    s.qwen_base_url = "http://localhost:11434/v1"
    return s


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<think>\nhmm, Bc4 eyes f7\n</think>\n\nGreat move!", "Great move!"),
        ("<think>\n\n</think>\n\nNice.", "Nice."),
        ("Plain answer.", "Plain answer."),
        ("reasoning without opening tag</think>Answer.", "Answer."),
        ("<think>never finished thinking", ""),
        ("<THINK>x</THINK> Case ok", "Case ok"),
    ],
)
def test_clean_reply(raw, expected):
    assert clean_reply(raw) == expected


def test_no_think_added_to_last_user_message_only_and_does_not_mutate():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    out = apply_no_think(msgs)
    assert out[-1]["content"].endswith(NO_THINK_SWITCH)
    assert NO_THINK_SWITCH not in out[1]["content"]
    assert msgs[-1]["content"] == "second"  # original untouched
    assert apply_no_think(out)[-1]["content"].count(NO_THINK_SWITCH) == 1


@pytest.mark.parametrize(
    "model, thinking, disabled",
    [
        ("qwen3:4b", "auto", True),
        ("qwen2.5:7b", "auto", False),
        ("qwen3:4b", "on", False),
        ("qwen2.5:7b", "off", True),
        # 2507 split models ignore the switch: don't pollute their prompt with it.
        ("qwen3:4b-instruct", "auto", False),
        ("qwen3:4b-thinking", "auto", False),
        ("qwen3:4b-instruct-2507-q4_K_M", "auto", False),
    ],
)
def test_thinking_switch(model, thinking, disabled):
    payload = QwenTeacher(make_settings(model, thinking)).build_payload(
        [{"role": "user", "content": "hi"}]
    )
    assert (NO_THINK_SWITCH in payload["messages"][-1]["content"]) is disabled
    assert payload["model"] == model


class FakeResponse:
    def __init__(self, content, status=200):
        self._content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_complete_strips_thinking(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured.update(url=url, json=json)
        return FakeResponse("<think>secret</think>\nDevelop your bishop to c4.")

    monkeypatch.setattr(qwen_mod.httpx, "post", fake_post)
    reply = QwenTeacher(make_settings())._complete([{"role": "user", "content": "q"}])
    assert reply == "Develop your bishop to c4."
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"


def test_complete_honors_per_request_route_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["timeout"] = timeout
        return FakeResponse('{"action":"lesson_question"}')

    monkeypatch.setattr(qwen_mod.httpx, "post", fake_post)
    teacher = QwenTeacher(make_settings())
    assert teacher.complete([{"role": "user", "content": "route"}], 360, timeout=1.25)
    assert captured["timeout"] == 1.25


def test_empty_reply_raises_so_app_falls_back(monkeypatch):
    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: FakeResponse("<think>only thoughts"))
    with pytest.raises(TeacherUnavailable):
        QwenTeacher(make_settings())._complete([{"role": "user", "content": "q"}])


def test_http_error_raises_teacher_unavailable(monkeypatch):
    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: FakeResponse("x", status=404))
    with pytest.raises(TeacherUnavailable):
        QwenTeacher(make_settings())._complete([{"role": "user", "content": "q"}])
