# Read-aloud (text-to-speech)

The coach can read explanations aloud. It is **off until you turn it on**. Only what matters is
read (see *What gets read*), and anything that fails falls back to text or your browser's own
voice, without interrupting the lesson.

## Which voice engine, and why

Providers compared in 2026 before choosing (prices per 1 million characters):

| Provider | Naturalness | Cost | Licence / terms | Runs | Notes |
|---|---|---|---|---|---|
| **Kokoro-82M** (default) | Near the top of public TTS rankings; flatter on emotion | Free | Apache-2.0, commercial use OK | On your PC (CPU, 3–5× faster than real time) | 88 MB int8 model; many real US/UK voices; no account, works offline |
| Kokoro-FastAPI server | Same model | Free | Apache-2.0 | Docker, local or server | OpenAI-compatible API, streaming |
| OpenAI `gpt-4o-mini-tts` / `tts-1` | Very natural, steerable | ~$15 per 1M (tts-1), no free tier | Commercial API | Cloud | Needs an API key; network latency |
| ElevenLabs | Best-in-class expressiveness | $60–180 per 1M; 10k chars/month free | Commercial API | Cloud | Expensive at tutoring volumes |
| Google Cloud TTS | Good (WaveNet/Neural2/Chirp) | $4–30 per 1M; 1–4M/month free | Commercial API | Cloud | Needs a GCP account |
| Azure Neural | Good | $16–22 per 1M; 500k/month free | Commercial API | Cloud | Needs an Azure account |
| Piper | Decent, robotic in places | Free | Active fork is GPL-3.0 (+ espeak-ng GPL); voice licences vary | Local | Licence is awkward for a product |
| edge-tts | Good | Free | Unofficial Edge endpoint, no terms for commercial use | Cloud | Too risky to depend on |
| Browser `speechSynthesis` | Varies by OS (often robotic) | Free | — | Browser | Always available: the last-resort fallback |

**Chosen:** Kokoro locally by default. It's free, private, commercially licensed and natural, and
it needs no account. The **OpenAI-compatible provider** covers both a Kokoro-FastAPI server and
OpenAI itself, so switching is configuration only. The **browser voice** is the fallback when
nothing else is set up or a request fails.

## How it fits together

```
frontend/speech.js  Narrator: settings, what to read, queue, LRU audio cache (80 clips),
                    browser-voice fallback, word/move highlighting
        │  GET /api/tts          → providers, voices, speeds, max_chars
        │  POST /api/tts/speak   → audio  (or 503 {fallback: "browser"})
backend/app/tts/
  base.py       Provider protocol + Voice (id, name, gender, accent, style)
  providers.py  KokoroProvider (kokoro-onnx, local) · OpenAICompatibleProvider (Kokoro-FastAPI / OpenAI)
  voices.py     2–3 curated real voices per coach persona, per provider (none faked)
  service.py    provider choice (TTS_PROVIDER), disk cache (TTS_CACHE_MB), timeouts
```

Adding a provider means implementing `available()`, `voices()` and `synthesize(text, voice, speed)`
in `providers.py`. Nothing else changes.

## Settings (⚙ in the header, saved across sessions)

- **Read aloud:** on or off.
- **Voice:** a real voice from the active provider. Each has a ▶ preview.
- **Speed:** slow / normal / fast (0.85× / 1× / 1.2×).
- **Auto-read:** lessons, important explanations, hints and puzzle instructions (on by default),
  plus analysis summaries (off by default). Each is its own switch.

Settings live in the browser (`localStorage` key `chess-tutor-speech`) and in your learner
profile, so they survive restarts.

## What gets read

Everything the coach says has an instructional importance: critical, important, supporting or
obvious. Only **critical and important** items are read automatically. Routine moves and small
print never are, so the voice follows the teaching rather than narrating everything. Any message
can still be read on demand with its 🔊 button.

## Speed

- Audio is generated only for text that will actually be played.
- Repeated phrases come from the cache: 80 clips in the browser, `TTS_CACHE_MB` on disk.
- Long text is split at sentence boundaries. The first chunk is kept short so the voice starts
  at once, and each next sentence is fetched while the current one plays, so there are no gaps.
- If the provider is slow or down, the browser voice (or plain text) takes over at once.

## Setup (Windows PowerShell)

**Local Kokoro (recommended; free, offline):**
```powershell
.\.venv\Scripts\pip install -r requirements-tts.txt
.\.venv\Scripts\python scripts\setup_tts.py            # 88 MB model, one time
# optional higher quality (310 MB): .\.venv\Scripts\python scripts\setup_tts.py --quality
```
Restart the app. The ⚙ panel then shows the Kokoro voices.

**Kokoro-FastAPI server (Docker):**
```powershell
docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest
$env:TTS_OPENAI_BASE_URL="http://localhost:8880/v1"
.\.venv\Scripts\uvicorn backend.app.main:app --port 8000
```

**OpenAI (paid):** set `TTS_OPENAI_BASE_URL=https://api.openai.com/v1`,
`TTS_OPENAI_API_KEY=...` and `TTS_OPENAI_MODEL=gpt-4o-mini-tts`.

Put any of these in `.env` to make them permanent.

| Variable | Default | Meaning |
|---|---|---|
| `TTS_PROVIDER` | `auto` | `auto` \| `kokoro` \| `openai` \| `browser` |
| `TTS_KOKORO_MODEL` / `TTS_KOKORO_VOICES` | `DATA_DIR/tts/…` | model files from `setup_tts.py` |
| `TTS_OPENAI_BASE_URL` / `TTS_OPENAI_API_KEY` / `TTS_OPENAI_MODEL` | — / — / `kokoro` | OpenAI-compatible server |
| `TTS_CACHE_MB` | `200` | disk cache for generated audio |
| `TTS_TIMEOUT` | `30` | seconds before falling back |

## Tests

- `backend/tests/test_tts.py`: providers, voice lists, cache, fallback, and 503 handling. Fake
  providers stand in for the real ones; one test runs the real `kokoro_onnx` wrapper when it's
  installed.
- `frontend/tests/tts.test.mjs` and `speech.test.mjs`: settings persistence, voice selection,
  preview, speeds, importance filtering, queueing, provider failure → browser fallback.
