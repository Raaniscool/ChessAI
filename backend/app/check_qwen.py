"""Diagnose the Qwen connection with one command.

    python -m backend.app.check_qwen          (Windows: .\\.venv\\Scripts\\python -m backend.app.check_qwen)

Checks, in order: is QWEN_MODEL set, is the server reachable, does it have the
model, and does a real chat request come back with a clean answer.
"""
from __future__ import annotations

import sys
import time

import httpx

from .config import DOTENV_APPLIED, DOTENV_PATH, get_settings
from .engine import MoveFeedback
from .engine.classification import Classification, Score
from .teacher.base import LessonContext
from .teacher.prompts import SYSTEM_PROMPT, build_move_feedback_messages
from .teacher.qwen import RESET, QwenTeacher, TeacherUnavailable


def _list_models(base_url: str, api_key: str) -> list[str]:
    r = httpx.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return [m.get("id", "") for m in r.json().get("data", [])]


def lesson_prompt() -> list[dict]:
    """The exact kind of prompt the app sends after a move (canned engine facts)."""
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    feedback = MoveFeedback(
        fen_before=fen, fen_after=fen, user_move_uci="f1c4", user_move_san="Bc4",
        category=Classification.EXCELLENT, loss_cp=10, best_move_uci="d2d4", best_move_san="d4",
        eval_before=Score("cp", 28), eval_after=Score("cp", 18),
        best_pv_san=["d4", "exd4", "Nxd4", "Nf6"], reply_pv_san=["Nf6", "d3", "Be7", "Nc3"])
    context = LessonContext("Italian Game", "The Italian Game: first moves",
                            ["development", "center", "king safety"],
                            "Develop your light-squared bishop to its most active square.", ["Bc4"])
    return build_move_feedback_messages(feedback, context)


RECOMMENDED_MODEL = "qwen3:4b-instruct"


def measure(teacher) -> tuple[float, float, int, int]:
    """(seconds to first visible word, total seconds, visible chunks ~ tokens,
    hidden reasoning chunks)."""
    started = time.monotonic()
    first = None
    chunks = 0
    reasoning = 0

    def count_reasoning():
        nonlocal reasoning
        reasoning += 1

    for piece in teacher.stream(lesson_prompt(), max_tokens=120, on_reasoning=count_reasoning):
        if piece == RESET:
            continue
        chunks += 1
        if first is None:
            first = time.monotonic() - started
    total = time.monotonic() - started
    return (first if first is not None else total), total, chunks, reasoning


def instruct_alternative(model: str) -> str:
    """Same-size Qwen3 build that answers without a thinking phase."""
    if model.startswith("qwen3:"):
        size = model.split(":", 1)[1].split("-", 1)[0]
        if size in ("4b", "30b", "235b"):  # sizes that have a 2507 instruct build
            return f"qwen3:{size}-instruct"
    return RECOMMENDED_MODEL


def thinking_model_advice(model: str) -> list[str]:
    alt = instruct_alternative(model)
    return [
        f"[FAIL] '{model}' thinks silently before every answer: you wait for text that is never shown,",
        "       and the thinking can use up the whole reply budget.",
        "       (Ollama's 'qwen3:4b' is now the thinking-only version, so /no_think no longer works.)",
        "       Use the same-size model that answers directly:",
        f"         ollama pull {alt}",
        f'         Set-Content .env "QWEN_MODEL={alt}"',
        "       then run this check again.",
    ]


def speed_test(teacher) -> int:
    print("Speed test (a real move explanation, streamed like in the app):")
    try:
        first, total, chunks, reasoning = measure(teacher)
    except TeacherUnavailable as exc:
        print(f"[WARN] streaming failed: {exc}")
        return 0
    rate = chunks / max(total - first, 0.01) if chunks > 1 else 0.0
    print(f"       first words after {first:.1f}s, finished in {total:.1f}s, ~{rate:.0f} tokens/s")
    if reasoning:
        for line in thinking_model_advice(teacher.settings.qwen_model):
            print(line)
        return 1
    if first > 8:
        print("[TIP ] Slow first words: the model may be (re)loading each time. Keep it in memory:")
        print('         setx OLLAMA_KEEP_ALIVE "2h"   then quit Ollama from the tray icon and start it again.')
        print("         Long prompts on CPU also delay the first word.")
    if rate:
        words = rate * 0.75
        pace = "faster than" if words >= 4 else "slower than"
        print(f"       ~{words:.0f} words/s appear on screen — {pace} typical reading speed (~4 words/s).")
    if rate and rate < 12:
        print("[TIP ] Under ~12 tokens/s usually means the model runs on the CPU. Check with:  ollama ps")
        print("         (PROCESSOR column: '100% GPU' is fast, 'CPU' is slow.)")
        if "1.7b" not in teacher.settings.qwen_model and "0.6b" not in teacher.settings.qwen_model:
            print("         Faster option, still free and unlimited:  ollama pull qwen3:1.7b")
            print('         then  Set-Content .env "QWEN_MODEL=qwen3:1.7b"   (about 2x faster than 4b, slightly less smart;')
            print("         the chess facts always come from Stockfish either way).")
    elif rate:
        print("[ OK ] That's a healthy speed for a local model.")
    return 0


def main() -> int:
    s = get_settings()
    print(f"QWEN_BASE_URL = {s.qwen_base_url}")
    source = " (from .env)" if "QWEN_MODEL" in DOTENV_APPLIED else (" (from this shell)" if s.qwen_model else "")
    print(f"QWEN_MODEL    = {s.qwen_model or '(not set)'}{source}")
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
            pick = (next((m for m in models if "instruct" in m), None)
                    or next((m for m in models if "coder" not in m), models[0]))
            print("       Save it once in the project's .env file (remembered in every new window):")
            print(f"         Set-Content .env \"QWEN_MODEL={pick}\"")
            print(f"       ({DOTENV_PATH}) — then run this check again.")
        return 1

    if models and s.qwen_model not in models:
        print(f"[FAIL] Model '{s.qwen_model}' isn't on the server. Use one of: {', '.join(models)}")
        print(f"       or download it:  ollama pull {s.qwen_model}")
        return 1
    print(f"[ OK ] Model '{s.qwen_model}' found.")

    print("       Sending a test question (the first one can be slow while the model loads)...")
    teacher = QwenTeacher(s)
    started = time.monotonic()
    try:
        reply = teacher._complete(
            [
                # Same system prompt as the app, so the speed test below reuses it from the
                # server's prompt cache, exactly like every move after the first in the app.
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "In one sentence: why is controlling the center good in chess?"},
            ]
        )
    except TeacherUnavailable as exc:
        print(f"[FAIL] {exc}")
        print('       If it timed out, raise the limit: add a line  QWEN_TIMEOUT=300  to .env')
        return 1
    print(f"[ OK ] Reply in {time.monotonic() - started:.1f}s: {reply}")
    print()
    if speed_test(teacher):
        return 1
    print()
    print("Qwen is connected. Restart the app so it picks up these settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
