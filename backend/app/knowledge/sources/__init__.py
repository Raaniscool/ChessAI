"""Source data → teaching-example candidates.

Source data is not teaching data. A Lichess puzzle, a named opening line or a
historical game is *where knowledge comes from*; a library entry is a teaching
example built from it. Importers here turn source records into candidate
examples (plain dicts in the library schema) that carry their provenance:

    source_type, source_id, source_game, source_url, source_license,
    import_date, reference

Every candidate then goes through the verification pipeline (pipeline.py); only
what passes becomes a library entry. Importers never mark anything verified.

    lichess_puzzles   Lichess puzzle database (CC0) — the verified set already
                      shipped in planner/data/puzzles.json
    lichess_openings  lichess-org/chess-openings (CC0) — named lines, ECO codes
    curated           hand-authored records with references (FIDE Laws of Chess
                      articles, classic games, endgame theory)
"""
from __future__ import annotations

import datetime as _dt

IMPORT_DATE = _dt.date(2026, 9, 25).isoformat()  # date of the current seed import


def provenance(source_type: str, source_id: str, license: str, url: str = "", reference: str = "",
               game: str = "", import_date: str = IMPORT_DATE) -> dict:
    out = {"source_type": source_type, "source_id": source_id, "source_license": license,
           "source_url": url, "reference": reference, "source_game": game, "import_date": import_date}
    return {k: v for k, v in out.items() if v}
