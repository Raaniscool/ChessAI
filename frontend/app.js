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

function moveInputHandler(event) {
  if (exerciseLocked()) return false
  switch (event.type) {
    case INPUT_EVENT_TYPE.moveInputStarted: {
      const piece = state.chess.get(event.squareFrom)
      if (!piece || piece.color !== state.chess.turn()) return false
      const moves = state.chess.moves({square: event.squareFrom, verbose: true})
      if (!moves.length) return false
      board.addLegalMovesMarkers(moves)
      return true
    }
    case INPUT_EVENT_TYPE.validateMoveInput: {
      const moves = state.chess.moves({square: event.squareFrom, verbose: true})
      return moves.some(m => m.to === event.squareTo)
    }
    case INPUT_EVENT_TYPE.moveInputFinished: {
      board.removeLegalMovesMarkers()
      handleUserMove(event.squareFrom, event.squareTo)
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

async function handleUserMove(from, to) {
  const candidates = state.chess.moves({square: from, verbose: true}).filter(m => m.to === to)
  if (!candidates.length) return
  const promotion = candidates.some(m => m.promotion) ? "q" : undefined
  const uci = from + to + (promotion || "")
  const fenBefore = state.chess.fen()
  applyUci(state.chess, uci)

  state.busy = true
  setStatus("Analyzing your move…")
  try {
    const result = await api(`/api/sessions/${state.sessionId}/move`, "POST", {uci})
    renderFeedback(result)
    if (result.accepted) {
      // Server pushed the move too; local mirror matches. Unlocks Continue.
      state.step.accepted = true
      showBtn("btn-continue")
      hideBtn("btn-hint"); hideBtn("btn-reveal")
      setStatus("")
      await board.setPosition(state.chess.fen(), true)
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
    `<div style="margin-top:6px">${escapeHtml(result.explanation)}</div>` +
    `<div class="meta">${escapeHtml(best)} analysis: ${fb.depth || "?"} plies · teacher: ${result.teacher}</div>`
  slot.appendChild(card)
  if (result.continue_text) addMsg(result.continue_text)
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
  ;["btn-play", "btn-continue", "btn-hint", "btn-reveal"].forEach(hideBtn)
}

// ---------- step rendering ----------

async function renderStep(step) {
  state.step = step
  state.busy = false
  hideControls()
  document.getElementById("feedback-slot").innerHTML = ""
  document.getElementById("lesson-title").textContent = step.lesson_title
  document.getElementById("step-indicator").textContent = `Step ${step.index + 1} / ${step.total_steps}`
  board.disableMoveInput()

  if (step.type === "teach") {
    addMsg(step.text)
    await showPosition(step.board.fen, {highlights: step.board.highlights || []})
    showBtn("btn-continue")
    setStatus(step.board.lock === false ? "" : "Board is locked while the teacher explains.")
  } else if (step.type === "demonstrate") {
    addMsg(step.text)
    await showPosition(step.start_fen)
    showBtn("btn-play")
    setStatus("Press “Play demonstration” to watch the moves.")
  } else if (step.type === "exercise") {
    addMsg(`🎯 **Exercise:** ${step.prompt}`.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"), "assistant", true)
    const side = step.side === "black" ? COLOR.black : COLOR.white
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
    setStatus("Demonstration complete.")
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
  addMsg(message, "user")
  if (!state.sessionId) {
    addMsg("Start a lesson first — I teach inside the lesson context.", "system")
    return
  }
  try {
    const res = await api(`/api/sessions/${state.sessionId}/chat`, "POST", {message})
    addMsg(res.reply)
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
      div.className = "course"
      div.innerHTML = `<h3>${escapeHtml(course.title)}</h3><p>${escapeHtml(course.description)}</p>`
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
    el.textContent = `${h.engine ? "⚙ engine ✓" : "⚠ engine unavailable"} · teacher: ${h.teacher}` +
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
document.getElementById("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter") sendChat()
})

loadHealth()
loadCourses()
