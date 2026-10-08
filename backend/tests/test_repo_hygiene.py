"""Guard against bundled data being silently left out of the repository.

A `.gitignore` rule once matched backend/app/planner/data/ — the catalog
existed locally (so every test passed) but was never pushed, and every plan
request crashed on a fresh clone. These checks fail loudly instead.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUNDLED_DATA = [
    *sorted((REPO / "backend" / "app" / "planner" / "data").glob("*.json")),
    *sorted((REPO / "backend" / "app" / "lessons" / "data").rglob("*.json")),
]


def test_bundled_data_files_exist():
    assert (REPO / "backend" / "app" / "planner" / "data" / "topics.json").is_file()
    assert BUNDLED_DATA


@pytest.mark.skipif(not shutil.which("git") or not (REPO / ".git").exists(), reason="not a git checkout")
@pytest.mark.parametrize("path", BUNDLED_DATA, ids=[p.name for p in BUNDLED_DATA])
def test_bundled_data_is_not_gitignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "-v", str(path.relative_to(REPO))],
        cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 1, f"{path} is git-ignored by: {result.stdout.strip()}"
