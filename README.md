# AI Chess Tutor (ChessAI)

A chess-learning application where **the AI teaches through the board itself**: it demonstrates concepts on a real chessboard, gives the learner positions, validates their moves with a chess library, analyzes them with Stockfish, and has a local Qwen model explain the result — learning by doing, not by reading.

## Architecture (short version)

```
Frontend (cm-chessboard) ──move──► Application (FastAPI)
        ▲                              │
        └──────── board state ◄────────┤── validates every move/command (python-chess)
                                       ├── analyzes positions (Stockfish UCI)
                                       └── explains facts (Qwen, or offline fallback)
```

- **Chess system** — `python-chess` is the only authority on legality, FEN, and history.
- **Engine** — Stockfish 19 (WASM via Node by default; any UCI engine via `ENGINE_CMD`) decides *what happened*.
- **Teacher** — a local Qwen model (OpenAI-compatible endpoint) *explains what happened*, from structured engine facts only. A deterministic fallback teacher keeps the app honest and usable when Qwen isn't running.
- **Application** — owns the authoritative board state and makes lessons interactive. No AI output ever reaches the board unvalidated.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## "I want to learn ___" — personal learning plans

Type what you want to learn (sidebar box, or just say it in the chat: *"I want to learn the Sicilian"*, *"teach me knight forks"*, *"I want to get better at endgames"*). The tutor builds a plan of lessons and adds it to the sidebar.

- Plans are built from a **verified topic catalog** (`backend/app/planner/data/topics.json`): 15 openings plus tactics, endgames and opening principles. Each opening becomes two lessons (watch + first moves, then the whole line from memory); each tactic/endgame topic becomes a set of positions to solve.
- **Correctness:** every catalog line and position is audited by Stockfish in the test suite (`test_catalog_engine.py`). Add a topic to the JSON and it's audited automatically.
- **With Qwen connected** the planner also *organizes* the plan (order, title, why each unit matters) and can add an opening that isn't in the catalog. Qwen's move sequence is only used after python-chess confirms it's legal **and** Stockfish checks it move by move; the line is cut at the first mistake, and anything unverifiable is listed as "left out" instead of being taught.
- Without Qwen the planner works from the catalog alone (keyword + typo-tolerant matching).
- Plans are saved to `data/plans/` (gitignored) and survive restarts. Delete one with the ✕ on its card.

While the teacher is explaining (or after a demonstration) you can **move pieces freely** to try ideas — those moves aren't graded; ↺ resets the board.

## Repository layout

```
backend/app/
  chess_system.py      # strict python-chess helpers (FEN, moves, board-command validation)
  config.py            # env-driven settings (engine cmd, Qwen endpoint, port)
  engine/              # Stockfish service + position-aware move classification
  teacher/             # Qwen client, prompts, deterministic fallback teacher
  lessons/             # lesson schema + loader + JSON course data
  lessons/data/italian_game/   # first course (structured lesson JSON)
  planner/             # "I want to learn ___": catalog, plan builder, lesson generator, storage
  planner/data/topics.json     # verified topic catalog (openings, tactics, endgames, strategy)
backend/tests/         # pytest suite (chess, classification, lessons, engine, teacher)
frontend/              # web UI (vendored cm-chessboard + chess.mjs, no CDNs)
scripts/ui_e2e.mjs     # browser-level UI smoke test (jsdom) against a running server
docs/ARCHITECTURE.md
requirements.txt
package.json           # Stockfish WASM engine dependency
```

## Quick start

```bash
# 1. Python deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Engine (Stockfish WASM over Node)
npm install

# 3. Tests
.venv/bin/python -m pytest backend/tests/ -q

# 4. Run the server (API + web UI)
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
# 1. Python deps (venv scripts live in Scripts\ on Windows)
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2. Engine (Stockfish WASM over Node — needs Node.js installed)
npm install

# 3. Tests
.\.venv\Scripts\python -m pytest backend/tests/ -q

# 4. Run the server
.\.venv\Scripts\uvicorn backend.app.main:app --port 8000
```

Then open http://localhost:8000
</details>

### Connecting your local Qwen

The teacher speaks the OpenAI protocol, so Ollama, LM Studio, vLLM, or llama.cpp all work. The app only uses Qwen when `QWEN_MODEL` is set — otherwise it runs the built-in fallback teacher (engine facts only — it never invents analysis).

**Windows / PowerShell (Ollama):**

```powershell
ollama list                                   # copy the exact NAME, e.g. qwen3:4b
$env:QWEN_MODEL = "qwen3:4b"                  # same window you start the server from
.\.venv\Scripts\python -m backend.app.check_qwen   # diagnoses the connection step by step
.\.venv\Scripts\uvicorn backend.app.main:app --port 8000
```

`$env:` lasts for that window only. To make it stick, save it once in a **`.env` file** in the project folder (git-ignored, read at every start; see `.env.example`):

```powershell
Set-Content .env "QWEN_MODEL=qwen3:4b"
```

Settings typed in the shell still override `.env`.

**macOS / Linux:**

```bash
export QWEN_MODEL=qwen3:4b
.venv/bin/python -m backend.app.check_qwen
```

| Variable | Default | Notes |
|---|---|---|
| `QWEN_MODEL` | *(empty → offline fallback)* | exact model name from `ollama list` |
| `QWEN_BASE_URL` | `http://localhost:11434/v1` | LM Studio: `http://localhost:1234/v1` |
| `QWEN_API_KEY` | `ollama` | ignored by local servers |
| `QWEN_TIMEOUT` | `120` | seconds; first reply is slow while the model loads |
| `QWEN_THINKING` | `auto` | `auto` = off for Qwen3 (sends `/no_think`; much faster). `on` / `off` to force |
| `QWEN_MAX_TOKENS` | `300` | cap on explanation length (shorter = faster on local hardware) |
| `QWEN_PLANNER` | `auto` | `auto` = ask Qwen only when the catalog can't answer (instant plans), `always`, `never` |
| `QWEN_WARMUP` | `1` | load the model when the server starts so the first reply isn't the slowest |

Qwen3 `<think>…</think>` reasoning is always stripped before it reaches the student (also mid-stream).

#### Speed with a local model

The app never makes you wait for the language model to see a result:

- **Move feedback is instant**: Stockfish's verdict plus a built-in explanation appear right away (~0.1 s), then Qwen's explanation **streams in word by word** (`POST /api/sessions/{id}/explain`, NDJSON events). Chat replies stream the same way.
- **Plans for known topics don't use Qwen at all** (`QWEN_PLANNER=auto`), so they're instant.
- `python -m backend.app.check_qwen` runs a **speed test** (time to first word, tokens/s) and tells you what to change. Typical fixes, all free and unlimited:
  - Keep the model loaded: `setx OLLAMA_KEEP_ALIVE "2h"`, then restart Ollama (otherwise it unloads after 5 idle minutes and the next reply has to reload it).
  - Check `ollama ps`: `100% GPU` is fast; `CPU` is slow.
  - Use a smaller model: `ollama pull qwen3:1.7b` (about 2× faster than 4b). The chess facts come from Stockfish either way. The status bar shows `teacher: qwen (model)` when connected.

## Status

**Phase 1 — MVP: core loop complete.** ✅ chess system, engine integration + position-aware move classification, teacher layer (Qwen + facts-only fallback), lesson schema/loader, Italian Game lesson 1, session API, web UI (board + demonstrations + exercises + hints + chat), test suite.

**Learning plans:** ✅ "I want to learn ___" planner (catalog + optional Qwen organization), verified topic catalog with Stockfish audit, generated lessons, persistent plans, free exploration on the board. Streaming AI text + instant move verdicts + speed diagnostics. Test suite: **118 passing** (+ `scripts/ui_e2e.mjs` UI smoke test).

**Next:** [docs/PERSONALIZATION.md](docs/PERSONALIZATION.md) — architecture + MVP proposal for the second pillar: personalized training from the user's own games (PGN import → pattern detection → weakness report → generated training).

The proven MVP loop:

```
User chooses lesson → AI teaches concept → board demonstrates it → user gets a
position → user moves → python-chess validates → Stockfish classifies → Qwen
explains → next teaching position → lesson completion ✓
```
