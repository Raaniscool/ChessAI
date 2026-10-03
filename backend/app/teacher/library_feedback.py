"""Instant feedback for a correct move in a verified Knowledge Library example.

A verified example already carries checked teaching text: its explanation (the pipeline's
explanation-consistency stage checked every piece, square and move it mentions), the move
notes of its line, the line itself (Stockfish-verified, so the opponent's reply is the real
best defence) and the moves Stockfish accepted as equally good. When the learner finds the
move, that is what they see — at once, with no language model in between. The AI teacher
is still one click away for a deeper or personal explanation (session.example_explain),
and it still explains wrong moves, where the learner's own move needs an answer.

    feedback = library_feedback(example, board_before, san, accepted, final=True, said=continue_text)
    -> {"text": "...", "sentences": [...]}  or None (not a verified example / nothing stored)

Only stored, verified text is used; nothing is generated here.
"""
from __future__ import annotations

import re

import chess

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
EXPLANATION_LIMIT = 420  # characters of the stored explanation shown in the feedback card


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(" ".join((text or "").split())) if s.strip()]


def _norm(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence.strip().lower())


def strip_said(text: str, said: set[str]) -> str:
    """`text` without the sentences already shown (paragraphs kept). Used so the lesson's
    closing explanation doesn't repeat what the instant feedback just said."""
    if not said:
        return text
    paras = []
    for para in (text or "").split("\n\n"):
        kept = [s for s in sentences(para) if _norm(s) not in said]
        if kept:
            paras.append(" ".join(kept))
    return "\n\n".join(paras)


def _ply_of(example, board: chess.Board) -> int | None:
    rep = example.replay()
    for i, b in enumerate(rep.boards[:-1]):
        if b.board_fen() == board.board_fen() and b.turn == board.turn:
            return i
    return None


def _limited(parts: list[str], limit: int) -> list[str]:
    out, size = [], 0
    for s in parts:
        if out and size + len(s) > limit:
            break
        out.append(s)
        size += len(s) + 1
    return out


def library_feedback(example, board: chess.Board, san: str, accepted: list[str], *, final: bool,
                     said: str = "", idea: str | None = None) -> dict | None:
    """Verified text for the correct move `san` played in `board` (None: use the usual path).

    final  -- the learner's last move of this example: the whole idea can be explained now
              (earlier moves only get their own verified move note, to keep later moves unspoiled).
    said   -- text already on screen for this move (the step's continue text), not repeated.
    idea   -- the concept's name, said once the example is solved."""
    if example is None or getattr(example, "status", "") != "verified":
        return None
    ply = _ply_of(example, board)
    if ply is None:
        return None
    labels, moves = example.labels, example.moves
    on_line = ply < len(moves) and moves[ply] == san
    already = {_norm(s) for s in sentences(said)}
    parts: list[str] = []

    note = example.notes.get(labels[ply], "") if on_line else ""
    parts += [s for s in sentences(note) if _norm(s) not in already]
    if final:
        stored = [s for s in sentences(example.explanation or example.description)
                  if _norm(s) not in already and s not in parts]
        parts += _limited(stored, EXPLANATION_LIMIT)
    if on_line and ply + 1 < len(moves):  # the verified line's reply: the opponent's best defence
        reply_note = example.notes.get(labels[ply + 1], "")
        side = "White" if (ply + 1) % 2 == (0 if chess.Board(example.start_fen).turn else 1) else "Black"
        reply = f"{side}'s best reply is {moves[ply + 1]}."
        if reply_note and _norm(reply_note) not in already:
            reply = f"{side}'s best reply is {moves[ply + 1]}: {reply_note[0].lower() + reply_note[1:]}"
            reply = reply if reply.endswith((".", "!", "?")) else reply + "."
        parts.append(reply)
    others = [m for m in accepted if m != san]
    if others:
        parts.append(f"{' or '.join(others)} would also have worked — Stockfish rates "
                     f"{'it' if len(others) == 1 else 'them'} as good.")
    elif not on_line:
        parts.append(f"Stockfish rates {san} as good as the move in the stored line ({moves[ply]}).")
    if final and idea and parts:
        parts.append(f"The idea: {idea[0].lower() + idea[1:]}.")
    if not parts:
        return None
    text = " ".join(parts)
    return {"text": text, "sentences": [_norm(s) for s in sentences(text)]}
