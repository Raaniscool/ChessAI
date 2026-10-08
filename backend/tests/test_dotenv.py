"""The .env file: settings that survive new PowerShell windows."""
import os

import pytest

from app import config


@pytest.mark.parametrize("encoding, bom", [
    ("utf-8", b""),                 # Set-Content (PowerShell 7) / most editors
    ("utf-8", b"\xef\xbb\xbf"),     # Notepad "UTF-8 with BOM"
    ("utf-16-le", b"\xff\xfe"),     # PowerShell 5: echo ... > .env / Out-File
    ("cp1252", b""),                # PowerShell 5: Set-Content (ANSI)
])
def test_decode_windows_encodings(encoding, bom):
    raw = bom + "QWEN_MODEL=qwen3:4b\r\n".encode(encoding)
    assert config.parse_dotenv(config._decode(raw)) == {"QWEN_MODEL": "qwen3:4b"}


def test_parse_dotenv_syntax():
    text = """
    # comment
    QWEN_MODEL = "qwen3:4b"
    export QWEN_TIMEOUT=300
    QWEN_BASE_URL='http://localhost:1234/v1'
    QWEN_PLANNER=auto   # inline comment
    not a setting
    """
    assert config.parse_dotenv(text) == {
        "QWEN_MODEL": "qwen3:4b",
        "QWEN_TIMEOUT": "300",
        "QWEN_BASE_URL": "http://localhost:1234/v1",
        "QWEN_PLANNER": "auto",
    }


def test_load_dotenv_does_not_override_shell(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("QWEN_MODEL=from-file\nQWEN_TIMEOUT=77\n", encoding="utf-8")
    monkeypatch.setenv("CHESSAI_DOTENV", "1")
    monkeypatch.setenv("QWEN_MODEL", "from-shell")
    monkeypatch.delenv("QWEN_TIMEOUT", raising=False)
    applied = config.load_dotenv(env)
    assert applied == {"QWEN_TIMEOUT": "77"}
    assert os.environ["QWEN_MODEL"] == "from-shell"
    assert os.environ["QWEN_TIMEOUT"] == "77"


def test_load_dotenv_disabled_and_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SOME_TEST_KEY=1\n", encoding="utf-8")
    monkeypatch.delenv("SOME_TEST_KEY", raising=False)
    monkeypatch.setenv("CHESSAI_DOTENV", "0")
    assert config.load_dotenv(env) == {}
    monkeypatch.setenv("CHESSAI_DOTENV", "1")
    assert config.load_dotenv(tmp_path / "missing.env") == {}
    assert "SOME_TEST_KEY" not in os.environ
