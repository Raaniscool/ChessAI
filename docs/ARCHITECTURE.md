# Architecture — AI Chess Tutor

## Principles

1. **Engine = truth, Qwen = explanation, App = interactivity.** Stockfish determines what happened; the LLM explains it; the application makes it interactive.
2. **Correctness before confidence.** Anything that touches the board is validated by `python-chess`. If engine facts are missing, the teacher says analysis is needed — it never invents chess facts.
3. **Lessons are data.** Adding a lesson or course means adding JSON, not code.

## Data flow (core teaching loop)

```
User chooses lesson
      │
      ▼
Lesson JSON (validated at load: FEN legal, moves legal, hints present)
      │  teach / demonstrate steps
      ▼
Board commands (fen, moves, highlights, lock) ──► validated again at send time
      │                                             by chess_system.validate_board_spec
      ▼
Exercise step: learner submits a move (UCI)
      │
      ▼
python-chess validates legality against the server-owned board ── reject if illegal
      │
      ▼
Stockfish evaluates: position before (best play) + position after the learner's move
      │
      ▼
classify_move() → {excellent | good | inaccurate | mistake | blunder} + facts
      │
      ▼
Teacher (Qwen or fallback) receives STRUCTURED FACTS + lesson context ──► explanation
      │
      ▼
Next teaching position / completion
```

## Safe AI board manipulation

The server owns the authoritative `chess.Board` per session; the browser only renders state it is given.

- Board commands follow a fixed schema: `{fen?, moves?, highlights?, lock?}`.
- Every command — whether authored in lesson JSON today or produced by an AI tool
  tomorrow — must pass `validate_board_spec()`: FEN must parse, moves must be legal
  in sequence, highlight squares/colors must be well-formed, unknown keys rejected.
- Qwen does **not** emit board state. It emits text. Board manipulation comes from
  validated lesson data; a future "AI can move pieces" feature will expose a tool
  whose arguments run through the same validator before the board changes.

## Move classification policy (engine/classification.py)

Loss = best-play eval − eval after the learner's move (learner's POV), with
thresholds excellent ≤15cp, good ≤50, inaccurate ≤120, mistake ≤300, blunder >300
(mates converted to ±(10000 − 10·moves)). Two modifiers:

- **missed_mate** — losing a forced mate is always at least a mistake (usually a blunder via the loss itself).
- **decided_position** — in a completely decided position (|eval| ≥ 700 before and after, same sign), a blunder is dampened to a mistake.

Three consistency rules keep the verdict, the lesson and the explanation in agreement:

- **Checkmate on the board** is its own score (`Score.checkmate`, worth the full 10000):
  a delivered mate is never a "mate in 0" of unknown sign, so it's always graded Excellent.
- **The engine's own best move loses 0** — the before/after scores come from two
  searches whose noise must never turn Stockfish's choice into a "mistake".
- **A move the lesson accepts is shown as at worst Good** (`session.agree_with_lesson`,
  note `engine_prefers`): lesson solutions were verified under the teaching policy
  (within 120 cp, or still clearly winning), so the card never says "Blunder" next to
  "Correct!"; the explanation names the engine's stronger move instead.

The move prompt states the lesson result ("CORRECT" / "NOT SOLVED"), and
`teacher/consistency.py` checks Qwen's finished reply: praise for a rejected or bad
move, or blame for an accepted good one, is replaced by the fallback explanation of
the same facts (NDJSON `replace` + `done.corrected`).

Thresholds live in exactly one place and are unit-tested. Further refinements
(tactical SEE checks, phase scaling, borderline re-search at higher depth) are
planned Phase 3 work.

## Teacher layer

- `QwenTeacher` — OpenAI-compatible `/chat/completions` against `QWEN_BASE_URL` (Ollama/LM Studio/vLLM). Prompts carry engine facts + lesson context and instruct: never invent analysis; if facts are missing, say engine analysis is needed.
- `FallbackTeacher` — deterministic templates built strictly from engine facts; also the behavior when Qwen is unreachable (`explain_or_fallback` / `chat_or_fallback` degrade instead of failing).
- `get_teacher()` picks based on `QWEN_MODEL`.

### Latency design (local models)

`POST /move` returns the engine verdict with the deterministic explanation immediately and never
calls Qwen. When Qwen is configured the response carries `ai_explanation: true`; the browser then
opens `POST /explain`, which streams NDJSON events: `start`, `delta`, `replace`, `done`. `replace`
covers the fallback case (Qwen down before any text) and retracting leaked reasoning (a stray
`</think>`). `ThinkFilter` strips reasoning incrementally even when tags are split across tokens.
Chat uses `POST /chat/stream`. The server loads the model at startup (`QWEN_WARMUP`) and caps
replies (`QWEN_MAX_TOKENS`).

On CPU-only machines time-to-first-word is dominated by prompt processing, so prompts are kept
small: move facts are short lines (no FENs: the model must not calculate from them anyway), engine
lines are trimmed to 4 plies, evaluations are given from the student's side, and chat sends the last
6 turns. `SYSTEM_PROMPT` is byte-identical across requests and the warm-up request includes it, so
Ollama/llama.cpp prefix caching means only the per-move facts are read. On Ollama every request
sends `reasoning_effort: "none"` (thinking off; ignored by models without a thinking mode).

## Lesson schema

`teach` (text + optional board), `demonstrate` (fen + legal move sequence + comments), `exercise` (fen, side, prompt, progressive hints, `advance_on`: `any_legal` | `accepted_move` | `min_classification`, continue text). Courses list available + planned lessons. Loader validates everything and fails fast (`LessonError`).

## Engine

`UciEngine` wraps Stockfish via `python-chess` (thread-locked singleton). Command resolution: `ENGINE_CMD` → Node + `node_modules/stockfish` WASM build → system `stockfish`. Tests skip engine tests when unavailable; a fake engine can be injected via `set_engine()`.

## Learning plans (planner/)

`POST /api/plans {"goal": "I want to learn the Sicilian"}` → a course of generated lessons.

```
goal ─► Catalog.search (aliases → "as Black against e4" → categories → general curriculum, only for truly general goals)
     └► Qwen (optional): picks catalog topic ids + order + reasons; may propose
        SAN lines for openings NOT in the catalog
                         │
   catalog topics ───────┼──► generator.py ──► lesson JSON ──► lessons.schema.parse_lesson
   Qwen lines ──► python-chess replay ──► Stockfish screen (cut at first mistake)
                         │
                         ▼
   store.py: data/plans/<id>.json  +  LessonLibrary.register_course(kind="plan")
```

- **Only what was asked for.** Matched topics become units; their prerequisites (e.g. opening principles before the Sicilian, K+Q mate before rook endgames) are listed in `plan.prerequisites` and offered in the UI, never added as units. A repertoire request ("what do I play as Black against 1.e4") picks one main catalog opening for that side and move (beginner-friendly first) and offers the others under `related`. The general beginner curriculum is used only when the goal has no specific subject ("I want to get better at chess"); "how to play the Stonewall" gets an honest "not yet", not forks and pins. Qwen's new opening lines are considered only for opening requests.
- **Staged library plans** (`knowledge_lessons.py`): *the idea* (a verified demonstration, then a guided example) → the catalog patterns on the same subject (easier puzzles, then harder several-move ones) → a mixed *review* on your own. Openings go *moves and ideas* → *verified lines* → *the whole line from memory*. Linked catalog topics are added only when the catalog also matches the goal, or the concept is broad ("checkmates" includes mate in one); "scholar's mate" doesn't pull in "attacking f7". Each unit's `reason` starts with its step ("Step 2: …") and the plan card shows it.
- **Qwen organizes, it never supplies truth.** Unknown topic ids are dropped; custom lines must be legal and mistake-free per Stockfish (depth 10, ≥6 plies) or they're reported under `skipped`.
- **Generated lessons use the same schema and validation** as hand-written ones, so sessions, hints, grading and the frontend need no special cases.
- **Catalog audit:** `test_catalog_engine.py` asserts every exercise's accepted move is the engine's top choice (the best move is never rejected), every accepted alternative rates good/excellent, mate-in-one tasks have a mate, open-ended tasks have ≥2 good moves, and every opening line is mistake-free.
- Frontend: the chat routes "I want to learn…" (or any message before a lesson starts) to the planner; the sidebar lists only the learner's plans (built-in courses stay available through the planner and the API, `/api/courses` still returns them).
- Lesson flow: the board takes moves only in exercises. Demonstrations auto-play; step payloads carry `next_type` so the button can say "Your turn — practise it".
- Read aloud (`frontend/speech.js`): Web Speech API, no server. `tokenize` finds SAN moves/squares, `buildSpeech` turns them into words and records their offsets; word-boundary events (or a time estimate for voices without them) map the spoken position back to a move, whose squares get a `marker-speech` on the board and whose text span is highlighted. In game review text (elements with `data-fens`), `frontend/lines.js` turns runs of adjacent moves that chess.js can play from one of those positions into lines: speaking, pointing at or clicking them moves the pieces instead of highlighting; bare squares stay highlights. Pure functions are unit-tested with `node --test` (run from pytest by `test_frontend_js.py`).

## Roadmap alignment

- **Phase 1 (current):** core modules + tests ✅; session API, web UI, end-to-end MVP loop.
- **Phase 2+:** more courses, puzzles + hints, game analysis, personalization, progress — all built on the same validated lesson/session primitives.

## Puzzle library (themed tactics, mates, endgames)

`planner/data/puzzles.json` is generated offline by `scripts/build_puzzle_library.py` from the
Lichess puzzle database (CC0). A topic in `topics.json` opts in with `"puzzle_theme"` (a Lichess
theme tag such as `smotheredMate`) and `"puzzle": {task, hint, done}` texts. For each theme the
builder takes the simplest candidates (3 one-move puzzles, then up to 3-move ones), replays them
with python-chess and runs Stockfish (depth 16, MultiPV 2) at every learner move: the solution must
be the engine's first choice and at least 150 cp (or a mate) better than the second choice, so a
student is never told an equally good move is wrong. On a final mating move all mates are accepted.

`generator.puzzle_lessons` turns a set into "learn the pattern" (3 puzzles) + "practice" lessons:
each puzzle is a `demonstrate` step for the opponent's move followed by an `accepted_move` exercise
per learner move, with the opponent's replies animated in between. Topics that also have hand-made
positions keep their original lesson and gain the puzzles as practice.

Planning never substitutes topics: units are only topics the request matched (a match contained in
a longer match, e.g. "checkmate" inside "smothered checkmate", doesn't count). Topics Qwen adds on
its own are shown as related suggestions; with no match the API answers 422 with suggestions.


## Knowledge Library (verified teaching examples)

`backend/app/knowledge/`: a verified library of examples (rules, tactics, mates, openings,
endgames, mistakes) imported from reliable sources (FIDE rules, the CC0 Lichess opening and
puzzle databases, curated classics) and checked by python-chess, concept validators and
Stockfish before use. Only verified entries are retrieved. See [KNOWLEDGE_LIBRARY.md](KNOWLEDGE_LIBRARY.md).

**In the tutor:**
- *Planning:* `plan_for_goal` (planner) tries the library first. `knowledge/retrieval.py`
  picks a short sequence, and `planner/knowledge_lessons.py` turns it into ordinary
  lessons: demonstration → guided example → practice. With no suitable verified
  example, the catalog → Qwen planner runs unchanged.
- *Sessions:* lesson steps carry `example: <id>`, so the session hands the example's
  verified facts (FEN, moves, legal moves, Stockfish verdicts, motif, explanation) to
  the teacher. Learner moves go through python-chess and Stockfish as in every lesson.

