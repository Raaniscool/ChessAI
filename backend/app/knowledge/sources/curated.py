"""Hand-curated source records (sources/data/curated/<category>.json).

For knowledge that doesn't come from a dataset: rules of chess (with the FIDE
Laws of Chess article), classic traps and miniatures from historical games,
and textbook endgame technique. Each record is a candidate in the library
schema *with* a `source` block saying where the knowledge comes from. Curated
records are candidates like any other: the pipeline verifies them (rules,
concept validator, Stockfish) before they enter the library.
"""
from __future__ import annotations

import json
from pathlib import Path

CURATED = Path(__file__).resolve().parent / "data" / "curated"
FILES = ("basics", "checkmates", "endgames", "mistakes")  # openings.json is read by lichess_openings


def candidates(directory: Path | None = None) -> list[dict]:
    out = []
    for name in FILES:
        path = (directory or CURATED) / f"{name}.json"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for rec in json.load(fh):
                rec = dict(rec)
                rec.setdefault("category", name)
                out.append(rec)
    return out
