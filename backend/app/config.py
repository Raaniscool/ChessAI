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

    # --- Server ---
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

    def qwen_disable_thinking(self) -> bool:
        """Whether to send Qwen3's `/no_think` soft switch."""
        if self.qwen_thinking in ("on", "true", "1", "yes"):
            return False
        if self.qwen_thinking in ("off", "false", "0", "no"):
            return True
        return "qwen3" in self.qwen_model.lower()


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
