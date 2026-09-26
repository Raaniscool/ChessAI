"""Qwen teacher via an OpenAI-compatible endpoint (Ollama, LM Studio, vLLM...)."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

import httpx

from ..config import Settings, get_settings
from .base import LessonContext
from .prompts import build_chat_messages, build_move_feedback_messages

log = logging.getLogger(__name__)

NO_THINK_SWITCH = "/no_think"
# Yielded by QwenTeacher.stream() when text already shown turns out to have been
# reasoning (a stray "</think>" arrived): consumers must discard what they showed.
RESET = "\x00RESET"

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


class ThinkFilter:
    """Incrementally strips <think>...</think> from a token stream.

    Tokens can split a tag ("<thi" + "nk>"), so text that might be the start of
    a tag is held back until it's unambiguous. Leading whitespace of the visible
    answer is dropped (Qwen3 emits "<think>\\n\\n</think>\\n\\n" before answering).
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_think = False
        self.started = False  # visible text emitted yet?
        self._reset = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        out = []
        while self.buffer:
            if self.in_think:
                end = self.buffer.lower().find(self.CLOSE)
                if end < 0:
                    # keep a tail that could be the start of "</think>"
                    self.buffer = self.buffer[-(len(self.CLOSE) - 1):]
                    break
                self.buffer = self.buffer[end + len(self.CLOSE):]
                self.in_think = False
                continue
            start = self.buffer.lower().find(self.OPEN)
            if start >= 0:
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(self.OPEN):]
                self.in_think = True
                continue
            # A stray "</think>" (server stripped the opening tag): everything before
            # it was reasoning — including text already emitted, which must be retracted.
            stray = self.buffer.lower().find(self.CLOSE)
            if stray >= 0:
                self.buffer = self.buffer[stray + len(self.CLOSE):]
                out.clear()
                if self.started:
                    self._reset = True
                    self.started = False
                continue
            # Hold back a suffix that could still become "<think>" / "</think>".
            hold = 0
            lower = self.buffer.lower()
            for tag in (self.OPEN, self.CLOSE):
                for n in range(min(len(tag) - 1, len(lower)), 0, -1):
                    if lower.endswith(tag[:n]):
                        hold = max(hold, n)
                        break
            emit = self.buffer[: len(self.buffer) - hold]
            self.buffer = self.buffer[len(self.buffer) - hold:]
            out.append(emit)
            break
        return self._visible("".join(out))

    def consume_reset(self) -> bool:
        """True once after emitted text was retracted by a stray closing tag."""
        reset, self._reset = self._reset, False
        return reset

    def flush(self) -> str:
        if self.in_think:
            return ""
        rest, self.buffer = self.buffer, ""
        return self._visible(rest)

    def _visible(self, text: str) -> str:
        if not self.started:
            text = text.lstrip()
            if text:
                self.started = True
        return text


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

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.qwen_api_key}"}

    def build_payload(self, messages: list[dict], stream: bool = False,
                      max_tokens: int | None = None) -> dict:
        if self.settings.qwen_disable_thinking():
            messages = apply_no_think(messages)
        payload = {
            "model": self.settings.qwen_model,
            "messages": messages,
            "temperature": 0.4,
            "stream": stream,
        }
        limit = max_tokens if max_tokens is not None else self.settings.qwen_max_tokens
        if limit and limit > 0:
            payload["max_tokens"] = limit
        return payload

    def _complete(self, messages: list[dict], max_tokens: int | None = None) -> str:
        payload = self.build_payload(messages, max_tokens=max_tokens)
        try:
            response = httpx.post(
                self.url,
                json=payload,
                headers=self.headers,
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

    def complete(self, messages: list[dict], max_tokens: int | None = None) -> str:
        """Send arbitrary messages (used by the planner); raises TeacherUnavailable."""
        return self._complete(messages, max_tokens=max_tokens)

    def stream(self, messages: list[dict], max_tokens: int | None = None) -> Iterator[str]:
        """Yield visible reply text as the model generates it (OpenAI SSE protocol).

        Raises TeacherUnavailable if the request fails; callers decide how to
        degrade depending on whether any text was already shown.
        """
        payload = self.build_payload(messages, stream=True, max_tokens=max_tokens)
        think = ThinkFilter()
        # Generous read timeout per chunk; the first token can take a while on CPU.
        timeout = httpx.Timeout(self.settings.qwen_timeout, connect=10.0)
        try:
            with httpx.stream("POST", self.url, json=payload, headers=self.headers,
                              timeout=timeout) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        choice = json.loads(data)["choices"][0]
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    # Only "content" is shown; "reasoning"/"reasoning_content" never is.
                    delta = (choice.get("delta") or {}).get("content") or ""
                    visible = think.feed(delta) if delta else ""
                    if think.consume_reset():
                        yield RESET
                    if visible:
                        yield visible
        except httpx.HTTPError as exc:
            log.warning("Qwen stream from %s failed: %s", self.url, exc)
            raise TeacherUnavailable(f"Qwen request failed: {exc}") from exc
        tail = think.flush()
        if tail:
            yield tail

    def warm_up(self) -> bool:
        """Ask for a single token so the server loads the model into memory."""
        try:
            httpx.post(self.url, headers=self.headers, timeout=self.settings.qwen_timeout,
                       json=self.build_payload([{"role": "user", "content": "Say OK."}], max_tokens=1),
                       ).raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("Qwen warm-up failed: %s", exc)
            return False

    def explain_move(self, feedback, context: LessonContext) -> str:
        return self._complete(build_move_feedback_messages(feedback, context))

    def chat(self, message: str, context: LessonContext, transcript: list[dict]) -> str:
        return self._complete(build_chat_messages(message, context, transcript))
