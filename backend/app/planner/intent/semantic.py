"""Qwen as the semantic interpreter: "what does the learner mean?" — never "what is true".

For requests that describe material ("beat a queen with two rooks", "I'm up two rooks but
they have a queen") Qwen returns a strict JSON intent: which pieces the learner has, which
the opponent has, what kind of topic it is and what the learner wants to focus on. The
JSON is schema-checked here and turned into a MaterialSpec by deterministic code. Qwen never
produces positions, moves or FENs; those are built and verified elsewhere.

The deterministic reading (intent.material) is always computed too. understand() compares
them: agreement → confirm with the learner when the request is specific; disagreement →
ask which one; Qwen unavailable or invalid → the deterministic reading alone.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from .model import MaterialSpec

log = logging.getLogger(__name__)

TOPIC_TYPES = ("endgame", "checkmate", "opening", "tactics", "strategy", "other")
RELATIONS = ("versus", "together", "single", "none")
SIDES = ("user", "opponent", "either")
FOCUS = ("general", "convert", "coordinate", "avoid_perpetual", "attack_king", "trade", "win_material", "hold")
PIECE_KEYS = {"queens": "Q", "rooks": "R", "bishops": "B", "knights": "N"}
MAX_COUNT = 4
BUDGET = 10.0  # seconds: past this the deterministic reading is used

SYSTEM = (
    "You read a chess learner's request and describe what they mean as JSON. You never give moves, "
    "positions or evaluations. Output ONLY one JSON object with exactly these keys:\n"
    '  "topic_type": one of ' + ", ".join(TOPIC_TYPES) + "\n"
    '  "user_pieces": {"queens": n, "rooks": n, "bishops": n, "knights": n}  — pieces the LEARNER has '
    "(not counting king and pawns)\n"
    '  "opponent_pieces": {"queens": n, "rooks": n, "bishops": n, "knights": n}  — pieces the OPPONENT has\n'
    '  "material_relation": one of ' + ", ".join(RELATIONS) + " (versus = each side has different pieces; "
    "none = the request names no material)\n"
    '  "side_to_train": one of ' + ", ".join(SIDES) + " (whose pieces the learner wants to play)\n"
    '  "requested_focus": one of ' + ", ".join(FOCUS) + " (general if not stated)\n"
    '  "needs_custom_generation": true or false\n'
    "Counts are exact numbers the learner said: 'two rooks' is 2, 'a queen' is 1. "
    "If the learner names pieces for only one side, put zeros for the other side."
)
EXAMPLES = [
    ("beat a queen with two rooks",
     {"topic_type": "endgame", "user_pieces": {"queens": 0, "rooks": 2, "bishops": 0, "knights": 0},
      "opponent_pieces": {"queens": 1, "rooks": 0, "bishops": 0, "knights": 0}, "material_relation": "versus",
      "side_to_train": "user", "requested_focus": "general", "needs_custom_generation": True}),
    ("knight fork puzzles",
     {"topic_type": "tactics", "user_pieces": {"queens": 0, "rooks": 0, "bishops": 0, "knights": 0},
      "opponent_pieces": {"queens": 0, "rooks": 0, "bishops": 0, "knights": 0}, "material_relation": "none",
      "side_to_train": "either", "requested_focus": "general", "needs_custom_generation": False}),
]


class SchemaError(ValueError):
    pass


@dataclass
class SemanticIntent:
    topic_type: str
    user: dict
    opponent: dict
    relation: str
    side_to_train: str
    focus: str
    needs_custom_generation: bool
    raw: dict = field(default_factory=dict)

    @property
    def spec(self) -> MaterialSpec | None:
        """The material as a spec — only for a two-sided request the learner plays."""
        mine = [l for k, l in PIECE_KEYS.items() for _ in range(self.user[k])]
        theirs = [l for k, l in PIECE_KEYS.items() for _ in range(self.opponent[k])]
        if self.relation != "versus" or not mine or not theirs or self.topic_type not in ("endgame", "checkmate"):
            return None
        if self.side_to_train == "opponent":
            mine, theirs = theirs, mine
        return MaterialSpec(tuple(mine), "versus", "endgame", against=tuple(theirs), owner="learner")

    def as_dict(self) -> dict:
        spec = self.spec
        return {"status": "ok", "topic_type": self.topic_type, "requested_material": {"user": self.user,
                "opponent": self.opponent}, "material_relation": self.relation, "side_to_train": self.side_to_train,
                "requested_focus": self.focus, "needs_custom_generation": self.needs_custom_generation,
                "spec": spec.slug() if spec else None}


def _counts(d) -> dict:
    if not isinstance(d, dict):
        raise SchemaError("piece counts must be an object")
    out = {}
    for k in PIECE_KEYS:
        v = d.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_COUNT:
            raise SchemaError(f"bad count for {k}: {v!r}")
        out[k] = v
    extra = set(d) - set(PIECE_KEYS) - {"pawns", "kings", "king"}
    if extra:
        raise SchemaError(f"unknown piece keys {sorted(extra)}")
    return out


def validate(data) -> SemanticIntent:
    """Strict schema check: anything unexpected is rejected, never repaired by guessing."""
    if not isinstance(data, dict):
        raise SchemaError("not a JSON object")

    def pick(key, allowed):
        v = data.get(key)
        if v not in allowed:
            raise SchemaError(f"{key}={v!r} is not one of {allowed}")
        return v

    flag = data.get("needs_custom_generation", False)
    if not isinstance(flag, bool):
        raise SchemaError("needs_custom_generation must be true/false")
    return SemanticIntent(pick("topic_type", TOPIC_TYPES), _counts(data.get("user_pieces")),
                          _counts(data.get("opponent_pieces")), pick("material_relation", RELATIONS),
                          pick("side_to_train", SIDES), pick("requested_focus", FOCUS), flag, data)


def messages(goal: str) -> list[dict]:
    out = [{"role": "system", "content": SYSTEM}]
    for text, answer in EXAMPLES:
        out += [{"role": "user", "content": text}, {"role": "assistant", "content": json.dumps(answer)}]
    return out + [{"role": "user", "content": goal.strip()[:300]}]


@dataclass
class Interpreted:
    """What the interpreter returned, for the debug view: ok / invalid / unavailable / timeout."""
    status: str
    intent: SemanticIntent | None = None
    error: str = ""

    def as_dict(self) -> dict:
        if self.intent is not None:
            return self.intent.as_dict()
        return {"status": self.status, "error": self.error}


def interpret(goal: str, teacher=None, budget: float = BUDGET) -> Interpreted:
    """Ask Qwen what the request means. Never raises: failures are reported, not guessed around."""
    from ...config import get_settings
    from ..planner import extract_json
    if teacher is None:
        if not get_settings().qwen_configured():
            return Interpreted("unavailable", error="Qwen is not configured")
        try:
            from ...teacher.qwen import QwenTeacher
            teacher = QwenTeacher()
        except Exception as exc:  # TeacherUnavailable and friends
            return Interpreted("unavailable", error=str(exc))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-intent")
    try:
        text = pool.submit(teacher.complete, messages(goal), 300).result(timeout=budget)
    except FutureTimeout:
        return Interpreted("timeout", error=f"no answer within {budget:.0f}s")
    except Exception as exc:
        log.warning("semantic interpreter failed: %s", exc)
        return Interpreted("unavailable", error=str(exc))
    finally:
        pool.shutdown(wait=False)
    try:
        return Interpreted("ok", validate(extract_json(text or "")))
    except (SchemaError, ValueError, TypeError) as exc:
        return Interpreted("invalid", error=str(exc)[:200])


def qwen_interpreter(teacher=None):
    """The callable understand() takes: goal → Interpreted."""
    return lambda goal: interpret(goal, teacher)
