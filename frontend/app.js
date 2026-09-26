// AI Chess Tutor — frontend
// Board renders server-validated state only; every learner move is checked
// locally (UX) and authoritatively on the server (rules).

import {Chessboard, COLOR, INPUT_EVENT_TYPE, FEN, BORDER_TYPE} from "./vendor/cm-chessboard/src/Chessboard.js"
import {Markers, MARKER_TYPE} from "./vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Chess} from "./vendor/chess.mjs/Chess.js"

// ---------- state ----------

const state = {
  sessionId: null,
  step: null,
  chess: new Chess(),      // local mirror; server stays authoritative
  busy: false,
  demoPlaying: false,
  stream: null,            // AbortController of the AI text currently streaming
  exploreChess: null,      // free-play copy while the teacher explains (never graded)
  exploreStartFen: null,
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

// ---------- free exploration (teach / after demonstrations) ----------

const exploreInput = legalInputHandler(
  () => (state.demoPlaying ? null : state.exploreChess),
  async (from, to) => {
    const chess = state.exploreChess
    const candidates = chess.moves({square: from, verbose: true}).filter(m => m.to === to)
    if (!candidates.length) return
    chess.move({from, to, promotion: candidates.some(m => m.promotion) ? "q" : undefined})
    await board.setPosition(chess.fen(), true)  // syncs castling rook / en passant / promotion
    showBtn("btn-reset")
    setStatus("Exploring freely — these moves aren't graded. Press ↺ to reset.")
  },
)

function enableExplore(fen) {
  state.exploreChess = new Chess(fen)
  state.exploreStartFen = fen
  board.disableMoveInput()
  board.enableMoveInput(exploreInput)  // both colours, turn order enforced by the rules
}

async function resetExplore() {
  if (!state.exploreStartFen) return
  state.exploreChess = new Chess(state.exploreStartFen)
  clearMarkers()
  await board.setPosition(state.exploreStartFen, true)
  hideBtn("btn-reset")
  setStatus("Board reset. Try any legal move — nothing here is graded.")
}

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
      showBtn("btn-continue")  // only once the board has settled, so the click isn't ignored
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

// ---------- messages / feedback ----------

const messagesEl = document.getElementById("messages")

function addMsg(text, kind = "assistant", allowHtml = false) {
  const div = document.createElement("div")
  div.className = `msg ${kind}`
  if (allowHtml) div.innerHTML = text
  else div.textContent = text
  messagesEl.appendChild(div)
  messagesEl.scrollTop = messagesEl.scrollHeight
  return div
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
  const best = fb.best_move ? ` Engine's choice: ${fb.best_move}.` : ""
  card.innerHTML =
    `<span class="cat ${fb.category}">${label}</span> — you played <b>${fb.user_move}</b>.` +
    `<div class="explanation" style="margin-top:6px">${escapeHtml(result.explanation)}</div>` +
    `<div class="meta">${escapeHtml(best)} analysis: ${fb.depth || "?"} plies · ` +
    `<span class="teacher-label">teacher: ${result.teacher}</span></div>`
  slot.appendChild(card)
  if (result.continue_text) addMsg(result.continue_text)
  if (result.ai_explanation) streamExplanation(card)
}

// The engine verdict is shown instantly; Qwen's explanation streams in after it.
async function streamExplanation(card) {
  const textEl = card.querySelector(".explanation")
  const label = card.querySelector(".teacher-label")
  const quick = textEl.textContent
  let text = ""
  label.textContent = "✍ Qwen is writing an explanation…"
  label.classList.add("typing")
  try {
    await streamEvents(`/api/sessions/${state.sessionId}/explain`, undefined, ev => {
      if (ev.type === "delta") {
        text += ev.text
        textEl.textContent = text
      } else if (ev.type === "replace") {
        text = ev.text
        textEl.textContent = text
      } else if (ev.type === "done") {
        label.textContent = `teacher: ${ev.teacher}`
      }
    })
  } catch (err) {
    textEl.textContent = quick
    label.textContent = "teacher: fallback (AI unavailable)"
  } finally {
    label.classList.remove("typing")
    if (label.textContent.startsWith("✍")) label.textContent = text ? "teacher: qwen" : "teacher: fallback"
    if (!text) textEl.textContent = quick
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
  ;["btn-play", "btn-continue", "btn-hint", "btn-reveal", "btn-reset"].forEach(hideBtn)
}

// ---------- step rendering ----------

async function renderStep(step) {
  stopStream()
  state.step = step
  state.busy = false
  hideControls()
  document.getElementById("feedback-slot").innerHTML = ""
  document.getElementById("lesson-title").textContent = step.lesson_title
  document.getElementById("step-indicator").textContent = `Step ${step.index + 1} / ${step.total_steps}`
  board.disableMoveInput()
  state.exploreChess = null
  state.exploreStartFen = null

  if (step.type === "teach") {
    addMsg(step.text)
    await showPosition(step.board.fen, {highlights: step.board.highlights || []})
    showBtn("btn-continue")
    enableExplore(step.board.fen)
    setStatus("You can try moves on the board — they aren't graded.")
  } else if (step.type === "demonstrate") {
    addMsg(step.text)
    await showPosition(step.start_fen)
    showBtn("btn-play")
    setStatus("Press “Play demonstration” to watch the moves.")
  } else if (step.type === "exercise") {
    addMsg(`🎯 **Exercise:** ${step.prompt}`.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"), "assistant", true)
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
      showBtn("btn-continue")
      hideBtn("btn-hint"); hideBtn("btn-reveal")
      setStatus("Solved — continue when ready.")
    }
  }
}

async function playDemonstration() {
  if (state.demoPlaying || !state.step || state.step.type !== "demonstrate") return
  state.demoPlaying = true
  board.disableMoveInput()
  hideBtn("btn-play")
  const btn = document.getElementById("btn-play")
  const step = state.step
  const demoChess = new Chess(step.start_fen)
  try {
    for (let i = 0; i < step.moves.length; i++) {
      applyUci(demoChess, step.moves[i])
      await board.setPosition(demoChess.fen(), true)
      await sleep(620)
      if (step.comments && step.comments[i]) {
        addMsg(step.comments[i], "system")
      }
    }
    const after = step.board_after || {}
    clearMarkers()
    applyHighlights(after.highlights || [])
    enableExplore(demoChess.fen())
    setStatus("Demonstration complete — try moves yourself, or press Continue.")
  } finally {
    state.demoPlaying = false
    showBtn("btn-continue")
    void btn
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
      addMsg(`💡 Hint ${res.index}/${res.total}: ${res.hint}`, "system")
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
    addMsg(`🔎 Solution: ${moves || "see the engine's best move"}`, "system")
    state.step.accepted = true
    showBtn("btn-continue")
    hideBtn("btn-hint"); hideBtn("btn-reveal")
    setStatus("Solution shown — continue when ready.")
  } catch (err) {
    addMsg(err.message, "system")
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
  }
}

// ---------- learning plans ----------

const LEARN_REQUEST = /^\s*(i\s*(really\s*)?(want|would like|'d like|wanna|need)\s*(to\s*)?(learn|study|practice|practise|get better at|improve|master)|teach me|help me (learn|with|improve|understand)|show me how|how do i (play|learn)|can you teach me|learn\b|plan\b)/i

function isLearnRequest(text) {
  return LEARN_REQUEST.test(text)
}

async function requestPlan(goal) {
  goal = goal.trim()
  if (!goal) return
  addMsg(goal, "user")
  const pending = addMsg("🧭 Building your learning plan… (with Qwen this can take a little while)", "system")
  const btn = document.getElementById("btn-plan")
  btn.disabled = true
  try {
    const res = await api("/api/plans", "POST", {goal})
    pending.remove()
    renderPlan(res)
    await loadCourses()
  } catch (err) {
    pending.remove()
    const suggestions = (err.data && err.data.suggestions) || []
    const div = addMsg(escapeHtml(err.message), "assistant", true)
    if (suggestions.length) {
      const row = document.createElement("div")
      row.className = "suggestions"
      for (const s of suggestions) {
        const chip = document.createElement("button")
        chip.className = "chip"
        chip.textContent = s
        chip.addEventListener("click", () => requestPlan(s))
        row.appendChild(chip)
      }
      div.appendChild(row)
    }
  } finally {
    btn.disabled = false
  }
}

const CATEGORY_ICONS = {opening: "♟", tactic: "⚔", endgame: "♔", strategy: "🧠"}

function renderPlan(res) {
  const plan = res.plan
  const units = plan.units.map((u, i) =>
    `<li><b>${escapeHtml(u.title)}</b> ${CATEGORY_ICONS[u.category] || ""}` +
    ` <span class="muted">— ${escapeHtml(u.reason)} (${u.lesson_ids.length} lesson${u.lesson_ids.length === 1 ? "" : "s"})</span></li>`
  ).join("")
  const skipped = (plan.skipped || []).map(s =>
    `<li>${escapeHtml(s.title)}: <span class="muted">${escapeHtml(s.reason)}</span></li>`).join("")
  const div = addMsg(
    `🧭 <b>${escapeHtml(plan.title)}</b><br>${escapeHtml(plan.summary)}<ol class="plan-units">${units}</ol>` +
    (skipped ? `<div class="muted">Left out because I couldn't verify them:</div><ul class="plan-units">${skipped}</ul>` : "") +
    `<div class="muted">Planned by ${plan.planner === "qwen" ? "Qwen" : "the built-in catalog"}; every move you'll be asked to find is checked by Stockfish.</div>`,
    "assistant", true)
  const start = document.createElement("button")
  start.className = "btn primary"
  start.textContent = "▶ Start the first lesson"
  start.addEventListener("click", () => startLesson(res.first_lesson_id))
  div.appendChild(start)
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

async function loadCourses() {
  const el = document.getElementById("course-list")
  try {
    const data = await api("/api/courses")
    el.innerHTML = ""
    for (const course of data.courses) {
      const div = document.createElement("div")
      div.className = "course" + (course.kind === "plan" ? " plan" : "")
      div.innerHTML = `<h3>${course.kind === "plan" ? "🧭 " : ""}${escapeHtml(course.title)}</h3>` +
        `<p>${escapeHtml(course.description)}</p>`
      if (course.kind === "plan") {
        const del = document.createElement("button")
        del.className = "icon-btn"
        del.title = "Delete this plan"
        del.textContent = "✕"
        del.addEventListener("click", () => deletePlan(course.plan.id, course.title))
        div.appendChild(del)
      }
      for (const lesson of course.lessons) {
        const btn = document.createElement("button")
        btn.className = "lesson-item" + (lesson.completed ? " done" : "")
        btn.disabled = lesson.status !== "available"
        btn.innerHTML = `<span>${escapeHtml(lesson.title)}</span>` +
          `<span class="badge">${lesson.completed ? "✓ done" : lesson.status}</span>`
        if (lesson.status === "available") {
          btn.addEventListener("click", () => startLesson(lesson.id))
        }
        div.appendChild(btn)
      }
      el.appendChild(div)
    }
  } catch (err) {
    el.textContent = `Failed to load courses: ${err.message}`
  }
}

async function loadHealth() {
  const el = document.getElementById("health")
  try {
    const h = await api("/api/health")
    el.textContent = `${h.engine ? "⚙ engine ✓" : "⚠ engine unavailable"} · teacher: ${h.teacher === "qwen" ? `qwen (${h.teacher_model})` : "offline fallback"}` +
      ` · ${h.lessons} lesson${h.lessons === 1 ? "" : "s"}`
  } catch (err) {
    el.textContent = "⚠ server unreachable"
  }
}

// ---------- wire up ----------

document.getElementById("btn-play").addEventListener("click", playDemonstration)
document.getElementById("btn-continue").addEventListener("click", advanceLesson)
document.getElementById("btn-hint").addEventListener("click", requestHint)
document.getElementById("btn-reveal").addEventListener("click", revealSolution)
document.getElementById("btn-chat").addEventListener("click", sendChat)
document.getElementById("btn-reset").addEventListener("click", resetExplore)
document.getElementById("plan-form").addEventListener("submit", e => {
  e.preventDefault()
  const input = document.getElementById("plan-input")
  const goal = input.value
  input.value = ""
  requestPlan(goal)
})
document.getElementById("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter") sendChat()
})

loadHealth()
loadCourses()
