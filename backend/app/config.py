"""Central configuration for the AI Chess Tutor backend.

Everything is environment-driven so the same code runs in the sandbox demo,
on a developer machine, and against a real local Qwen server.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"


def _decode(raw: bytes) -> str:
    """Windows PowerShell 5 writes UTF-16 with `>`/Out-File; editors may add a UTF-8 BOM."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=value lines; '#' comments, blank lines, 'export ' prefixes and quotes allowed."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path = DOTENV_PATH) -> dict[str, str]:
    """Apply settings from .env without overriding variables already set in the shell.

    Returns the keys that were applied. Disabled with CHESSAI_DOTENV=0 (the test
    suite does this so a developer's .env can't change test behaviour).
    """
    if os.environ.get("CHESSAI_DOTENV", "1").lower() in ("0", "false", "no", "off"):
        return {}
    try:
        values = parse_dotenv(_decode(path.read_bytes()))
    except OSError:
        return {}
    applied = {}
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


# Must run before Settings below evaluates its defaults.
DOTENV_APPLIED = load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _find_node() -> str | None:
    return shutil.which("node")


def resolve_engine_command() -> list[str] | None:
    """Find a usable UCI engine command.

    Priority:
      1. ENGINE_CMD env var (explicit override, space-separated)
      2. Stockfish WASM via Node from package.json dependency
      3. A `stockfish` binary on PATH
    Returns None when no engine is available (features degrade gracefully).
    """
    explicit = _env("ENGINE_CMD")
    if explicit:
        return explicit.split()

    node = _find_node()
    if node:
        wasm = REPO_ROOT / "node_modules" / "stockfish" / "bin" / "stockfish-19-lite-single.js"
        if wasm.exists():
            return [node, str(wasm)]

    system_sf = shutil.which("stockfish")
    if system_sf:
        return [system_sf]
    return None


@dataclass
class Settings:
    # --- Engine ---
    engine_cmd: list[str] | None = field(default_factory=resolve_engine_command)
    engine_depth: int = int(_env("ENGINE_DEPTH", "14") or "14")
    engine_timeout: float = float(_env("ENGINE_TIMEOUT", "20") or "20")
    # Game analysis: a quick pass over every position at this depth, then each candidate
    # mistake is re-checked at ENGINE_DEPTH before it is shown (40 moves ~ 30-60 s on a laptop).
    game_analysis_depth: int = int(_env("GAME_ANALYSIS_DEPTH", "12") or "12")

    # --- Qwen / AI teacher (OpenAI-compatible endpoint) ---
    # Works with Ollama (/v1), LM Studio, vLLM, llama.cpp server, etc.
    qwen_base_url: str = _env("QWEN_BASE_URL", "http://localhost:11434/v1") or ""
    qwen_api_key: str = _env("QWEN_API_KEY", "ollama") or "ollama"
    qwen_model: str = _env("QWEN_MODEL", "") or ""
    # Local models can take a while to load on the first request.
    qwen_timeout: float = float(_env("QWEN_TIMEOUT", "120") or "120")
    # Qwen3 "thinking" mode: auto (= off for qwen3 models), on, off.
    # Thinking makes small local models much slower and adds no value here,
    # because the chess facts come from Stockfish, not from the model.
    qwen_thinking: str = (_env("QWEN_THINKING", "auto") or "auto").lower()
    # Cap reply length: explanations are 2-5 sentences; long rambles are slow on local hardware.
    qwen_max_tokens: int = int(_env("QWEN_MAX_TOKENS", "300") or "300")
    # Planner: "auto" = ask Qwen only when the catalog can't answer (instant plans
    # for known topics), "always" = Qwen organizes every plan, "never".
    qwen_planner: str = (_env("QWEN_PLANNER", "auto") or "auto").lower()
    # Load the model into memory when the server starts so the first reply isn't the slowest.
    qwen_warmup: bool = (_env("QWEN_WARMUP", "1") or "1").lower() not in ("0", "false", "no", "off")

    # --- Server ---
    # --- read-aloud (backend/app/tts; docs/TTS.md) ---
    # auto: Kokoro (local) if its model files are installed, else an OpenAI-compatible
    # speech server if TTS_OPENAI_BASE_URL is set, else the browser's own voices.
    tts_provider: str = (_env("TTS_PROVIDER", "auto") or "auto").lower()  # auto | kokoro | openai | browser
    tts_kokoro_model: str = _env("TTS_KOKORO_MODEL", "") or ""   # default: DATA_DIR/tts/kokoro-v1.0.int8.onnx
    tts_kokoro_voices: str = _env("TTS_KOKORO_VOICES", "") or ""  # default: DATA_DIR/tts/voices-v1.0.bin
    tts_openai_base_url: str = _env("TTS_OPENAI_BASE_URL", "") or ""  # e.g. http://localhost:8880/v1
    tts_openai_api_key: str = _env("TTS_OPENAI_API_KEY", "") or ""
    tts_openai_model: str = _env("TTS_OPENAI_MODEL", "kokoro") or "kokoro"  # kokoro | gpt-4o-mini-tts | tts-1
    tts_cache_mb: int = int(_env("TTS_CACHE_MB", "200") or "200")
    tts_timeout: float = float(_env("TTS_TIMEOUT", "30") or "30")

    host: str = _env("HOST", "0.0.0.0") or "0.0.0.0"
    port: int = int(_env("PORT", "8000") or "8000")

    # --- User data (generated plans; gitignored) ---
    data_dir: Path = Path(_env("DATA_DIR", str(REPO_ROOT / "data")) or "")

    # --- Lessons ---
    lessons_dir: Path = Path(
        _env("LESSONS_DIR", str(REPO_ROOT / "backend" / "app" / "lessons" / "data")) or ""
    )

    def qwen_configured(self) -> bool:
        """Qwen is used only when the user explicitly names a model.

        If no model is configured we use the deterministic fallback teacher so
        the app never pretends an LLM is available when it isn't.
        """
        return bool(self.qwen_model)

    def qwen_is_ollama(self) -> bool:
        url = self.qwen_base_url.lower()
        return ":11434" in url or "ollama" in url

    def qwen_reasoning_off(self) -> bool:
        """Send Ollama's `reasoning_effort: "none"` (= think off) unless thinking was requested.

        It is the reliable switch for models with a thinking mode (qwen3 hybrids, qwen3.5,
        deepseek-r1...) and harmless for models without one. Other servers don't get the field.
        """
        return self.qwen_is_ollama() and self.qwen_thinking not in ("on", "true", "1", "yes")

    def qwen_disable_thinking(self) -> bool:
        """Whether to send Qwen3's `/no_think` soft switch."""
        if self.qwen_thinking in ("on", "true", "1", "yes"):
            return False
        if self.qwen_thinking in ("off", "false", "0", "no"):
            return True
        name = self.qwen_model.lower()
        # Only hybrid Qwen3 builds obey the switch. The 2507 split models don't:
        # "-instruct" never thinks, "-thinking" always does.
        return "qwen3" in name and "instruct" not in name and "thinking" not in name


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Testing helper: force settings to be rebuilt from the environment."""
    global _settings
    _settings = None
