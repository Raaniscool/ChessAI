import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests must never write learner data into the repo, and must not depend on
# whether the developer has a local Qwen configured (e.g. via `setx QWEN_MODEL`).
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="chessai-test-data-")
os.environ["CHESSAI_DOTENV"] = "0"  # ignore the developer's .env (QWEN_MODEL etc.)
os.environ.pop("QWEN_MODEL", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_learner(tmp_path_factory):
    """Every test starts with a brand-new learner, so one test's results never
    change the difficulty another test's lessons are built at."""
    from app.games.quota import AnalysisQuota, set_quota
    from app.learner import LearnerStore, set_store
    set_store(LearnerStore(tmp_path_factory.mktemp("learners")))
    # and a fresh game-analysis allowance (the data dir is shared by the whole session)
    # (unlimited here; test_analysis_quota.py turns limits on)
    set_quota(AnalysisQuota(tmp_path_factory.mktemp("quota") / "quota.json", enabled=False))
    # engine data of generated puzzles: per test (the bundled library data is shared, read-only)
    from app.puzzles.profile import EngineProfiles, set_engine_profiles
    set_engine_profiles(EngineProfiles(runtime=tmp_path_factory.mktemp("profiles") / "engine_profiles.json"))
    yield
    set_store(None)
    set_quota(None)
    set_engine_profiles(None)
