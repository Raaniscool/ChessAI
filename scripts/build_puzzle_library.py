"""Build backend/app/planner/data/puzzles.json: small, Stockfish-verified puzzle sets per theme.

Source: the Lichess puzzle database (CC0, https://database.lichess.org/#puzzles), as
per-theme samples in PGN-in-CSV form (e.g. github.com/pwenker/chessli2/tree/main/puzzles).
Only the chess content (position, moves, theme tags, Lichess puzzle id) is used.

    python scripts/build_puzzle_library.py CSV_DIR [--per-theme 7] [--depth 16] [--themes a,b]

Every kept puzzle is replayed with python-chess and checked move by move with Stockfish:
at each of the learner's moves the solution must be the engine's best move AND clearly
better than the second-best (so an equally good alternative is never marked wrong).
On a final mating move every mate-in-one is accepted.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402

OUT = ROOT / "backend" / "app" / "planner" / "data" / "puzzles.json"
MAX_LEARNER_MOVES = 3
MAX_CANDIDATES = 40  # per difficulty tier
EASY_COUNT = 3  # one-move puzzles for the "learn the pattern" lesson; the rest are longer
UNIQUE_MARGIN_CP = 150
MATE = 100_000

_SITE = re.compile(r'\[Site "+https?://(?:www\.)?lichess\.org/training/(\w+)"+\]')
_FEN = re.compile(r'\[FEN "+([^"]+)"+\]')
_MOVE_NUMBER = re.compile(r"^\d+\.(\.\.)?$")
_NUMBERED_SAN = re.compile(r"^\d+\.(?:\.\.)?(.+)$")


def parse_pgn_csv(path: Path) -> list[dict]:
    """Records {id, fen, sans, themes}. FEN is before the opponent's setup move."""
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            # Files saved on Windows have \r\n inside the quoted PGN field too.
            pgn = row[0].replace("\r\n", "\n").replace("\r", "\n")
            themes = row[1].split()
            site, fen = _SITE.search(pgn), _FEN.search(pgn)
            if not (site and fen):
                continue
            body = pgn.split("\n\n", 1)[-1]
            sans = []
            for tok in body.split():
                if _MOVE_NUMBER.match(tok) or tok in ("*", "1-0", "0-1", "1/2-1/2"):
                    continue
                m = _NUMBERED_SAN.match(tok)
                sans.append(m.group(1) if m else tok)
            out.append({"id": site.group(1), "fen": fen.group(1), "sans": sans, "themes": themes})
    return out


def to_ucis(fen: str, sans: list[str]) -> list[str] | None:
    board = chess.Board(fen)
    ucis = []
    for san in sans:
        try:
            move = board.parse_san(san)
        except ValueError:
            return None
        ucis.append(move.uci())
        board.push(move)
    return ucis


def simplicity(rec: dict) -> tuple:
    learner_moves = len(rec["ucis"]) // 2
    pieces = len(chess.Board(rec["fen"]).piece_map())
    return (learner_moves, pieces, rec["id"])


def _cp(score: chess.engine.PovScore, pov: chess.Color) -> int:
    return score.pov(pov).score(mate_score=MATE)


def verify(engine: chess.engine.SimpleEngine, rec: dict, depth: int) -> dict | None:
    """Returns the stored puzzle, or None if Stockfish doesn't confirm a unique solution."""
    board = chess.Board(rec["fen"])
    setup, solution = rec["ucis"][0], rec["ucis"][1:]
    board.push_uci(setup)
    learner = board.turn
    final_accepted: list[str] = []
    for i in range(0, len(solution), 2):
        move = chess.Move.from_uci(solution[i])
        final = i + 1 >= len(solution)
        after = board.copy()
        after.push(move)
        if final and after.is_checkmate():
            final_accepted = sorted(board.san(m) for m in board.legal_moves
                                    if _mates(board, m))
        else:
            infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=2)
            if not infos or not infos[0].get("pv") or infos[0]["pv"][0] != move:
                return None
            if len(infos) > 1 and infos[1].get("pv"):
                best, second = infos[0]["score"], infos[1]["score"]
                if best.pov(learner).is_mate():
                    s2 = second.pov(learner)
                    if s2.is_mate() and s2.mate() > 0:
                        return None  # another move also mates: not unique
                elif _cp(best, learner) - _cp(second, learner) < UNIQUE_MARGIN_CP:
                    return None
            if final:
                final_accepted = [board.san(move)]
        board.push(move)
        if not final:
            board.push_uci(solution[i + 1])
    return {
        "id": rec["id"],
        "fen": rec["fen"],
        "moves": rec["ucis"],  # opponent's setup move, then the solution (UCI)
        "final_accepted": final_accepted,
        "mate": board.is_checkmate(),
    }


def _mates(board: chess.Board, move: chess.Move) -> bool:
    board.push(move)
    try:
        return board.is_checkmate()
    finally:
        board.pop()


def build(csv_dir: Path, themes: list[str], per_theme: int, depth: int, existing: dict) -> dict:
    cmd = get_settings().engine_cmd
    if not cmd:
        sys.exit("No engine: run `npm install` or set ENGINE_CMD")
    engine = chess.engine.SimpleEngine.popen_uci(cmd)
    library = dict(existing)
    try:
        for theme in themes:
            path = csv_dir / f"{theme}.csv"
            if not path.exists():
                print(f"!! {theme}: {path} missing", flush=True)
                continue
            started = time.monotonic()
            candidates = []
            for rec in parse_pgn_csv(path):
                ucis = to_ucis(rec["fen"], rec["sans"])
                if not ucis or len(ucis) < 2 or len(ucis) % 2 != 0:
                    continue  # setup + learner moves; a learner move must end the line
                if len(ucis) // 2 > MAX_LEARNER_MOVES:
                    continue
                rec["ucis"] = ucis
                candidates.append(rec)
            candidates.sort(key=simplicity)
            easy = [r for r in candidates if len(r["ucis"]) == 2]
            longer = [r for r in candidates if len(r["ucis"]) > 2]
            seen: set[str] = set()
            tried = 0

            def pick(pool: list[dict], want: int) -> list[dict]:
                nonlocal tried
                got = []
                for rec in pool[:MAX_CANDIDATES]:
                    if len(got) >= want:
                        break
                    board_key = rec["fen"].split(" ")[0]
                    if board_key in seen:
                        continue
                    seen.add(board_key)
                    tried += 1
                    puzzle = verify(engine, rec, depth)
                    if puzzle:
                        got.append(puzzle)
                return got

            kept = pick(easy, EASY_COUNT)
            kept += pick(longer, per_theme - len(kept))
            if len(kept) < per_theme:  # not enough longer ones: top up with easy ones
                kept += pick(easy[EASY_COUNT:], per_theme - len(kept))
            kept.sort(key=lambda p: len(p["moves"]))
            library[theme] = kept
            print(f"{theme:18s} kept {len(kept)}/{tried} tried ({time.monotonic() - started:.0f}s)", flush=True)
    finally:
        engine.quit()
    return library


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_dir", type=Path)
    ap.add_argument("--per-theme", type=int, default=7)
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--themes", default="", help="comma-separated; default: every theme used in topics.json")
    args = ap.parse_args()

    topics = json.loads((OUT.parent / "topics.json").read_text(encoding="utf-8"))["topics"]
    themes = [t for t in args.themes.split(",") if t] or sorted(
        {t["puzzle_theme"] for t in topics if t.get("puzzle_theme")})
    existing = json.loads(OUT.read_text(encoding="utf-8"))["themes"] if OUT.exists() else {}
    library = build(args.csv_dir, themes, args.per_theme, args.depth, existing)
    OUT.write_text(json.dumps({
        "source": "Lichess puzzle database (https://database.lichess.org/#puzzles)",
        "license": "CC0 1.0 (public domain)",
        "verified_with": f"Stockfish, depth {args.depth}, unique best move at every learner move",
        "themes": {k: library[k] for k in sorted(library)},
    }, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
