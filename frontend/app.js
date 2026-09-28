// AI Chess Tutor — frontend
// Board renders server-validated state only; every learner move is checked
// locally (UX) and authoritatively on the server (rules).

import {Chessboard, COLOR, INPUT_EVENT_TYPE, FEN, BORDER_TYPE} from "./vendor/cm-chessboard/src/Chessboard.js"
import {Markers, MARKER_TYPE} from "./vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Chess} from "./vendor/chess.mjs/Chess.js"
import {annotateLines} from "./lines.js"
import {Narrator, decorateMoves, targetSquare} from "./speech.js"
import {setupGameAnalysis} from "./analysis.js"
import {errorMessage, readEvents, reach} from "./net.js"
import {OTHER, clarifyBody, optionLabel, understoodLine, verificationBadge} from "./plan-view.js"
import {setupCoach} from "./coach.js"

// ---------- stale page guard ----------
// A browser can combine a cached old index.html with a newer app.js. Instead of
// failing with "Cannot read properties of null", say what to do.
const REQUIRED_IDS = ["board", "board-status", "messages", "feedback-slot", "lesson-title", "step-indicator",
  "course-list", "health", "chat-input", "btn-chat", "btn-continue", "btn-hint", "btn-reveal", "btn-play",
  "btn-explain-example", "speech-controls", "btn-read-aloud", "speech-rate", "speech-voice", "btn-stop-speech",
  "btn-coach", "btn-settings", "coach-panel", "settings-panel",
  "welcome", "tab-lessons", "tab-games", "lessons-side", "games-side", "analysis-pane", "ga-games", "ga-pgn",
  "ga-username", "ga-fetch-btn", "ga-paste", "ga-import-btn", "ga-status", "ga-import", "ga-overview", "ga-review", "ga-title", "ga-counter",
  "ga-back", "ga-prev", "ga-next", "ga-body"]
const missingIds = REQUIRED_IDS.filter(id => !document.getElementById(id))
if (missingIds.length) {
  const bar = document.createElement("div")
  bar.className = "stale-page"
  bar.innerHTML = "This page was updated, but your browser is showing an old copy. " +
    "Press <b>Ctrl+F5</b> (or click here) to load the new version."
  bar.addEventListener("click", () => location.reload())
  document.body.prepend(bar)
  throw new Error(`Outdated page (missing: ${missingIds.join(", ")}) — press Ctrl+F5`)
}

// ---------- state ----------

const state = {
  sessionId: null,
  step: null,
  chess: new Chess(),      // local mirror; server stays authoritative
  busy: false,
  demoPlaying: false,
  stream: null,            // AbortController of the AI text currently streaming
  view: "lessons",         // "lessons" | "games"
}

// ---------- api ----------

async function api(path, method = "GET", body = undefined) {
  const opts = {method, headers: {"Content-Type": "application/json"}}
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await reach(path, opts)
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
// Exclusive streams (teacher replies) replace each other; a background stream
// (game analysis progress) runs alongside them and is never cancelled by them.
async function streamEvents(path, body, onEvent, {exclusive = true} = {}) {
  const controller = new AbortController()
  if (exclusive) {
    if (state.stream) state.stream.abort()
    state.stream = controller
  }
  try {
    const res = await reach(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    if (!res.ok) throw new Error(await errorMessage(res))
    await readEvents(res.body, onEvent, controller.signal)
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

// Board updates run one after another: two views (a lesson and a game review) may
// ask for a position at nearly the same moment, and cm-chessboard can't overlap them.
let boardQueue = Promise.resolve()
function showPosition(fen, {orientation = COLOR.white, highlights = [], animated = false} = {}) {
  const run = async () => {
    clearMarkers()
    if (board.getOrientation() !== orientation) await board.setOrientation(orientation)
    await board.setPosition(fen, animated)
    applyHighlights(highlights)
  }
  boardQueue = boardQueue.then(run, run)
  return boardQueue
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
      setStatus(retryText(result.tries || 1))
      if (result.help) renderHelpOffer(result.help)
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

// "Try again", said a little differently each time (the words matter less than not sounding stuck).
function retryText(tries) {
  return tries <= 1 ? "Try again — you can do it."
    : tries === 2 ? "Try again — take a moment to look at every check and capture."
      : "Try again, or use a hint — no shame in it."
}

// After repeated misses: the right kind of help, offered once, not forced.
function renderHelpOffer(help) {
  if (document.querySelector("#messages .help-offer[data-offer='" + help.offer + "']")) return
  const div = addMsg(help.text, "system")
  div.classList.add("help-offer")
  div.dataset.offer = help.offer
  const btn = document.createElement("button")
  btn.className = "btn no-speech"
  btn.textContent = help.offer === "hint" ? "💡 Show a hint" : "Show the answer"
  btn.addEventListener("click", () => {
    btn.disabled = true
    if (help.offer === "hint") requestHint()
    else revealSolution()
  })
  div.appendChild(btn)
}

// A note from the coach: the lesson changed to fit how it's going ("a harder one next").
function showCoachNote(text) {
  if (!text || text === state.lastNote) return
  state.lastNote = text
  const div = addMsg(text, "system")
  div.classList.add("coach-note")
  narrator.auto(div, "lessons")
}

// ---------- read aloud ----------

const SPEECH_MARKER = {class: "marker-speech", slice: "markerSquare"}

const HOVER_MARKER = {class: "marker-speech", slice: "markerSquare"}

// [from, to] when `san` is legal in `fen`, else null.
function legalSquares(fen, san) {
  try {
    const move = new Chess(fen).move(san.replace(/[+#]$/, ""))
    return move ? [move.from, move.to] : null
  } catch (_) {
    return null
  }
}

// Squares for a spoken (or pointed-at) move: destination, plus the origin when the move is
// legal. Text that talks about a line of moves (a game review) lists its positions in a
// data-fens attribute; otherwise the board as shown is tried for both sides.
function squaresFor(mark, element) {
  if (!mark) return []
  const holder = element && element.closest ? element.closest("[data-fens]") : null
  if (holder) {
    let fens = []
    try { fens = JSON.parse(holder.dataset.fens) } catch (_) { /* ignore */ }
    for (const fen of fens) {
      const squares = legalSquares(fen, mark.san)
      if (squares) return squares
    }
  }
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

// ---------- lines of moves: the pieces move ----------
// In a game review, a line of moves ("the line goes Nc7+ Kd8 Nxa7") is shown by playing it
// on the board — while it's read aloud, when pointed at, or all at once on a click. Only
// single squares are highlighted. The board goes back to where it was afterwards.
const lineView = {restore: null, timer: null, token: 0}

const placementOf = fen => (fen || "").split(" ")[0]

async function showLineMove(span) {
  if (!board || !span.chessLine) return
  const {positions, index} = span.chessLine
  clearTimeout(lineView.timer)
  const token = ++lineView.token
  if (lineView.restore === null) lineView.restore = board.getPosition()
  board.removeMarkers(SPEECH_MARKER)
  board.removeMarkers(HOVER_MARKER)
  if (placementOf(board.getPosition()) !== placementOf(positions[index])) await board.setPosition(positions[index], false)
  if (token !== lineView.token) return
  await board.setPosition(positions[index + 1], true)
}

function endLineView(delay) {
  if (lineView.restore === null) return
  clearTimeout(lineView.timer)
  lineView.timer = setTimeout(async () => {
    const fen = lineView.restore
    lineView.restore = null
    lineView.token++
    if (board && fen) await board.setPosition(fen, true)
  }, delay)
}

async function playWholeLine(span) {
  if (!board || !span.chessLine) return
  const {positions} = span.chessLine
  clearTimeout(lineView.timer)
  lineView.restore = null  // asked for: the line's final position stays on the board
  const token = ++lineView.token
  board.removeMarkers(SPEECH_MARKER)
  board.removeMarkers(HOVER_MARKER)
  await board.setPosition(positions[0], false)
  for (let i = 1; i < positions.length; i++) {
    await new Promise(r => setTimeout(r, 600))
    if (token !== lineView.token) return
    await board.setPosition(positions[i], true)
  }
}

document.addEventListener("click", ev => {
  const span = ev.target.closest ? ev.target.closest(".mv-line") : null
  if (span) playWholeLine(span)
})

const narrator = new Narrator({
  onMove(mark, element, node) {
    if (node && node.chessLine) {
      showLineMove(node)
      return
    }
    board.removeMarkers(SPEECH_MARKER)
    if (!mark) endLineView(1200)  // finished reading: back to the position being reviewed
    for (const sq of squaresFor(mark, element)) board.addMarker(SPEECH_MARKER, sq)
  },
  onState(speaking) {
    document.getElementById("btn-stop-speech").classList.toggle("hidden", !speaking)
  },
  onNotice(text) {
    addMsg(text, "system")
  },
})

// Pointing at a move or square in any text lights it up on the board (no speech needed).
let hoveredMove = null
document.addEventListener("mouseover", ev => {
  const mv = ev.target.closest ? ev.target.closest(".mv") : null
  if (mv === hoveredMove) return
  hoveredMove = mv
  if (!board) return
  board.removeMarkers(HOVER_MARKER)
  if (mv && mv.chessLine) { showLineMove(mv); return }
  if (!narrator.current) endLineView(700)  // pointer left the line: put the position back
  if (!mv) return
  const san = mv.dataset.san
  for (const sq of squaresFor({san, square: targetSquare(san)}, mv)) board.addMarker(HOVER_MARKER, sq)
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
  annotateLines(el, Chess)  // review text: lines of moves play on the board
  if (narrator.supported && !el.querySelector(":scope > .speak-btn")) el.appendChild(speakButton(el))
  return el
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
function finishStreamedMsg(el, kind = "explanations", importance = undefined) {
  delete el.dataset.moves
  el.querySelectorAll(":scope > .speak-btn").forEach(b => b.remove())
  makeSpeakable(el)
  return narrator.auto(el, kind, importance)
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
  // Read out only when the move matters (importance), never every recapture.
  if (result.ai_explanation) streamExplanation(card, result.importance)
  else { makeSpeakable(card); narrator.auto(card, "explanations", result.importance) }
  if (result.adapted && result.adapted.note) showCoachNote(result.adapted.note)
}

// The engine verdict is shown instantly; Qwen's explanation streams in after it.
async function streamExplanation(card, importance) {
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
    if (card.isConnected) finishStreamedMsg(card, "explanations", importance)
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
  if (step.coach_note) showCoachNote(step.coach_note)

  if (step.type === "teach") {
    const msg = addMsg(step.text)
    await showPosition(step.board.fen, {highlights: step.board.highlights || []})
    showContinue()
    if (step.example && step.example.explainable) showBtn("btn-explain-example")
    setStatus("")
    narrator.auto(msg, "lessons")
  } else if (step.type === "demonstrate") {
    const msg = addMsg(step.text)
    await showPosition(step.start_fen)
    setStatus("Watch the demonstration…")
    // Plays by itself (read aloud first when that's on); the learner can watch it again.
    await narrator.auto(msg, "lessons")
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
    narrator.auto(msg, "puzzles")
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
      if (state.step !== step || state.view !== "lessons") return  // learner moved on
      applyUci(demoChess, step.moves[i])
      await board.setPosition(demoChess.fen(), true)
      const comment = step.comments && step.comments[i]
      if (comment) {
        const msg = addMsg(comment, "system")
        // Read aloud: the next move waits until the comment has been spoken.
        if (narrator.enabled && narrator.prefs.read.lessons !== false) await narrator.speak(msg)
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
      renderCompletion(res)
      setStatus("Lesson complete!")
      loadCourses()
      coach.refresh()
    } else {
      await renderStep(res.step)
    }
  } catch (err) {
    renderError(err.message)
  } finally {
    state.busy = false
  }
}

// The end of a lesson: how it went, in plain words, and where to go next.
function renderCompletion(res) {
  const summary = res.summary || {}
  const div = addMsg(`✅ ${res.completion_text}`)
  div.classList.add("lesson-summary")
  if (summary.text) {
    const line = document.createElement("div")
    line.className = "stats"
    line.textContent = summary.text
    div.insertBefore(line, div.querySelector(".speak-btn"))
  }
  const next = res.next_steps || []
  if (next.length) {
    const label = document.createElement("div")
    label.className = "muted"
    label.textContent = "Where to next:"
    div.appendChild(label)
    const row = suggestionChips(next.map(s => s.goal), next.map(s => s.title))
    next.forEach((s, i) => { if (row.children[i]) row.children[i].title = s.reason })
    div.appendChild(row)
  }
  narrator.auto(div, "lessons")
}

async function startLesson(lessonId) {
  try {
    const res = await api(`/api/lessons/${lessonId}/start`, "POST")
    state.sessionId = res.session_id
    state.lastNote = null
    await renderStep(res.step)
  } catch (err) {
    renderError(err.message)
  }
}

async function requestHint() {
  try {
    const res = await api(`/api/sessions/${state.sessionId}/hint`, "POST")
    if (res.hint) {
      narrator.auto(addMsg(`💡 Hint ${res.index}/${res.total}: ${res.hint}`, "system"), "hints")
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
    narrator.auto(addMsg(`🔎 Solution: ${moves || "see the engine's best move"}`, "system"), "hints")
    state.step.accepted = true
    hideBtn("btn-hint"); hideBtn("btn-reveal")
    board.disableMoveInput()
    // Play the answer on the board, so the learner sees the idea rather than just reads it.
    if (res.uci && state.step.board) {
      try {
        const chess = new Chess(state.step.board.fen)
        const move = applyUci(chess, res.uci)
        await showPosition(state.step.board.fen, {orientation: board.getOrientation()})
        await board.setPosition(chess.fen(), true)
        clearMarkers()
        board.addMarker(MARKER_TYPE.square, move.from)
        board.addMarker(MARKER_TYPE.square, move.to)
        state.chess = chess
      } catch (_) { /* the text answer is enough */ }
    }
    showContinue()
    setStatus("Solution shown — continue when ready.")
    if (res.adapted && res.adapted.note) showCoachNote(res.adapted.note)
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
    else if (bubble.isConnected) bubble.remove()  // stopped before any text arrived
  }
}

async function sendChat() {
  const input = document.getElementById("chat-input")
  const message = input.value.trim()
  if (!message) return
  input.value = ""
  // "What's a fork?" — answered at once from the library (no AI call, no lesson built).
  if (MAYBE_DEFINITION.test(message) && await quickAnswer(message)) return
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
    else if (bubble.isConnected) bubble.remove()  // stopped before any text arrived
  }
}

const MAYBE_DEFINITION = /^\s*(?:(?:so|ok|okay|hey|please)[,\s]+)?(what|whats|define|definition|meaning|explain what|tell me what|how (does|do|can) (?!i\b|you\b|we\b))/i

async function quickAnswer(message) {
  let res
  try { res = await api("/api/knowledge/answer", "POST", {message}) } catch (_) { return false }
  const a = res && res.answer
  if (!a) return false
  addMsg(message, "user")
  const div = addMsg(`📖 <b>${escapeHtml(a.term)}</b> — ${escapeHtml(a.text)}`, "assistant", true)
  div.classList.add("quick-answer")
  if (!a.verified) {
    const note = document.createElement("div")
    note.className = "muted"
    note.textContent = "From my glossary — I don't have checked example positions for this yet."
    div.appendChild(note)
  }
  if (a.source !== "rules") {
    div.appendChild(suggestionChips([a.lesson_goal], [a.examples ? `Make me a lesson on ${a.term.toLowerCase()}` :
      `Show me what you can teach about ${a.term.toLowerCase()}`]))
  }
  narrator.auto(div, "explanations")
  return true
}

// ---------- learning plans ----------

const LEARN_REQUEST = /^\s*(i\s*(really\s*)?(want|would like|'d like|wanna|need)\s*(to\s*)?(learn|study|practice|practise|get better at|improve|master)|teach me|help me (learn|with|improve|understand)|show me how|how do i (play|learn)|can you teach me|learn\b|plan\b)/i

const MAYBE_LESSON = /^\s*(please\s+)?(show|give|quiz|test)\s+me\b|^\s*let\s+me\s+(see|practi[cs]e|try)\b/i

function isLearnRequest(text) {
  return LEARN_REQUEST.test(text)
}

// `body` overrides the request (a clarification answer or "ask me again"); the learner's
// message is only echoed for a new request.
async function requestPlan(goal, body = null) {
  goal = goal.trim()
  if (!goal) return
  if (!body) addMsg(goal, "user")
  const pending = addMsg("🧭 Looking for verified examples and building your lesson…", "system")
  // Without library examples the server builds new positions and checks each with Stockfish.
  const slow = setTimeout(() => {
    pending.textContent = "🧭 Still working: I'm checking every position with Stockfish before you see it — " +
      "new material takes a few seconds the first time…"
  }, 3000)
  const btn = document.getElementById("btn-chat")
  btn.disabled = true
  try {
    // library: true → verified Knowledge Library examples first, then the catalog/Qwen planner.
    const res = await api("/api/plans", "POST", body || {goal, library: true})
    pending.remove()
    if (res.clarify) {  // several different readings: ask instead of guessing
      renderClarify(res.clarify, res.goal || goal)
      return
    }
    renderPlan(res)
    await loadCourses()
  } catch (err) {
    pending.remove()
    const suggestions = (err.data && err.data.suggestions) || []
    const div = addMsg(escapeHtml(err.message), "assistant", true)
    if (suggestions.length) div.appendChild(suggestionChips(suggestions))
  } finally {
    clearTimeout(slow)
    btn.disabled = false
  }
}


// A question card: one button per reading, plus "Something else" with a text box.
function renderClarify(question, goal) {
  const div = addMsg(`🤔 <b>${escapeHtml(question.question)}</b>`, "assistant", true)
  div.classList.add("clarify-card")
  const row = document.createElement("div")
  row.className = "clarify-options no-speech"
  const other = document.createElement("form")
  other.className = "clarify-other no-speech"
  other.hidden = true
  other.innerHTML = `<input type="text" maxlength="200" placeholder="Tell me what you'd like to learn">` +
    `<button class="btn primary" type="submit">Send</button>`
  const note = document.createElement("div")
  note.className = "muted clarify-note"
  const answer = (choice, text = "") => {
    const got = clarifyBody(goal, question, choice, text)
    if (got.error) { note.textContent = got.error; return }
    div.querySelectorAll("button, input").forEach(el => { el.disabled = true })
    const picked = question.options.find(o => o.id === choice)
    addMsg(choice === OTHER ? text.trim() : optionLabel(picked), "user")
    requestPlan(goal, got.body)
  }
  for (const opt of question.options) {
    const b = document.createElement("button")
    b.className = "chip"
    b.textContent = optionLabel(opt)
    b.addEventListener("click", () => {
      if (opt.id === OTHER) { other.hidden = false; other.querySelector("input").focus(); return }
      answer(opt.id)
    })
    row.appendChild(b)
  }
  other.addEventListener("submit", e => { e.preventDefault(); answer(OTHER, other.querySelector("input").value) })
  div.append(row, other, note)
  narrator.auto(div, "lessons")
}

function renderPlan(res) {
  const plan = res.plan
  const units = plan.units.map(u =>
    `<li><b>${escapeHtml(u.title)}</b>` +
    ` <span class="muted">(${u.lesson_ids.length} lesson${u.lesson_ids.length === 1 ? "" : "s"})</span>` +
    (u.reason ? `<div class="unit-reason muted">${escapeHtml(u.reason)}</div>` : "") + `</li>`
  ).join("")
  const skipped = (plan.skipped || []).map(s =>
    `<li>${escapeHtml(s.title)}: <span class="muted">${escapeHtml(s.reason)}</span></li>`).join("")
  const div = addMsg(
    `🧭 <b>${escapeHtml(plan.title)}</b><br>${escapeHtml(plan.summary)}<ol class="plan-units">${units}</ol>` +
    (skipped ? `<div class="muted">Left out of this plan:</div><ul class="plan-units">${skipped}</ul>` : ""),
    "assistant", true)
  const badge = verificationBadge(plan)
  if (badge) {
    const el = document.createElement("div")
    el.className = `plan-badge ${badge.kind}`
    el.textContent = badge.text
    el.title = badge.detail
    const why = document.createElement("div")
    why.className = "muted plan-badge-detail"
    why.textContent = badge.detail
    div.append(el, why)
  }
  const personal = plan.personalization && plan.personalization.reason
  if (personal) {
    const line = document.createElement("div")
    line.className = "personal-note"
    line.textContent = "Personalized for you: " + personal
    div.appendChild(line)
  }
  const understood = understoodLine(plan)
  if (understood) {
    const line = document.createElement("div")
    line.className = "muted understood no-speech"
    line.textContent = understood + " "
    const again = document.createElement("button")
    again.className = "link-btn"
    again.textContent = "Ask me again"
    again.addEventListener("click", () => requestPlan(plan.goal, {goal: plan.goal, library: true, reclarify: true}))
    line.appendChild(again)
    div.appendChild(line)
  }
  const start = document.createElement("button")
  start.className = "btn primary"
  start.textContent = "▶ Start the first lesson"
  start.addEventListener("click", () => startLesson(res.first_lesson_id))
  div.appendChild(start)
  narrator.auto(div, "lessons")
  // Foundations the plan assumes (e.g. opening principles before the Sicilian): offered, never
  // added — the plan contains only what was asked for.
  if ((plan.prerequisites || []).length) {
    const label = document.createElement("div")
    label.className = "muted"
    label.textContent = "Good to know first (optional, not in this plan):"
    div.appendChild(label)
    div.appendChild(suggestionChips(plan.prerequisites.map(p => `I want to learn ${p.title}`)))
  }
  if ((plan.related || []).length) {
    const label = document.createElement("div")
    label.className = "muted"
    label.textContent = plan.title.startsWith("Plan: play as") ? "Other good answers (not in this plan):"
      : "Related topics (not in this plan):"
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

// ---------- tabs: lessons / game analysis ----------

const gameAnalysis = setupGameAnalysis({
  api, streamEvents, stopStream, board, showPosition, clearMarkers, Chess, COLOR, MARKER_TYPE, escapeHtml,
  setStatus, makeSpeakable, narrator,
  // A training plan built from the learner's games is an ordinary plan: show it in the lessons view.
  onTraining: async res => {
    await switchView("lessons")
    renderPlan(res)
    await loadCourses()
  },
})

let lessonBoard = null  // what the lesson view showed, restored when coming back

async function switchView(name) {
  if (state.view === name) return
  narrator.stop()
  const games = name === "games"
  if (games) {
    // A lesson reply still streaming keeps going (its pane is only hidden).
    lessonBoard = {fen: board.getPosition(), orientation: board.getOrientation(),
      status: document.getElementById("board-status").textContent}
  } else {
    gameAnalysis.leave()
  }
  state.view = name
  for (const [id, on] of [["tab-lessons", !games], ["tab-games", games]]) {
    const tab = document.getElementById(id)
    tab.classList.toggle("active", on)
    tab.setAttribute("aria-selected", String(on))
  }
  document.getElementById("lessons-side").classList.toggle("hidden", games)
  document.getElementById("games-side").classList.toggle("hidden", !games)
  document.querySelector(".lesson-pane:not(.analysis-pane)").classList.toggle("hidden", games)
  document.getElementById("analysis-pane").classList.toggle("hidden", !games)
  if (games) {
    await gameAnalysis.enter()
  } else {
    board.disableMoveInput()
    if (lessonBoard) {
      await showPosition(lessonBoard.fen, {orientation: lessonBoard.orientation})
      setStatus(lessonBoard.status)
    }
    const step = state.step
    if (step && step.type === "exercise" && !step.accepted) {
      state.chess = new Chess(step.board.fen)
      await showPosition(step.board.fen, {orientation: step.side === "black" ? COLOR.black : COLOR.white})
      board.enableMoveInput(moveInputHandler, step.side === "black" ? COLOR.black : COLOR.white)
    }
  }
}

// ---------- wire up ----------

document.getElementById("tab-lessons").addEventListener("click", () => switchView("lessons"))
document.getElementById("tab-games").addEventListener("click", () => switchView("games"))

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
const coach = setupCoach({api, narrator, requestPlan, addMsg, messagesEl, escapeHtml,
  // the username from onboarding: one click from "thanks" to the learner's games being analyzed
  analyzeGames: async username => { await switchView("games"); await gameAnalysis.fetchFor(username) }})
coach.init({
  readCurrent() {
    const last = state.view === "games" ? gameAnalysis.readable()
      : [...messagesEl.querySelectorAll(".msg.assistant, .msg.system")].pop()
    if (last) narrator.speak(last)
  },
}).then(() => { if (!state.sessionId) coach.offerOnboarding() })
loadHealth()
loadCourses()
