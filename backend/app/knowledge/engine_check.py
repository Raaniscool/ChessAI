"""Stockfish verification of teaching examples.

Stockfish is the authority on concrete evaluation, but the goal is teaching,
not maximising the engine score. So a move is judged on a scale instead of
"is it the single top move":

    best                 the engine's choice (or within 10 cp of it)
    strong_alternative   within 50 cp of the best move
    acceptable           instructional: within 120 cp, or still clearly winning
    inaccuracy / mistake / blunder   by the app's usual loss thresholds

Other moves that are about as good are recorded as `alternatives`, so an
interactive exercise can accept them instead of calling a good move wrong.

Each concept has an engine *profile* (concepts.json → "engine"):

    none          rules-only concepts (castling, en passant, ...)
    tactic        the learner's moves must be at least acceptable and win something
    mate          as tactic, and the key move must keep a forced mate
    opening       no move of the line may lose more than 120 cp (250 = needs review)
    mistake       the mistake must lose >= 150 cp and the punishment must be good
    endgame_win   the learner's side is winning and every learner move keeps the win
    endgame_draw  the final position is a draw (|eval| <= 80 cp or stalemate)
    defence       a defensive key move: it must be good, and ignoring the threat (passing)
                  must cost at least THREAT_MIN_CP (200 cp) — the threat is real
    zugzwang      as tactic, and the key move is quiet and leaves the opponent in zugzwang:
                  could they pass instead of moving, they would be at least ZUGZWANG_MIN_CP
                  (200 cp) better off
    principle     principle violations (early queen, same piece twice, no development):
                  rarely a single blunder, so the violating side must simply end the
                  line measurably worse (>= 80 cp, 40-80 = needs review)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chess

from ..engine.classification import MATE_CP, Classification, Score, classify_loss

BEST_CP = 10
STRONG_CP = 50
ACCEPTABLE_CP = 120
WINNING_CP = 300
ADVANTAGE_CP = 150
MISTAKE_MIN_LOSS = 150
MISTAKE_REVIEW_LOSS = 80
OPENING_MAX_LOSS = 120
OPENING_REVIEW_LOSS = 250
DRAW_CP = 80
PRINCIPLE_MIN_DROP = 80
PRINCIPLE_REVIEW_DROP = 40
GOOD_CATEGORIES = ("best", "strong_alternative", "acceptable")
THREAT_MIN_CP = 200
ZUGZWANG_MIN_CP = 200


@dataclass
class MoveJudgement:
    move: str
    category: str
    loss_cp: int
    eval_cp: int  # after the move, mover's point of view
    best_move: str
    best_cp: int
    alternatives: list[str] = field(default_factory=list)
    mate_after: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class EngineReport:
    outcome: str  # pass | fail | uncertain | skipped
    profile: str
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "profile": self.profile, "reasons": self.reasons, **self.details}


def _terminal_cp(board: chess.Board, pov: chess.Color) -> int | None:
    if board.is_checkmate():
        return -MATE_CP if board.turn == pov else MATE_CP
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    return None


def eval_cp(engine, board: chess.Board, pov: chess.Color, depth: int) -> tuple[int, bool]:
    """(centipawns from `pov`'s side, is a forced mate for `pov`)."""
    terminal = _terminal_cp(board, pov)
    if terminal is not None:
        return terminal, terminal == MATE_CP
    lines = engine.analyse_lines(board, depth=depth, multipv=1, fresh=True)
    if not lines:
        raise RuntimeError("engine returned no lines")
    score: Score = lines[0].score
    return score.for_side(pov), score.is_mate_for(pov)


def judge_move(engine, board: chess.Board, move: chess.Move, depth: int, multipv: int = 4) -> MoveJudgement:
    mover = board.turn
    lines = engine.analyse_lines(board, depth=depth, multipv=multipv, fresh=True)
    if not lines:
        raise RuntimeError("engine returned no lines")
    best = lines[0]
    best_cp = best.score.for_side(mover)
    mine = next((ln for ln in lines if ln.move == move), None)
    after = board.copy(stack=False)
    after.push(move)
    if mine is not None:
        move_cp, mate_after = mine.score.for_side(mover), mine.score.is_mate_for(mover)
        if after.is_checkmate():
            move_cp, mate_after = MATE_CP, True
    else:
        move_cp, mate_after = eval_cp(engine, after, mover, depth)
    loss = max(0, best_cp - move_cp)
    if move == best.move or loss <= BEST_CP:
        category = "best"
    elif loss <= STRONG_CP:
        category = "strong_alternative"
    elif loss <= ACCEPTABLE_CP or (move_cp >= WINNING_CP and not (best.score.is_mate_for(mover) and not mate_after)):
        category = "acceptable"
    else:
        category = {Classification.MISTAKE: "mistake", Classification.BLUNDER: "blunder"}.get(
            classify_loss(loss), "inaccuracy")
    alternatives = []
    for ln in lines:
        cp = ln.score.for_side(mover)
        if ln.move == move:
            continue
        if best_cp - cp <= STRONG_CP or (cp >= WINNING_CP and move_cp >= WINNING_CP and
                                         ln.score.is_mate_for(mover) == mate_after):
            alternatives.append(ln.san)
    return MoveJudgement(move=board.san(move), category=category, loss_cp=loss, eval_cp=move_cp,
                         best_move=best.san, best_cp=best_cp, alternatives=alternatives, mate_after=mate_after)


def verify(example, engine, profile: str, depth: int = 14) -> EngineReport:
    """Run engine profile `profile` on `example` (a schema.Example)."""
    if profile == "none":
        return EngineReport("skipped", profile, ["rules-only concept: no engine check needed"])
    if engine is None:
        return EngineReport("uncertain", profile, ["engine unavailable"])
    rep = example.replay()
    try:
        return _PROFILES[profile](example, rep, engine, depth)
    except Exception as exc:  # engine crashed / timed out: never pass silently
        return EngineReport("uncertain", profile, [f"engine error: {exc}"])


def _learner_plies(rep, start: int) -> list[int]:
    color = rep.boards[start].turn
    return [i for i in range(start, len(rep.moves)) if rep.boards[i].turn == color]


def _tactic(example, rep, engine, depth, need_mate: bool = False) -> EngineReport:
    if example.key_ply is None:
        return EngineReport("skipped", "tactic", ["demonstration without a key move: rules checks only"])
    judgements = []
    for i in _learner_plies(rep, example.key_ply):
        j = judge_move(engine, rep.boards[i], rep.moves[i], depth)
        j_dict = j.as_dict()
        j_dict["label"] = rep.labels[i]
        judgements.append(j_dict)
    key = judgements[0]
    details = {"depth": depth, "key_move": key, "learner_moves": judgements[1:],
               "alternatives": key["alternatives"]}
    reasons = []
    bad = [j for j in judgements if j["category"] in ("mistake", "blunder")]
    weak = [j for j in judgements if j["category"] == "inaccuracy"]
    if bad:
        return EngineReport("fail", "tactic", [f"{j['label']} is a {j['category']} (loses {j['loss_cp']} cp; "
                                               f"best {j['best_move']})" for j in bad], details)
    if weak:
        reasons += [f"{j['label']} is only an inaccuracy (best {j['best_move']})" for j in weak]
    if need_mate and not key["mate_after"]:
        reasons.append(f"{key['label']} does not keep a forced mate")
    elif key["eval_cp"] < ADVANTAGE_CP:
        reasons.append(f"after {key['label']} the learner is only {key['eval_cp']} cp better — the tactic "
                       f"doesn't clearly win")
    if reasons:
        return EngineReport("uncertain", "tactic", reasons, details)
    if key["alternatives"]:
        reasons.append("other good moves: " + ", ".join(key["alternatives"]))
    return EngineReport("pass", "mate" if need_mate else "tactic", reasons, details)


def _mate(example, rep, engine, depth) -> EngineReport:
    report = _tactic(example, rep, engine, depth, need_mate=True)
    report.profile = "mate"
    return report


def _opening(example, rep, engine, depth) -> EngineReport:
    depth = min(depth, 12)
    evals = []  # side-to-move POV before each ply, then final
    for board in rep.boards:
        cp, _ = eval_cp(engine, board, board.turn, depth)
        evals.append(cp)
    losses = []
    for i in range(len(rep.moves)):
        loss = max(0, evals[i] + evals[i + 1])  # best for mover minus what the move achieves
        losses.append({"label": rep.labels[i], "loss_cp": loss})
    worst = max(losses, key=lambda x: x["loss_cp"])
    details = {"depth": depth, "move_losses": losses, "final_eval_white": evals[-1] if rep.final.turn else -evals[-1]}
    if worst["loss_cp"] > OPENING_REVIEW_LOSS:
        return EngineReport("fail", "opening", [f"{worst['label']} loses {worst['loss_cp']} cp"], details)
    if worst["loss_cp"] > OPENING_MAX_LOSS:
        return EngineReport("uncertain", "opening",
                            [f"{worst['label']} loses {worst['loss_cp']} cp (a gambit? needs review)"], details)
    return EngineReport("pass", "opening", [], details)


def _mistake(example, rep, engine, depth) -> EngineReport:
    i = example.mistake_ply
    if i is None:
        return EngineReport("fail", "mistake", ["no mistake_move"])
    mistake = judge_move(engine, rep.boards[i], rep.moves[i], depth)
    details = {"depth": depth, "mistake": {**mistake.as_dict(), "label": rep.labels[i]}}
    if mistake.loss_cp < MISTAKE_REVIEW_LOSS:
        return EngineReport("fail", "mistake", [f"{rep.labels[i]} only loses {mistake.loss_cp} cp — "
                                                f"not a real mistake (best {mistake.best_move})"], details)
    reasons = []
    if mistake.loss_cp < MISTAKE_MIN_LOSS:
        reasons.append(f"{rep.labels[i]} loses {mistake.loss_cp} cp — borderline")
    if i + 1 < len(rep.moves):
        punish = judge_move(engine, rep.boards[i + 1], rep.moves[i + 1], depth)
        details["punishment"] = {**punish.as_dict(), "label": rep.labels[i + 1]}
        if punish.category not in GOOD_CATEGORIES:
            reasons.append(f"the punishment {rep.labels[i + 1]} is itself a {punish.category} "
                           f"(best {punish.best_move})")
    return EngineReport("uncertain" if reasons else "pass", "mistake", reasons, details)


def _endgame_win(example, rep, engine, depth) -> EngineReport:
    start = example.key_ply or 0
    color = rep.boards[start].turn
    cp, _ = eval_cp(engine, rep.boards[start], color, depth)
    details = {"depth": depth, "start_eval": cp}
    if cp < WINNING_CP:
        return EngineReport("fail", "endgame_win", [f"the position is not winning ({cp} cp)"], details)
    worst = None
    for i in _learner_plies(rep, start):
        after_cp, _ = eval_cp(engine, rep.boards[i + 1], color, depth)
        if after_cp < WINNING_CP:
            worst = (rep.labels[i], after_cp)
            break
    if worst:
        return EngineReport("fail", "endgame_win", [f"{worst[0]} throws away the win ({worst[1]} cp)"], details)
    return EngineReport("pass", "endgame_win", [], details)


def _endgame_draw(example, rep, engine, depth) -> EngineReport:
    final = rep.final
    cp, _ = eval_cp(engine, final, chess.WHITE, depth)
    details = {"depth": depth, "final_eval_white": cp}
    if abs(cp) > DRAW_CP:
        return EngineReport("uncertain", "endgame_draw", [f"final position is not clearly drawn ({cp} cp)"], details)
    return EngineReport("pass", "endgame_draw", [], details)


def _principle(example, rep, engine, depth) -> EngineReport:
    i = example.mistake_ply
    if i is None:
        return EngineReport("fail", "principle", ["no mistake_move"])
    side = rep.boards[i].turn
    before, _ = eval_cp(engine, rep.boards[i], side, depth)
    after, _ = eval_cp(engine, rep.final, side, depth)
    drop = before - after
    details = {"depth": depth, "side": "white" if side else "black", "eval_before": before,
               "eval_end": after, "drop_cp": drop}
    label = rep.labels[i]
    if drop < PRINCIPLE_REVIEW_DROP:
        return EngineReport("fail", "principle", [f"from {label} to the end the position only changes by "
                                                  f"{drop} cp — the lesson isn't borne out"], details)
    if drop < PRINCIPLE_MIN_DROP:
        return EngineReport("uncertain", "principle", [f"only {drop} cp worse by the end — borderline"], details)
    return EngineReport("pass", "principle", [], details)


def _defence(example, rep, engine, depth) -> EngineReport:
    """A defensive key move: it must be good, and the threat must be real — if the learner
    just passed, they would lose at least THREAT_MIN_CP."""
    if example.key_ply is None:
        return EngineReport("fail", "defence", ["no key move"])
    board = rep.boards[example.key_ply]
    side = board.turn
    key = judge_move(engine, board, rep.moves[example.key_ply], depth).as_dict()
    key["label"] = rep.labels[example.key_ply]
    details = {"depth": depth, "key_move": key, "alternatives": key["alternatives"]}
    if key["category"] not in GOOD_CATEGORIES:
        return EngineReport("fail", "defence", [f"{key['label']} is a {key['category']} (best {key['best_move']})"],
                            details)
    passed = board.copy(stack=False)
    passed.push(chess.Move.null())
    passed = chess.Board(passed.fen())  # "pass": same position, other side to move (no null move in history)
    if passed.is_check() or not any(passed.legal_moves):
        return EngineReport("fail", "defence", ["the threat can't be measured (null move gives check)"], details)
    ignored, _ = eval_cp(engine, passed, side, depth)
    details["eval_if_ignored"] = ignored
    drop = key["eval_cp"] - ignored
    details["threat_cp"] = drop
    if drop < THREAT_MIN_CP:
        return EngineReport("fail", "defence", [f"ignoring the threat only costs {drop} cp — it isn't a real threat"],
                            details)
    if key["eval_cp"] < -DRAW_CP:
        return EngineReport("uncertain", "defence", [f"even after {key['label']} the learner is worse "
                                                     f"({key['eval_cp']} cp)"], details)
    return EngineReport("pass", "defence", [], details)


def _zugzwang(example, rep, engine, depth) -> EngineReport:
    """The tactic checks, then the zugzwang itself: after the quiet key move the opponent is
    to move, and moving costs them at least ZUGZWANG_MIN_CP compared with passing."""
    report = _tactic(example, rep, engine, depth)
    report.profile = "zugzwang"
    if report.outcome != "pass":
        return report
    after = rep.boards[example.key_ply + 1]
    opponent = after.turn
    if after.is_check() or not any(after.legal_moves):
        return EngineReport("fail", "zugzwang", ["the opponent is in check or has no move: not a zugzwang"],
                            report.details)
    passed = chess.Board(after.fen())
    passed.turn = not opponent  # "pass": same position, the learner to move again
    passed.ep_square = None
    if passed.is_check() or not passed.is_valid():
        return EngineReport("fail", "zugzwang", ["passing can't be measured in this position"], report.details)
    to_move, _ = eval_cp(engine, after, opponent, depth)
    if_passing, _ = eval_cp(engine, passed, opponent, depth)
    cost = if_passing - to_move
    report.details.update({"opponent_to_move": to_move, "opponent_if_passing": if_passing, "zugzwang_cp": cost})
    if cost < ZUGZWANG_MIN_CP:
        return EngineReport("fail", "zugzwang", [f"having to move only costs the opponent {cost} cp — "
                                                 f"not a zugzwang"], report.details)
    return report


PRACTICAL_ACCEPT_CP = 40   # the key move may be this much below Stockfish's best
PRACTICAL_GAP_CP = 100     # ...and some natural alternative must be at least this much worse
PRACTICAL_DECIDED_CP = 900


def _practical(example, rep, engine, depth) -> EngineReport:
    """A practical position (material endgames): not a forced mate, not already decided, the
    key move is (nearly) Stockfish's best, and the choice matters — an alternative is clearly
    worse. Accepted alternatives are checked by the solution stage."""
    if example.key_ply is None:
        return EngineReport("fail", "practical", ["no key move"])
    board = rep.boards[example.key_ply]
    side = board.turn
    lines = engine.analyse_lines(board, depth=depth, multipv=4, fresh=True)
    if not lines:
        return EngineReport("fail", "practical", ["engine returned no lines"])
    scores = [l.score.for_side(side == chess.WHITE) for l in lines]
    best = scores[0]
    details = {"depth": depth, "side": "white" if side else "black", "best_cp": best,
               "best_move": lines[0].san}
    if abs(best) >= PRACTICAL_DECIDED_CP * 10:
        return EngineReport("fail", "practical", ["a forced mate — a tactic, not a practical position"], details)
    if abs(best) > PRACTICAL_DECIDED_CP:
        return EngineReport("fail", "practical", [f"already decided ({best} cp)"], details)
    key = rep.moves[example.key_ply]
    key_cp = next((s for l, s in zip(lines, scores) if l.move == key), None)
    if key_cp is None:
        key_cp, _ = eval_cp(engine, rep.boards[example.key_ply + 1], side, depth)
    details["key_cp"] = key_cp
    if best - key_cp > PRACTICAL_ACCEPT_CP:
        return EngineReport("fail", "practical", [f"the key move is {best - key_cp} cp below the best"], details)
    if not any(best - s >= PRACTICAL_GAP_CP for s in scores[1:]):
        return EngineReport("fail", "practical", ["every move is about as good — nothing to find"], details)
    return EngineReport("pass", "practical", [], details)


def _underpromotion(example, rep, engine, depth) -> EngineReport:
    """The tactic profile, plus: promoting to a queen on the same move must be clearly worse
    (at least MISTAKE_MIN_LOSS) or stalemate — otherwise the underpromotion teaches nothing."""
    report = _tactic(example, rep, engine, depth)
    report.profile = "underpromotion"
    if report.outcome == "fail" or example.key_ply is None:
        return report
    for i in _learner_plies(rep, example.key_ply):
        move = rep.moves[i]
        if move.promotion not in (chess.KNIGHT, chess.BISHOP, chess.ROOK):
            continue
        board = rep.boards[i]
        side = board.turn
        queen = board.copy(stack=False)
        queen.push(chess.Move(move.from_square, move.to_square, chess.QUEEN))
        actual, _ = eval_cp(engine, rep.boards[i + 1], side, depth)
        if rep.boards[i + 1].is_checkmate():
            actual = MATE_CP
        if queen.is_stalemate():
            report.details["queen_instead"] = "stalemate"
            return report
        if queen.is_checkmate():
            return EngineReport("fail", "underpromotion", ["a queen would mate as well"], report.details)
        q_cp, _ = eval_cp(engine, queen, side, depth)
        report.details["queen_instead_cp"] = q_cp
        if actual - q_cp < MISTAKE_MIN_LOSS:
            return EngineReport("fail", "underpromotion",
                                [f"a queen is about as good ({q_cp} cp against {actual} cp)"], report.details)
        return report
    return EngineReport("fail", "underpromotion", ["no underpromotion in the learner's moves"], report.details)


_PROFILES = {"zugzwang": _zugzwang, "practical": _practical, "underpromotion": _underpromotion, "defence": _defence, "principle": _principle, "tactic": _tactic, "mate": _mate, "opening": _opening, "mistake": _mistake,
             "endgame_win": _endgame_win, "endgame_draw": _endgame_draw}
