"""Qwen teacher via an OpenAI-compatible endpoint (Ollama, LM Studio, vLLM...)."""
from __future__ import annotations

import logging
import re

import httpx

from ..config import Settings, get_settings
from .base import LessonContext
from .prompts import build_chat_messages, build_move_feedback_messages

log = logging.getLogger(__name__)

NO_THINK_SWITCH = "/no_think"

# Qwen3 (and other reasoning models) may emit their chain of thought inline as
# <think>...</think>. It must never reach the student.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


class TeacherUnavailable(RuntimeError):
    pass


def clean_reply(text: str) -> str:
    """Remove reasoning blocks from a model reply and trim it."""
    text = _THINK_BLOCK.sub("", text or "")
    # A reply that ends mid-thought (hit a token limit) has nothing usable after it.
    text = _THINK_UNCLOSED.sub("", text)
    # Some servers strip the opening tag but leave "...</think>" behind.
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


def apply_no_think(messages: list[dict]) -> list[dict]:
    """Append Qwen3's `/no_think` soft switch to the last user message.

    Returns a new list; the caller's messages (and the session transcript) are
    not modified.
    """
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") == "user":
            if NO_THINK_SWITCH not in m["content"]:
                m["content"] = f"{m['content']}\n\n{NO_THINK_SWITCH}"
            break
    return out


class QwenTeacher:
    name = "qwen"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.qwen_model:
            raise TeacherUnavailable("QWEN_MODEL is not configured")

    @property
    def url(self) -> str:
        return self.settings.qwen_base_url.rstrip("/") + "/chat/completions"

    def build_payload(self, messages: list[dict]) -> dict:
        if self.settings.qwen_disable_thinking():
            messages = apply_no_think(messages)
        return {
            "model": self.settings.qwen_model,
            "messages": messages,
            "temperature": 0.4,
            "stream": False,
        }

    def _complete(self, messages: list[dict]) -> str:
        payload = self.build_payload(messages)
        headers = {"Authorization": f"Bearer {self.settings.qwen_api_key}"}
        try:
            response = httpx.post(
                self.url,
                json=payload,
                headers=headers,
                timeout=self.settings.qwen_timeout,
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Qwen request to %s failed: %s", self.url, exc)
            raise TeacherUnavailable(f"Qwen request failed: {exc}") from exc
        reply = clean_reply(raw)
        if not reply:
            log.warning("Qwen returned an empty reply (only reasoning?)")
            raise TeacherUnavailable("Qwen returned an empty reply")
        return reply

    def complete(self, messages: list[dict]) -> str:
        """Send arbitrary messages (used by the planner); raises TeacherUnavailable."""
        return self._complete(messages)

    def explain_move(self, feedback, context: LessonContext) -> str:
        return self._complete(build_move_feedback_messages(feedback, context))

    def chat(self, message: str, context: LessonContext, transcript: list[dict]) -> str:
        return self._complete(build_chat_messages(message, context, transcript))
