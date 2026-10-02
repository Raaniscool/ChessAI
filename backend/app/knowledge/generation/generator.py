"""Generate new, engine-verified puzzle positions for a concept.

    proposal  ← a constructor (procedural, python-chess) or Qwen (a FEN + intended move)
      → rules          python-chess: legal position, legal key move
      → concept        the concept's validator must find the idea after the key move
      → discrimination Stockfish multipv: every move about as good as the key move must
                       use the same idea (else the puzzle doesn't test the concept)
      → line           short engine continuation that shows the gain (tactics only)
      → words          title/explanation/hints filled in from the validator's facts
      → pipeline       the same verify_candidate() every library entry goes through:
                       rules, concept, engine profile, solution, explanation, duplicates
      → saved          only `verified` entries are used. Qwen proposals that end up
                       `needs_review` are kept for a human; procedural ones are dropped.

Nothing here decides chess correctness by itself: python-chess decides legality,
Stockfish decides which moves work, the validators decide whether the idea is on the
board. Qwen only ever *proposes*.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import chess

from .. import dedupe, validators
from ..pipeline import verify_candidate
from ..positions import replay
from .constructors import CONSTRUCTORS, Proposal
from .describe import describe
from .log import GenerationLog, get_log

log = logging.getLogger(__name__)

# requested / weak concept → [(concept the new puzzle is filed under, constructor)]
_FORKS = [("knight_fork", "knight_fork"), ("queen_fork", "queen_fork"), ("pawn_fork", "pawn_fork")]
_MATES = [("back_rank_mate", "back_rank_mate"), ("checkmate", "support_mate")]
_THREATS = [("spotting_threats", "threat")]
PLANS: dict[str, list[tuple[str, str]]] = {
    "fork": _FORKS,
    "knight_fork": [("knight_fork", "knight_fork")],
    "queen_fork": [("queen_fork", "queen_fork")],
    "pawn_fork": [("pawn_fork", "pawn_fork")],
    "double_attack": [("double_attack", "queen_fork"), ("double_attack", "knight_fork")],
    "walked_into_fork": [("knight_fork", "knight_fork")],
    "hanging_piece": [("hanging_piece", "hanging_piece")],
    "captures": [("hanging_piece", "hanging_piece")],
    "hung_piece": _THREATS + [("hanging_piece", "hanging_piece")],
    "hanging_queen": _THREATS,
    "missed_threat": _THREATS,
    "spotting_threats": _THREATS,
    "beginner_mistakes": _THREATS + [("hanging_piece", "hanging_piece")],
    "checkmate": _MATES,
    "mate_in_one": [("mate_in_one", "back_rank_mate"), ("mate_in_one", "support_mate")],
    "back_rank_mate": [("back_rank_mate", "back_rank_mate")],
    "queen_mate": [("queen_mate", "bare_queen_mate")],
    "skewer": [("skewer", "skewer")],
    "pin": [("absolute_pin", "absolute_pin")],
    "absolute_pin": [("absolute_pin", "absolute_pin")],
    "tactics": _FORKS + [("skewer", "skewer"), ("absolute_pin", "absolute_pin"), ("hanging_piece", "hanging_piece")],
    "opposition": [("opposition", "opposition")],
    "king_pawn_endgame": [("opposition", "opposition")],
    "endgames": [("opposition", "opposition"), ("queen_mate", "bare_queen_mate")],
}
MOTIF_OF = {"bare_queen_mate": "support_mate"}


def motif_of(cname: str) -> str:
    return "material_mate" if cname.startswith("material_mate_") else MOTIF_OF.get(cname, cname)
QWEN_HINTS = {
    "knight_fork": "The solution is a knight move that attacks the king and an undefended rook or queen.",
    "queen_fork": "The solution is a queen move that attacks the king and an undefended piece.",
    "pawn_fork": "The solution is a protected pawn push that attacks two pieces.",
    "hanging_piece": "One black piece is undefended and can be captured for free.",
    "back_rank_mate": "The black king is on its back rank behind its own pawns; a rook or queen mates.",
    "support_mate": "The queen mates next to the king, protected by the white king.",
    "skewer": "A bishop, rook or queen checks the king (or attacks the queen) with a piece behind it.",
    "absolute_pin": "A bishop or rook pins a black piece to its king.",
}
CLOSE_CP = 120        # an alternative this close to the key move is "also good" (engine_check "acceptable")
WINNING_CP = 300      # endgame_win: every move that keeps ≥ this is also winning
DISCRIMINATION_PV = 5
EXTEND_MOTIFS = {"knight_fork", "queen_fork", "pawn_fork", "skewer", "absolute_pin"}


def supported(concept_id: str) -> bool:
    return concept_id in PLANS


@dataclass
class GenerationResult:
    concept: str
    accepted: list = field(default_factory=list)       # verified Examples (saved)
    needs_review: list = field(default_factory=list)   # Qwen candidates a human should look at
    rejected: list[dict] = field(default_factory=list)  # {source, motif, fen, stage, reason}
    attempts: int = 0
    elapsed: float = 0.0
    stopped: str = ""  # why generation ended early ("time budget", "no engine", ...)

    def as_dict(self) -> dict:
        return {"concept": self.concept, "accepted": [e.id for e in self.accepted],
                "needs_review": [e.id for e in self.needs_review], "rejected": self.rejected,
                "attempts": self.attempts, "elapsed": round(self.elapsed, 1), "stopped": self.stopped}


class Rejected(Exception):
    def __init__(self, stage: str, reason: str):
        super().__init__(reason)
        self.stage, self.reason = stage, reason


# ------------------------------------------------------------------ helpers
def _validator(library, concept: str) -> tuple[str, dict]:
    spec = library.validator_for(concept)
    if not spec:
        raise Rejected("concept", f"concept {concept} has no validator")
    return spec["type"], {k: v for k, v in spec.items() if k != "type"}


def _check_idea(library, concept: str, fen: str, sans: list[str]) -> dict:
    kind, params = _validator(library, concept)
    try:
        return validators.run(kind, validators.Ctx(replay(fen, sans), key_ply=0), params)
    except validators.Fail as exc:
        raise Rejected("concept", str(exc)) from None


def _close(key_score, alt_score, white: bool, profile: str) -> bool:
    if key_score.is_mate_for(white):  # a forced mate is only matched by a mate at least as fast
        return alt_score.is_mate_for(white) and abs(alt_score.value) <= abs(key_score.value)
    k, a = key_score.for_side(white), alt_score.for_side(white)
    if profile == "endgame_win" and k >= WINNING_CP and a >= WINNING_CP:
        return True
    return k - a <= CLOSE_CP


def _discriminate(library, engine, concept: str, board: chess.Board, key: chess.Move, depth: int) -> list[str]:
    """Every move Stockfish rates about as good as `key` must also show the concept.
    Returns those moves (SAN) as accepted alternatives; raises Rejected otherwise."""
    profile = library.engine_profile_for(concept)
    lines = engine.analyse_lines(board, depth=depth, multipv=DISCRIMINATION_PV, fresh=True)
    white = board.turn == chess.WHITE
    key_line = next((ln for ln in lines if ln.move == key), None)
    if key_line is None:
        best = lines[0].san if lines else "?"
        raise Rejected("discrimination", f"Stockfish doesn't rate {board.san(key)} among the best moves ({best} is best)")
    alternatives = []
    for line in lines:
        if line.move == key or not _close(key_line.score, line.score, white, profile):
            continue
        try:
            _check_idea(library, concept, board.fen(), [line.san])
        except Rejected:
            raise Rejected("discrimination", f"{line.san} is about as good as {key_line.san} but doesn't use the "
                                             "idea, so the position doesn't test it") from None
        alternatives.append(line.san)
    return alternatives


def _extend(engine, board: chess.Board, key: chess.Move, depth: int) -> list[str]:
    """Key move + the opponent's best reply + our follow-up, when the follow-up collects
    material (shows *why* the tactic works). Just the key move otherwise."""
    b = board.copy(stack=False)
    sans = [b.san(key)]
    b.push(key)
    if b.is_game_over():
        return sans
    reply = engine.analyse_lines(b, depth=depth, multipv=1, fresh=True)
    if not reply:
        return sans
    reply_san = b.san(reply[0].move)
    b.push(reply[0].move)
    if b.is_game_over():
        return sans
    follow = engine.analyse_lines(b, depth=depth, multipv=1, fresh=True)
    if not follow or not b.is_capture(follow[0].move):
        return sans
    return sans + [reply_san, b.san(follow[0].move)]


def _id_for(fen: str, key_san: str, concept: str, tier: str) -> str:
    digest = hashlib.sha1(f"{chess.Board(fen).board_fen()} {key_san}".encode()).hexdigest()[:10]
    return f"{'mygen' if tier == 'personal' else 'gen'}_{concept}_{digest}"


def _raw(proposal: Proposal, concept: str, category: str, sans: list[str], alternatives: list[str],
         facts: dict, source: dict, tier: str) -> dict:
    board = chess.Board(proposal.fen)
    side = "white" if board.turn else "black"
    labels = replay(proposal.fen, sans).labels
    words = describe(MOTIF_OF.get(proposal.motif, proposal.motif), facts, labels[0], side)
    return {
        "id": _id_for(proposal.fen, sans[0], concept, tier), "title": words["title"], "concept": concept, "category": category,
        "difficulty": max(1, min(5, proposal.difficulty)), "description": words["description"],
        "start_fen": proposal.fen, "moves": sans, "key_move": labels[0], "accepted": alternatives,
        "prompt": words["prompt"], "hints": words["hints"], "explanation": words["explanation"],
        "tags": words["tags"], "presentation_modes": ["interactive", "hint", "practice"],
        "teaching_purpose": "practice", "source": source,
    }


# ------------------------------------------------------------------ one candidate
def evaluate(proposal: Proposal, concept: str, library, engine, *, depth: int = 14, tier: str = "generated",
             source: dict | None = None, method: str = "generator"):
    """Run one proposal through every check. Returns the VerificationReport of the full
    pipeline (status verified / needs_review / rejected); raises Rejected for the cheaper
    pre-pipeline checks (idea missing, not discriminating)."""
    board = chess.Board(proposal.fen)
    if not board.is_valid():
        raise Rejected("position", f"illegal position ({board.status()!r})")
    key = proposal.key
    if key is None:
        best = engine.analyse_lines(board, depth=depth, multipv=1, fresh=True)
        if not best:
            raise Rejected("engine", "Stockfish found no move")
        key = best[0].move
    if key not in board.legal_moves:
        raise Rejected("moves", "the intended move is illegal")
    for t in ("generated", "personal"):  # same position + answer already made (the pipeline's
        known = _id_for(proposal.fen, board.san(key), concept, t)  # duplicate check skips same-id entries)
        if known in library.entries:
            raise Rejected("duplicate", f"this position is already in the library ({known})")
    facts = _check_idea(library, concept, proposal.fen, [board.san(key)])
    alternatives = _discriminate(library, engine, concept, board, key, depth)
    sans = _extend(engine, board, key, depth) if proposal.motif in EXTEND_MOTIFS else [board.san(key)]
    if len(sans) > 1:  # the facts must describe the line that is stored
        facts = _check_idea(library, concept, proposal.fen, sans)
    category = library.concepts[concept].category
    raw = _raw(proposal, concept, category, sans, alternatives, facts, source or {}, tier)
    report = verify_candidate(raw, library, engine, depth=depth, tier=tier, method=method)
    return report


def _duplicate_of(example, library, accepted: list) -> str | None:
    """The pipeline checks global + generated; personal entries and this run's picks too."""
    pool = [e for e in library.entries.values() if e.tier == "personal" and e.id != example.id] + accepted
    dup = dedupe.check(example, pool)
    return None if dup.allowed else dup.reason


# ------------------------------------------------------------------ orchestrator
def _profile(example, engine) -> None:
    """Store the engine's view of each learner move (puzzles.profile) while the engine is up."""
    try:
        from ...puzzles.profile import remember
        remember(example, engine)
    except Exception:  # the puzzle stays valid; the profile falls back to heuristics
        pass


def generate(concept: str, library, engine, *, count: int = 1, tier: str = "generated",
             personal: dict | None = None, seed: int | None = None, time_budget: float = 25.0,
             max_attempts: int = 40, use_qwen: bool | None = None, teacher=None, depth: int = 14,
             gen_log: GenerationLog | None = None, save: bool = True, on_progress=None,
             avoid_positions: set[str] | None = None,
             plans: list[tuple[str, str]] | None = None) -> GenerationResult:
    """Generate up to `count` verified puzzles for `concept`.

    `personal` = {"target_weakness", "evidence": [...], ...} for learner-specific puzzles
    (saved in the personal tier, never mixed into the shared library). `avoid_positions`:
    board FENs never to produce (the learner's own game positions). `plans`: explicit
    [(target concept, constructor)] pairs instead of the concept's default plan (used for
    parameterized constructors such as mates with given material)."""
    started = time.monotonic()
    result = GenerationResult(concept)
    gen_log = gen_log or get_log()
    plans = plans or PLANS.get(concept)
    if not plans:
        result.stopped = f"no generator for {concept}"
        return result
    if engine is None:  # without Stockfish nothing can be verified — don't guess
        result.stopped = "Stockfish is not available, so nothing can be verified"
        return result
    if use_qwen is None:
        from ...config import get_settings
        use_qwen = get_settings().qwen_configured()
    rng = random.Random(seed)
    seen: set[str] = set()

    def pick() -> tuple[str, str]:
        weights = []
        for target, cname in plans:
            recent = gen_log.recent_variants(target)
            used = sum(n for sig, n in recent.items() if sig and sig.split(":")[0] == motif_of(cname))
            weights.append(1.0 / (1 + used))
        return rng.choices(plans, weights=weights, k=1)[0]

    # Latency: a Qwen proposal (seconds on local hardware) runs in a background thread while the
    # constructors keep producing candidates. It's checked like any other candidate once it
    # arrives, and dropped if enough verified positions were found first.
    qwen_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-gen") if use_qwen else None
    qwen_job = None
    try:
        while len(result.accepted) < count:
            if result.attempts >= max_attempts:
                result.stopped = "attempt limit"
                break
            if time.monotonic() - started > time_budget:
                result.stopped = "time budget"
                break
            result.attempts += 1
            target, cname = pick()
            side = rng.choice(["white", "black"])
            via_qwen = False
            proposal = None
            if use_qwen and qwen_job is None and cname in QWEN_HINTS and result.attempts % 3 == 1:
                probe = SimpleNamespace(rejected=[], stopped="")
                qwen_job = (qwen_pool.submit(_qwen_proposal, library, target, cname, side, teacher, probe),
                            target, cname, probe)
            if qwen_job is not None:
                try:  # a quick answer is used right away, a slow one on a later attempt
                    got = qwen_job[0].result(timeout=QWEN_WAIT)
                except FutureTimeout:
                    got = _PENDING
                except Exception as exc:  # a broken proposal is just a failed attempt
                    log.warning("Qwen proposal failed: %s", exc)
                    got = None
                if got is not _PENDING:
                    _, q_target, q_cname, probe = qwen_job
                    qwen_job = None
                    result.rejected.extend(probe.rejected)
                    if probe.stopped == "qwen unavailable":
                        use_qwen = False
                    elif got is not None:
                        proposal, via_qwen, target, cname = got, True, q_target, q_cname
            if proposal is None:
                proposal = _constructed(cname, rng, side)
                if proposal is None:
                    continue
            if proposal.fen in seen or chess.Board(proposal.fen).board_fen() in (avoid_positions or ()):
                continue
            seen.add(proposal.fen)
            source_kind = "qwen_generated" if via_qwen else "procedural"
            source = _source(source_kind, proposal, personal)
            entry = {"concept": target, "motif": proposal.motif, "signature": proposal.signature,
                     "source": source_kind, "fen": proposal.fen, "tier": tier,
                     "target_weakness": (personal or {}).get("target_weakness")}
            try:
                report = evaluate(proposal, target, library, engine, depth=depth, tier=tier, source=source,
                                  method=f"generator:{source_kind}")
            except Rejected as exc:
                _reject(result, gen_log, entry, exc.stage, exc.reason)
                continue
            except Exception as exc:  # engine crash, timeouts: this candidate only...
                log.warning("generation candidate failed: %s", exc)
                _reject(result, gen_log, entry, "engine", f"analysis failed: {exc}")
                if _engine_gone(exc):  # ...unless the engine itself is gone: the rest would fail too
                    result.stopped = "Stockfish stopped working"
                    break
                continue
            if report.status == "verified":
                dup = _duplicate_of(report.example, library, result.accepted)
                if dup:
                    _reject(result, gen_log, entry, "duplicate", dup)
                    continue
                if save:
                    library.save_entry(report.example)
                    _profile(report.example, engine)
                result.accepted.append(report.example)
                gen_log.record({**entry, "outcome": "verified", "id": report.example.id})
                if on_progress:
                    on_progress(report.example)
            elif report.status == "needs_review" and via_qwen and report.example is not None:
                if save:
                    library.save_entry(report.example)
                result.needs_review.append(report.example)
                gen_log.record({**entry, "outcome": "needs_review", "id": report.example.id})
            else:
                failed = next((s for s in report.stages if s.outcome in ("fail", "uncertain")), None)
                _reject(result, gen_log, entry, failed.name if failed else "pipeline",
                        failed.detail if failed else report.status)
    finally:
        if qwen_pool is not None:
            qwen_pool.shutdown(wait=False, cancel_futures=True)
    result.elapsed = time.monotonic() - started
    return result


def _engine_gone(exc: Exception) -> bool:
    import chess.engine

    from ...engine import EngineUnavailable
    return isinstance(exc, (EngineUnavailable, chess.engine.EngineTerminatedError)) or "event loop dead" in str(exc)


def _reject(result: GenerationResult, gen_log: GenerationLog, entry: dict, stage: str, reason: str) -> None:
    row = {"source": entry["source"], "motif": entry["motif"], "fen": entry["fen"], "stage": stage,
           "reason": reason[:300]}
    result.rejected.append(row)
    gen_log.record({**entry, "outcome": "rejected", "stage": stage, "reason": reason[:300]})


def _constructed(cname: str, rng: random.Random, side: str, tries: int = 40) -> Proposal | None:
    build = CONSTRUCTORS[cname]
    for _ in range(tries):  # constructors bail out on unlucky random draws; that's cheap
        proposal = build(rng, side)
        if proposal is not None:
            return proposal
    return None


QWEN_WAIT = 0.05   # seconds an attempt waits for a pending Qwen proposal before building its own
_PENDING = object()


def _qwen_proposal(library, target: str, cname: str, side: str, teacher, result):
    from ...teacher.qwen import TeacherUnavailable
    from .qwen_proposer import QwenProposalError, propose

    concept = library.concepts[target]
    motif = MOTIF_OF.get(cname, cname)
    try:
        return propose(concept.name, concept.summary, side, motif, QWEN_HINTS[cname], teacher=teacher)
    except QwenProposalError as exc:
        result.rejected.append({"source": "qwen_generated", "motif": motif, "fen": "", "stage": "rules",
                                "reason": str(exc)[:300]})
    except TeacherUnavailable:
        result.stopped = "qwen unavailable"
    return None


def _source(kind: str, proposal: Proposal, personal: dict | None) -> dict:
    src = {
        "source_type": kind,
        "source_id": f"{proposal.motif}:{proposal.variant}",
        "source_license": "generated by ChessAI (no third-party content)",
        "reference": ("Position proposed by Qwen, checked by python-chess and Stockfish" if kind == "qwen_generated"
                      else "Position built by ChessAI's generator, checked by python-chess and Stockfish"),
        "import_date": date.today().isoformat(),
        "generated": True,
    }
    if personal:
        src["personal"] = {k: v for k, v in personal.items() if v not in (None, "", [], {})}
    return src
