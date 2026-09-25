"""Qwen teacher via an OpenAI-compatible endpoint (Ollama, LM Studio, vLLM...)."""
from __future__ import annotations

import logging

import httpx

from ..config import Settings, get_settings
from .base import LessonContext
from .prompts import build_chat_messages, build_move_feedback_messages

log = logging.getLogger(__name__)


class TeacherUnavailable(RuntimeError):
    pass


class QwenTeacher:
    name = "qwen"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.qwen_model:
            raise TeacherUnavailable("QWEN_MODEL is not configured")

    @property
    def url(self) -> str:
        return self.settings.qwen_base_url.rstrip("/") + "/chat/completions"

    def _complete(self, messages: list[dict]) -> str:
        payload = {
            "model": self.settings.qwen_model,
            "messages": messages,
            "temperature": 0.4,
            "stream": False,
        }
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
            return data["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise TeacherUnavailable(f"Qwen request failed: {exc}") from exc

    def explain_move(self, feedback, context: LessonContext) -> str:
        return self._complete(build_move_feedback_messages(feedback, context))

    def chat(self, message: str, context: LessonContext, transcript: list[dict]) -> str:
        return self._complete(build_chat_messages(message, context, transcript))
