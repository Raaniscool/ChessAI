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

### Connecting your local Qwen

The teacher speaks the OpenAI protocol, so Ollama, LM Studio, vLLM, or llama.cpp all work:

```bash
export QWEN_BASE_URL=http://localhost:11434/v1   # Ollama's OpenAI endpoint
export QWEN_MODEL=qwen2.5:7b                     # your model tag
# optional: QWEN_API_KEY, QWEN_TIMEOUT, ENGINE_CMD, ENGINE_DEPTH
```

Without `QWEN_MODEL` the app uses the built-in fallback teacher (engine facts only — it never invents analysis).

## Status

**Phase 1 — MVP: core loop complete.** ✅ chess system, engine integration + position-aware move classification, teacher layer (Qwen + facts-only fallback), lesson schema/loader, Italian Game lesson 1, session API, web UI (board + demonstrations + exercises + hints + chat), test suite (25 passing).

**Next:** [docs/PERSONALIZATION.md](docs/PERSONALIZATION.md) — architecture + MVP proposal for the second pillar: personalized training from the user's own games (PGN import → pattern detection → weakness report → generated training).

The proven MVP loop:

```
User chooses lesson → AI teaches concept → board demonstrates it → user gets a
position → user moves → python-chess validates → Stockfish classifies → Qwen
explains → next teaching position → lesson completion ✓
```
