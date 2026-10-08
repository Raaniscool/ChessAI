"""Build the global (seed) Knowledge Library from reliable sources.

    python scripts/build_knowledge_seed.py [--depth 14] [--puzzles-per-concept 3] [--dry-run]

    # (re)build only the difficulty tiers on top of the library as it is (nothing else is touched):
    python scripts/build_knowledge_seed.py --tiers [--starter-per-concept 3] [--harder-per-concept 5]

Sources (never Qwen):
  * curated records    backend/app/knowledge/sources/data/curated/{basics,checkmates,endgames,mistakes}.json
                       rules (FIDE Laws), classic traps and textbook endgames, each with provenance
  * opening database   lichess-org/chess-openings (CC0) + curated teaching text (curated/openings.json)
  * Lichess puzzles    real game positions (CC0) from planner/data/puzzles.json, as tactics and
                       as mistakes ("walked into a fork", "hung a piece")
  * starter tier       simple positions built by the app's own generator (constructors only, fixed
                       seed, no Qwen) for tactics whose easiest real-game puzzle is still too hard
                       for a beginner -> examples/<category>/procedural.json
  * harder tier        longer / combination Lichess puzzles (sources/data/lichess_puzzles/
                       pool_harder.json) -> examples/<category>/lichess_harder.json, so a strong
                       learner isn't limited to the easiest positions of each idea
  Both tiers stay inside the per-concept balance cap; the starter tier is filled first.

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
    ap.add_argument("--starter-per-concept", type=int, default=3)
    ap.add_argument("--tiers", action="store_true",
                    help="rebuild only the starter + harder tiers against the current library")
    ap.add_argument("--expand", action="store_true",
                    help="rebuild only the item-9 expansion (new tactics, mates, endgames, mistakes)")
    ap.add_argument("--expand-per-concept", type=int, default=6)
    ap.add_argument("--deepen", choices=("middlegame", "opening", "both"),
                    help="verify more real-game positions whose key position is in this phase")
    ap.add_argument("--deepen-per-concept", type=int, default=10,
                    help="candidates proposed per concept by --deepen (MAX_PER_CONCEPT still applies)")
    args = ap.parse_args()
    if args.tiers:
        return add_tiers(args)
    if args.expand:
        return add_expansion(args)
    if args.deepen:
        return add_deepen(args)

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
    starter_stage(library, engine, run, args.starter_per_concept, args.depth)
    harder_stage(library, run, args.harder_per_concept)
    return results


# Balance: no idea dominates the library (tests/test_knowledge_engine.py). Raised from 15 when the
# middlegame was deepened: the cap is what kept every common tactic at its first ~15 (mostly short
# endgame) positions, so the middlegame pool stayed thin. At 24 no concept is over ~4% of the
# library and the guard still catches a runaway build.
MAX_PER_CONCEPT = 24
# tactics the generator can build simple positions for; a starter is added only when the easiest
# verified example of the concept is rated above STARTER_ABOVE (i.e. too hard for a beginner)
STARTER_CONCEPTS = ("knight_fork", "queen_fork", "pawn_fork", "skewer", "absolute_pin", "hanging_piece",
                    "back_rank_mate")
STARTER_ABOVE = 850
STARTER_MAX_RATING = 950  # a starter position must itself be easy
STARTER_SEED = 20260927


def starter_stage(library, engine, run, per_concept: int, depth: int) -> None:
    """Easy constructed positions, each through the full pipeline via run() (fixed seed: reproducible)."""
    import random
    from app.knowledge.difficulty import puzzle_rating
    from app.knowledge.generation.generator import PLANS, Rejected, _constructed, _source, evaluate

    for concept in STARTER_CONCEPTS:
        existing = [e for e in library.examples_for(concept) if e.key_ply is not None and e.concept == concept]
        total = sum(1 for e in library.verified() if e.concept == concept)
        if existing and min(puzzle_rating(e) for e in existing) <= STARTER_ABOVE:
            print(f"starter      {concept:18s} not needed (easiest {min(puzzle_rating(e) for e in existing)})")
            continue
        constructor = next(c for t, c in PLANS[concept] if t == concept)
        rng = random.Random(f"{STARTER_SEED}:{concept}")
        kept = tries = 0
        while kept < min(per_concept, MAX_PER_CONCEPT - total) and tries < 60:
            tries += 1
            proposal = _constructed(constructor, rng, rng.choice(["white", "black"]))
            if proposal is None:
                continue
            source = _source("procedural", proposal, None)
            source["import_date"] = IMPORT_DATE
            try:  # evaluate(): idea check + Stockfish discrimination + line, then the pipeline
                report = evaluate(proposal, concept, library, engine, depth=depth, tier="global", source=source,
                                  method="seed_builder:procedural")
            except Rejected as exc:
                print(f"rejected      procedural {concept}: {exc.stage}: {exc.reason[:120]}")
                continue
            if report.status != "verified" or puzzle_rating(report.example) > STARTER_MAX_RATING:
                continue
            record = report.example.to_record()
            record["tags"] = list(dict.fromkeys([*record.get("tags", []), "starter"]))
            if run(record, "procedural") == "verified":
                kept += 1
                total += 1


def harder_stage(library, run, per_concept: int) -> None:
    """The harder tier, after everything else so the easier entries win duplicate checks."""
    kept: Counter = Counter()
    total = Counter(ex.concept for ex in library.verified())
    for cand in _hardest_first(library, lichess_puzzles.candidates(library, per_concept=1000,
                                                                    path=lichess_puzzles.POOL_HARDER)):
        if kept[cand["concept"]] >= per_concept or total[cand["concept"]] >= MAX_PER_CONCEPT:
            continue
        cand["tags"] = list(dict.fromkeys([*cand.get("tags", []), "harder"]))
        if run(cand, "lichess_harder") == "verified":
            kept[cand["concept"]] += 1
            total[cand["concept"]] += 1


TIER_FILES = ("procedural.json", "lichess_harder.json")


def _hardest_first(library, cands: list[dict]) -> list[dict]:
    """Slots under the balance cap are scarce: spend them on the most demanding positions."""
    from app.knowledge.difficulty import puzzle_rating
    from app.knowledge.schema import KnowledgeError, parse_example

    def rating(cand: dict) -> int:
        try:
            return puzzle_rating(parse_example({**cand, "status": "candidate"}, library.concepts, "tier",
                                               tier="global"))
        except (KnowledgeError, ValueError, KeyError):
            return 0
    return sorted(cands, key=lambda c: (-rating(c), c["id"]))


def add_tiers(args) -> int:
    """Verify the starter + harder tiers against the library as it is and write only their files."""
    examples = KNOWLEDGE / "examples"
    old_files = [p for name in TIER_FILES for p in examples.rglob(name)]
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
            results.append({"status": rep.status, "record": record, "reasons": rep.reasons, "id": cand.get("id"),
                            "importer": importer})
            print(f"{rep.status:13s} {cand.get('id', '?'):40s} {time.time() - t:5.1f}s", flush=True)
            for reason in rep.reasons:
                print(f"      - {reason[:200]}", flush=True)
            return rep.status

        try:
            starter_stage(library, engine, run, args.starter_per_concept, args.depth)
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
    for r in results:
        if r["status"] == "verified":
            by_file[examples / r["record"]["category"] / f"{r['importer']}.json"].append(r["record"])
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
    for name, importer in (("starter_tier", "procedural"), ("harder_tier", "lichess_harder")):
        mine = [r for r in results if r["importer"] == importer]
        report[name] = {
            "totals": dict(Counter(r["status"] for r in mine)),
            "by_concept": dict(sorted(Counter(r["record"]["concept"] for r in mine
                                              if r["status"] == "verified").items())),
            "not_verified": [{"id": r["id"], "status": r["status"], "reasons": r["reasons"]}
                             for r in mine if r["status"] != "verified"],
        }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


EXPANSION_FILES = ("lichess_expansion.json", "curated_expansion.json")


def add_expansion(args) -> int:
    """Library expansion (decoys, clearance, interference, underpromotion, mate in two, hook and
    dovetail mates, pawn-ending technique, rook-ending defence, real-game king-safety and
    back-rank mistakes, avoiding stalemate). Verifies the candidates against the library as it is
    — every stage of the pipeline, nothing relaxed — and writes only the expansion's own files."""
    examples = KNOWLEDGE / "examples"
    old_files = [p for name in EXPANSION_FILES for p in examples.rglob(name)]
    with tempfile.TemporaryDirectory() as tmp:  # the library without any previous expansion
        shutil.copy(KNOWLEDGE / "concepts.json", Path(tmp) / "concepts.json")
        for path in examples.rglob("*.json"):
            if path not in old_files:
                target = Path(tmp) / "examples" / path.relative_to(examples)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        library = KnowledgeLibrary(data_dir=Path(tmp), load_runtime=False)
        # puzzle ids already in the library (as tactics or mistakes) are not imported twice
        used = {(e.source or {}).get("source_id") for e in library.verified()}
        results: list[dict] = []
        engine = get_engine()

        def run(cand: dict, importer: str) -> str:
            t = time.time()
            rep = verify_candidate(cand, library, engine=engine, depth=args.depth, tier="global",
                                   method="seed_builder")
            record = rep.example.to_record() if rep.example is not None else cand
            if rep.status == "verified":
                record["verification"]["verified_at"] = lichess_puzzles.EXPANSION_DATE
                library.add(rep.example)
            results.append({"status": rep.status, "record": record, "reasons": rep.reasons, "id": cand.get("id"),
                            "importer": importer})
            print(f"{rep.status:13s} {cand.get('id', '?'):40s} {time.time() - t:5.1f}s", flush=True)
            for reason in rep.reasons:
                print(f"      - {reason[:200]}", flush=True)
            return rep.status

        try:
            for cand in curated.candidates(files=curated.EXPANSION_FILES):
                run(cand, "curated_expansion")
            per = args.expand_per_concept
            for cand in lichess_puzzles.expansion_candidates(library, per_concept=per, exclude=used):
                run(cand, "lichess_expansion")
            for cand in lichess_puzzles.expansion_mistake_candidates(library, per_concept=per, exclude=used):
                run(cand, "lichess_expansion")
        finally:
            engine.close()
        everything = library.verified()
    verified = [r["record"] for r in results if r["status"] == "verified"]
    print(f"\n{dict(Counter(r['status'] for r in results))}  by concept: "
          f"{dict(sorted(Counter(r['concept'] for r in verified).items()))}")
    if args.dry_run:
        return 0
    by_file: dict[Path, list[dict]] = defaultdict(list)
    for r in results:
        if r["status"] == "verified":
            by_file[examples / r["record"]["category"] / f"{r['importer']}.json"].append(r["record"])
    for old in old_files:
        old.unlink()
    for path, records in sorted(by_file.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        records.sort(key=lambda rec: (rec["concept"], rec["difficulty"], rec["id"]))
        path.write_text(json.dumps(records, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(records):3d} -> {path.relative_to(ROOT)}")
    report_path = KNOWLEDGE / "seed_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["totals"]["verified"] = len(everything)
    report["by_category"] = dict(Counter(e.category for e in everything))
    report["by_concept"] = dict(sorted(Counter(e.concept for e in everything).items()))
    report["expansion"] = {
        "import_date": lichess_puzzles.EXPANSION_DATE,
        "totals": dict(Counter(r["status"] for r in results)),
        "by_concept": dict(sorted(Counter(r["record"]["concept"] for r in results
                                          if r["status"] == "verified").items())),
        "not_verified": [{"id": r["id"], "concept": r["record"].get("concept"), "status": r["status"],
                          "reasons": r["reasons"]} for r in results if r["status"] != "verified"],
    }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


DEEPEN_FILES = ("lichess_midgame.json", "lichess_opening.json")
DEEPEN_IMPORTER = {"middlegame": "lichess_midgame", "opening": "lichess_opening"}


def add_deepen(args) -> int:
    """Verify more real-game positions whose key position is in a thin phase (--deepen).

    Training's position pool *is* the puzzle library, and every importer above spends its
    per-concept budget on the shortest solutions — which are mostly endgame positions. The
    middlegame pool stayed around 140 positions against nearly 300 endgame ones, while ~190
    verified-able Lichess positions out of the same local pools were never imported. This stage
    imports them: same candidate sources, same verification pipeline, same per-concept cap
    (MAX_PER_CONCEPT); only the order of the candidates changes, so the thin phase and the thin
    concepts go first. Nothing is relaxed and nothing is invented.
    """
    phases = ("middlegame", "opening") if args.deepen == "both" else (args.deepen,)
    examples = KNOWLEDGE / "examples"
    old_files = [p for name in DEEPEN_FILES for p in examples.rglob(name)]
    with tempfile.TemporaryDirectory() as tmp:  # the library without any previous deepening
        shutil.copy(KNOWLEDGE / "concepts.json", Path(tmp) / "concepts.json")
        for path in examples.rglob("*.json"):
            if path not in old_files:
                target = Path(tmp) / "examples" / path.relative_to(examples)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        library = KnowledgeLibrary(data_dir=Path(tmp), load_runtime=False)
        used = {(e.source or {}).get("source_id") for e in library.verified()}
        total: Counter = Counter(e.concept for e in library.verified())
        results: list[dict] = []
        engine = get_engine()

        def run(cand: dict, importer: str) -> str:
            t = time.time()
            rep = verify_candidate(cand, library, engine=engine, depth=args.depth, tier="global",
                                   method="seed_builder")
            record = rep.example.to_record() if rep.example is not None else cand
            if rep.status == "verified":
                record["verification"]["verified_at"] = lichess_puzzles.DEEPENING_DATE
                library.add(rep.example)
            results.append({"status": rep.status, "record": record, "reasons": rep.reasons, "id": cand.get("id"),
                            "importer": importer, "phase": None})
            print(f"{rep.status:13s} {cand.get('id', '?'):40s} {time.time() - t:5.1f}s", flush=True)
            for reason in rep.reasons:
                print(f"      - {reason[:200]}", flush=True)
            return rep.status

        try:
            for phase in phases:
                importer = DEEPEN_IMPORTER[phase]
                kept = 0
                for cand in lichess_puzzles.deepening_candidates(library, phase, per_concept=args.deepen_per_concept,
                                                                 exclude=used):
                    if total[cand["concept"]] >= MAX_PER_CONCEPT:
                        continue
                    cand["tags"] = list(dict.fromkeys([*cand.get("tags", []), "deepening", phase]))
                    status = run(cand, importer)
                    used.add(cand["source"]["source_id"])
                    if status == "verified":
                        total[cand["concept"]] += 1
                        kept += 1
                print(f"\n{phase}: {kept} verified positions added", flush=True)
        finally:
            engine.close()
        everything = library.verified()
    verified = [r["record"] for r in results if r["status"] == "verified"]
    print(f"\n{dict(Counter(r['status'] for r in results))}  by concept: "
          f"{dict(sorted(Counter(r['concept'] for r in verified).items()))}")
    if args.dry_run:
        return 0
    by_file: dict[Path, list[dict]] = defaultdict(list)
    for r in results:
        if r["status"] == "verified":
            by_file[examples / r["record"]["category"] / f"{r['importer']}.json"].append(r["record"])
    for old in old_files:
        old.unlink()
    for path, records in sorted(by_file.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        records.sort(key=lambda rec: (rec["concept"], rec["difficulty"], rec["id"]))
        path.write_text(json.dumps(records, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(records):3d} -> {path.relative_to(ROOT)}")
    report_path = KNOWLEDGE / "seed_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["totals"]["verified"] = len(everything)
    report["by_category"] = dict(Counter(e.category for e in everything))
    report["by_concept"] = dict(sorted(Counter(e.concept for e in everything).items()))
    report["deepening"] = {
        "import_date": lichess_puzzles.DEEPENING_DATE,
        "phases": list(phases),
        "totals": dict(Counter(r["status"] for r in results)),
        "by_concept": dict(sorted(Counter(r["record"]["concept"] for r in results
                                          if r["status"] == "verified").items())),
        "not_verified": [{"id": r["id"], "concept": r["record"].get("concept"), "status": r["status"],
                          "reasons": r["reasons"]} for r in results if r["status"] != "verified"],
    }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
