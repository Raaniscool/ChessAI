import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests must never write learner data into the repo, and must not depend on
# whether the developer has a local Qwen configured (e.g. via `setx QWEN_MODEL`).
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="chessai-test-data-")
os.environ.pop("QWEN_MODEL", None)
