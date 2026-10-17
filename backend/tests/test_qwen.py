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


def test_qwen_teacher_refuses_missing_or_whitespace_model_configuration():
    for model in ("", "   "):
        with pytest.raises(TeacherUnavailable) as error:
            QwenTeacher(make_settings(model))
        assert error.value.reason == "model_not_configured"


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


def test_complete_honors_per_request_deadline_with_fast_connect_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["timeout"] = timeout
        return FakeResponse('{"action":"lesson_question"}')

    monkeypatch.setattr(qwen_mod.httpx, "post", fake_post)
    teacher = QwenTeacher(make_settings())
    assert teacher.complete([{"role": "user", "content": "route"}], 360, timeout=1.25)
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].read == pytest.approx(1.25)
    assert captured["timeout"].connect == pytest.approx(1.0)


def test_empty_reply_has_a_distinct_unusable_response_reason(monkeypatch):
    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: FakeResponse("<think>only thoughts"))
    with pytest.raises(TeacherUnavailable) as error:
        QwenTeacher(make_settings())._complete([{"role": "user", "content": "q"}])
    assert error.value.reason == "empty_response"


def test_http_error_raises_teacher_unavailable_with_missing_model_or_route_reason(monkeypatch):
    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: FakeResponse("x", status=404))
    with pytest.raises(TeacherUnavailable) as error:
        QwenTeacher(make_settings())._complete([{"role": "user", "content": "q"}])
    assert error.value.reason == "model_or_endpoint_not_found"


def test_timeout_and_connection_errors_are_categorized_without_raw_provider_details(monkeypatch):
    def timed_out(url, **kwargs):
        raise httpx.ReadTimeout("private timeout details", request=httpx.Request("POST", url))

    monkeypatch.setattr(qwen_mod.httpx, "post", timed_out)
    with pytest.raises(TeacherUnavailable) as timeout_error:
        QwenTeacher(make_settings()).complete([{"role": "user", "content": "q"}], timeout=4)
    assert timeout_error.value.reason == "timeout"
    assert "private timeout details" not in str(timeout_error.value)

    def refused(url, **kwargs):
        raise httpx.ConnectError("private endpoint details", request=httpx.Request("POST", url))

    monkeypatch.setattr(qwen_mod.httpx, "post", refused)
    with pytest.raises(TeacherUnavailable) as connect_error:
        QwenTeacher(make_settings()).complete([{"role": "user", "content": "q"}], timeout=4)
    assert connect_error.value.reason == "endpoint_unreachable"
    assert "private endpoint details" not in str(connect_error.value)


def test_invalid_base_url_is_reported_as_unsupported_configuration():
    settings = make_settings()
    settings.qwen_base_url = "not-a-valid-url"
    with pytest.raises(TeacherUnavailable) as error:
        QwenTeacher(settings).complete([{"role": "user", "content": "q"}], timeout=4)
    assert error.value.reason == "unsupported_configuration"


def test_invalid_openai_response_shape_is_distinguished_from_empty_content(monkeypatch):
    class BadShape(FakeResponse):
        def json(self):
            return {"choices": []}

    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: BadShape(""))
    with pytest.raises(TeacherUnavailable) as error:
        QwenTeacher(make_settings()).complete([{"role": "user", "content": "q"}], timeout=4)
    assert error.value.reason == "malformed_provider_response"
