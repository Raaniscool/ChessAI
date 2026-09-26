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

## Repository layout

```
backend/app/
  chess_system.py      # strict python-chess helpers (FEN, moves, board-command validation)
  config.py            # env-driven settings (engine cmd, Qwen endpoint, port)
  engine/              # Stockfish service + position-aware move classification
  teacher/             # Qwen client, prompts, deterministic fallback teacher
  lessons/             # lesson schema + loader + JSON course data
  lessons/data/italian_game/   # first course (structured lesson JSON)
backend/tests/         # pytest suite (chess, classification, lessons, engine, teacher)
frontend/              # web UI (vendored cm-chessboard + chess.mjs, no CDNs)
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

`$env:` lasts for that window only; `setx QWEN_MODEL "qwen3:4b"` makes it permanent (open a new window afterwards).

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

Qwen3 `<think>…</think>` reasoning is always stripped before it reaches the student. The status bar shows `teacher: qwen (model)` when connected.

## Status

**Phase 1 — MVP: core loop complete.** ✅ chess system, engine integration + position-aware move classification, teacher layer (Qwen + facts-only fallback), lesson schema/loader, Italian Game lesson 1, session API, web UI (board + demonstrations + exercises + hints + chat), test suite (39 passing).

**Next:** [docs/PERSONALIZATION.md](docs/PERSONALIZATION.md) — architecture + MVP proposal for the second pillar: personalized training from the user's own games (PGN import → pattern detection → weakness report → generated training).

The proven MVP loop:

```
User chooses lesson → AI teaches concept → board demonstrates it → user gets a
position → user moves → python-chess validates → Stockfish classifies → Qwen
explains → next teaching position → lesson completion ✓
```
