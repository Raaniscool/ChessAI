"""Analysis of a finished Training segment.

The segment is a short game from a FEN, so it goes through the same pipeline as an imported game:
games.model.GameRecord -> analysis.analyzer.GameAnalyzer (Stockfish quick pass on every position,
mistakes re-checked at full depth, analysis.motifs naming them with the Knowledge Library
validators). Nothing here re-grades a move; it only reads that analysis.

Measured (learner moves only):
    accuracy        mean per-move accuracy from the win-probability drop (the widely used
                    Lichess formula: win% = 50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1),
                    accuracy = 103.1668 * exp(-0.04354 * drop) - 3.1669, clamped 0..100)
    avg loss        average centipawn loss (each move capped at 1000)
    best-move rate  share of moves graded "excellent" (within EXCELLENT_CP of Stockfish's best)
    errors          mistakes and blunders, inaccuracies — per phase (phase_of each position)
    missed          confirmed mistakes where motifs found a "missed" opportunity (the engine's best
                    line wins material or mates) — a plain centipawn loss is NOT a missed tactic

Concept evidence (`concepts`) — only what a validator or the verified position supports:
    tested    the position's hidden idea (a verified puzzle position): the first move is one of the
              accepted moves -> found; the full-depth check confirms the first move as a mistake
              or blunder (a game-analysis moment) -> missed; a key move that simply mates while the
              concept isn't a checkmate idea -> no evidence for that concept; a different move the engine doesn't call a mistake -> no evidence
    missed    a confirmed mistake whose motifs finding (family "missed") names a specific concept
    allowed   a confirmed mistake whose finding (family "allowed") names one (hung piece, fork...)
Generic labels (tactics, endgames, check, positional/opening mistake) are never concept evidence.
"""
from __future__ import annotations

import math

from ..engine.classification import Score

GENERIC_CONCEPTS = {"tactics", "endgames", "check", None}
ERRORS = ("mistake", "blunder")
LOSS_CAP = 1000
CP_CAP = 2000


def win_percent(cp: float) -> float:
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(before_cp: float, after_cp: float) -> float:
    drop = max(0.0, win_percent(before_cp) - win_percent(after_cp))
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


def _cp(score: dict | None, white: bool) -> float | None:
    if not score:
        return None
    s = Score(score["kind"], int(score["value"]))
    return max(-CP_CAP, min(CP_CAP, s.for_side(white)))


def game_record(seg):
    from ..games.model import GameRecord
    learner = seg.position.side
    return GameRecord(id=f"training-{seg.id}", source="training",
                      white="You" if learner == "white" else "Training bot",
                      black="You" if learner == "black" else "Training bot", result="*",
                      start_fen=seg.position.fen, moves_san=list(seg.moves_san), moves_uci=list(seg.moves_uci),
                      player="You", player_color=learner)


def _specific(concept: str | None, knowledge) -> bool:
    return concept not in GENERIC_CONCEPTS and concept in knowledge.concepts


def analyze(seg, engine, knowledge, analyzer=None) -> dict:
    """The facts of the segment (see the module doc). `analyzer` defaults to GameAnalyzer(engine)."""
    from ..analysis.analyzer import GameAnalyzer, phase_of
    game = game_record(seg)
    analysis = (analyzer or GameAnalyzer(engine)).analyze(game)
    white = seg.position.side == "white"
    boards = game.boards()
    evals = analysis.get("evals") or []
    moves = []
    phases: dict[str, dict] = {}
    for i, entry in enumerate(analysis.get("plies") or []):
        if "category" not in entry:
            continue
        before, after = _cp(evals[i] if i < len(evals) else None, white), \
            _cp(evals[i + 1] if i + 1 < len(evals) else None, white)
        acc = move_accuracy(before, after) if before is not None and after is not None else None
        phase = phase_of(boards[i])
        moves.append({"ply": i, "san": entry["san"], "uci": entry["uci"], "category": entry["category"],
                      "loss_cp": int(entry.get("loss_cp") or 0), "accuracy": None if acc is None else round(acc, 1),
                      "phase": phase})
        ph = phases.setdefault(phase, {"moves": 0, "errors": 0, "inaccuracies": 0, "best": 0, "loss_sum": 0,
                                       "accuracy_sum": 0.0, "accuracy_n": 0})
        ph["moves"] += 1
        ph["errors"] += entry["category"] in ERRORS
        ph["inaccuracies"] += entry["category"] == "inaccurate"
        ph["best"] += entry["category"] == "excellent"
        ph["loss_sum"] += min(LOSS_CAP, int(entry.get("loss_cp") or 0))
        if acc is not None:
            ph["accuracy_sum"] += acc
            ph["accuracy_n"] += 1
    accs = [m["accuracy"] for m in moves if m["accuracy"] is not None]
    n = len(moves)

    moments = analysis.get("moments") or []
    concepts: list[dict] = []
    missed: list[dict] = []
    mistakes: list[dict] = []
    tested = _tested(seg, moves, moments, knowledge)
    if tested:
        concepts.append(tested)
    for m in moments:
        names, fam_missed = set(), False
        for f in m.get("findings") or []:
            fam_missed = fam_missed or f.get("family") == "missed"
            c = f.get("concept")
            if f.get("family") not in ("missed", "allowed") or not _specific(c, knowledge) or c in names:
                continue
            if tested and tested["ply"] == m["ply"] and c == tested["concept"]:
                continue     # the hidden test already counted this one
            names.add(c)
            concepts.append({"concept": c, "kind": f["family"], "result": f["family"], "ply": m["ply"],
                             "motif": f.get("motif"), "phase": m.get("phase"),
                             "text": (f.get("fact_lines") or [None])[0]})
        item = {"ply": m["ply"], "label": m.get("label"), "san": m.get("san"), "category": m.get("category"),
                "loss_cp": m.get("loss_cp"), "best_move": m.get("best_move"), "phase": m.get("phase"),
                "motif": m.get("motif"),
                "concept": next((c for c in (f.get("concept") for f in m.get("findings") or [])
                                 if _specific(c, knowledge)), None),
                "lines": [ln for f in m.get("findings") or [] for ln in (f.get("fact_lines") or [])][:2]}
        mistakes.append(item)
        if fam_missed:
            missed.append(item)
    if tested and tested["result"] == "missed" and not any(x["ply"] == tested["ply"] for x in missed):
        missed.insert(0, {"ply": tested["ply"], "label": None, "san": moves[0]["san"] if moves else None,
                          "category": moves[0]["category"] if moves else None,
                          "loss_cp": moves[0]["loss_cp"] if moves else None, "best_move": tested.get("key_move"),
                          "phase": seg.position.phase, "motif": "hidden_idea", "concept": tested["concept"],
                          "lines": []})
    for ph in phases.values():
        ph["accuracy"] = round(ph.pop("accuracy_sum") / ph["accuracy_n"], 1) if ph["accuracy_n"] else None
        ph.pop("accuracy_n")
    return {
        "moves": moves,
        "learner_moves": n,
        "accuracy": round(sum(accs) / len(accs), 1) if accs else None,
        "avg_loss_cp": round(sum(min(LOSS_CAP, m["loss_cp"]) for m in moves) / n) if n else 0,
        "best_moves": sum(1 for m in moves if m["category"] == "excellent"),
        "best_move_rate": round(sum(1 for m in moves if m["category"] == "excellent") / n, 2) if n else None,
        "blunders": sum(1 for m in moves if m["category"] == "blunder"),
        "mistakes": sum(1 for m in moves if m["category"] == "mistake"),
        "inaccuracies": sum(1 for m in moves if m["category"] == "inaccurate"),
        "phases": phases,
        "important_mistakes": sorted(mistakes, key=lambda x: -(x.get("loss_cp") or 0))[:3],
        "missed_opportunities": missed,
        "concepts": concepts,
        "tested": tested,
        "analysis_depth": analysis.get("depth"),
        "confirm_depth": analysis.get("confirm_depth"),
    }


def _tested(seg, moves: list[dict], moments: list[dict], knowledge) -> dict | None:
    hidden = seg.position.hidden
    if not hidden or not moves or moves[0]["ply"] != 0 or hidden.get("concept") not in knowledge.concepts:
        return None
    if _mate_overshadows(seg.position.fen, hidden, knowledge):
        return None
    base = {"concept": hidden["concept"], "kind": "tested", "ply": 0, "rating": hidden.get("rating"),
            "key_move": hidden.get("key_move"), "phase": seg.position.phase, "motif": None, "text": None}
    if moves[0]["san"] in (hidden.get("accepted") or []):
        return {**base, "result": "found"}
    if any(m["ply"] == 0 for m in moments):   # confirmed at full depth, like any game moment
        return {**base, "result": "missed"}
    return None    # another move Stockfish doesn't call a mistake: no evidence either way


def _mate_overshadows(fen: str, hidden: dict, knowledge) -> bool:
    """The key move mates at once but the position's concept isn't a checkmate idea (e.g. a pawn
    "fork" of king and knight that is simply mate in one): finding or missing it says nothing
    reliable about that concept — the mate itself is reported by the motifs finding instead."""
    import chess
    from .needs import _ancestors
    key = hidden.get("key_move")
    if not key:
        return False
    board = chess.Board(fen)
    try:
        board.push_san(key)
    except ValueError:
        return False
    return board.is_checkmate() and "checkmate" not in _ancestors(hidden["concept"], knowledge)
