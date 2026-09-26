"""Replaying example move sequences and small board helpers (python-chess is the authority)."""
from __future__ import annotations

from dataclasses import dataclass

import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
               chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
NAME_TO_TYPE = {v: k for k, v in PIECE_NAMES.items()}
SLIDERS = (chess.BISHOP, chess.ROOK, chess.QUEEN)


class ReplayError(ValueError):
    pass


@dataclass
class Replay:
    """boards[i] is the position *before* ply i; boards[-1] is the final position."""

    boards: list[chess.Board]
    moves: list[chess.Move]
    labels: list[str]  # "1.e4", "1...e5", ...
    sans: list[str]

    def ply(self, label: str) -> int:
        try:
            return self.labels.index(label)
        except ValueError:
            raise ReplayError(f"no move {label!r} in this example ({' '.join(self.labels)})") from None

    def position(self, at: str) -> chess.Board:
        """Position after move `at` ('start' = before the first move, 'end' = final)."""
        if at == "start":
            return self.boards[0]
        if at == "end":
            return self.boards[-1]
        return self.boards[self.ply(at) + 1]

    @property
    def final(self) -> chess.Board:
        return self.boards[-1]


def move_label(board: chess.Board, move: chess.Move) -> str:
    n, san = board.fullmove_number, board.san(move)
    return f"{n}.{san}" if board.turn == chess.WHITE else f"{n}...{san}"


def replay(start_fen: str, sans: list[str]) -> Replay:
    try:
        board = chess.Board(start_fen)
    except ValueError as exc:
        raise ReplayError(f"invalid FEN {start_fen!r}: {exc}") from exc
    status = board.status()
    if status != chess.STATUS_VALID:
        raise ReplayError(f"illegal position {start_fen!r} ({status!r})")
    boards, moves, labels, out = [board.copy()], [], [], []
    for i, san in enumerate(sans):
        try:
            move = board.parse_san(san)
        except ValueError:
            try:  # accept UCI too (Qwen and importers sometimes produce it)
                move = chess.Move.from_uci(san)
                if move not in board.legal_moves:
                    raise ValueError
            except ValueError:
                raise ReplayError(f"move {i + 1} ({san!r}) is illegal in {board.fen()}") from None
        labels.append(move_label(board, move))
        out.append(board.san(move))
        moves.append(move)
        board.push(move)
        boards.append(board.copy())
    return Replay(boards, moves, labels, out)


def piece_fact(board: chess.Board, square: chess.Square) -> dict:
    piece = board.piece_at(square)
    return {"piece": PIECE_NAMES[piece.piece_type], "color": "white" if piece.color else "black",
            "square": chess.square_name(square)}


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values()
               if p.color == color and p.piece_type != chess.KING)


def material_signature(board: chess.Board) -> str:
    """'KQRPPvKRR' style signature (white v black), used for similarity."""
    def side(color):
        return "".join(sorted((board.piece_at(s).symbol().upper() for s in chess.SQUARES
                               if board.piece_at(s) and board.piece_at(s).color == color),
                              key="KQRBNP".index))
    return f"{side(chess.WHITE)}v{side(chess.BLACK)}"


def is_worth_attacking(board: chess.Board, attacker_sq: chess.Square, target_sq: chess.Square) -> bool:
    """A real threat: the king, a more valuable piece, or an undefended piece (not a pawn)."""
    attacker, target = board.piece_at(attacker_sq), board.piece_at(target_sq)
    if target is None or attacker is None or target.color == attacker.color:
        return False
    if target.piece_type == chess.KING:
        return True
    if VALUES[target.piece_type] > VALUES[attacker.piece_type]:
        return True
    return target.piece_type != chess.PAWN and not board.attackers(target.color, target_sq)


def threatened_targets(board: chess.Board, attacker_sq: chess.Square) -> list[chess.Square]:
    return [sq for sq in board.attacks(attacker_sq) if is_worth_attacking(board, attacker_sq, sq)]


def developed_minors(board: chess.Board, color: chess.Color) -> int:
    home = chess.BB_RANK_1 if color == chess.WHITE else chess.BB_RANK_8
    minors = board.pieces(chess.KNIGHT, color) | board.pieces(chess.BISHOP, color)
    return len([sq for sq in minors if not chess.BB_SQUARES[sq] & home])


def king_escape_report(board: chess.Board) -> list[dict]:
    """For the side to move: why each square next to its king is unavailable."""
    color = board.turn
    king = board.king(color)
    out = []
    for sq in chess.SquareSet(chess.BB_KING_ATTACKS[king]):
        occupant = board.piece_at(sq)
        if occupant and occupant.color == color:
            out.append({"square": chess.square_name(sq), "blocked_by": PIECE_NAMES[occupant.piece_type]})
            continue
        probe = board.copy(stack=False)
        probe.remove_piece_at(king)
        attackers = probe.attackers(not color, sq)
        if attackers:
            a = min(attackers, key=lambda s: VALUES[board.piece_at(s).piece_type])
            out.append({"square": chess.square_name(sq), "covered_by": piece_fact(board, a)})
        else:
            out.append({"square": chess.square_name(sq), "free": True})
    return out
