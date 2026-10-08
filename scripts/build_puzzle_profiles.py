"""Precompute Stockfish data for every Puzzle Library puzzle (backend/app/puzzles/data/engine_profiles.json).

For each learner move of each verified puzzle line: the engine's top 5 moves with scores
(multipv 5). puzzles.profile uses it to tell real decisions from forced/obvious moves, trim the
line at its objective and rate difficulty from the decisions. The file holds raw engine output
only, so the classification rules can change without re-running Stockfish.

    python scripts/build_puzzle_profiles.py            # missing or stale entries only
    python scripts/build_puzzle_profiles.py --all      # recompute everything
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.engine.service import get_engine, shutdown_engine  # noqa: E402
from app.knowledge.library import get_knowledge  # noqa: E402
from app.puzzles.model import NOT_PUZZLES  # noqa: E402
from app.puzzles.profile import BUNDLED, engine_data, signature  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--depth", type=int, default=12)
    args = ap.parse_args()
    current = json.loads(BUNDLED.read_text(encoding="utf-8")) if BUNDLED.exists() else {}
    knowledge = get_knowledge()
    engine = get_engine()
    todo = []
    for e in knowledge.entries.values():
        if e.tier != "global" or e.status != "verified" or not e.key_move or e.category in NOT_PUZZLES:
            continue
        sig = signature(e.start_fen, e.moves, e.key_ply)
        if args.all or (current.get(e.id) or {}).get("sig") != sig:
            todo.append((e, sig))
    known = {e.id for e in knowledge.entries.values() if e.tier == "global"}
    current = {k: v for k, v in current.items() if k in known}
    start = time.time()
    for i, (e, sig) in enumerate(todo, start=1):
        current[e.id] = {"sig": sig, "plies": engine_data(e.start_fen, list(e.moves), e.key_ply, engine, args.depth)}
        if i % 25 == 0 or i == len(todo):
            BUNDLED.write_text(json.dumps(current, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            print(f"{i}/{len(todo)} ({time.time() - start:.0f}s)", flush=True)
    BUNDLED.write_text(json.dumps(current, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    print(f"DONE {len(current)} puzzles profiled", flush=True)
    shutdown_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
