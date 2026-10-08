"""Detecting models that think silently (Ollama's qwen3:4b is now Qwen3-4B-Thinking-2507)."""
import json

import pytest

from app import check_qwen
from app.teacher import qwen as qwen_mod
from app.teacher.qwen import QwenTeacher

from tests.test_qwen import make_settings
from tests.test_streaming import FakeStream


def lines(*deltas):
    return [f"data: {json.dumps({'choices': [{'delta': d}]})}" for d in deltas] + ["data: [DONE]"]


THINKING_OLLAMA = lines(*[{"content": "", "reasoning": "hmm "}] * 5, {"content": "Knights "}, {"content": "love the center."})
THINKING_OTHER = lines(*[{"reasoning_content": "hmm "}] * 3, {"content": "Answer."})
THINKING_INLINE = lines({"content": "<think>"}, {"content": "let me see"}, {"content": "</think>"}, {"content": "Answer."})
DIRECT = lines({"content": "Knights "}, {"content": "love the center."})


def run_stream(monkeypatch, stream_lines):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream(stream_lines))
    hits = []
    text = "".join(QwenTeacher(make_settings("qwen3:4b")).stream(
        [{"role": "user", "content": "hi"}], on_reasoning=lambda: hits.append(1)))
    return text, len(hits)


@pytest.mark.parametrize("stream_lines, expected_text, reasoning", [
    (THINKING_OLLAMA, "Knights love the center.", 5),
    (THINKING_OTHER, "Answer.", 3),
    (THINKING_INLINE, "Answer.", 2),  # "<think>" and "let me see" chunks
    (DIRECT, "Knights love the center.", 0),
])
def test_stream_reports_hidden_reasoning_but_never_shows_it(monkeypatch, stream_lines, expected_text, reasoning):
    text, hits = run_stream(monkeypatch, stream_lines)
    assert text.strip() == expected_text
    assert "hmm" not in text and "let me see" not in text
    assert hits == reasoning


def test_stream_without_callback_still_works(monkeypatch):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream(THINKING_OLLAMA))
    assert "".join(QwenTeacher(make_settings()).stream([{"role": "user", "content": "hi"}])).strip() \
        == "Knights love the center."


def test_speed_test_flags_thinking_model(monkeypatch, capsys):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream(THINKING_OLLAMA))
    assert check_qwen.speed_test(QwenTeacher(make_settings("qwen3:4b"))) == 1
    out = capsys.readouterr().out
    assert "thinks silently" in out
    assert "ollama pull qwen3:4b-instruct" in out
    assert 'Set-Content .env "QWEN_MODEL=qwen3:4b-instruct"' in out


def test_speed_test_passes_direct_model(monkeypatch, capsys):
    monkeypatch.setattr(qwen_mod.httpx, "stream", lambda *a, **k: FakeStream(DIRECT))
    assert check_qwen.speed_test(QwenTeacher(make_settings("qwen3:4b-instruct"))) == 0
    assert "thinks silently" not in capsys.readouterr().out


@pytest.mark.parametrize("model, alt", [
    ("qwen3:4b", "qwen3:4b-instruct"),
    ("qwen3:4b-thinking", "qwen3:4b-instruct"),
    ("qwen3:30b", "qwen3:30b-instruct"),
    ("qwen3:8b", "qwen3:4b-instruct"),       # no 8b instruct build: recommend the 4b one
    ("deepseek-r1:7b", "qwen3:4b-instruct"),
])
def test_instruct_alternative(model, alt):
    assert check_qwen.instruct_alternative(model) == alt


class FakeResponse:
    def __init__(self, message):
        self.message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": self.message}]}


@pytest.mark.parametrize("message, warns", [
    ({"content": "", "reasoning": "Okay"}, True),
    ({"content": "<think>Okay"}, True),
    ({"content": "OK"}, False),
])
def test_warm_up_warns_about_thinking_model(monkeypatch, caplog, message, warns):
    monkeypatch.setattr(qwen_mod.httpx, "post", lambda *a, **k: FakeResponse(message))
    with caplog.at_level("WARNING"):
        assert QwenTeacher(make_settings("qwen3:4b")).warm_up() is True
    assert ("thinks silently" in caplog.text) is warns
