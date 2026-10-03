"""Pre-filter Lichess puzzle CSVs with the library's own concept validators.

Lichess theme tags are noisy and some standard ideas (x-ray, Greek gift, windmill, desperado,
perpetual check, ...) have no tag at all. Instead of loosening a validator, this script keeps only
the CSV rows whose solution line passes the concept's rules-only validator, writing one CSV per
output theme. Those files are then verified with Stockfish by build_puzzle_library.py like every
other pool (pool_finish.json), and the library pipeline checks every candidate again.

    python scripts/prefilter_lichess_csv.py SRC_DIR DST_DIR [theme ...]

SRC_DIR holds Lichess theme CSVs in the format read by build_puzzle_library.py (the CC0
`puzzles/<theme>.csv` files of github.com/pwenker/chessli2). With theme names, only those output
themes are written.
"""
from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_puzzle_library import MAX_LEARNER_MOVES, _SITE, parse_pgn_csv, to_ucis  # noqa: E402
from backend.app.knowledge import validators  # noqa: E402
from backend.app.knowledge.positions import replay  # noqa: E402

TACTIC, MISTAKE = "tactic", "mistake"  # mistake: the setup move is the mistake, the solution punishes it


def jobs(all_themes: list[str]) -> dict[str, list[tuple[str, str, dict, str]]]:
    """output theme -> [(source CSV theme, validator, params, mode)]"""
    every = lambda kind, params=None, mode=TACTIC: [(t, kind, params or {}, mode) for t in all_themes]  # noqa: E731
    return {
        "xRay": [("xRayAttack", "x_ray", {}, TACTIC)],
        "doubleBishopMate": [("doubleBishopMate", "double_bishop_mate", {}, TACTIC)],
        "epauletteMate": every("epaulette_mate"),
        "greekGift": every("greek_gift"),
        "windmill": every("windmill"),
        "desperado": [(t, "desperado", {}, TACTIC) for t in ("intermezzo", "sacrifice", "crushing", "advantage")],
        "zugzwangQuiet": [("zugzwang", "zugzwang", {}, TACTIC)],
        "perpetual": [(t, "perpetual_check", {}, TACTIC) for t in ("equality", "defensiveMove", "endgame", "queenEndgame")],
        "stalemateTrick": every("stalemate_trick"),
        # a threat parried in an attacking puzzle is incidental: defensive and level themes only
        "threatDefence": [(t, "parries_threat", {}, TACTIC)
                          for t in ("defensiveMove", "equality", "endgame", "rookEndgame", "queenEndgame")],
        "doubleAttack": [(t, "double_attack", {}, TACTIC) for t in ("fork", "discoveredAttack", "advantage")],
        "pawnFork": every("fork", {"piece": "pawn"}),
        "relativePin": [("pin", "pin", {"kind": "relative"}, TACTIC)],
        "hangingQueen": [(t, "hung_piece", {"piece": "queen"}, MISTAKE)
                         for t in ("hangingPiece", "fork", "crushing", "opening")],
        "poisonedPawn": [(t, "poisoned_pawn", {}, MISTAKE)
                         for t in ("opening", "crushing", "advantage", "trappedPiece", "hangingPiece")],
    }


# subsets by Lichess's own tags, written next to their parent: zugzwangs that win (the engine
# profile asks for a clear win; drawing zugzwangs are refused anyway), and checks that save a level
# position (not a mating attack).
TAG_SUBSETS = {
    "zugzwangWin": ("zugzwangQuiet", lambda tags: bool(tags & {"crushing", "advantage", "mate"})),
    "perpetualDraw": ("perpetual", lambda tags: "equality" in tags and not any(t.startswith("mate") for t in tags)),
}


def load(src: Path, theme: str, cache: dict) -> list:
    if theme in cache:
        return cache[theme]
    path = src / f"{theme}.csv"
    out = []
    if path.exists():
        rows = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                m = len(row) >= 2 and _SITE.search(row[0].replace("\r\n", "\n"))
                if m:
                    rows.setdefault(m.group(1), row)
        for rec in parse_pgn_csv(path):
            ucis = to_ucis(rec["fen"], rec["sans"])
            if not ucis or len(ucis) % 2 or len(ucis) // 2 > MAX_LEARNER_MOVES:
                continue
            try:
                out.append((rows[rec["id"]], rec["id"], replay(rec["fen"], rec["sans"])))
            except Exception:
                continue  # unreadable row: skipped, never repaired
    cache[theme] = out
    return out


def shows(rep, kind: str, params: dict, mode: str) -> bool:
    ctx = validators.Ctx(rep, key_ply=1, mistake_ply=0 if mode == MISTAKE else None)
    try:
        validators.run(kind, ctx, params)
    except validators.Fail:
        return False
    if kind == "double_attack":  # two pieces attacking, not a one-piece fork (that is the fork concept)
        try:
            validators.run("fork", validators.Ctx(rep, key_ply=1), {})
            return False
        except validators.Fail:
            pass
    return True


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src, dst, only = Path(sys.argv[1]), Path(sys.argv[2]), set(sys.argv[3:])
    dst.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    for key, specs in jobs(sorted(p.stem for p in src.glob("*.csv"))).items():
        if only and key not in only:
            continue
        keep, seen, hits = [], set(), collections.Counter()
        for theme, kind, params, mode in specs:
            for row, pid, rep in load(src, theme, cache):
                if pid not in seen and shows(rep, kind, params, mode):
                    seen.add(pid)
                    keep.append(row)
                    hits[theme] += 1
        with open(dst / f"{key}.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(keep)
        print(f"{key:18s} {len(keep):5d}  {dict(hits)}", flush=True)
        for sub, (parent, wanted) in TAG_SUBSETS.items():
            if parent == key:
                rows = [row for row in keep if wanted(set(row[1].split()))]
                with open(dst / f"{sub}.csv", "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerows(rows)
                print(f"{sub:18s} {len(rows):5d}  (subset of {key})", flush=True)


if __name__ == "__main__":
    main()
