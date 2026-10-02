"""A puzzle: one verified position where the learner must find the move(s).

Puzzles are *derived* from verified Knowledge Library entries (global, generated, or the
learner's personal tier). Nothing here is decided by Qwen. Every field comes from the entry's
verified data:

- `fen` / `side_to_move`: the board at the key move (replayed with python-chess)
- `solution`: the moves from there on (learner moves and the opponent's replies)
- `accepted_first`: moves accepted at the first step (the key move + accepted alternatives)
- `uniqueness`: "unique" when Stockfish found no equally good alternative at any learner
  move, "multiple" when it did (those moves are accepted), "unchecked" when the entry has
  no engine record
- `rating`: the estimated rating needed to solve it about half the time (knowledge.difficulty)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

TYPES = ("tactic", "checkmate", "calculation", "defense", "endgame", "opening", "mistake_correction")
# concepts whose puzzles ask the learner to *stop* something (find the defence)
DEFENSIVE = {"spotting_threats", "missed_threat", "hanging_queen", "hung_piece"}
CALCULATION_MOVES = 3  # a tactic needing this many learner moves is a calculation puzzle


@dataclass(frozen=True)
class Puzzle:
    id: str                 # same id as the Knowledge Library entry it comes from
    type: str
    concept: str
    concepts: tuple[str, ...]
    difficulty: int         # 1-5 label from the library
    rating: int             # estimated solve rating
    fen: str                # the position the learner sees (learner to move)
    side_to_move: str
    solution: tuple[str, ...]          # SAN from the key move on (learner, reply, learner, ...)
    learner_moves: int
    accepted_first: tuple[str, ...]
    uniqueness: str         # unique | multiple | unchecked
    main_idea: str
    required_skill: str
    tags: tuple[str, ...]
    source: dict = field(default_factory=dict, compare=False, hash=False)
    verification_state: str = "verified"
    tier: str = "global"    # global | generated | personal
    weakness: str | None = None   # personal puzzles: the weakness they were made for
    # Lichess-style profile (puzzles.profile): what the learner actually has to find
    steps: tuple = field(default=(), compare=False, hash=False)   # per learner move: kind, accepted, reply
    objective: str = "idea"         # mate | material | defense | idea (where the puzzle ends)
    critical_moves: tuple[str, ...] = ()   # SAN of the real decisions
    forced_moves: tuple[str, ...] = ()     # SAN of forced/obvious learner moves
    decision_points: tuple[int, ...] = ()  # which learner moves (1-based) are critical
    meaningful_moves: int = 1
    trimmed: int = 0                # moves of the stored line cut after the objective
    clear_start: bool = True        # the first move is a real decision
    engine_profiled: bool = False
    difficulty_basis: dict = field(default_factory=dict, compare=False, hash=False)
    facts: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def primary_concept(self) -> str:
        return self.concept

    @property
    def expected_solution_length(self) -> int:
        return self.learner_moves

    def as_dict(self) -> dict:
        d = asdict(self)
        for key in ("concepts", "solution", "accepted_first", "tags", "steps", "critical_moves", "forced_moves",
                    "decision_points"):
            d[key] = list(d[key])
        d["primary_concept"] = self.primary_concept
        d["expected_solution_length"] = self.expected_solution_length
        return d


def puzzle_type(category: str, concept: str, mistake: bool, learner_moves: int) -> str:
    if concept in DEFENSIVE:
        return "defense"
    if category == "mistakes":  # elsewhere mistake_move is the opponent's error being punished
        return "mistake_correction"
    if category == "checkmates":
        return "checkmate"
    if category == "endgames":
        return "endgame"
    if category == "openings":
        return "opening"
    if learner_moves >= CALCULATION_MOVES:
        return "calculation"
    return "tactic"


def uniqueness_of(verification: dict, accepted: list[str]) -> str:
    engine = (verification or {}).get("engine") or {}
    if not engine:
        return "multiple" if accepted else "unchecked"
    steps = [engine.get("key_move") or {}] + list(engine.get("learner_moves") or [])
    alternatives = [a for s in steps for a in (s.get("alternatives") or [])] + list(engine.get("alternatives") or [])
    return "multiple" if (alternatives or accepted) else "unique"


NOT_PUZZLES = {"basics"}  # rule drills (how en passant works) are lessons, not puzzles
MATE_FAMILY = {"checkmate", "mate_in_one", "back_rank_mate", "smothered_mate", "queen_mate", "anastasia_mate",
               "arabian_mate", "boden_mate", "support_mate"}
RATING_BANDS = ((700, 1), (1000, 2), (1400, 3), (1800, 4))


def difficulty_of(rating: int) -> int:
    return next((label for top, label in RATING_BANDS if rating < top), 5)


def accepted_by_ply(example, ply: int) -> dict[int, list[str]]:
    """{ply: [SAN]} moves verified as equally good at each learner move."""
    engine = (example.verification or {}).get("engine") or {}
    out = {ply: list(dict.fromkeys([*example.accepted, *((engine.get("key_move") or {}).get("alternatives") or [])]))}
    for i, step in enumerate(engine.get("learner_moves") or [], start=1):
        if step.get("alternatives"):
            out[ply + 2 * i] = list(step["alternatives"])
    return out


def family_of(example, library=None) -> str:
    concepts = {example.concept, *example.concepts}
    if example.category == "checkmates" or concepts & MATE_FAMILY or (
            library is not None and example.concept in library.concepts
            and example.concept in library.descendants("checkmate")):
        return "mate"
    if example.concept in DEFENSIVE:  # "mistakes" entries punish a mistake: the learner attacks
        return "defense"
    return "tactic"


def from_example(example, library=None, profiles=None) -> Puzzle | None:
    """The puzzle in a verified entry, or None (no key move, not replayable, not verified)."""
    if example.status != "verified" or not example.key_move or example.category in NOT_PUZZLES:
        return None
    try:
        rep = example.replay()
        ply = example.key_ply
    except (ValueError, AttributeError, IndexError):
        return None
    if ply is None or ply >= len(example.moves):
        return None
    from .profile import build, signature
    board = rep.boards[ply]
    data = profiles.get(example.id, signature(example.start_fen, example.moves, ply)) if profiles else None
    try:
        prof = build(example.start_fen, list(example.moves), ply, family=family_of(example, library),
                     accepted=accepted_by_ply(example, ply), data=data)
    except (ValueError, IndexError):
        return None
    solution = tuple(prof.solution)
    learner_moves = len(prof.steps)
    concept = example.concept
    meta = (example.source or {}).get("personal") or {}
    name = library.concepts[concept].name if library is not None and concept in library.concepts else concept
    src = example.source or {}
    return Puzzle(
        id=example.id,
        type=puzzle_type(example.category, concept, bool(example.mistake_move), learner_moves),
        concept=concept,
        concepts=tuple(dict.fromkeys([concept, *example.concepts, *example.related])),
        difficulty=difficulty_of(prof.rating),
        rating=prof.rating,
        fen=board.fen(),
        side_to_move="white" if board.turn else "black",
        solution=solution,
        learner_moves=learner_moves,
        accepted_first=tuple(dict.fromkeys([solution[0], *example.accepted])),
        uniqueness=uniqueness_of(example.verification, list(example.accepted)),
        main_idea=example.title or name,
        required_skill=concept,
        tags=tuple(example.tags),
        source={"type": src.get("source_type"), "id": src.get("source_id"), "license": src.get("source_license"),
                "url": src.get("source_url")},
        verification_state=example.status,
        tier=example.tier,
        weakness=meta.get("target_weakness"),
        steps=tuple(asdict(st) for st in prof.steps),
        objective=prof.objective,
        critical_moves=tuple(st.san for st in prof.critical),
        forced_moves=tuple(st.san for st in prof.forced),
        decision_points=tuple(i for i, st in enumerate(prof.steps, start=1) if st.kind == "critical"),
        meaningful_moves=len(prof.critical),
        trimmed=prof.trimmed,
        clear_start=prof.clear_start,
        engine_profiled=prof.engine,
        difficulty_basis=prof.basis,
        facts=dict(example.facts or {}),
    )
