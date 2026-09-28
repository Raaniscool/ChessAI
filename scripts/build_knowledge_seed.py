"""Build the global (seed) Knowledge Library from reliable sources.

    python scripts/build_knowledge_seed.py [--depth 14] [--puzzles-per-concept 3] [--dry-run]

    # add only the harder tier to the library as it is (nothing else is rebuilt or touched):
    python scripts/build_knowledge_seed.py --only-harder [--harder-per-concept 5]

Sources (never Qwen):
  * curated records    backend/app/knowledge/sources/data/curated/{basics,checkmates,endgames,mistakes}.json
                       rules (FIDE Laws), classic traps and textbook endgames, each with provenance
  * opening database   lichess-org/chess-openings (CC0) + curated teaching text (curated/openings.json)
  * Lichess puzzles    real game positions (CC0) from planner/data/puzzles.json, as tactics and
                       as mistakes ("walked into a fork", "hung a piece")
  * harder tier        longer / combination Lichess puzzles (sources/data/lichess_puzzles/
                       pool_harder.json) -> examples/<category>/lichess_harder.json, so a strong
                       learner isn't limited to the easiest positions of each idea

Every candidate runs through the full verification pipeline (rules, concept validator,
Stockfish, solution, explanation consistency, duplicates) against the entries accepted so
far. Only `verified` entries are written to knowledge/data/examples/<category>/<source>.json;
`needs_review` and `rejected` candidates go to knowledge/data/seed_report.json with their
reasons, so a reviewer can fix the source record or approve it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app.engine.service import get_engine  # noqa: E402
from app.knowledge.library import KnowledgeLibrary  # noqa: E402
from app.knowledge.pipeline import verify_candidate  # noqa: E402
from app.knowledge.sources import IMPORT_DATE, curated, lichess_openings, lichess_puzzles  # noqa: E402

KNOWLEDGE = ROOT / "backend" / "app" / "knowledge" / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--puzzles-per-concept", type=int, default=3)
    ap.add_argument("--mistakes-per-concept", type=int, default=3)
    ap.add_argument("--harder-per-concept", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="verify and report, write nothing")
    ap.add_argument("--only-harder", action="store_true",
                    help="verify the harder tier against the current library and write only lichess_harder.json")
    args = ap.parse_args()
    if args.only_harder:
        return add_harder(args)

    with tempfile.TemporaryDirectory() as tmp:  # an empty library with the real concept graph
        shutil.copy(KNOWLEDGE / "concepts.json", Path(tmp) / "concepts.json")
        library = KnowledgeLibrary(data_dir=Path(tmp), load_runtime=False)
        engine = get_engine()
        try:
            results = build(library, engine, args)
        finally:
            engine.close()

    verified = [r for r in results if r["status"] == "verified"]
    by_file: dict[Path, list[dict]] = defaultdict(list)
    for r in verified:
        rec = r["record"]
        by_file[KNOWLEDGE / "examples" / rec["category"] / f"{r['importer']}.json"].append(rec)
    report = {
        "import_date": IMPORT_DATE, "depth": args.depth,
        "totals": dict(Counter(r["status"] for r in results)),
        "by_category": dict(Counter(r["record"]["category"] for r in verified)),
        "by_concept": dict(sorted(Counter(r["record"]["concept"] for r in verified).items())),
        "needs_review": [{"id": r["id"], "reasons": r["reasons"], "record": r["record"]}
                         for r in results if r["status"] == "needs_review"],
        "rejected": [{"id": r["id"], "source": r["source_id"], "reasons": r["reasons"]}
                     for r in results if r["status"] == "rejected"],
    }
    print(f"\n{report['totals']}  by category: {report['by_category']}")
    if args.dry_run:
        return 0
    examples = KNOWLEDGE / "examples"
    for old in examples.rglob("*.json"):
        old.unlink()
    for path, records in sorted(by_file.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        records.sort(key=lambda rec: (rec["concept"], rec["difficulty"], rec["id"]))
        path.write_text(json.dumps(records, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(records):3d} -> {path.relative_to(ROOT)}")
    (KNOWLEDGE / "seed_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                                                encoding="utf-8")
    print(f"report -> {(KNOWLEDGE / 'seed_report.json').relative_to(ROOT)}")
    return 0


def build(library: KnowledgeLibrary, engine, args) -> list[dict]:
    results: list[dict] = []

    def run(cand: dict, importer: str) -> str:
        t = time.time()
        rep = verify_candidate(cand, library, engine=engine, depth=args.depth, tier="global",
                               method="seed_builder")
        record = rep.example.to_record() if rep.example is not None else cand
        if rep.status == "verified":
            record["verification"]["verified_at"] = IMPORT_DATE  # stable output across rebuilds
            library.add(rep.example)
        src = cand.get("source") or {}
        results.append({"id": cand.get("id"), "status": rep.status, "reasons": rep.reasons, "record": record,
                        "importer": importer, "source_id": src.get("source_id")})
        print(f"{rep.status:13s} {cand.get('id', '?'):40s} {time.time() - t:5.1f}s", flush=True)
        for reason in rep.reasons:
            print(f"      - {reason[:200]}", flush=True)
        return rep.status

    for cand in curated.candidates():
        run(cand, "curated")
    for cand in lichess_openings.candidates():
        run(cand, "lichess_openings")

    kept: Counter = Counter()
    used_puzzles: set[str] = set()
    for cand in lichess_puzzles.candidates(library, per_concept=1000):
        if kept[cand["concept"]] >= args.puzzles_per_concept:
            continue
        status = run(cand, "lichess_puzzles")
        if status != "rejected":  # a position rejected as a tactic may still teach a mistake
            used_puzzles.add(cand["source"]["source_id"])
        if status == "verified":
            kept[cand["concept"]] += 1

    for cand in lichess_puzzles.mistake_candidates(library, per_concept=args.mistakes_per_concept,
                                                   exclude=used_puzzles):
        run(cand, "lichess_puzzles")
    harder_stage(library, run, args.harder_per_concept)
    return results


MAX_PER_CONCEPT = 15  # balance: no idea dominates the library (tests/test_knowledge_engine.py)


def harder_stage(library, run, per_concept: int) -> None:
    """The harder tier, after everything else so the easier entries win duplicate checks."""
    kept: Counter = Counter()
    total = Counter(ex.concept for ex in library.verified())
    for cand in lichess_puzzles.candidates(library, per_concept=1000, path=lichess_puzzles.POOL_HARDER):
        if kept[cand["concept"]] >= per_concept or total[cand["concept"]] >= MAX_PER_CONCEPT:
            continue
        cand["tags"] = list(dict.fromkeys([*cand.get("tags", []), "harder"]))
        if run(cand, "lichess_harder") == "verified":
            kept[cand["concept"]] += 1
            total[cand["concept"]] += 1


def add_harder(args) -> int:
    """Verify the harder tier against the library as it is and write only its own files."""
    examples = KNOWLEDGE / "examples"
    old_files = list(examples.rglob("lichess_harder.json"))
    with tempfile.TemporaryDirectory() as tmp:  # the library without any previous harder tier
        shutil.copy(KNOWLEDGE / "concepts.json", Path(tmp) / "concepts.json")
        for path in examples.rglob("*.json"):
            if path not in old_files:
                target = Path(tmp) / "examples" / path.relative_to(examples)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        library = KnowledgeLibrary(data_dir=Path(tmp), load_runtime=False)
        results: list[dict] = []
        engine = get_engine()

        def run(cand: dict, importer: str) -> str:
            t = time.time()
            rep = verify_candidate(cand, library, engine=engine, depth=args.depth, tier="global",
                                   method="seed_builder")
            record = rep.example.to_record() if rep.example is not None else cand
            if rep.status == "verified":
                record["verification"]["verified_at"] = IMPORT_DATE
                library.add(rep.example)
            results.append({"status": rep.status, "record": record, "reasons": rep.reasons, "id": cand.get("id")})
            print(f"{rep.status:13s} {cand.get('id', '?'):40s} {time.time() - t:5.1f}s", flush=True)
            for reason in rep.reasons:
                print(f"      - {reason[:200]}", flush=True)
            return rep.status

        try:
            harder_stage(library, run, args.harder_per_concept)
        finally:
            engine.close()
        everything = library.verified()
    verified = [r["record"] for r in results if r["status"] == "verified"]
    print(f"\n{dict(Counter(r['status'] for r in results))}  by concept: "
          f"{dict(sorted(Counter(r['concept'] for r in verified).items()))}")
    if args.dry_run:
        return 0
    by_file: dict[Path, list[dict]] = defaultdict(list)
    for rec in verified:
        by_file[examples / rec["category"] / "lichess_harder.json"].append(rec)
    for old in old_files:
        old.unlink()
    for path, records in sorted(by_file.items()):
        records.sort(key=lambda rec: (rec["concept"], rec["difficulty"], rec["id"]))
        path.write_text(json.dumps(records, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(records):3d} -> {path.relative_to(ROOT)}")
    report_path = KNOWLEDGE / "seed_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # the headline numbers describe the whole library; the tier's own outcome is kept separately
    report["totals"]["verified"] = len(everything)
    report["by_category"] = dict(Counter(e.category for e in everything))
    report["by_concept"] = dict(sorted(Counter(e.concept for e in everything).items()))
    report["harder_tier"] = {
        "totals": dict(Counter(r["status"] for r in results)),
        "by_concept": dict(sorted(Counter(rec["concept"] for rec in verified).items())),
        "not_verified": [{"id": r["id"], "status": r["status"], "reasons": r["reasons"]}
                         for r in results if r["status"] != "verified"],
    }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
