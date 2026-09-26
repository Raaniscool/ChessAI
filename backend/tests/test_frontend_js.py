"""Runs the frontend's JavaScript unit tests (read-aloud text layer) when Node is available."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_speech_js_unit_tests():
    files = sorted(str(p) for p in (ROOT / "frontend" / "tests").glob("*.test.mjs"))
    assert files
    result = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
