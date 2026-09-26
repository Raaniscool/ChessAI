// AI Chess Tutor — frontend
// Board renders server-validated state only; every learner move is checked
// locally (UX) and authoritatively on the server (rules).

import {Chessboard, COLOR, INPUT_EVENT_TYPE, FEN, BORDER_TYPE} from "./vendor/cm-chessboard/src/Chessboard.js"
import {Markers, MARKER_TYPE} from "./vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Chess} from "./vendor/chess.mjs/Chess.js"
import {Narrator, decorateMoves} from "./speech.js"

// ---------- state ----------

const state = {
  sessionId: null,
  step: null,
  chess: new Chess(),      // local mirror; server stays authoritative
  busy: false,
  demoPlaying: false,
  stream: null,            // AbortController of the AI text currently streaming
}

// ---------- api ----------

async function api(path, method = "GET", body = undefined) {
  const opts = {method, headers: {"Content-Type": "application/json"}}
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(path, opts)
  let data = null
  try { data = await res.json() } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`)
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

// Stream newline-delimited JSON events (AI text appears while it's generated).
async function streamEvents(path, body, onEvent) {
  if (state.stream) state.stream.abort()
  const controller = new AbortController()
  state.stream = controller
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    if (!res.ok) {
      let msg = `Request failed (${res.status})`
      try { msg = (await res.json()).error || msg } catch (_) { /* not JSON */ }
      throw new Error(msg)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    for (;;) {
      const {value, done} = await reader.read()
      if (done) break
      buffer += decoder.decode(value, {stream: true})
      let nl
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim()
        buffer = buffer.slice(nl + 1)
        if (line) onEvent(JSON.parse(line))
      }
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer))
  } catch (err) {
    if (err.name !== "AbortError") throw err
  } finally {
    if (state.stream === controller) state.stream = null
  }
}

function stopStream() {
  if (state.stream) state.stream.abort()
  state.stream = null
}

// ---------- board ----------

const board = new Chessboard(document.getElementById("board"), {
  position: FEN.start,
  style: {cssClass: "green", borderType: BORDER_TYPE.frame, showCoordinates: true},
  assetsUrl: "./vendor/cm-chessboard/assets/",
  extensions: [{class: Markers, props: {}}],
})

const HIGHLIGHT_MARKERS = {
  green: MARKER_TYPE.square,
  red: MARKER_TYPE.circleDanger,
  yellow: MARKER_TYPE.circle,
  blue: MARKER_TYPE.circlePrimary,
  purple: MARKER_TYPE.circlePrimary,
  orange: MARKER_TYPE.circle,
  grey: MARKER_TYPE.frame,
}

function clearMarkers() {
  if (board.removeMarkers) board.removeMarkers()
  if (board.removeLegalMovesMarkers) board.removeLegalMovesMarkers()
}

function applyHighlights(highlights = []) {
  for (const h of highlights) {
    const type = HIGHLIGHT_MARKERS[h.color] || MARKER_TYPE.square
    board.addMarker(type, h.square)
  }
}

async function showPosition(fen, {orientation = COLOR.white, highlights = [], animated = false} = {}) {
  clearMarkers()
  await board.setOrientation(orientation)
  await board.setPosition(fen, animated)
  applyHighlights(highlights)
}

function applyUci(chess, uci) {
  return chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
}

// ---------- move input (exercise) ----------

function exerciseLocked() {
  const step = state.step
  if (!step || step.type !== "exercise" || state.busy || step.accepted) return true
  return false
}

// Shared legal-move gate for both graded exercises and free exploration.
function legalInputHandler(getChess, onMove) {
  return (event) => {
    const chess = getChess()
    if (!chess) return false
    switch (event.type) {
      case INPUT_EVENT_TYPE.moveInputStarted: {
        const piece = chess.get(event.squareFrom)
        if (!piece || piece.color !== chess.turn()) return false
        const moves = chess.moves({square: event.squareFrom, verbose: true})
        if (!moves.length) return false
        board.addLegalMovesMarkers(moves)
        return true
      }
      case INPUT_EVENT_TYPE.validateMoveInput: {
        const moves = chess.moves({square: event.squareFrom, verbose: true})
        return moves.some(m => m.to === event.squareTo)
      }
      case INPUT_EVENT_TYPE.moveInputFinished: {
        board.removeLegalMovesMarkers()
        onMove(event.squareFrom, event.squareTo)
        return true
      }
      case INPUT_EVENT_TYPE.moveInputCanceled: {
        board.removeLegalMovesMarkers()
        return false
      }
      default:
        return false
    }
  }
}

const exerciseInput = legalInputHandler(
  () => (exerciseLocked() ? null : state.chess),
  (from, to) => handleUserMove(from, to),
)

// ---------- board locked outside exercises ----------
// Pieces can only be moved in an exercise. Anywhere else a click on the board explains
// what to do instead of silently doing nothing.

function boardAcceptsMoves() {
  return state.step && state.step.type === "exercise" && !state.step.accepted && !state.busy
}

document.getElementById("board").addEventListener("pointerdown", () => {
  if (!state.step || boardAcceptsMoves()) return
  if (state.demoPlaying) {
    setStatus("Watch the demonstration — you'll get your turn right after.")
    return
  }
  const cont = document.getElementById("btn-continue")
  if (!cont.classList.contains("hidden")) {
    setStatus(state.step.next_type === "exercise" && state.step.type !== "exercise"
      ? "Your turn is next — press “Your turn” to practise."
      : "Press Continue to go on.")
    cont.classList.remove("nudge"); void cont.offsetWidth; cont.classList.add("nudge")
  }
})

function moveInputHandler(event) {
  return exerciseInput(event)
}

async function handleUserMove(from, to) {
  const candidates = state.chess.moves({square: from, verbose: true}).filter(m => m.to === to)
  if (!candidates.length) return
  const promotion = candidates.some(m => m.promotion) ? "q" : undefined
  const uci = from + to + (promotion || "")
  const fenBefore = state.chess.fen()
  applyUci(state.chess, uci)
  stopStream()
  narrator.stop()

  state.busy = true
  setStatus("Analyzing your move…")
  try {
    const result = await api(`/api/sessions/${state.sessionId}/move`, "POST", {uci})
    renderFeedback(result)
    if (result.accepted) {
      // Server pushed the move too; local mirror matches. Unlocks Continue.
      state.step.accepted = true
      hideBtn("btn-hint"); hideBtn("btn-reveal")
      setStatus("")
      await board.setPosition(state.chess.fen(), true)
      showContinue()  // only once the board has settled, so the click isn't ignored
    } else {
      // Not good enough for the lesson's goal: reset and let them retry.
      await board.setPosition(result.reset_fen || fenBefore, true)
      state.chess = new Chess(result.reset_fen || fenBefore)
      setStatus("Try again — you can do it.")
    }
  } catch (err) {
    state.chess = new Chess(fenBefore)
    await board.setPosition(fenBefore, true)
    renderError(err.message)
    setStatus("")
  } finally {
    state.busy = false
  }
}

// ---------- read aloud ----------

const SPEECH_MARKER = {class: "marker-speech", slice: "markerSquare"}

// Squares for a spoken move: destination, plus the origin when the move is legal on the
// board as shown (tried for both sides — the text may talk about either player's move).
function squaresFor(mark) {
  if (!mark) return []
  const squares = mark.square ? [mark.square] : []
  const placement = (board.getPosition() || "").split(" ")[0]
  if (!placement) return squares
  for (const turn of ["w", "b"]) {
    try {
      const chess = new Chess(`${placement} ${turn} - - 0 1`)
      const move = chess.move(mark.san.replace(/[+#]$/, ""))
      if (move) return [move.from, move.to]
    } catch (_) { /* not legal for this side */ }
  }
  return squares
}

const narrator = new Narrator({
  onMove(mark) {
    board.removeMarkers(SPEECH_MARKER)
    for (const sq of squaresFor(mark)) board.addMarker(SPEECH_MARKER, sq)
  },
  onState(speaking) {
    document.getElementById("btn-stop-speech").classList.toggle("hidden", !speaking)
  },
})

function speakButton(target) {
  const btn = document.createElement("button")
  btn.className = "speak-btn no-speech"
  btn.title = "Read this aloud"
  btn.setAttribute("aria-label", "Read this aloud")
  btn.textContent = "🔊"
  btn.addEventListener("click", ev => {
    ev.stopPropagation()
    narrator.stop()
    narrator.speak(target)
  })
  return btn
}

// Moves in the text become highlightable; a 🔊 button reads the message aloud.
function makeSpeakable(el) {
  decorateMoves(el)
  if (narrator.supported && !el.querySelector(":scope > .speak-btn")) el.appendChild(speakButton(el))
  return el
}

function setupSpeechControls() {
  if (!narrator.supported) return
  const controls = document.getElementById("speech-controls")
  const toggle = document.getElementById("btn-read-aloud")
  const rate = document.getElementById("speech-rate")
  const voiceSel = document.getElementById("speech-voice")
  controls.classList.remove("hidden")
  const syncToggle = () => {
    toggle.classList.toggle("on", narrator.enabled)
    toggle.setAttribute("aria-pressed", String(narrator.enabled))
  }
  syncToggle()
  rate.value = String(narrator.rate)
  if (!rate.value) rate.value = "1"
  toggle.addEventListener("click", () => {
    narrator.enabled = !narrator.enabled
    narrator.save()
    syncToggle()
    if (!narrator.enabled) narrator.stop()
    else {  // start with the newest tutor message, so the learner hears it works
      const last = [...messagesEl.querySelectorAll(".msg.assistant, .msg.system")].pop()
      if (last) narrator.speak(last)
    }
  })
  rate.addEventListener("change", () => { narrator.rate = Number(rate.value) || 1; narrator.save() })
  const fillVoices = () => {
    const voices = narrator.voices()
    voiceSel.innerHTML = ""
    for (const v of voices) {
      const opt = document.createElement("option")
      opt.value = v.name
      opt.textContent = v.name.replace(/^Microsoft\s+/, "").replace(/\s+-\s+.*$/, "").replace(/\s*\(.*\)$/, "")
      voiceSel.appendChild(opt)
    }
    const current = narrator.voice()
    if (current) voiceSel.value = current.name
    voiceSel.classList.toggle("hidden", voices.length < 2)
  }
  fillVoices()
  window.speechSynthesis.addEventListener("voiceschanged", fillVoices)
  voiceSel.addEventListener("change", () => { narrator.voiceName = voiceSel.value; narrator.save() })
  document.getElementById("btn-stop-speech").addEventListener("click", () => narrator.stop())
}

// ---------- messages / feedback ----------

const messagesEl = document.getElementById("messages")

// Keep the newest message in view when the pane shrinks (buttons appear under it),
// unless the learner scrolled up to re-read something.
let pinnedToBottom = true
messagesEl.addEventListener("scroll", () => {
  pinnedToBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 40
})
if (typeof ResizeObserver === "function") {
  new ResizeObserver(() => { if (pinnedToBottom) messagesEl.scrollTop = messagesEl.scrollHeight }).observe(messagesEl)
}

function addMsg(text, kind = "assistant", allowHtml = false) {
  const div = document.createElement("div")
  div.className = `msg ${kind}`
  if (allowHtml) div.innerHTML = text
  else div.textContent = text
  if (kind !== "user") makeSpeakable(div)
  messagesEl.appendChild(div)
  messagesEl.scrollTop = messagesEl.scrollHeight
  return div
}

// Streamed text arrives in pieces: make it speakable once it's complete.
function finishStreamedMsg(el) {
  delete el.dataset.moves
  el.querySelectorAll(":scope > .speak-btn").forEach(b => b.remove())
  makeSpeakable(el)
  return narrator.auto(el)
}

function renderError(text) {
  const div = document.createElement("div")
  div.className = "error-banner"
  div.textContent = text
  messagesEl.appendChild(div)
  messagesEl.scrollTop = messagesEl.scrollHeight
}

const CATEGORY_LABELS = {
  excellent: "Excellent", good: "Good", inaccurate: "Inaccurate",
  mistake: "Mistake", blunder: "Blunder",
}

function renderFeedback(result) {
  const slot = document.getElementById("feedback-slot")
  slot.innerHTML = ""
  const fb = result.feedback
  const card = document.createElement("div")
  card.className = `feedback-card ${fb.category}`
  const label = CATEGORY_LABELS[fb.category] || fb.category
  card.innerHTML =
    `<span class="cat ${fb.category}">${label}</span> — you played <b>${escapeHtml(fb.user_move)}</b>.` +
    `<div class="explanation">${escapeHtml(result.explanation)}</div>` +
    `<div class="meta"><span class="teacher-label"></span></div>`
  slot.appendChild(card)
  if (result.continue_text) addMsg(result.continue_text)
  if (result.ai_explanation) streamExplanation(card)
  else { makeSpeakable(card); narrator.auto(card) }
}

// The engine verdict is shown instantly; Qwen's explanation streams in after it.
async function streamExplanation(card) {
  const textEl = card.querySelector(".explanation")
  const label = card.querySelector(".teacher-label")
  const quick = textEl.textContent
  let text = ""
  label.textContent = "✍ The AI teacher is writing…"
  label.classList.add("typing")
  try {
    await streamEvents(`/api/sessions/${state.sessionId}/explain`, undefined, ev => {
      if (ev.type === "delta") {
        text += ev.text
        textEl.textContent = text
      } else if (ev.type === "replace") {
        text = ev.text
        textEl.textContent = text
      }
    })
  } catch (err) {
    textEl.textContent = quick
  } finally {
    label.classList.remove("typing")
    label.textContent = ""
    if (!text) textEl.textContent = quick
    if (card.isConnected) finishStreamedMsg(card)
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]))
}

function setStatus(text) {
  document.getElementById("board-status").textContent = text
}

function showBtn(id) { document.getElementById(id).classList.remove("hidden") }
function hideBtn(id) { document.getElementById(id).classList.add("hidden") }

function hideControls() {
  ;["btn-play", "btn-continue", "btn-hint", "btn-reveal", "btn-explain-example"].forEach(hideBtn)
}

// Continue says where it leads: into practice after a demonstration or explanation.
function showContinue() {
  const btn = document.getElementById("btn-continue")
  const step = state.step
  const intoPractice = step && step.type !== "exercise" && step.next_type === "exercise"
  btn.textContent = !step || step.next_type === null ? "Finish lesson ✓"
    : intoPractice ? "Your turn — practise it →" : "Continue →"
  showBtn("btn-continue")
}

// ---------- step rendering ----------

async function renderStep(step) {
  stopStream()
  narrator.stop()
  state.step = step
  state.busy = false
  hideControls()
  document.getElementById("feedback-slot").innerHTML = ""
  document.getElementById("lesson-title").textContent = step.lesson_title
  document.getElementById("step-indicator").textContent = `Step ${step.index + 1} / ${step.total_steps}`
  board.disableMoveInput()

  if (step.type === "teach") {
    const msg = addMsg(step.text)
    await showPosition(step.board.fen, {highlights: step.board.highlights || []})
    showContinue()
    if (step.example && step.example.explainable) showBtn("btn-explain-example")
    setStatus("")
    narrator.auto(msg)
  } else if (step.type === "demonstrate") {
    const msg = addMsg(step.text)
    await showPosition(step.start_fen)
    setStatus("Watch the demonstration…")
    // Plays by itself (read aloud first when that's on); the learner can watch it again.
    await narrator.auto(msg)
    if (state.step === step) await playDemonstration()
  } else if (step.type === "exercise") {
    const msg = addMsg(`🎯 **Exercise:** ${escapeHtml(step.prompt)}`.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"), "assistant", true)
    const side = step.side === "black" ? COLOR.black : COLOR.white
    // The local mirror MUST match the exercise position, otherwise only pieces
    // that could move in the previous position are draggable.
    state.chess = new Chess(step.board.fen)
    await showPosition(step.board.fen, {orientation: side, highlights: []})
    board.enableMoveInput(moveInputHandler, side)
    showBtn("btn-hint"); showBtn("btn-reveal")
    setStatus(`Your move — you are playing ${step.side}.`)
    if (step.accepted) {
      state.step.accepted = true
      showContinue()
      hideBtn("btn-hint"); hideBtn("btn-reveal")
      setStatus("Solved — continue when ready.")
    }
    narrator.auto(msg)
  }
}

async function playDemonstration() {
  if (state.demoPlaying || !state.step || state.step.type !== "demonstrate") return
  state.demoPlaying = true
  board.disableMoveInput()
  hideBtn("btn-play"); hideBtn("btn-continue")
  const step = state.step
  const demoChess = new Chess(step.start_fen)
  clearMarkers()
  await board.setPosition(step.start_fen, false)
  setStatus("Watch the demonstration…")
  try {
    for (let i = 0; i < step.moves.length; i++) {
      if (state.step !== step) return  // learner moved on
      applyUci(demoChess, step.moves[i])
      await board.setPosition(demoChess.fen(), true)
      const comment = step.comments && step.comments[i]
      if (comment) {
        const msg = addMsg(comment, "system")
        // Read aloud: the next move waits until the comment has been spoken.
        if (narrator.enabled) await narrator.speak(msg)
        else await sleep(900)
      } else {
        await sleep(620)
      }
    }
    const after = step.board_after || {}
    clearMarkers()
    applyHighlights(after.highlights || [])
    setStatus(step.next_type === "exercise" ? "Now it's your turn." : "")
  } finally {
    state.demoPlaying = false
    if (state.step === step) {
      showBtn("btn-play")
      showContinue()
    }
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// ---------- navigation ----------

async function advanceLesson() {
  if (state.busy) return
  state.busy = true
  try {
    const res = await api(`/api/sessions/${state.sessionId}/advance`, "POST")
    if (res.completed) {
      hideControls()
      board.disableMoveInput()
      addMsg(`✅ ${res.completion_text}`)
      setStatus("Lesson complete!")
      loadCourses()
    } else {
      await renderStep(res.step)
    }
  } catch (err) {
    renderError(err.message)
  } finally {
    state.busy = false
  }
}

async function startLesson(lessonId) {
  try {
    const res = await api(`/api/lessons/${lessonId}/start`, "POST")
    state.sessionId = res.session_id
    await renderStep(res.step)
  } catch (err) {
    renderError(err.message)
  }
}

async function requestHint() {
  try {
    const res = await api(`/api/sessions/${state.sessionId}/hint`, "POST")
    if (res.hint) {
      narrator.auto(addMsg(`💡 Hint ${res.index}/${res.total}: ${res.hint}`, "system"))
      if (res.exhausted) hideBtn("btn-hint")
    } else {
      addMsg("No more hints — try “Show solution”.", "system")
      hideBtn("btn-hint")
    }
  } catch (err) {
    addMsg(err.message, "system")
  }
}

async function revealSolution() {
  try {
    const res = await api(`/api/sessions/${state.sessionId}/reveal`, "POST")
    const moves = res.accepted_moves.join(" or ")
    narrator.auto(addMsg(`🔎 Solution: ${moves || "see the engine's best move"}`, "system"))
    state.step.accepted = true
    showContinue()
    hideBtn("btn-hint"); hideBtn("btn-reveal")
    setStatus("Solution shown — continue when ready.")
  } catch (err) {
    addMsg(err.message, "system")
  }
}

// Verified library example: the AI explains it from the library's facts (offline: the
// library's own verified explanation).
async function explainExample() {
  if (!state.sessionId) return
  hideBtn("btn-explain-example")
  const bubble = addMsg("…", "assistant")
  bubble.classList.add("typing")
  let text = ""
  try {
    await streamEvents(`/api/sessions/${state.sessionId}/example/explain`, undefined, ev => {
      if (ev.type === "delta") text += ev.text
      else if (ev.type === "replace" || ev.type === "done") text = ev.text
      if (text) {
        bubble.textContent = "🧠 " + text
        messagesEl.scrollTop = messagesEl.scrollHeight
      }
    })
  } catch (err) {
    bubble.remove()
    addMsg(err.message, "system")
  } finally {
    bubble.classList.remove("typing")
    if (bubble.isConnected && text) finishStreamedMsg(bubble)
  }
}

async function sendChat() {
  const input = document.getElementById("chat-input")
  const message = input.value.trim()
  if (!message) return
  input.value = ""
  // "I want to learn ___" (or anything typed before a lesson starts) builds a plan.
  if (!state.sessionId || isLearnRequest(message)) {
    await requestPlan(message)
    return
  }
  // "Show me checkmates" / "Give me an endgame lesson": ask the server whether the
  // verified library covers it; questions like "show me why…" stay in the chat.
  if (MAYBE_LESSON.test(message)) {
    try {
      const intent = await api("/api/knowledge/intent", "POST", {message})
      if (intent.lesson_request) {
        await requestPlan(message)
        return
      }
    } catch (_) { /* fall through to the chat */ }
  }
  addMsg(message, "user")
  const bubble = addMsg("…", "assistant")
  bubble.classList.add("typing")
  let text = ""
  try {
    await streamEvents(`/api/sessions/${state.sessionId}/chat/stream`, {message}, ev => {
      if (ev.type === "delta") text += ev.text
      else if (ev.type === "replace" || ev.type === "done") text = ev.text
      if (text) {
        bubble.textContent = text
        messagesEl.scrollTop = messagesEl.scrollHeight
      }
    })
  } catch (err) {
    bubble.remove()
    renderError(err.message)
  } finally {
    bubble.classList.remove("typing")
    if (bubble.isConnected && text) finishStreamedMsg(bubble)
  }
}

// ---------- learning plans ----------

const LEARN_REQUEST = /^\s*(i\s*(really\s*)?(want|would like|'d like|wanna|need)\s*(to\s*)?(learn|study|practice|practise|get better at|improve|master)|teach me|help me (learn|with|improve|understand)|show me how|how do i (play|learn)|can you teach me|learn\b|plan\b)/i

const MAYBE_LESSON = /^\s*(please\s+)?(show|give|quiz|test)\s+me\b|^\s*let\s+me\s+(see|practi[cs]e|try)\b/i

function isLearnRequest(text) {
  return LEARN_REQUEST.test(text)
}

async function requestPlan(goal) {
  goal = goal.trim()
  if (!goal) return
  addMsg(goal, "user")
  const pending = addMsg("🧭 Looking for verified examples and building your lesson…", "system")
  const btn = document.getElementById("btn-chat")
  btn.disabled = true
  try {
    // library: true → verified Knowledge Library examples first, then the catalog/Qwen planner.
    const res = await api("/api/plans", "POST", {goal, library: true})
    pending.remove()
    renderPlan(res)
    await loadCourses()
  } catch (err) {
    pending.remove()
    const suggestions = (err.data && err.data.suggestions) || []
    const div = addMsg(escapeHtml(err.message), "assistant", true)
    if (suggestions.length) div.appendChild(suggestionChips(suggestions))
  } finally {
    btn.disabled = false
  }
}


function renderPlan(res) {
  const plan = res.plan
  const units = plan.units.map(u =>
    `<li><b>${escapeHtml(u.title)}</b>` +
    ` <span class="muted">(${u.lesson_ids.length} lesson${u.lesson_ids.length === 1 ? "" : "s"})</span></li>`
  ).join("")
  const skipped = (plan.skipped || []).map(s =>
    `<li>${escapeHtml(s.title)}: <span class="muted">${escapeHtml(s.reason)}</span></li>`).join("")
  const div = addMsg(
    `🧭 <b>${escapeHtml(plan.title)}</b><br>${escapeHtml(plan.summary)}<ol class="plan-units">${units}</ol>` +
    (skipped ? `<div class="muted">Left out because I couldn't verify them:</div><ul class="plan-units">${skipped}</ul>` : ""),
    "assistant", true)
  const start = document.createElement("button")
  start.className = "btn primary"
  start.textContent = "▶ Start the first lesson"
  start.addEventListener("click", () => startLesson(res.first_lesson_id))
  div.appendChild(start)
  narrator.auto(div)
  if ((plan.related || []).length) {
    const label = document.createElement("div")
    label.className = "muted"
    label.textContent = "Related topics (not in this plan):"
    div.appendChild(label)
    div.appendChild(suggestionChips(plan.related.map(t => `I want to learn ${t}`)))
  }
}

function suggestionChips(suggestions, labels = suggestions) {
  const row = document.createElement("div")
  row.className = "suggestions no-speech"
  for (const [i, s] of suggestions.entries()) {
    const chip = document.createElement("button")
    chip.className = "chip"
    chip.textContent = labels[i] || s
    chip.addEventListener("click", () => requestPlan(s))
    row.appendChild(chip)
  }
  return row
}

async function deletePlan(planId, title) {
  if (!confirm(`Delete the plan “${title}”?`)) return
  try {
    await api(`/api/plans/${planId}`, "DELETE")
    await loadCourses()
  } catch (err) {
    renderError(err.message)
  }
}

// ---------- sidebar / health ----------

const STARTERS = ["Opening principles", "Knight forks", "Checkmate patterns", "Rook endgames"]

// The sidebar lists the learner's own lessons. Built-in courses (e.g. the Italian Game
// lessons) aren't advertised here: ask for a topic and the tutor builds the lesson.
async function loadCourses() {
  const el = document.getElementById("course-list")
  try {
    const data = await api("/api/courses")
    const plans = data.courses.filter(c => c.kind === "plan")
    el.innerHTML = ""
    if (!plans.length) {
      const empty = document.createElement("p")
      empty.className = "muted empty"
      empty.textContent = "Nothing here yet — lessons you ask for will appear here."
      el.appendChild(empty)
      return
    }
    for (const course of plans) {
      const div = document.createElement("div")
      div.className = "course plan"
      const h3 = document.createElement("h3")
      const subject = course.title.replace(/^Learn:\s*/, "")
      h3.textContent = subject
      div.appendChild(h3)
      const del = document.createElement("button")
      del.className = "icon-btn"
      del.title = "Delete this plan"
      del.textContent = "✕"
      del.addEventListener("click", () => deletePlan(course.plan.id, course.title))
      div.appendChild(del)
      course.lessons.forEach((lesson, n) => {
        const btn = document.createElement("button")
        btn.className = "lesson-item" + (lesson.completed ? " done" : "")
        btn.disabled = lesson.status !== "available"
        btn.innerHTML = `<span>${n + 1}. ${escapeHtml(shortTitle(lesson.title, subject))}</span>` +
          (lesson.completed ? `<span class="badge">✓</span>` : "")
        if (lesson.status === "available") btn.addEventListener("click", () => startLesson(lesson.id))
        div.appendChild(btn)
      })
      el.appendChild(div)
    }
  } catch (err) {
    el.textContent = `Couldn't load your lessons: ${err.message}`
  }
}

// "Back-rank mate: practice" under the "Back-rank mate" heading reads as "Practice".
function shortTitle(title, subject) {
  const prefix = subject.toLowerCase() + ":"
  if (!title.toLowerCase().startsWith(prefix)) return title
  const rest = title.slice(prefix.length).trim()
  return rest ? rest[0].toUpperCase() + rest.slice(1) : title
}

// A quiet dot when everything works; words only when something needs attention.
async function loadHealth() {
  const el = document.getElementById("health")
  try {
    const h = await api("/api/health")
    const teacher = h.teacher === "qwen" ? `AI teacher: ${h.teacher_model}` : "AI teacher offline (built-in explanations)"
    el.title = `${h.engine ? "Chess engine ready" : "Chess engine unavailable"} · ${teacher}`
    el.className = "health " + (h.engine ? (h.teacher === "qwen" ? "ok" : "partial") : "bad")
    el.textContent = h.engine ? "" : "⚠ Chess engine unavailable"
  } catch (err) {
    el.className = "health bad"
    el.title = ""
    el.textContent = "⚠ Server unreachable"
  }
}

// ---------- wire up ----------

document.getElementById("btn-play").addEventListener("click", playDemonstration)
document.getElementById("btn-continue").addEventListener("click", () => { narrator.stop(); advanceLesson() })
document.getElementById("btn-hint").addEventListener("click", requestHint)
document.getElementById("btn-reveal").addEventListener("click", revealSolution)
document.getElementById("btn-chat").addEventListener("click", sendChat)
document.getElementById("btn-explain-example").addEventListener("click", explainExample)
document.getElementById("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter") sendChat()
})

{
  const welcome = document.getElementById("welcome")
  welcome.appendChild(suggestionChips(STARTERS.map(t => `I want to learn ${t.toLowerCase()}`), STARTERS))
  makeSpeakable(welcome)
}
setupSpeechControls()
loadHealth()
loadCourses()
