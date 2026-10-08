"""Build the verified opening trees (backend/app/knowledge/data/opening_trees/).

    python scripts/build_opening_trees.py                 # all openings in the curated spec
    python scripts/build_opening_trees.py --only sicilian_defense,london_system

Where everything comes from:
  moves      the Lichess opening database (CC0, knowledge/sources/data/lichess_openings): an opening's
             tree is every database line whose name belongs to it (so transpositions are included).
             Openings with fewer than --min-positions database positions get their named lines
             continued by Stockfish's best moves (at most --max-extension plies per line, marked
             "engine" — never presented as named theory).
  text       knowledge/sources/data/curated/opening_trees.json: main line choice, plans, traps, games.
  verdicts   Stockfish (the repo engine, fixed depth, fresh hash per position): every position gets an
             evaluation; every move a loss and a verdict — sound (<= 120 cp, the library's opening
             threshold), dubious (<= 250 cp, gambits) or mistake. Only sound moves are ever taught as
             the learner's moves.
  traps      replayed by python-chess; the victim's mistake must lose >= 150 cp, every move of the
             side springing the trap must be sound (<= 80 cp) and the trap must end with a real gain
             (mate, or >= +150 cp for the trapper). Otherwise the trap is left out.
  games      replayed by python-chess; a game is kept only if it reaches the tree, and its recorded
             result agrees with the final position (checkmate for the winner, or Stockfish >= +200 cp).

Evaluations are cached by position in knowledge/sources/data/opening_evals.json, so the build can
resume after an interruption and a rebuild without changes produces identical files.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.engine.classification import Score  # noqa: E402
from backend.app.knowledge.sources.lichess_openings import LICENSE, TSV_DIR, UPSTREAM, UPSTREAM_COMMIT  # noqa: E402

SPEC = ROOT / "backend/app/knowledge/sources/data/curated/opening_trees.json"
OUT = ROOT / "backend/app/knowledge/data/opening_trees"
CACHE = ROOT / "backend/app/knowledge/sources/data/opening_evals.json"
CATALOG = ROOT / "backend/app/planner/data/topics.json"

SOUND_CP, DUBIOUS_CP = 120, 250          # the library's opening profile (engine_check._opening)
TRAP_MISTAKE_CP, TRAP_MOVE_CP, TRAP_GAIN_CP = 150, 80, 150
GAME_DECISIVE_CP, GAME_MIN_TREE_PLIES = 200, 6
CLAMP = 1500                             # losses are measured between clamped evals (mate = huge)
PIPELINE_VERSION = 1
_NUM = re.compile(r"^\d+\.(\.\.)?$")


# ---------------------------------------------------------------- helpers

def sans_of(pgn: str) -> list[str]:
    board, out = chess.Board(), []
    for tok in pgn.split():
        if _NUM.match(tok) or tok in ("1-0", "0-1", "1/2-1/2", "*"):
            continue
        move = board.parse_san(tok)
        out.append(board.san(move))
        board.push(move)
    return out


def db_rows() -> list[dict]:
    rows = []
    for path in sorted(TSV_DIR.glob("*.tsv")):
        with open(path, encoding="utf-8", newline="") as fh:
            rows += list(csv.DictReader(fh, delimiter="\t"))
    return rows


def belongs(name: str, names: list[str]) -> bool:
    return any(name.startswith(n) or (n == "London System" and n in name) for n in names)


class Evaluator:
    """Stockfish evaluations by position (EPD), cached on disk."""

    def __init__(self, depth: int):
        self.depth = depth
        self.cache: dict[str, list] = {}
        if CACHE.exists():
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            if data.get("depth") == depth:
                self.cache = data["evals"]
        self.engine = None
        self.fresh = 0

    def _engine(self):
        if self.engine is None:
            from backend.app.engine.service import UciEngine
            self.engine = UciEngine()
        return self.engine

    def eval(self, board: chess.Board) -> tuple[int, str | None]:
        """(cp from White's view, Stockfish's best move in SAN)."""
        key = board.epd()
        hit = self.cache.get(key)
        if hit is not None:
            return hit[0], hit[1]
        if board.is_checkmate():
            cp, best = Score.checkmate(white_won=board.turn == chess.BLACK).to_cp(), None
        elif board.is_stalemate() or board.is_insufficient_material():
            cp, best = 0, None
        else:
            lines = self._engine().analyse_lines(board, depth=self.depth, multipv=1, fresh=True)
            cp, best = lines[0].score.to_cp(), lines[0].san
        self.cache[key] = [cp, best]
        self.fresh += 1
        if self.fresh % 100 == 0:
            self.save()
        return cp, best

    def save(self) -> None:
        CACHE.write_text(json.dumps({"depth": self.depth, "evals": dict(sorted(self.cache.items()))},
                                    separators=(",", ":")) + "\n", encoding="utf-8")

    def close(self) -> None:
        self.save()
        if self.engine is not None:
            self.engine.close()


def loss_for_mover(parent_cp: int, child_cp: int, white_moved: bool) -> int:
    a, b = max(-CLAMP, min(CLAMP, parent_cp)), max(-CLAMP, min(CLAMP, child_cp))
    return max(0, (a - b) if white_moved else (b - a))


def verdict(loss: int) -> str:
    return "sound" if loss <= SOUND_CP else "dubious" if loss <= DUBIOUS_CP else "mistake"


# ---------------------------------------------------------------- tree

def build_tree(spec: dict, rows: list[dict], ev: Evaluator, min_positions: int, max_extension: int) -> tuple[dict, dict]:
    lines = []
    for row in rows:
        if belongs(row["name"], spec["names"]):
            lines.append((sans_of(row["pgn"]), row["eco"], row["name"]))
    main = sans_of(spec["main"])
    if not any(tuple(s) == tuple(main) for s, _e, _n in lines):
        raise SystemExit(f"{spec['id']}: the main line is not a database line of this opening: {spec['main']}")

    nodes: dict[tuple, dict] = {(): {"san": None, "names": [], "src": "db"}}
    for sans, eco, name in sorted(lines, key=lambda x: (len(x[0]), x[0], x[2])):
        for k in range(1, len(sans) + 1):
            nodes.setdefault(tuple(sans[:k]), {"san": sans[k - 1], "names": [], "src": "db"})
        node = nodes[tuple(sans)]
        if (eco, name) not in node["names"]:
            node["names"].append((eco, name))

    db_positions = len(nodes) - 1
    extended = 0
    if db_positions < min_positions:
        # thin opening: continue the named lines with Stockfish's best moves (main line first)
        leaves = [p for p in nodes if p and not any(len(q) == len(p) + 1 and q[:len(p)] == p for q in nodes)]
        leaves.sort(key=lambda p: (p != tuple(main), len(p), p))
        for leaf in leaves:
            if len(nodes) - 1 >= min_positions:
                break
            board = chess.Board()
            for san in leaf:
                board.push_san(san)
            path = leaf
            for _ in range(max_extension):
                if board.is_game_over():
                    break
                _cp, best = ev.eval(board)
                if not best:
                    break
                board.push_san(best)
                path = path + (best,)
                if path not in nodes:
                    nodes[path] = {"san": best, "names": [], "src": "engine"}
                    extended += 1

    # order: depth-first, main line first, then the larger (more named) subtrees
    def named_below(path: tuple) -> int:
        return sum(1 for q, n in nodes.items() if n["names"] and q[:len(path)] == path)

    children: dict[tuple, list[tuple]] = {}
    for p in nodes:
        if p:
            children.setdefault(p[:-1], []).append(p)
    weight = {p: named_below(p) for p in nodes}
    out_nodes: list[dict] = []
    index: dict[tuple, int] = {}
    main_set = {tuple(main[:k]) for k in range(len(main) + 1)}

    def visit(path: tuple, parent: int, board: chess.Board, parent_cp: int | None) -> None:
        cp, best = ev.eval(board)
        node = nodes[path]
        rec = {"i": len(out_nodes), "p": parent, "m": node["san"], "e": cp, "b": best}
        if node["san"] is not None:
            white_moved = board.turn == chess.BLACK
            loss = loss_for_mover(parent_cp, cp, white_moved)
            rec["l"] = loss
            rec["v"] = verdict(loss)
        if node["names"]:
            eco, name = sorted(node["names"], key=lambda x: (len(x[1]), x[1]))[0]
            rec["n"], rec["eco"] = name, eco
            if len(node["names"]) > 1:
                rec["aka"] = sorted({n for _e, n in node["names"]} - {name})
        if node["src"] != "db":
            rec["src"] = node["src"]
        if path in main_set:
            rec["main"] = 1
        index[path] = rec["i"]
        out_nodes.append(rec)
        kids = sorted(children.get(path, []), key=lambda q: (q not in main_set, -weight[q], q[-1]))
        for kid in kids:
            board.push_san(kid[-1])
            visit(kid, rec["i"], board, cp)
            board.pop()

    sys.setrecursionlimit(10000)
    visit((), -1, chess.Board(), None)
    stats = {"db_positions": db_positions, "engine_positions": extended, "lines": len(lines)}
    return {"nodes": out_nodes, "index": index}, stats


# ---------------------------------------------------------------- traps & games

def verify_trap(trap: dict, ev: Evaluator, index: dict) -> tuple[dict | None, str]:
    sans = sans_of(trap["moves"])
    k = trap["mistake"]
    victim_white = k % 2 == 0
    board, plies = chess.Board(), []
    cp_prev, _ = ev.eval(board)
    for i, san in enumerate(sans):
        white_moved = board.turn == chess.WHITE
        board.push_san(san)
        cp, best = ev.eval(board)
        plies.append({"m": san, "e": cp, "l": loss_for_mover(cp_prev, cp, white_moved)})
        cp_prev = cp
    for i, ply in enumerate(plies[:k]):
        if ply["l"] > DUBIOUS_CP:
            return None, f"move {i + 1} ({ply['m']}) before the mistake already loses {ply['l']} cp"
    if plies[k]["l"] < TRAP_MISTAKE_CP:
        return None, f"the mistake {plies[k]['m']} only loses {plies[k]['l']} cp (needs {TRAP_MISTAKE_CP})"
    for i in range(k + 1, len(plies)):
        trapper_move = (i % 2 == 0) != victim_white
        if trapper_move and plies[i]["l"] > TRAP_MOVE_CP:
            return None, f"the trapping side's {plies[i]['m']} loses {plies[i]['l']} cp"
    final = plies[-1]["e"]
    gain = final if not victim_white else -final
    if not board.is_checkmate() and gain < TRAP_GAIN_CP:
        return None, f"the trap ends only {gain} cp up for the trapping side"
    # where the trap leaves the tree (the deepest tree node on its path)
    leaves_at = 0
    for j in range(len(sans), -1, -1):
        if tuple(sans[:j]) in index:
            leaves_at = j
            break
    rec = {"name": trap["name"], "moves": sans, "mistake": k, "victim": "white" if victim_white else "black",
           "explanation": trap["explanation"], "plies": plies, "branch_node": index[tuple(sans[:leaves_at])],
           "branch_ply": leaves_at, "mate": board.is_checkmate(), "gain_cp": gain if not board.is_checkmate() else None}
    return rec, "verified"


def verify_game(game: dict, ev: Evaluator, epds: set[str]) -> tuple[dict | None, str]:
    try:
        sans = sans_of(game["moves"])
    except ValueError as exc:
        return None, f"illegal move in the record: {exc}"
    board, in_tree = chess.Board(), 0
    for i, san in enumerate(sans):
        board.push_san(san)
        if board.epd() in epds:
            in_tree = i + 1
    if in_tree < GAME_MIN_TREE_PLIES:
        return None, f"the game leaves the opening tree after {in_tree} plies"
    result = game["result"]
    if board.is_checkmate():
        winner = "1-0" if board.turn == chess.BLACK else "0-1"
        if winner != result:
            return None, f"the record ends in mate for the other side ({winner}, recorded {result})"
        final_cp, check = None, "checkmate on the board"
    else:
        final_cp, _best = ev.eval(board)
        want = {"1-0": 1, "0-1": -1}.get(result, 0)
        if want and final_cp * want < GAME_DECISIVE_CP:
            return None, f"Stockfish doesn't confirm {result} in the final position ({final_cp:+d} cp)"
        if not want and abs(final_cp) > GAME_DECISIVE_CP:
            return None, f"Stockfish doesn't confirm a draw ({final_cp:+d} cp)"
        check = f"final position {final_cp:+d} cp (Stockfish)"
    rec = {k: game[k] for k in ("white", "black", "event", "year", "result", "lesson")}
    rec.update({"moves": sans, "in_tree_plies": in_tree, "final_cp": final_cp, "result_check": check})
    return rec, "verified"


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--min-positions", type=int, default=100)
    ap.add_argument("--max-extension", type=int, default=6)
    args = ap.parse_args(argv)

    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    topics = {t["id"] for t in json.loads(CATALOG.read_text(encoding="utf-8"))["topics"]}
    wanted = {x for x in args.only.split(",") if x}
    rows = db_rows()
    ev = Evaluator(args.depth)
    OUT.mkdir(parents=True, exist_ok=True)
    report_path = OUT / "_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {"openings": {}}
    try:
        for op in spec["openings"]:
            if wanted and op["id"] not in wanted:
                continue
            if op.get("catalog_topic") and op["catalog_topic"] not in topics:
                raise SystemExit(f"{op['id']}: unknown catalog topic {op['catalog_topic']}")
            started = time.time()
            tree, stats = build_tree(op, rows, ev, args.min_positions, args.max_extension)
            nodes, index = tree["nodes"], tree["index"]
            epds = set()
            for path in index:
                b = chess.Board()
                for san in path:
                    b.push_san(san)
                epds.add(b.epd())
            traps, games, dropped = [], [], []
            for trap in op.get("traps", []):
                rec, why = verify_trap(trap, ev, index)
                (traps.append(rec) if rec else dropped.append({"trap": trap["name"], "reason": why}))
            for game in op.get("games", []):
                rec, why = verify_game(game, ev, epds)
                label = f"{game['white']} - {game['black']}, {game['year']}"
                (games.append(rec) if rec else dropped.append({"game": label, "reason": why}))
            verdicts = {v: sum(1 for n in nodes if n.get("v") == v) for v in ("sound", "dubious", "mistake")}
            counts = {"positions": len(nodes) - 1, "unique_positions": len(epds) - 1, **stats,
                      "named": sum(1 for n in nodes if n.get("n")), **verdicts,
                      "traps": len(traps), "games": len(games)}
            doc = {
                "id": op["id"], "title": op["title"], "side": op["side"], "aliases": op["aliases"],
                "names": op["names"], "catalog_topic": op.get("catalog_topic"),
                "library_concept": op.get("library_concept"), "summary": op["summary"],
                "plans": op["plans"], "watch_for": op.get("watch_for", []),
                "counts": counts,
                "source": {"moves": f"Lichess opening database ({UPSTREAM}, commit {UPSTREAM_COMMIT[:10]})",
                           "license": LICENSE,
                           "text": "hand-written (knowledge/sources/data/curated/opening_trees.json)",
                           "games": "historical game scores (facts), replayed and checked at build time"},
                "verification": {"pipeline_version": PIPELINE_VERSION, "engine": "Stockfish (repo engine)",
                                 "depth": args.depth, "sound_cp": SOUND_CP, "dubious_cp": DUBIOUS_CP,
                                 "trap_mistake_cp": TRAP_MISTAKE_CP, "trap_move_cp": TRAP_MOVE_CP,
                                 "trap_gain_cp": TRAP_GAIN_CP, "game_decisive_cp": GAME_DECISIVE_CP},
                "nodes": nodes, "traps": traps, "games": games,
            }
            (OUT / f"{op['id']}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n",
                                                  encoding="utf-8")
            report["openings"][op["id"]] = {"counts": counts, "dropped": dropped}
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                   encoding="utf-8")
            print(f"{op['id']:24} {counts['positions']:5} positions ({stats['engine_positions']} engine) "
                  f"sound={verdicts['sound']} dubious={verdicts['dubious']} mistake={verdicts['mistake']} "
                  f"traps={len(traps)} games={len(games)} dropped={len(dropped)} {time.time() - started:.0f}s",
                  flush=True)
            for d in dropped:
                print("    dropped:", d, flush=True)
    finally:
        ev.close()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
