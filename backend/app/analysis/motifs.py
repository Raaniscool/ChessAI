"""Concept detection for one important moment of a game.

Input: the position before the learner's move, the move, Stockfish's best line
from that position and Stockfish's line after the move (what the opponent does
about it). Output: findings tagged with the Knowledge Library's own concept ids.

The detection reuses the library's concept validators (knowledge/validators.py),
the same python-chess geometry checks that prove a library example really shows
a fork, pin, skewer, back-rank mate... Two families:

  missed   the engine's best move creates the motif (Nc7+ forks king and queen)
           -> "missed_fork" / concept knight_fork
  allowed  the learner's move lets the opponent use it (left the bishop hanging,
           walked into a fork, ignored a threat, weakened the king)
           -> "hung_piece", "walked_into_fork", "missed_threat", ...

A motif is only reported when the engine line actually cashes in on it
(material won or mate) — the move being bad was already decided by Stockfish;
this module only names *why*, it never decides *whether*.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import chess

from ..engine.classification import Score
from ..knowledge import validators as V
from ..knowledge.facts import render_facts
from ..knowledge.positions import VALUES, Replay, material, move_label

LINE_PLIES = 7          # enough for a tactic and its pay-off, short enough to stay relevant
MIN_SWING = 2           # pawns: what counts as "wins/loses material"

# Knowledge Library concept per detected motif (piece-specific forks resolved below).
MOTIF_CONCEPT = {
    "missed_checkmate": "checkmate", "allowed_checkmate": "checkmate",
    "missed_fork": "fork", "missed_pin": "pin", "missed_skewer": "skewer",
    "missed_discovered_attack": "discovered_attack", "missed_double_check": "double_check",
    "missed_free_piece": "hanging_piece", "missed_trapped_piece": "trapped_piece",
    "missed_check": "check", "missed_tactic": "tactics",
    "hung_piece": "hung_piece", "walked_into_fork": "walked_into_fork", "missed_threat": "missed_threat",
    "allowed_pin": "pin", "allowed_skewer": "skewer", "allowed_discovered_attack": "discovered_attack",
    "king_safety": "king_safety_mistake", "poisoned_pawn": "poisoned_pawn", "bad_trade": None,
    "tactical_oversight": "tactics", "opening_mistake": None, "endgame_mistake": "endgames",
    "positional_mistake": None,
    # game-level habits (habits.py)
    "early_queen": "early_queen", "repeated_moves": "repeated_moves",
    "poor_development": "ignoring_development", "missed_castling": "king_safety",
}
# Catalog topics used for follow-up practice when the concept has none (or no concept exists).
MOTIF_TOPIC = {"opening_mistake": "opening_principles", "bad_trade": "hanging_piece",
               "missed_castling": "opening_principles", "positional_mistake": None}
FAMILY = {m: ("missed" if m.startswith("missed_") and m != "missed_threat" else "allowed")
          for m in MOTIF_CONCEPT}
FAMILY.update({"early_queen": "habit", "repeated_moves": "habit", "poor_development": "habit",
               "missed_castling": "habit", "opening_mistake": "phase", "endgame_mistake": "phase",
               "positional_mistake": "phase", "tactical_oversight": "allowed"})
# Most telling first: this order decides a moment's headline.
PRIORITY = ["missed_checkmate", "allowed_checkmate", "hung_piece", "walked_into_fork", "missed_fork",
            "missed_double_check", "missed_discovered_attack", "missed_pin", "missed_skewer",
            "missed_free_piece", "missed_trapped_piece", "allowed_discovered_attack", "allowed_pin",
            "allowed_skewer", "missed_threat", "king_safety", "poisoned_pawn", "bad_trade", "missed_check",
            "missed_tactic", "tactical_oversight", "endgame_mistake", "opening_mistake", "positional_mistake"]
MATE_COMPANIONS = {"missed_checkmate", "allowed_checkmate", "missed_threat", "king_safety"}
MATE_PATTERNS = ("smothered_mate", "back_rank_mate", "anastasia_mate", "arabian_mate", "boden_mate")
FORK_CONCEPT = {chess.KNIGHT: "knight_fork", chess.PAWN: "pawn_fork", chess.QUEEN: "queen_fork"}


@dataclass
class Finding:
    motif: str                      # what happened, e.g. "missed_fork", "hung_piece"
    concept: str | None             # Knowledge Library concept id (None: the library has no such concept)
    family: str                     # missed | allowed | habit | phase
    facts: dict = field(default_factory=dict)       # verified geometry from the validators
    fact_lines: list[str] = field(default_factory=list)  # the same facts as short sentences
    topic: str | None = None        # planner catalog topic for follow-up practice

    def as_dict(self) -> dict:
        return asdict(self)


def finding(motif: str, facts: dict | None = None, concept: str | None = "", lines: list[str] | None = None
            ) -> Finding:
    concept = MOTIF_CONCEPT.get(motif) if concept == "" else concept
    facts = facts or {}
    return Finding(motif=motif, concept=concept, family=FAMILY.get(motif, "allowed"), facts=facts,
                   fact_lines=lines if lines is not None else render_facts({motif: facts}) if facts else [],
                   topic=MOTIF_TOPIC.get(motif))


# ----------------------------------------------------------------- helpers

def replay_moves(start: chess.Board, moves: list[chess.Move]) -> Replay:
    """A Replay (validators' input) of legal moves from `start`; stops at the first illegal one."""
    board = start.copy(stack=False)
    boards, played, labels, sans = [board.copy(stack=False)], [], [], []
    for move in moves:
        if move not in board.legal_moves:
            break
        labels.append(move_label(board, move))
        sans.append(board.san(move))
        played.append(move)
        board.push(move)
        boards.append(board.copy(stack=False))
    return Replay(boards, played, labels, sans)


def swing(rep: Replay, side: chess.Color) -> int:
    """Material change (pawns) for `side` from the start to the end of the line."""
    start, end = rep.boards[0], rep.final
    return (material(end, side) - material(end, not side)) - (material(start, side) - material(start, not side))


def _run(name: str, ctx: V.Ctx, params: dict | None = None) -> dict | None:
    try:
        return V.run(name, ctx, params)
    except (V.Fail, ValueError, KeyError, IndexError, AttributeError):
        return None


def _squares(facts: dict, *roles: str) -> set[int]:
    out = set()
    for role in roles:
        value = facts.get(role)
        for p in (value if isinstance(value, list) else [value]):
            if isinstance(p, dict) and "square" in p:
                out.add(chess.parse_square(p["square"]))
    return out


def cashes_in(rep: Replay, start: int, squares: set[int]) -> bool:
    """Does the side that played move `start` later capture on one of `squares` (or mate)?

    A geometric motif only matters if the line uses it: a pin nobody exploits is not why a
    move was bad."""
    side = rep.boards[start].turn
    for j in range(start, len(rep.moves)):
        board, move = rep.boards[j], rep.moves[j]
        if board.turn == side and board.is_capture(move) and move.to_square in squares:
            return True
    return rep.final.is_checkmate() and rep.final.turn != side


# Which pieces a motif is "about" (validators' fact roles).
TARGET_ROLES = {"fork": ("targets",), "pin": ("pinned", "behind"), "skewer": ("front", "back"),
                "discovered_attack": ("target",), "double_check": ("checkers",),
                "trapped_piece": ("trapped",)}


def mate_concept(rep: Replay, ctx: V.Ctx) -> tuple[str, dict]:
    """The most specific checkmate concept for a line that ends in mate."""
    if not rep.moves or not rep.final.is_checkmate():
        return "checkmate", {}
    for name in MATE_PATTERNS:
        facts = _run(name, ctx)
        if facts:
            return name, facts
    facts = _run("checkmate", ctx) or {}
    mover_moves = (len(rep.moves) + 1) // 2
    return ("mate_in_one" if mover_moves == 1 else "checkmate"), facts


def _material_word(pawns: int) -> str:
    return {1: "a pawn", 2: "two pawns", 3: "a piece", 5: "a rook", 9: "the queen"}.get(abs(pawns),
                                                                                     f"{abs(pawns)} pawns of material")


# --------------------------------------------------------------- detection

def detect(before: chess.Board, move: chess.Move, best_line: list[chess.Move], reply_line: list[chess.Move],
           eval_before: Score, eval_after: Score, phase: str) -> list[Finding]:
    """Name what went wrong with `move` (already graded a mistake by Stockfish). Most telling first."""
    me = before.turn
    found: dict[str, Finding] = {}

    def add(f: Finding | None) -> None:
        if f is not None and f.motif not in found:
            found[f.motif] = f

    best = replay_moves(before, best_line[:LINE_PLIES])
    actual = replay_moves(before, [move] + reply_line[:LINE_PLIES - 1])
    best_gain = swing(best, me) if best.moves else 0
    actual_gain = swing(actual, me) if actual.moves else 0

    # --- mates
    if eval_before.is_mate_for(me) and not eval_after.is_mate_for(me):
        concept, facts = mate_concept(best, V.Ctx(best, key_ply=0))
        add(finding("missed_checkmate", {**facts, "best_line": best.sans}, concept=concept,
                    lines=_mate_lines(best, facts, "your")))
    if eval_after.is_mate_for(not me) and not eval_before.is_mate_for(not me):
        concept, facts = mate_concept(actual, V.Ctx(actual, mistake_ply=0))
        add(finding("allowed_checkmate", {**facts, "line": actual.sans}, concept=concept,
                    lines=_mate_lines(actual, facts, "your opponent's")))

    # --- what the move allowed (the opponent's line cashes in)
    if len(actual.moves) >= 2 and (actual_gain <= -MIN_SWING or actual.final.is_checkmate()):
        ctx = V.Ctx(actual, mistake_ply=0)
        hung = _run("hung_piece", ctx)
        if hung and hung["hung"]["piece"] == "pawn":
            hung = None  # a lost pawn is reported through the threat/tactic it came from, not as "hanging a piece"
        if hung:
            concept = "hanging_queen" if hung["hung"]["piece"] == "queen" else "hung_piece"
            add(finding("hung_piece", hung, concept=concept,
                        lines=[f"after {actual.labels[0]} the {_describe(hung['hung'])} can be taken "
                               f"(attacked by the {_describe(hung['attacked_by'][0])}) and it is lost "
                               f"in the engine line"]))
        fork = _run("walked_into_fork", ctx)
        if fork:
            add(finding("walked_into_fork", fork, lines=render_facts({"fork": fork["fork"]})))
        opp = V.Ctx(actual, key_ply=1)
        at = {"at": actual.labels[1]}
        for name, motif in (("discovered_attack", "allowed_discovered_attack"), ("pin", "allowed_pin"),
                            ("skewer", "allowed_skewer")):
            facts = _run(name, opp, at)
            if facts and cashes_in(actual, 1, _squares(facts, *TARGET_ROLES[name])):
                add(finding(motif, facts))
        threat = _run("missed_threat", ctx)
        if threat and _real_threat(before, move, actual.moves[1]):
            add(finding("missed_threat", threat,
                        lines=[f"{threat['ignored_threat']} was already threatened before {actual.labels[0]}, "
                               f"and the move did nothing about it"]))
        king = _run("king_safety_mistake", ctx)
        if king:
            add(finding("king_safety", king,
                        lines=[f"{actual.labels[0]} weakens the {_side(me)} king on {king['king']}"]))
        pawn = _run("poisoned_pawn", ctx)
        if pawn:
            add(finding("poisoned_pawn", pawn,
                        lines=[f"{actual.labels[0]} grabs the pawn on {pawn['grabbed']['square']}, "
                               f"and the engine line wins back more"]))
        if before.is_capture(move) and not found:
            add(finding("bad_trade", {"capture": actual.labels[0], "material_after_line": actual_gain},
                        lines=[f"the exchange that starts with {actual.labels[0]} ends "
                               f"{_material_word(actual_gain)} down"]))

    # --- what the engine's best move would have won
    # (Not compared with the played line's material: Stockfish already judged the played move much
    # worse — material in a short line can't see e.g. that the pawn it keeps is the whole point.)
    if best.moves and (best_gain >= MIN_SWING or best.final.is_checkmate()):
        ctx = V.Ctx(best, key_ply=0)
        at = {"at": best.labels[0]}
        fork = _run("fork", ctx, at)
        if fork and cashes_in(best, 0, _squares(fork, "targets")):
            ptype = {"knight": chess.KNIGHT, "pawn": chess.PAWN, "queen": chess.QUEEN}.get(fork["attacker"]["piece"])
            add(finding("missed_fork", fork, concept=FORK_CONCEPT.get(ptype, "fork")))
        for name, motif in (("double_check", "missed_double_check"),
                            ("discovered_attack", "missed_discovered_attack"),
                            ("pin", "missed_pin"), ("skewer", "missed_skewer"),
                            ("hanging_piece", "missed_free_piece"), ("trapped_piece", "missed_trapped_piece")):
            facts = _run(name, ctx, at)
            if facts and name in TARGET_ROLES and not cashes_in(best, 0, _squares(facts, *TARGET_ROLES[name])):
                continue
            if facts:
                lines = None
                if motif == "missed_free_piece":
                    lines = [f"{best.labels[0]} would have taken the {_describe(facts['captured'])}"
                             + ("" if facts["was_defended"] else ", which was not defended")]
                add(finding(motif, facts, lines=lines))
        if not any(f.family == "missed" for f in found.values()):
            check = _run("check", ctx, at)
            if check:
                add(finding("missed_check", check,
                            lines=[f"{best.labels[0]} gives check and the engine line wins "
                                   f"{_material_word(best_gain)}"]))
            elif best_gain >= MIN_SWING:
                add(finding("missed_tactic", {"best_line": best.sans, "material": best_gain},
                            lines=[f"the engine line {' '.join(best.sans[:4])} wins {_material_word(best_gain)}"]))

    # --- nothing concrete: name the phase
    if not found:
        if actual_gain <= -MIN_SWING:
            add(finding("tactical_oversight", {"line": actual.sans, "material": actual_gain},
                        lines=[f"after {actual.labels[0]} the engine line loses {_material_word(actual_gain)}"]))
        elif phase == "endgame":
            add(finding("endgame_mistake", {"phase": phase}, lines=[]))
        elif phase == "opening":
            add(finding("opening_mistake", {"phase": phase}, lines=[]))
        else:
            add(finding("positional_mistake", {"phase": phase}, lines=[]))

    if any(f.motif in ("missed_checkmate", "allowed_checkmate") for f in found.values()):
        # Next to a checkmate every other tactic is noise for a beginner; keep only the
        # lessons that explain how the mate became possible.
        found = {k: f for k, f in found.items() if k in MATE_COMPANIONS}
    out = list(found.values())
    out.sort(key=lambda f: PRIORITY.index(f.motif) if f.motif in PRIORITY else len(PRIORITY))
    if phase == "endgame":
        for f in out:
            f.topic = f.topic or endgame_topic(before)
    return out


def _real_threat(before: chess.Board, move: chess.Move, reply: chess.Move) -> bool:
    """Was the opponent's punishing reply already a real threat before `move`?

    Real means: had the learner passed, the reply would have mated, or captured a piece
    that was undefended or worth more than the capturer. Capturing the piece the learner
    just moved is never an ignored threat — that piece wasn't there before."""
    if reply.to_square == move.to_square:
        return False
    probe = before.copy(stack=False)
    probe.push(chess.Move.null())
    if reply not in probe.legal_moves:
        return False
    target = probe.piece_at(reply.to_square)
    attacker = probe.piece_at(reply.from_square)
    probe.push(reply)
    if probe.is_checkmate():
        return True
    if target is None or attacker is None or target.piece_type == chess.PAWN:
        return False
    defended = bool(probe.attackers(target.color, reply.to_square))
    return not defended or VALUES[target.piece_type] > VALUES[attacker.piece_type]


def endgame_topic(board: chess.Board) -> str:
    """Catalog topic that matches the material on the board."""
    pieces = {pt for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
              if board.pieces(pt, chess.WHITE) or board.pieces(pt, chess.BLACK)}
    if not pieces:
        return "pawn_endgames"
    if chess.QUEEN in pieces:
        return "queen_endgames"
    if chess.ROOK in pieces:
        return "rook_endgames"
    return "bishop_endgames" if chess.BISHOP in pieces else "knight_endgames"


def _describe(p: dict) -> str:
    return f"{p['color']} {p['piece']} on {p['square']}"


def _side(color: chess.Color) -> str:
    return "white" if color else "black"


def _mate_lines(rep: Replay, facts: dict, whose: str) -> list[str]:
    lines = [f"{whose} line {' '.join(rep.sans)} ends in checkmate"]
    if facts.get("pattern"):
        lines.append(f"the pattern is a {facts['pattern']}")
    return lines


def material_value(board: chess.Board, side: chess.Color) -> int:
    return material(board, side) - material(board, not side)


def non_pawn_material(board: chess.Board) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values()
               if p.piece_type not in (chess.PAWN, chess.KING))
