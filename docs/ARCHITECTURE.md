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

## Lesson schema

`teach` (text + optional board), `demonstrate` (fen + legal move sequence + comments), `exercise` (fen, side, prompt, progressive hints, `advance_on`: `any_legal` | `accepted_move` | `min_classification`, continue text). Courses list available + planned lessons. Loader validates everything and fails fast (`LessonError`).

## Engine

`UciEngine` wraps Stockfish via `python-chess` (thread-locked singleton). Command resolution: `ENGINE_CMD` → Node + `node_modules/stockfish` WASM build → system `stockfish`. Tests skip engine tests when unavailable; a fake engine can be injected via `set_engine()`.

## Learning plans (planner/)

`POST /api/plans {"goal": "I want to learn the Sicilian"}` → a course of generated lessons.

```
goal ─► Catalog.search (aliases, fuzzy tokens, categories, general curriculum)
     └► Qwen (optional): picks catalog topic ids + order + reasons; may propose
        SAN lines for openings NOT in the catalog
                         │
   catalog topics ───────┼──► generator.py ──► lesson JSON ──► lessons.schema.parse_lesson
   Qwen lines ──► python-chess replay ──► Stockfish screen (cut at first mistake)
                         │
                         ▼
   store.py: data/plans/<id>.json  +  LessonLibrary.register_course(kind="plan")
```

- **Qwen organizes, it never supplies truth.** Unknown topic ids are dropped; custom lines must be legal and mistake-free per Stockfish (depth 10, ≥6 plies) or they're reported under `skipped`.
- **Generated lessons use the same schema and validation** as hand-written ones, so sessions, hints, grading and the frontend need no special cases.
- **Catalog audit:** `test_catalog_engine.py` asserts every exercise's accepted move is the engine's top choice (the best move is never rejected), every accepted alternative rates good/excellent, mate-in-one tasks have a mate, open-ended tasks have ≥2 good moves, and every opening line is mistake-free.
- Frontend: the chat routes "I want to learn…" (or any message before a lesson starts) to the planner; plan cards list first in the sidebar.

## Roadmap alignment

- **Phase 1 (current):** core modules + tests ✅; session API, web UI, end-to-end MVP loop.
- **Phase 2+:** more courses, puzzles + hints, game analysis, personalization, progress — all built on the same validated lesson/session primitives.
