"""How much a move deserves to be explained ("instructional importance").

A coach doesn't give a paragraph for every move. Each graded move gets one of:

    critical     the moment the lesson is about, or a real error to learn from:
                 a mistake/blunder, a missed mate, the key move of a tactic
                 (check, capture of more than it risks, sacrifice, mate)
    important    worth a few sentences: an inaccuracy, a sound move that isn't the
                 lesson's idea, the key move of a quieter example
    supporting   a correct move inside a line the learner is already following
    obvious      forced or natural: the only legal move, a recapture, finishing a
                 mate that was already set up

The level decides explanation length, whether the AI teacher is asked at all, and
whether it's read aloud automatically (see ``budget``). Facts only: everything used
here comes from python-chess and the engine's verdict.
"""
from __future__ import annotations

import chess

CRITICAL, IMPORTANT, SUPPORTING, OBVIOUS = "critical", "important", "supporting", "obvious"
LEVELS = (CRITICAL, IMPORTANT, SUPPORTING, OBVIOUS)
VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

# Words for the explanation, by importance; scaled by the learner's style preference.
WORDS = {CRITICAL: 70, IMPORTANT: 45, SUPPORTING: 20, OBVIOUS: 8}
STYLE = {"brief": 0.6, "balanced": 1.0, "detailed": 1.5}


def classify(feedback, *, accepted: bool | None, key_move: bool = False,
             board: chess.Board | None = None) -> tuple[str, list[str]]:
    """(importance, reasons) for a graded move. `key_move`: the first move the learner
    has to find in this example; later moves of the same line are follow-ups."""
    category = feedback.category.value
    reasons: list[str] = []
    if "missed_mate" in feedback.notes and not accepted:
        return CRITICAL, ["missed a forced mate"]
    if category in ("blunder", "mistake"):
        return CRITICAL, [f"{category}"]
    if accepted is False:
        # not the move the exercise wants: say why, briefly for sound alternatives
        return IMPORTANT, ["not the exercise's move" if category in ("excellent", "good") else category]
    if category == "inaccurate":
        return IMPORTANT, ["inaccuracy"]

    move = None
    if board is not None:
        try:
            move = chess.Move.from_uci(feedback.user_move_uci)
        except ValueError:
            move = None
    if board is not None and move is not None and move in board.legal_moves:
        legal = board.legal_moves.count()
        tactical = _tactical(board, move)
        if legal == 1:
            return OBVIOUS, ["the only legal move"]
        if key_move:
            return (CRITICAL, ["key move"] + tactical) if tactical else (IMPORTANT, ["key move"])
        if _recapture(board, move):
            return OBVIOUS, ["recapture"]
        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            return SUPPORTING, ["delivers the mate"]
        return SUPPORTING, ["follow-up move"] + tactical
    if key_move:
        return IMPORTANT, ["key move"]
    return SUPPORTING, reasons or ["correct move"]


def _tactical(board: chess.Board, move: chess.Move) -> list[str]:
    out = []
    if board.gives_check(move):
        out.append("check")
    captured = board.piece_at(move.to_square)
    piece = board.piece_at(move.from_square)
    if captured is not None:
        out.append("capture")
    if piece is not None and piece.piece_type != chess.KING:
        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            out.append("mate")
        elif after.is_attacked_by(not board.turn, move.to_square) and \
                VALUE[piece.piece_type] > (VALUE[captured.piece_type] if captured else 0) + 1:
            out.append("sacrifice")
    return out


def _recapture(board: chess.Board, move: chess.Move) -> bool:
    if not board.move_stack:
        return False
    last = board.peek()
    return board.is_capture(move) and move.to_square == last.to_square and board.is_capture(move)


def budget(importance: str, style: str = "balanced") -> int:
    """Target explanation length in words."""
    return max(6, round(WORDS.get(importance, 45) * STYLE.get(style, 1.0)))


def wants_ai(importance: str, style: str = "balanced") -> bool:
    """Is a Qwen explanation worth the wait? Not for supporting/obvious moves (unless the
    learner asked for detailed explanations and it's a supporting move)."""
    return importance in (CRITICAL, IMPORTANT) or (importance == SUPPORTING and style == "detailed")


def read_aloud(importance: str) -> bool:
    """Auto-read (when the learner turned it on) only what matters."""
    return importance in (CRITICAL, IMPORTANT)
