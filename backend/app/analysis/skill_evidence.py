"""Skill evidence from the learner's analyzed games: how strong they play, not what they get wrong.

Weaknesses (analysis.weaknesses / history) answer "what goes wrong repeatedly". This module
answers "how well does this player handle chess positions overall", from the same Stockfish
analysis, over *many* games. Everything is counted, nothing is guessed:

    moves / errors      learner moves and how many were mistakes or blunders (engine loss
                        >= 120cp, confirmed at full depth), split by phase
    allowed             errors that walked into the opponent's tactic (hung piece, fork, mate)
    chances             positions where the learner was suddenly winning (the opponent's last
                        move swung the evaluation >= CHANCE_SWING towards the learner and left
                        them >= CHANCE_MIN ahead). Each one is a real-game tactics test:
                          found   the learner's move kept the advantage (loss <= FOUND_LOSS)
                          missed  it threw most of it away (loss >= MISSED_LOSS)
                        and is rated with the same decision formula as puzzles
                        (puzzles.profile.decision_rating) from the winning move itself: the move
                        played when found, the engine's best move when missed
    conversion          games where the learner was clearly winning (>= WINNING) and won
    saves               games where the learner was clearly losing (<= -WINNING) and didn't lose

learner.difficulty turns these counts into skill estimates; this module only collects them.
"""
from __future__ import annotations

import chess

from ..engine.classification import Score
from ..knowledge.difficulty import move_features

CHANCE_SWING = 200
CHANCE_MIN = 150
FOUND_LOSS = 100
MISSED_LOSS = 200
WINNING = 300
CAP = 2000
ERROR_CATEGORIES = ("mistake", "blunder")
MAX_CHANCES = 60   # stored (the most recent games first); counts cover all of them


def _cp(score: dict | None, white: bool) -> int | None:
    if not score:
        return None
    try:
        return max(-CAP, min(CAP, Score(score["kind"], int(score["value"])).for_side(white)))
    except (KeyError, TypeError, ValueError):
        return None


def _rated(board: chess.Board, move: chess.Move) -> dict:
    from ..puzzles.profile import _plausible, decision_rating
    f = move_features(board, move)
    f["plausible"] = _plausible(board, None)
    f["payoff_plies"] = 1 if f["capture"] else 3
    kind = "pattern" if (f["check"] or f["capture"]) and not f["sacrifice"] else "calculation"
    return {"rating": decision_rating(f), "kind": kind, "quiet": f["quiet"], "check": f["check"],
            "capture": f["capture"], "sacrifice": f["sacrifice"]}


def game_evidence(game: dict, analysis: dict) -> dict | None:
    """The counts for one analyzed game (None when it can't be replayed)."""
    from .analyzer import phase_of
    color = game.get("player_color")
    if color not in ("white", "black") or not analysis:
        return None
    white = color == "white"
    side = chess.WHITE if white else chess.BLACK
    try:
        board = chess.Board(game.get("start_fen") or chess.STARTING_FEN)
        moves = [chess.Move.from_uci(u) for u in game.get("moves_uci") or []]
    except ValueError:
        return None
    plies = analysis.get("plies") or []
    evals = analysis.get("evals") or []
    best_by_ply = {m["ply"]: m.get("best_move_uci") for m in analysis.get("moments", [])}
    allowed_plies = {m["ply"] for m in analysis.get("moments", [])
                     if any(f.get("family") == "allowed" for f in m.get("findings", []))}
    out = {"game_id": game.get("id"), "moves": 0, "errors": 0, "allowed": len(allowed_plies),
           "phases": {p: {"moves": 0, "errors": 0} for p in ("opening", "middlegame", "endgame")},
           "chances": [], "best": None, "worst": None}
    for i, move in enumerate(moves):
        if i >= len(plies):
            break
        entry = plies[i]
        if board.turn == side and "category" in entry:
            phase = phase_of(board)
            err = entry["category"] in ERROR_CATEGORIES
            out["moves"] += 1
            out["errors"] += int(err)
            out["phases"][phase]["moves"] += 1
            out["phases"][phase]["errors"] += int(err)
            now = _cp(evals[i] if i < len(evals) else None, white)
            before = _cp(evals[i - 1] if 0 < i <= len(evals) else None, white)
            loss = int(entry.get("loss_cp") or 0)
            if now is not None:
                out["best"] = now if out["best"] is None else max(out["best"], now)
                out["worst"] = now if out["worst"] is None else min(out["worst"], now)
            if now is not None and before is not None and now >= CHANCE_MIN and now - before >= CHANCE_SWING:
                found = loss <= FOUND_LOSS
                missed = loss >= MISSED_LOSS
                key = move if found else None
                if missed and best_by_ply.get(i):
                    try:
                        key = chess.Move.from_uci(best_by_ply[i])
                    except ValueError:
                        key = None
                if (found or missed) and key is not None and key in board.legal_moves:
                    out["chances"].append({"ply": i, "found": found, **_rated(board, key)})
        if move not in board.legal_moves:
            return None
        board.push(move)
    return out


def summarize(docs: list[dict]) -> dict:
    """Skill evidence over every analyzed game in `docs` ([{game, analysis}], newest first)."""
    from .history import _learner_result
    games = []
    for d in docs:
        if not d.get("analysis"):
            continue
        ev = game_evidence(d.get("game") or {}, d["analysis"])
        if ev is not None:
            ev["result"] = _learner_result(d.get("game") or {})
            games.append(ev)
    phases = {p: {"moves": 0, "errors": 0} for p in ("opening", "middlegame", "endgame")}
    chances = []
    for g in games:
        for p, c in g["phases"].items():
            phases[p]["moves"] += c["moves"]
            phases[p]["errors"] += c["errors"]
        chances += [{**c, "game_id": g["game_id"]} for c in g["chances"]]
    winning = [g for g in games if (g["best"] or 0) >= WINNING]
    losing = [g for g in games if (g["worst"] or 0) <= -WINNING]
    return {
        "games": len(games),
        "moves": sum(g["moves"] for g in games),
        "errors": sum(g["errors"] for g in games),
        "allowed": sum(g["allowed"] for g in games),
        "phases": phases,
        "chances": {"found": sum(1 for c in chances if c["found"]), "missed": sum(1 for c in chances if not c["found"]),
                    "items": chances[:MAX_CHANCES]},
        "conversion": {"winning_games": len(winning), "won": sum(1 for g in winning if g["result"] == "win")},
        "saves": {"losing_games": len(losing), "saved": sum(1 for g in losing if g["result"] in ("win", "draw"))},
        "thresholds": {"chance_swing_cp": CHANCE_SWING, "chance_min_cp": CHANCE_MIN, "found_loss_cp": FOUND_LOSS,
                       "missed_loss_cp": MISSED_LOSS, "winning_cp": WINNING},
    }
