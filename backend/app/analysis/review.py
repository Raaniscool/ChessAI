"""Human-readable game review: headlines, verified facts for Qwen, deterministic fallback.

Stockfish decided the verdict and the better move; python-chess replayed the
lines; the validators named the motif. This module turns those facts into
beginner-friendly text:

- ``headline``      "You missed a tactical opportunity here." (always deterministic)
- ``moment_facts``  the fact sheet Qwen explains from (and nothing else)
- ``fallback_why``  the explanation when Qwen is off, fails, or contradicts the facts
- ``conflicts``     catches a Qwen reply that praises a blunder or mentions moves/pieces
                    that aren't in the positions (reuses the lesson checks)
"""
from __future__ import annotations

from types import SimpleNamespace

import chess

from ..engine import Classification, MoveFeedback, Score
from ..knowledge.facts import check_explanation
from ..knowledge.positions import PIECE_NAMES, Replay
from ..teacher.base import _fmt_eval
from ..teacher.consistency import verdict_conflicts
from .motifs import _material_word, replay_moves

LINE_PLIES = 4

HEADLINES = {
    "missed_checkmate": "You missed a checkmate here.",
    "allowed_checkmate": "This move allowed checkmate.",
    "hung_piece": "This move left a piece where it could be taken.",
    "walked_into_fork": "This move walked into a fork.",
    "missed_threat": "This move ignored your opponent's threat.",
    "allowed_pin": "This move let your opponent pin one of your pieces.",
    "allowed_skewer": "This move let your opponent skewer your pieces.",
    "allowed_discovered_attack": "This move allowed a discovered attack.",
    "king_safety": "This move weakened your king.",
    "poisoned_pawn": "Taking this pawn was a trap.",
    "bad_trade": "This trade lost material.",
    "tactical_oversight": "This move lost material.",
    "endgame_mistake": "An endgame mistake: this makes your position much worse.",
    "opening_mistake": "An opening mistake: this makes your position much worse.",
    "positional_mistake": "This move makes your position much worse.",
    "early_queen": "Your queen came out early and got chased.",
    "repeated_moves": "You moved the same piece many times in the opening.",
    "poor_development": "You fell behind in development.",
    "missed_castling": "Your king stayed in the centre.",
}
MISSED_HEADLINE = "You missed a tactical opportunity here."
MISSED_WHAT = {"missed_fork": "a fork", "missed_pin": "a pin", "missed_skewer": "a skewer",
               "missed_discovered_attack": "a discovered attack", "missed_double_check": "a double check",
               "missed_free_piece": "a free piece", "missed_trapped_piece": "a chance to trap a piece",
               "missed_check": "a strong check"}
CONCEPT_WHAT = {"knight_fork": "a knight fork", "pawn_fork": "a pawn fork", "queen_fork": "a queen fork"}

TIPS = {
    "missed_checkmate": "Always look at every check first — a forcing move might end the game.",
    "allowed_checkmate": "Before each move, look at every check your opponent could give and whether your "
                         "king has an escape square.",
    "hung_piece": "Before you let go of a piece, ask: what can my opponent capture now, and is it defended?",
    "walked_into_fork": "Check which squares your opponent's knights and pawns can jump to next move — "
                        "don't leave two valuable pieces on squares one piece can hit at once.",
    "missed_threat": "After every opponent move, ask: what does that move threaten?",
    "allowed_pin": "Watch out for your pieces lining up with your king or queen on one line.",
    "allowed_skewer": "Keep your king and your valuable pieces off the same line as an enemy rook, bishop or queen.",
    "allowed_discovered_attack": "When an enemy piece stands in front of their rook, bishop or queen, ask what "
                                 "happens if it moves away.",
    "king_safety": "Leave the pawns in front of your castled king alone unless you have a concrete reason.",
    "poisoned_pawn": "Before grabbing a pawn, check what your opponent gets in return.",
    "bad_trade": "Before trading, count: what do you give, and what do you get back?",
    "tactical_oversight": "Before each move, look at every capture and check for both sides.",
    "missed_fork": "Look for moves where one of your pieces attacks two things at once — especially checks.",
    "missed_pin": "Look for enemy pieces standing on one line with their king or queen behind them.",
    "missed_skewer": "When the enemy king or queen is on a line with another piece behind it, attack along that line.",
    "missed_discovered_attack": "When your piece stands in front of your own rook, bishop or queen, moving it "
                                "can unleash two attacks at once.",
    "missed_double_check": "A double check forces the king to move — look for it when two of your pieces aim at the king.",
    "missed_free_piece": "Look at every capture: is any enemy piece undefended?",
    "missed_trapped_piece": "Look for enemy pieces with no safe square — you can attack them and win them.",
    "missed_check": "Look at every check you can give — checks limit your opponent's choices.",
    "missed_tactic": "Look at checks, captures and threats first on every move.",
    "endgame_mistake": "In the endgame, bring your king forward, push passed pawns, and count moves carefully.",
    "opening_mistake": "Follow the opening principles: fight for the centre, develop knights and bishops, castle.",
    "positional_mistake": "Compare your move with the engine's idea — which pieces does it improve?",
    "early_queen": "Develop your knights and bishops before bringing out the queen.",
    "repeated_moves": "In the opening, move each piece once and get them all into play before attacking.",
    "poor_development": "Count developed pieces for both sides; if you're behind, develop before anything else.",
    "missed_castling": "Castle early — usually within the first 10 moves — to get your king out of the centre.",
}

VERDICT_WORDS = {"blunder": "a blunder", "mistake": "a mistake", "inaccurate": "an inaccuracy"}


def _side(color: str | None) -> str:
    return "White" if color == "white" else "Black"


def headline(moment: dict) -> str:
    motif = moment.get("motif")
    finding = (moment.get("findings") or [{}])[0]
    if motif in HEADLINES:
        return HEADLINES[motif]
    what = CONCEPT_WHAT.get(moment.get("concept") or "") or MISSED_WHAT.get(motif or "")
    if what:
        return f"You missed {what}."
    if finding.get("family") == "missed":
        return MISSED_HEADLINE
    return "This move makes your position much worse."


def tip(moment: dict) -> str:
    return TIPS.get(moment.get("motif") or "", TIPS["tactical_oversight"])


def title(moment: dict) -> str:
    """'Move 17: Nf3??'"""
    if moment["category"] == "habit":
        return HEADLINES.get(moment.get("motif"), "Opening habit").rstrip(".")
    sep = "..." if moment.get("side") == "black" else ": "  # "Move 17: Nf3??" / "Move 17...Nf6??"
    return f"Move {moment['move_number']}{sep}{moment['san']}{moment.get('symbol', '')}"


def _score(d: dict | None) -> Score | None:
    return Score(d["kind"], d["value"]) if d else None


def _clean(line: str) -> str:
    """Drop the validator key prefix ("missed_fork: after ...")."""
    head, sep, rest = line.partition(": ")
    return rest if sep and head.replace("_", "").isalpha() and head == head.lower() else line


def fact_lines(moment: dict) -> list[str]:
    out = []
    after = moment.get("fen_after")
    if after and chess.Board(after).is_stalemate():
        out.append(f"{moment['san']} leaves the opponent with no legal move while not in check: "
                   f"that is stalemate, and the game ends in a draw")
    for f in moment.get("findings", []):
        out += [_clean(line) for line in f.get("fact_lines", [])]
    return out


def _material_sentence(pawns: int | None) -> str | None:
    if not pawns:
        return None
    what = _material_word(pawns)
    return f"{'wins' if pawns > 0 else 'loses'} {what} for the student"


def _pieces(fen: str) -> list[str]:
    board = chess.Board(fen)
    out = []
    for color in (chess.WHITE, chess.BLACK):
        pieces = [f"{PIECE_NAMES[p.piece_type]} {chess.square_name(s)}"
                  for s, p in sorted(board.piece_map().items()) if p.color == color]
        out.append(f"{'White' if color else 'Black'} pieces: {', '.join(pieces)}")
    return out


def moment_facts(moment: dict, library=None, level: str = "beginner") -> list[str]:
    """Everything Qwen may use — each line verified by python-chess, Stockfish or the validators."""
    student_white = moment.get("side") == "white"
    src = moment.get("source") or {}
    facts = []
    if src.get("opponent"):
        facts.append(f"Game: the student played {_side(moment.get('side'))} against {src['opponent']}")
    facts += [
        f"Move {moment['move_number']}: the student played {moment['san']} "
        f"- Stockfish verdict: {moment['category']} ({moment['phase']})",
        f"Position before the move (FEN, reference only - do not calculate from it): {moment['fen_before']}",
        *_pieces(moment["fen_before"]),
        f"Stockfish's best move: {moment.get('best_move') or 'unknown'}",
    ]
    if moment.get("alternatives"):
        facts.append("Other moves Stockfish rates about as good: " + ", ".join(moment["alternatives"]))
    facts.append(
        f"Evaluation for the student (pawns, + = good for them): "
        f"{_fmt_eval(moment.get('eval_before'), for_white=student_white)} with best play, "
        f"{_fmt_eval(moment.get('eval_after'), for_white=student_white)} after {moment['san']}")
    if moment.get("best_line"):
        facts.append(f"Engine line after the best move: {' '.join(moment['best_line'][:LINE_PLIES])}")
    if moment.get("reply_line"):
        facts.append(f"Likely continuation after {moment['san']}: {' '.join(moment['reply_line'][:LINE_PLIES])}")
    material = _material_sentence(moment.get("material_change"))
    if material:
        facts.append(f"Material: the continuation after {moment['san']} {material}")
    facts += fact_lines(moment)
    for f in moment.get("findings", []):
        concept = library.concepts.get(f.get("concept")) if (library is not None and f.get("concept")) else None
        if concept:
            facts.append(f"Lesson-library concept: {concept.name} - {concept.summary}")
            break
    facts.append(f"Learner level: {level}")
    return facts


def fallback_why(moment: dict) -> str:
    """Deterministic explanation from the same facts (never contradicts them)."""
    if moment["category"] == "habit":
        lines = fact_lines(moment)
        return " ".join(([lines[0][0].upper() + lines[0][1:] + "."] if lines else [])
                        + ["Stockfish confirms you came out of the opening worse.", f"Next time: {tip(moment)}"])
    verdict = VERDICT_WORDS.get(moment["category"], moment["category"])
    parts = [f"{moment['san']} is {verdict} according to Stockfish."]
    lines = fact_lines(moment)
    if lines:
        parts.append(lines[0][0].upper() + lines[0][1:] + ".")
    best = moment.get("best_move")
    if best and best != moment["san"]:
        line = moment.get("best_line") or []
        text = f"Stockfish prefers {best}"
        if len(line) > 1:
            text += f" (the line goes {' '.join(line[:LINE_PLIES])})"
        parts.append(text + ".")
    material = _material_sentence(moment.get("material_change"))
    if material and not lines:
        parts.append(f"After {moment['san']}, the engine's continuation {material.replace(' for the student', '')}.")
    parts.append(f"Next time: {tip(moment)}")
    return " ".join(parts)


def review_card(moment: dict) -> dict:
    """What the review screen shows before anyone asks the AI."""
    return {"title": title(moment), "headline": headline(moment), "why": fallback_why(moment),
            "tip": tip(moment), "facts": fact_lines(moment)}


def _lines_view(moment: dict) -> SimpleNamespace:
    """The positions the explanation may talk about: the best line and the played line."""
    before = chess.Board(moment["fen_before"])
    boards, sans = [], []
    for sans_line, start in ((moment.get("best_line") or [], before),
                             ([moment["san"]] + (moment.get("reply_line") or []), before)):
        moves, probe = [], start.copy(stack=False)
        for san in sans_line:
            try:
                move = probe.parse_san(san)
            except ValueError:
                break
            moves.append(move)
            probe.push(move)
        rep = replay_moves(start, moves)
        boards += rep.boards
        sans += rep.sans
    facts = {f["motif"]: f.get("facts", {}) for f in moment.get("findings", [])}
    rep = Replay(boards, [], [], sans)
    return SimpleNamespace(replay=lambda: rep, facts=facts)


def conflicts(text: str, moment: dict) -> list[str]:
    """Problems where a Qwen explanation contradicts the verified facts (empty = fine)."""
    feedback = MoveFeedback(
        fen_before=moment["fen_before"], fen_after=moment.get("fen_after", moment["fen_before"]),
        user_move_uci=moment["uci"], user_move_san=moment["san"],
        category=Classification(moment["category"]) if moment["category"] in Classification._value2member_map_
        else Classification.MISTAKE,
        loss_cp=moment.get("loss_cp", 0), best_move_uci=moment.get("best_move_uci"),
        best_move_san=moment.get("best_move"), eval_before=_score(moment.get("eval_before")),
        eval_after=_score(moment.get("eval_after")), best_pv_san=moment.get("best_line") or [],
        reply_pv_san=moment.get("reply_line") or [])
    problems = verdict_conflicts(text, feedback, None)
    problems += check_explanation(text, _lines_view(moment))
    return problems
