# Response-pipeline latency audit

## Scope and result

This pass keeps the existing frontend → FastAPI → semantic coach router → lesson/context and verified-knowledge paths → teacher/Stockfish → SSE rendering architecture. It does not add a parallel AI pipeline or replace semantic routing with keyword-only routing.

The main round-trip reduction is in `frontend/app.js`: the normal path asks `/api/coach/route` once; that endpoint performs the local answer lookup and then continues through the existing route logic. If `/api/coach/route` itself errors, the frontend again tries the legacy `/api/knowledge/answer` endpoint before falling back to lesson/session handling. Closed greetings/discovery and narrow active-lesson status questions avoid unnecessary setup or route-model work. For ordinary active-session tutor questions, the local `QuestionIntent` computed during quick-answer lookup is reused to make a conservative, non-mutating route; explicit state changes, unresolved concepts, and engine-related requests still reach semantic routing. The same intent is also reused by route post-processing, avoiding a duplicate local classification. Active-board decisions still use the session's authoritative state and required Python-chess/Stockfish paths.

Latency is now observable at the browser and backend. No message/prompt text is written by the new performance logs.

## Measurement method and caveats

- **Real-model limitation (confirmed in the follow-up environment audit):** this workspace has no `.env`, `QWEN_MODEL` is unset, the `ollama` executable/model cache is absent, and `127.0.0.1:11434` refuses connections. The configured ChessAI teacher here is therefore the offline fallback, not Qwen. The current `.env.example` comments out candidate identifiers and activates no model. No value below is a real-Qwen benchmark.
- **Mock-model profile:** a temporary OpenAI-compatible endpoint simulated Qwen with a fixed 50 ms first-token delay; Stockfish was real. Treat those values as pipeline measurements with a mock teacher, not as production inference time.
- **Offline profile:** Uvicorn ran with Qwen disabled, a fresh `DATA_DIR` under `/tmp`, and the installed Stockfish WASM engine. These are local API/SSE observations, not browser paint timings. One-shot API responses use response completion as the output proxy; streamed TTFO is measured at the first SSE text delta.
- **Legacy-flow comparison:** the “legacy” column replays the old frontend request sequence—`/api/knowledge/answer`, then `/api/coach/route` on a miss, then the existing stream when applicable—against the current backend. It measures the cost of that request topology, not an old Git checkout. Exact historical browser-paint measurements were not captured.
- Paired requests were made in one process. File-system, learner-profile, and engine warm-cache effects are visible; do not treat a single paired time as a statistically controlled benchmark.

## Actual local Qwen profile: blocked in this workspace

The follow-up inspection confirms that an actual Qwen run is impossible from this checkout: no `.env` is present, `QWEN_MODEL` and `QWEN_BASE_URL` are unset, the checked `OLLAMA_*` tuning variables are unset, `ollama` is not installed, `127.0.0.1:11434` refuses connections, and no Ollama model cache was found in the common locations. An attempt to reach `https://ollama.com` for setup failed with a TLS connection error, so the model/runtime cannot be downloaded into this sandbox. The sandbox exposes 2 vCPUs and 3.8 GiB RAM; `nvidia-smi` is unavailable. The model and hardware used on the developer's PC cannot be inferred from this isolated sandbox.

Code defaults (not evidence of the developer's actual configuration): Qwen is disabled unless `QWEN_MODEL` is supplied; the default base URL is `http://localhost:11434/v1`, general timeout 120 s, `QWEN_THINKING=auto`, max output 300 tokens, and asynchronous background warm-up enabled. The current example file comments candidate IDs without selecting one; quantization, context length, CPU thread count, GPU offload, VRAM/RAM use, and actual keep-alive policy remain unknown. No cold/warm load time, real TTFT, generated tokens/s, or real-model before/after comparison can be reported. Do not treat the fixed-delay mock measurements below as a substitute.

Source inspection: when configured, ChessAI starts a one-token Qwen warm-up in a background thread at app startup (unless `QWEN_WARMUP=0`) and has no per-request model-unload call. The server, not the `QwenTeacher` Python object, controls weight retention; the project README documents Ollama's default five-minute idle unload and recommends configuring a longer keep-alive when appropriate, but this workspace cannot inspect the actual server's version, `OLLAMA_KEEP_ALIVE`, or `ollama ps`. The Qwen stream logger records request-to-first-visible-token, total model stream time, prompt size, and visible characters, but not an exact generated token count; the existing `check_qwen` speed test treats text chunks as an approximate token count. Ollama's native response usage/server logs would be needed for exact load/eval counts. Backend streaming uses incremental NDJSON with `Cache-Control: no-cache` and `X-Accel-Buffering: no`; the browser consumes each event and records first rendered text after `requestAnimationFrame`. These are source-level checks only, not a measured Qwen-to-screen result.

## Legacy request flow vs consolidated route

Local no-Qwen replay, Knowledge Library warmed, Stockfish installed. Counts are HTTP requests for the turn; all model-call counts were zero in this profile.

| Request | Legacy replay TTFO / total | Consolidated route TTFO / total | HTTP requests | Notes |
|---|---:|---:|---:|---|
| `hi` | 31.8 / 31.8 ms | 2.3 / 2.3 ms | 2 → 1 | Fast greeting, no model/retrieval/engine call. |
| `What is a pin?` | 6.1 / 6.1 ms | 5.8 / 5.8 ms | 1 → 1 | One curated local answer lookup in either flow. |
| `What can you teach me?` | 8.5 / 8.5 ms | 1.9 / 1.9 ms | 2 → 1 | Closed discovery route. |
| `Teach me the Sicilian.` (route only) | 37.2 / 37.2 ms | 24.0 / 24.0 ms | 2 → 1 | One retrieval attempt later returns no lesson examples; catalog plan is used. |
| `Why can't Black take that pawn?` | 36.2 / 36.6 ms | 26.3 / 26.7 ms | 3 → 2 | Same fast board-question route and local fallback; no Stockfish search needed. |
| `What is our lesson goal?` | 126.3 / 127.1 ms | 109.7 / 110.1 ms | 3 → 2 | Local fallback profile; no Qwen call in this run. |

The full Sicilian route → plan → course refresh → lesson start was **134.5 ms / 5 requests** in the legacy replay and **58.7 ms / 4 requests** in the combined replay. The second plan was warm, so the request-count reduction is the robust result; the wall-clock difference is not a controlled speedup estimate. These no-Qwen legacy/consolidated timings predate the latest active-lesson intent reuse; request-count comparisons remain applicable, while the active lesson-question latency is refreshed in the post-change profiles below.

For `Why is this move good?`, the paired stream observations were 268.9 ms (legacy) and 57.5 ms (combined), each with one Stockfish search and 3 → 2 HTTP requests. This is **not** an apples-to-apples latency improvement: the first search took 241 ms and warmed Stockfish's transposition table; the second took 36 ms. The repeat demonstrates why engine-cache state must be reported alongside TTFO.

## Current offline profile (Qwen unavailable, real Stockfish)

A second isolated Uvicorn run logged a 1,639.8 ms Knowledge Library startup warm-up (617 verified examples). Selected request results:

| Request | TTFO / total | Calls and stage observations |
|---|---:|---|
| `hi` | 22.6 / 22.6 ms | 0 model, retrieval, or Stockfish calls. |
| `What is a pin?` | 15.5 / 15.5 ms | 1 local quick-answer lookup; 0 model/engine calls. |
| `What can you teach me?` | 2.5 / 2.5 ms | 0 model, retrieval, or engine calls. |
| `Teach me the Sicilian.` route → plan → verified lesson start | 149.9 ms | 0 model/engine calls; catalog plan; 1 retrieval attempt with no matching examples. Plan-build stage was 95.0 ms. |
| Verified `Teach me forks` retrieval plan, first call after startup | 1,111.7 ms | 1 retrieval, no model/engine calls; retrieval stage 1,054.6 ms. Subsequent calls were 67.7 and 66.5 ms total (retrieval stages 15.4 and 14.3 ms). This one-off cold-path cost remains unexplained and should be watched. |
| `Why can't Black take that pawn?` in a lesson | 74.7 / 75.8 ms in the mock-teacher profile; 30.8 / 31.3 ms offline | Fast route, 1 answer stream/0 Stockfish in the mock profile; offline fallback has no model stream. Context build was 0.4 ms and prompt 1,652 chars (about 413 tokens by a chars/4 estimate) in the mock profile. |
| `Why is this move good?` | 458.2 / 458.7 ms offline | 1 depth-14 Stockfish search; first engine startup 179.9 ms, search 255.2 ms, context stage 436.1 ms. Mock-teacher run: 561.4 / 562.9 ms, with 1 search (268.3 ms) and cold start 219.9 ms. Mock prompt: 1,876 chars (about 469 tokens). |
| `What is our lesson goal?` with lesson context | 15.5 / 15.7 ms offline after the fast-route change | Local structured-state route; no model, retrieval, or Stockfish call. |

The offline value above is from the final route implementation. The earlier mock result (162.4 / 163.9 ms) is superseded by the post-change mock profile below. In the original mock sample set, fast routing removes the lesson-status route completion: the expected counts are now **1 semantic route completion and 3 streamed answer calls** (the Sicilian route uses the one route completion; board/engine/lesson streams are unchanged). Greetings/discovery and the simple pin answer use no model call. The verified fork plan uses retrieval without a model or engine call. The separate post-change two-question mock retest is reported below; these are per-scenario counts, not a production traffic average.

## Post-change active-lesson profile (fixed-delay mock teacher)

After sharing `QuestionIntent` between the local quick-answer classifier and `route_message`, a second isolated mock run used the same fake OpenAI-compatible endpoint (50 ms artificial response delay). Qwen remains unconfigured, so these figures are **not real-Qwen latency**. TTFO is from request start to the first SSE text delta; total is stream completion. The recorded `model_calls` are mock endpoint calls.

| Active-session question | Route API | TTFO / total | Model / retrieval / Stockfish calls | Context and prompt |
|---|---:|---:|---|---|
| `What is our lesson goal?` | 2.6 ms | 98.9 / 100.0 ms | 1 answer stream; 0 route completions, 0 retrieval, 0 Stockfish | Context 0.3 ms; prompt 1,771 chars (~442 tokens); no local question-classifier call. |
| `Why do bishops sometimes beat knights in open positions?` | 17.7 ms | 104.8 / 106.0 ms | 1 answer stream; 0 route completions, 1 local quick-answer lookup/classification, 0 Stockfish | Classifier 13.7 ms; context 0.2 ms; prompt 1,918 chars (~479 tokens). The same `QuestionIntent` is reused for routing and post-processing. |

Both requests previously went through a semantic route completion and then an answer stream; after the change, each needs only the answer stream (2 model calls → 1), while lesson context is retained. In the 50 ms mock setup, lesson-status TTFO fell from 162.4 ms to 98.9 ms (about 39%); the normal tutor question measured 104.8 ms. This isolates request topology and local work, not real model prefill/inference performance.

## Optimizations and instrumentation

- **One normal frontend routing request:** local knowledge lookup is folded into `/api/coach/route`; a miss proceeds into the same semantic router. This removes the old extra round trip on unmatched messages (3 → 2 requests for an active streamed chat; 2 → 1 for a no-session route miss).
- **Fast, closed intents first:** greetings/discovery avoid Knowledge Library setup/search and semantic-model routing. A narrow status-question route reads only the existing structured lesson state. Ordinary tutor questions can reuse the local `QuestionIntent` from quick-answer lookup; this route is non-mutating and does not replace semantic interpretation for explicit topic/mode/lesson changes, unresolved concepts, or engine-related requests. The full Qwen answer stream and structured lesson context are retained.
- **Request-level route deadline:** semantic-route Qwen completions receive a five-second HTTP request timeout. The deadline closes the underlying request rather than abandoning a `Future` while model inference continues in the background; route failures still flow through the existing confidence-aware local fallback.
- **Prompt/context work remains correctness-preserving:** mock measurements logged context-stage duration and prompt size. Structured lesson context, verified examples, authoritative board state, legal-move facts, and required Stockfish analysis were retained.
- **Performance log lines:** `chessai.performance` records warm-up, coach route, local answer lookup, semantic position classifier, knowledge retrieval, plan build, chat-context sub-stages, Stockfish startup/search, prompt size, and teacher completion/stream timing. Logs include counts and durations, not learner text or prompt contents.
- **Browser debug view:** open the app with `?debug=1` and inspect DevTools Console for route/plan timing, TTFO after the next paint, and total turn duration. The first-text metric is recorded after `requestAnimationFrame`, so it is closer to visible output than a server timestamp.
- **Fallback restored:** if `/api/coach/route` fails, the client attempts the prior local-answer endpoint. Failure of that endpoint is contained; the ordinary explicit lesson/session fallback still runs.

## Remaining bottlenecks and Decision Puzzle readiness

1. **Real Qwen TTFO is unmeasured.** The 50 ms first-token delay in the mock profile is artificial. A local Qwen model's load state, prompt-prefill time, quantization, and CPU/GPU placement can dominate actual response time. Use the browser debug timings and backend `qwen_stream` logs on the target machine.
2. **Cold engine work is measurable.** In the isolated run, first Stockfish startup was about 180 ms and a depth-14 search about 255 ms. The engine must still run when an answer truly depends on analysis; do not remove that work to optimize a result.
3. **Knowledge has a startup cost and a cold retrieval outlier.** Startup warm-up took about 1.6 s, outside the first learner request. A first verified fork retrieval after warm-up took about 1.05 s, while warm repeats took roughly 15 ms in the retrieval stage. If that occurs in normal learner sessions, profile the learner-personalization/profile calculation before adding caching.
4. **Browser-paint/network latency was not measured here.** API/SSE timings run on loopback and exclude remote network conditions and actual browser scheduling; the new `?debug=1` metric is available for that final check.

**Decision Puzzles v2** uses a required Yes/No verdict for the side to move plus a separate explanation. Python-chess verifies legality; Stockfish owns move quality, and an optional teacher only assesses the explanation against supplied engine facts. Qwen is now bounded to 4 seconds/140 tokens for this path, while the two Stockfish searches share a 6-second budget. Exact position/move/config engine feedback and the verified candidate pool are cached; no learner-specific explanation is cached.

Local measurement on 2026-10-17 with the installed Stockfish engine and checked-in seed data (627 eligible candidates, no Qwen call) recorded: candidate-pool cold build **739.7 ms**, cached build **1.42 ms**, task issuance with the pool warm **28.9 ms** at Decision depth 10, and repeat exact-analysis cache lookup **0.16 ms**. The request's selected position was classified `excellent`. The baseline captured before the changes was a depth-14 cold pool build of **2,163.7 ms**, subsequent builds of **309.1–555.9 ms**, and issue time of **320.5–350.6 ms**; the depth/configuration changed, so this is not a controlled apples-to-apples engine-quality comparison. These are local sandbox measurements, not browser-paint or target-machine guarantees. The `QWEN_MODEL` variable was unset and the configured Ollama-compatible endpoint returned `ConnectError` to a 2-second `/models` probe; no lightweight compatible model was verified or substituted. Functional coverage includes real-Stockfish behavior, nonresponsive-engine deadline handling, scripted teacher outcomes, persistence/adaptation, and UI contract tests. No live browser E2E or real-Qwen quality/latency test was run; treat model latency readiness as **unverified** on intended hardware.

### Decision explanation-grading failure reproduction (2026-10-17)

Before changing configuration, the runtime was checked without launching a server: no `.env` existed; `QWEN_MODEL` and the other Qwen/Decision override variables were unset; `ollama` was not installed; and a 1.5-second bounded request to the default OpenAI-compatible `/v1/models` endpoint (`http://localhost:11434/v1/models`) returned `ConnectError: [Errno 111] Connection refused`. `get_teacher()` therefore chose `FallbackTeacher`, which intentionally cannot grade natural-language explanations. The backend returned `unavailable`; the frontend mapped it to the generic “Explanation: not assessed.” The reproduced cause is **missing model configuration/service**, not a chess verdict, model-declared uncertainty, or a malformed response.

Decision responses now expose a safe `assessment_status` and reason code. The UI distinguishes no model configured, unreachable endpoint, timeout, invalid configuration, provider/model/route errors, malformed provider response, invalid grading JSON/labels/fact IDs, and genuinely uncertain reasoning. Objective Stockfish choice feedback remains independently saved; no unavailable/uncertain explanation becomes negative reasoning evidence or enters the difficulty-adjustment window. The offline fallback remains honest—it does not claim to grade free text. `.env.example` no longer selects an unverified model merely because it was copied.

For an enabled local Qwen grader, set `QWEN_MODEL` to an exact installed model tag and point `QWEN_BASE_URL` at its OpenAI-compatible `/v1` base. The Decision call is capped at 140 output tokens, a four-second response/read timeout, and one second to connect. `qwen3:1.7b` is listed as an optional smaller candidate, not as a model installed, tested, or quality-certified in this runtime. The mocked API-to-`QwenTeacher`-to-profile integration test asserts under one second with the model response stubbed; that figure excludes inference and is not a real-Qwen latency result. The environment has no reachable model, so actual Qwen grading quality and latency remain unverified.

### Follow-up: timeout diagnosis and real-model availability (2026-10-17)

- Current audit machine: Linux x86_64, 2 vCPUs (Xeon @ 2.60 GHz), 3.8 GiB RAM, no swap/GPU, 19 GiB free disk. No `.env`, Qwen-related environment overrides, Ollama/llama.cpp/LM Studio/vLLM binary/process, or local model cache. `QWEN_MODEL` resolves empty; the default is `http://localhost:11434/v1`.
- A bounded `GET http://localhost:11434/v1/models` was refused in **47.3 ms**. `python -m backend.app.check_qwen` confirms no service. This is the actual failure reproduced here: absent configuration and unreachable model service. It is not a four-second model inference, and no actual inference ran.
- For a Windows/local environment that has configured a service, the source-level timeout finding is important: Decision passes `DECISION_ASSESSMENT_TIMEOUT=4` to Qwen's separate HTTP call (connect cap 1 s, response/read cap 4 s). `QWEN_TIMEOUT=120` applies to general teacher calls and startup warm-up; changing it does not change the Decision deadline. Warm-up is started in a background thread and is not a readiness gate, so first-use cold loading can overlap with an answer. A response timeout can include load, prefill and generation; code alone cannot identify which dominates.
- Qwen transport errors now report `connection_timeout`, `response_timeout`, and `request_timeout` separately (plus connection refusal, provider errors and malformed output). The frontend labels the connection and response cases separately. `check_qwen` displays both deadlines and warns that its general-response speed test does not prove the Decision pipeline's latency. No retries were added; identical task submissions are already protected by per-task idempotency.
- The existing prompt is bounded: the system instruction and compact evidence JSON, up to 2,000 characters of explanation, 140 output tokens; sample integration-test prompts are about 2,500 characters (roughly 625 tokens by `chars / 4`). It is not obviously oversized, and no further prompt reduction or timeout increase was made without real-model measurement.
- Candidate exact Ollama identifier: `qwen3:1.7b` from the current README. Expected package download is roughly **1.4 GB**; leave at least **2 GB disk**, estimate **2–4 GB runtime RAM at a short context**, and prefer **8 GB or more total Windows RAM**. These are estimates only; registry metadata and runtime memory were not accessible/verified. The sandbox outbound allow-list does not include the model registry/weight hosts, it has only 3.8 GiB RAM, and no Windows host is available; no package/model was downloaded or installed.
- Cold load latency, warm inference latency, actual full pipeline latency, and actual explanation quality remain **unmeasured/unverified**. No real examples were graded; the under-one-second fake-HTTP test excludes inference. Do not interpret the Stockfish-only issue timings or mocked Qwen tests as real model latency.

## Commands

From the repository root, Linux/macOS:

```bash
.venv/bin/python -m compileall -q backend/app backend/tests
.venv/bin/python -m pytest backend/tests/ -q
node --test frontend/tests/*.test.mjs
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8080
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python -m compileall -q backend\app backend\tests
.\.venv\Scripts\python -m pytest backend\tests\ -q
$frontendTests = Get-ChildItem frontend\tests\*.test.mjs | ForEach-Object { $_.FullName }
node --test $frontendTests

# Optional isolated runtime data; configure QWEN_MODEL normally for a real-model run.
$env:DATA_DIR = Join-Path $env:TEMP "chessai-latency-data"
.\.venv\Scripts\uvicorn backend.app.main:app --host 0.0.0.0 --port 8080
```

For the browser-level TTFO, open `http://localhost:8080/?debug=1`, run the target turns, and capture the `[ChessAI latency]` Console entries together with backend stage logs.
