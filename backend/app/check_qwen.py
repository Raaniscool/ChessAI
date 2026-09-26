"""Diagnose the Qwen connection with one command.

    python -m backend.app.check_qwen          (Windows: .\\.venv\\Scripts\\python -m backend.app.check_qwen)

Checks, in order: is QWEN_MODEL set, is the server reachable, does it have the
model, and does a real chat request come back with a clean answer.
"""
from __future__ import annotations

import sys
import time

import httpx

from .config import get_settings
from .teacher.qwen import QwenTeacher, TeacherUnavailable


def _list_models(base_url: str, api_key: str) -> list[str]:
    r = httpx.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return [m.get("id", "") for m in r.json().get("data", [])]


SPEED_PROMPT = [
    {"role": "system", "content": "You are a chess teacher."},
    {"role": "user", "content": "In about four sentences, explain why knights belong in the center."},
]


def measure(teacher) -> tuple[float, float, int]:
    """(seconds to first visible word, total seconds, streamed chunks ~ tokens)."""
    started = time.monotonic()
    first = None
    chunks = 0
    for _ in teacher.stream(SPEED_PROMPT, max_tokens=120):
        chunks += 1
        if first is None:
            first = time.monotonic() - started
    total = time.monotonic() - started
    return (first if first is not None else total), total, chunks


def speed_test(teacher) -> None:
    print("Speed test (a typical explanation, streamed):")
    try:
        first, total, chunks = measure(teacher)
    except TeacherUnavailable as exc:
        print(f"[WARN] streaming failed: {exc}")
        return
    rate = chunks / max(total - first, 0.01) if chunks > 1 else 0.0
    print(f"       first words after {first:.1f}s, finished in {total:.1f}s, ~{rate:.0f} tokens/s")
    if first > 8:
        print("[TIP ] Slow first words: the model may be (re)loading each time. Keep it in memory:")
        print('         setx OLLAMA_KEEP_ALIVE "2h"   then quit Ollama from the tray icon and start it again.')
        print("         Long prompts on CPU also delay the first word.")
    if rate and rate < 12:
        print("[TIP ] Under ~12 tokens/s usually means the model runs on the CPU. Check with:  ollama ps")
        print("         (PROCESSOR column: '100% GPU' is fast, 'CPU' is slow.)")
        print("         Faster options, still free and unlimited:  ollama pull qwen3:1.7b")
        print('         then  $env:QWEN_MODEL = "qwen3:1.7b"   (about 2x faster than 4b, slightly less smart;')
        print("         the chess facts always come from Stockfish either way).")
    elif rate:
        print("[ OK ] That's a healthy speed for a local model.")


def main() -> int:
    s = get_settings()
    print(f"QWEN_BASE_URL = {s.qwen_base_url}")
    print(f"QWEN_MODEL    = {s.qwen_model or '(not set)'}")
    print(f"QWEN_TIMEOUT  = {s.qwen_timeout:g}s   thinking disabled: {s.qwen_disable_thinking()}")
    print()

    try:
        models = _list_models(s.qwen_base_url, s.qwen_api_key)
    except httpx.HTTPError as exc:
        print(f"[FAIL] Can't reach the model server at {s.qwen_base_url}: {exc}")
        print("       Is Ollama running? Start it from the Start menu or run `ollama serve`.")
        print("       LM Studio users: start its server and set QWEN_BASE_URL=http://localhost:1234/v1")
        return 1
    print(f"[ OK ] Server reachable. Models: {', '.join(models) or '(none)'}")

    if not s.qwen_model:
        print("[FAIL] QWEN_MODEL is not set, so the app uses the offline fallback teacher.")
        if models:
            print(f'       PowerShell:  $env:QWEN_MODEL = "{models[0]}"   then run this check again.')
        return 1

    if models and s.qwen_model not in models:
        print(f"[FAIL] Model '{s.qwen_model}' isn't on the server. Use one of: {', '.join(models)}")
        return 1
    print(f"[ OK ] Model '{s.qwen_model}' found.")

    print("       Sending a test question (the first one can be slow while the model loads)...")
    teacher = QwenTeacher(s)
    started = time.monotonic()
    try:
        reply = teacher._complete(
            [
                {"role": "system", "content": "You are a chess teacher. Answer in one sentence."},
                {"role": "user", "content": "Why is controlling the center good in chess?"},
            ]
        )
    except TeacherUnavailable as exc:
        print(f"[FAIL] {exc}")
        print("       If it timed out, raise the limit: $env:QWEN_TIMEOUT = \"300\"")
        return 1
    print(f"[ OK ] Reply in {time.monotonic() - started:.1f}s: {reply}")
    print()
    speed_test(teacher)
    print()
    print("Qwen is connected. Start the app from THIS same window so it sees these settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
