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


def _ids(text: str) -> set[str]:
    import re
    return set(re.findall(r'\bid="([\w-]+)"', text))


def test_every_element_the_scripts_look_up_exists_in_the_page():
    """A renamed/removed element otherwise only shows up as 'Cannot read properties of null'."""
    import re
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    ids = _ids(html)
    for script in (ROOT / "frontend").glob("*.js"):
        used = set(re.findall(r'getElementById\("([\w-]+)"\)', script.read_text(encoding="utf-8")))
        assert used <= ids, f"{script.name} looks up missing elements: {sorted(used - ids)}"
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    required = re.search(r"REQUIRED_IDS = \[(.*?)\]", app, re.S).group(1)
    assert set(re.findall(r'"([\w-]+)"', required)) <= ids


def test_page_files_are_revalidated_but_api_is_not_touched():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        for path in ("/", "/app.js", "/speech.js", "/styles.css"):
            res = client.get(path)
            assert res.status_code == 200 and res.headers["cache-control"] == "no-cache", path
        assert client.get("/api/topics").headers.get("cache-control") != "no-cache"
