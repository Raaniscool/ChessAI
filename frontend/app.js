// AI Chess Tutor — frontend
// Board renders server-validated state only; every learner move is checked
// locally (UX) and authoritatively on the server (rules).

import {Chessboard, COLOR, INPUT_EVENT_TYPE, FEN, BORDER_TYPE} from "./vendor/cm-chessboard/src/Chessboard.js"
import {Markers, MARKER_TYPE} from "./vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Chess} from "./vendor/chess.mjs/Chess.js"
import {CheckHighlight} from "./check-highlight.js"
import {annotateLines} from "./lines.js"
import {Narrator, decorateMoves, targetSquare} from "./speech.js"
import {setupGameAnalysis} from "./analysis.js"
import {errorMessage, readEvents, reach} from "./net.js"
import {OTHER, clarifyBody, debugEnabled, debugLines, optionLabel, puzzleReasons, understoodLine, verificationBadge} from "./plan-view.js"
import {setupCoach} from "./coach.js"
import {setupPuzzles} from "./puzzles.js"
import {MoveHistory, createBoardNav, navKey} from "./board-nav.js"
import {CalcArrows} from "./calc-arrows.js"
import {createBoardSounds} from "./sounds.js"
import {isContinueRequest, isRestartRequest} from "./lesson-progression.js"
import {parsePosition} from "./position.js"
import {BoardStateError, VerifiedBoard, computePositionAfterMove} from "./board-state.js"

// ---------- stale page guard ----------
// A browser can combine a cached old index.html with a newer app.js. Instead of
// failing with "Cannot read properties of null", say what to do.
const REQUIRED_IDS = ["board", "board-status", "messages", "feedback-slot", "lesson-title", "step-indicator",
  "course-list", "health", "chat-input", "btn-chat", "btn-continue", "btn-hint", "btn-reveal", "btn-play",
  "btn-explain-example", "speech-controls", "btn-read-aloud", "speech-rate", "speech-voice", "btn-stop-speech",
  "btn-coach", "btn-settings", "coach-panel", "settings-panel",
  "welcome", "tab-lessons", "tab-games", "lessons-side", "games-side", "analysis-pane", "ga-games", "ga-pgn",
  "ga-username", "ga-fetch-btn", "ga-paste", "ga-import-btn", "ga-status", "ga-import", "ga-overview", "ga-review", "ga-title", "ga-counter",
  "ga-back", "ga-prev", "ga-next", "ga-body",
  "tab-puzzles", "puzzles-side", "puzzles-pane", "pz-set", "pz-title", "pz-counter", "pz-dashboard", "pz-solver",
  "pz-mode-personal", "pz-mode-practice", "pz-personal", "pz-practice", "pz-status", "pz-info", "pz-feedback",
  "pz-explain", "pz-hint", "pz-solution", "pz-retry", "pz-next", "pz-back",
  "board-nav", "nav-prev", "nav-next", "nav-label", "nav-back"]
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

const ACTIVE_PLAN_KEY = "chessai.activePlanId"
const ACTIVE_LESSON_KEY = "chessai.activeLessonId"
function readStoredId(key) {
  try { return globalThis.localStorage.getItem(key) || null } catch (_) { return null }
}
function writeStoredId(key, id) {
  try {
    if (id) globalThis.localStorage.setItem(key, id)
    else globalThis.localStorage.removeItem(key)
  } catch (_) { /* storage is optional; the server still owns lesson progress */ }
}

const state = {
  sessionId: null,
  sessionPlanId: null,
  sessionCompleted: false,
  learningState: null,        // structured topic/mode/progress; board truth stays in the backend
  coachHistory: [],           // pre-lesson conversation only; active lessons use server transcript
  parkedLessons: [],          // opaque session references so topic switches can be resumed safely
  activeLessonId: readStoredId(ACTIVE_LESSON_KEY),
  activePlanId: readStoredId(ACTIVE_PLAN_KEY),
  step: null,
  chess: new Chess(),      // local mirror; server stays authoritative
  busy: false,
  demoPlaying: false,
  stream: null,            // AbortController of the AI text currently streaming
  view: "lessons",         // "lessons" | "games" | "puzzles"
}

function setActiveLessonId(id) {
  state.activeLessonId = id || null
  writeStoredId(ACTIVE_LESSON_KEY, state.activeLessonId)
}

function setActivePlanId(id) {
  const next = id || null
  if (next !== state.activePlanId) setActiveLessonId(null)
  state.activePlanId = next
  writeStoredId(ACTIVE_PLAN_KEY, next)
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

function reportCoachLatency(stage, durationMs, details = {}) {
  if (!debugEnabled()) return
  console.info(`[ChessAI latency] ${stage}: ${durationMs.toFixed(1)} ms`, details)
}

function reportCoachPaint(stage, startedAt, details = {}) {
  if (!debugEnabled()) return
  const report = () => reportCoachLatency(stage, performance.now() - startedAt, details)
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(report)
  else report()
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

let nav = null  // the ← → controls (below), created once the board exists
const board = new Chessboard(document.getElementById("board"), {
  position: FEN.start,
  style: {cssClass: "green", borderType: BORDER_TYPE.frame, showCoordinates: true},
  assetsUrl: "./vendor/cm-chessboard/assets/",
  // right-drag arrows for calculation: off while the AI is moving pieces by itself
  // a king in check glows red (check-highlight.js), whatever put the position on the board
  extensions: [{class: Markers, props: {}}, {class: CheckHighlight, props: {}},
    {class: CalcArrows, props: {isDisabled: () => Boolean(nav && nav.isLocked())}}],
})

// Move / capture / check sounds (sounds.js): played once where a move is made, never on redraws.
const sounds = createBoardSounds()

const HIGHLIGHT_MARKERS = {
  green: MARKER_TYPE.square,
  red: MARKER_TYPE.circleDanger,
  yellow: MARKER_TYPE.circle,
  blue: MARKER_TYPE.circlePrimary,
  purple: MARKER_TYPE.circlePrimary,
  orange: MARKER_TYPE.circle,
  grey: MARKER_TYPE.frame,
}

const verifiedBoard = new VerifiedBoard(board, Chess, {fallbackFen: FEN.start})

function clearMarkers(expectedFen = null) {
  return verifiedBoard.clearMarkers(expectedFen)
}

function removeMarkers(marker, expectedFen = verifiedBoard.currentFen()) {
  return verifiedBoard.removeMarkers(marker, expectedFen).catch(err => {
    console.warn("A stale marker removal was skipped.", err)
    return false
  })
}

function addMarkers(markers, expectedFen = verifiedBoard.currentFen(), label = "Board highlight") {
  return verifiedBoard.addMarkers(markers, expectedFen, label).catch(err => {
    console.warn("A stale or unsupported board highlight was skipped.", err)
    if (state.view === "lessons") setStatus("I skipped a highlight because the board position could not be confirmed.")
    return false
  })
}

function addMarker(marker, square, expectedFen = verifiedBoard.currentFen(), requiredPiece = null) {
  const operation = requiredPiece
    ? verifiedBoard.addPieceMarker(marker, square, requiredPiece, expectedFen)
    : verifiedBoard.addMarkers([{marker, square}], expectedFen)
  return operation.catch(err => {
    console.warn("A stale or unsupported piece highlight was skipped.", err)
    if (state.view === "lessons") setStatus("I skipped a highlight because the board position could not be confirmed.")
    return false
  })
}

function verifiedMove(fen, uci, options = {}) {
  return verifiedBoard.move(fen, uci, options)
}

function isPositionVerified(fen) {
  return verifiedBoard.matches(fen)
}

function showPosition(fen, {orientation = COLOR.white, highlights = [], animated = false} = {}) {
  // Every setup is an atomic, serialized FEN write followed by model + rendered-piece verification.
  const markers = highlights.map(h => ({marker: HIGHLIGHT_MARKERS[h.color] || MARKER_TYPE.square,
    square: h.square, piece: h.piece || null}))
  return verifiedBoard.setPosition(fen, {orientation, highlights: markers, animated, label: "Board position"})
}

function positionKey(fen) {
  return parsePosition(Chess, fen, "Move-line position").fen().split(" ").slice(0, 4).join(" ")
}

function uciBetweenPositions(beforeFen, afterFen) {
  const before = parsePosition(Chess, beforeFen, "Move-line starting position")
  const target = positionKey(afterFen)
  for (const candidate of before.moves({verbose: true})) {
    const trial = parsePosition(Chess, beforeFen, "Move-line starting position")
    const move = trial.move({from: candidate.from, to: candidate.to, promotion: candidate.promotion || "q"})
    if (move && positionKey(trial.fen()) === target) return `${move.from}${move.to}${move.promotion || ""}`
  }
  return null
}

// ---------- move history: ← → under the board ----------
// Each view keeps a MoveHistory of what it put on the board (board-nav.js); the buttons and the
// arrow keys step through the one of the view being shown. Locked while the AI moves pieces.

const LASTMOVE_MARKER = {class: "marker-lastmove", slice: "markerSquare"}

async function showNode(node, _history = null, orientation = board.getOrientation()) {
  await showPosition(node.fen, {orientation})
  if (node.uci) {
    await addMarkers([{marker: LASTMOVE_MARKER, square: node.uci.slice(0, 2)},
      {marker: LASTMOVE_MARKER, square: node.uci.slice(2, 4)}], node.fen, "Move-history highlight")
  }
}

nav = createBoardNav({
  prev: document.getElementById("nav-prev"), next: document.getElementById("nav-next"),
  label: document.getElementById("nav-label"), back: document.getElementById("nav-back"),
  root: document.getElementById("board-nav"), show: showNode, getView: () => state.view,
})

document.addEventListener("keydown", ev => {
  const step = navKey(ev)
  if (!step || document.querySelector("dialog[open]")) return
  ev.preventDefault()
  nav.go(step)
})

// Lessons: the current step's sequence (a demonstration's moves, an exercise and its answer).
const lessonHistory = new MoveHistory(FEN.start)
let reviewStatus = null  // the status line to put back when the learner returns to the latest position

async function lessonOnView(h) {
  const step = state.step
  if (!step) return
  if (!h.atLatest) {
    if (reviewStatus === null) reviewStatus = document.getElementById("board-status").textContent
    setStatus("Reviewing earlier moves — → steps forward again.")
    return
  }
  if (reviewStatus !== null) setStatus(reviewStatus)
  reviewStatus = null
  const highlights = step.type === "teach" ? (step.board || {}).highlights
    : step.type === "demonstrate" ? (step.board_after || {}).highlights : null
  if (highlights) await addMarkers(highlights.map(h => ({marker: HIGHLIGHT_MARKERS[h.color] || MARKER_TYPE.square,
    square: h.square, piece: h.piece || null})), h.current.fen, "Lesson step highlight")
}

function resetLessonHistory(fen) {
  lessonHistory.reset(fen)
  reviewStatus = null
  nav.set("lessons", lessonHistory, {onView: lessonOnView})
}
resetLessonHistory(FEN.start)

// ---------- move input (exercise) ----------

function exerciseLocked() {
  const step = state.step
  if (!step || step.type !== "exercise" || state.busy || step.accepted) return true
  return false
}

// Shared legal-move gate for both graded exercises and free exploration.
function legalInputHandler(getChess, onMove, onMoveError = () => {}) {
  let pending = null
  return event => {
    const chess = getChess()
    if (!chess) return false
    switch (event.type) {
      case INPUT_EVENT_TYPE.moveInputStarted: {
        const piece = chess.get(event.squareFrom)
        const beforeFen = chess.fen()
        if (!piece || piece.color !== chess.turn() || !verifiedBoard.matches(beforeFen) ||
            !verifiedBoard.isPieceAt(event.squareFrom, piece, beforeFen) ||
            normalizeBoardPiece(event.piece) !== `${piece.color}${piece.type}`) return false
        const moves = chess.moves({square: event.squareFrom, verbose: true})
        if (!moves.length) return false
        pending = {beforeFen, from: event.squareFrom, piece: {color: piece.color, type: piece.type}}
        board.addLegalMovesMarkers(moves)
        return true
      }
      case INPUT_EVENT_TYPE.validateMoveInput: {
        if (!pending || pending.from !== event.squareFrom || chess.fen() !== pending.beforeFen ||
            normalizeBoardPiece(board.getPiece(event.squareFrom)) !== `${pending.piece.color}${pending.piece.type}`) return false
        const moves = chess.moves({square: event.squareFrom, verbose: true})
        return moves.some(m => m.to === event.squareTo)
      }
      case INPUT_EVENT_TYPE.moveInputFinished: {
        board.removeLegalMovesMarkers()
        const moveInfo = pending
        pending = null
        if (!moveInfo || moveInfo.from !== event.squareFrom) return false
        const legal = chess.moves({square: event.squareFrom, verbose: true})
          .find(m => m.to === event.squareTo)
        if (!legal) return false
        const uci = `${event.squareFrom}${event.squareTo}${legal.promotion || ""}`
        board.disableMoveInput()
        state.busy = true // also guards tab switches while cm-chessboard completes this drag
        verifiedBoard.acknowledgeExternalMove(moveInfo.beforeFen, uci, {
          expectedPiece: moveInfo.piece, label: "Learner move",
        }).then(transition => {
          state.busy = false
          return onMove(event.squareFrom, event.squareTo, {...moveInfo, transition})
        }).catch(err => {
          state.busy = false
          setStatus(`I couldn't confirm that move on the board, so it was not sent or graded. ${err.message}`)
          onMoveError(err, moveInfo)
        })
        return true
      }
      case INPUT_EVENT_TYPE.moveInputCanceled: {
        pending = null
        board.removeLegalMovesMarkers()
        return false
      }
      default:
        return false
    }
  }
}

function normalizeBoardPiece(piece) {
  if (typeof piece === "string") return piece.toLowerCase()
  if (piece && piece.color && piece.type) return `${piece.color}${piece.type}`.toLowerCase()
  return null
}

const exerciseInput = legalInputHandler(
  () => (exerciseLocked() ? null : state.chess),
  (from, to, receipt) => handleUserMove(from, to, receipt),
  () => {
    if (state.step && state.step.type === "exercise" && !state.step.accepted && state.view === "lessons") {
      board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
    }
  },
)

// ---------- board locked outside exercises ----------
// Pieces can only be moved in an exercise. Anywhere else a click on the board explains
// what to do instead of silently doing nothing.

function boardAcceptsMoves() {
  return state.step && state.step.type === "exercise" && !state.step.accepted && !state.busy
}

document.getElementById("board").addEventListener("pointerdown", ev => {
  // only the lesson view nudges; the other tabs own the board while they are shown.
  // A right click draws calculation arrows: no nudge for that.
  if (ev.button > 0 || state.view !== "lessons" || !state.step || boardAcceptsMoves()) return
  if (!lessonHistory.atLatest && !state.demoPlaying) {
    setStatus("You're looking at an earlier position — press → to step forward again.")
    return
  }
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

function enableLessonMoveInput() {
  if (state.step && state.step.type === "exercise" && !state.step.accepted && state.view === "lessons" && !state.busy) {
    board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
  }
}

async function handleUserMove(from, to, receipt = null) {
  if (!state.chess || !receipt || receipt.beforeFen !== state.chess.fen()) {
    setStatus("I couldn't verify the board before that move, so it was not sent or graded.")
    enableLessonMoveInput()
    return
  }
  const fenBefore = state.chess.fen()
  const uci = `${from}${to}${receipt.transition && receipt.transition.move.promotion || ""}`
  let transition
  try {
    transition = computePositionAfterMove(Chess, fenBefore, uci, {expectedPiece: receipt.piece, label: "Lesson move"})
    if (transition.afterFen !== receipt.transition.afterFen || !verifiedBoard.matches(transition.afterFen)) {
      throw new BoardStateError("The board did not match the verified result of the learner's move.")
    }
  } catch (err) {
    state.chess = parsePosition(Chess, fenBefore, "Lesson position before the move")
    await showPosition(fenBefore, {orientation: state.step.side === "black" ? COLOR.black : COLOR.white})
    setStatus(`I couldn't verify that move, so I restored the lesson position. ${err.message}`)
    enableLessonMoveInput()
    return
  }

  state.chess = parsePosition(Chess, transition.afterFen, "Lesson move result")
  sounds.playMove(transition.move, transition.afterFen)
  stopStream()
  narrator.stop()

  state.busy = true
  setStatus("Analyzing your move…")
  try {
    const result = await api(`/api/sessions/${state.sessionId}/move`, "POST", {uci, expected_fen: fenBefore})
    const authoritativeFen = parsePosition(Chess,
      result.fen || (result.accepted ? transition.afterFen : result.reset_fen || fenBefore),
      "Server lesson position").fen()
    await showPosition(authoritativeFen, {orientation: state.step.side === "black" ? COLOR.black : COLOR.white})
    if (!verifiedBoard.matches(result.accepted ? transition.afterFen : fenBefore)) {
      // The server owns grading/state. Resync the board, but do not speak feedback for a different
      // position or unlock another lesson step until a learner can see the mismatch was handled.
      state.chess = parsePosition(Chess, authoritativeFen, "Server lesson position")
      if (result.accepted) state.step.accepted = true
      setStatus("The lesson service returned a different position. I synchronized the board and withheld the move explanation.")
      if (result.accepted) {
        hideBtn("btn-hint"); hideBtn("btn-reveal")
        showContinue()
      } else {
        setStatus(result.reset_fen ? retryText(result.tries || 1) : "The position was synchronized; you can retry the move.")
        board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
      }
      return
    }
    state.chess = parsePosition(Chess, authoritativeFen, "Server lesson position")
    renderFeedback(result) // only after the board has been confirmed against the server's FEN
    if (result.accepted) {
      state.step.accepted = true
      hideBtn("btn-hint"); hideBtn("btn-reveal")
      setStatus("")
      lessonHistory.append(authoritativeFen, uci, transition.move.san)
      nav.refresh()
      showContinue()  // only once the verified board has settled
    } else {
      setStatus(retryText(result.tries || 1))
      if (result.help) renderHelpOffer(result.help)
      board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
    }
  } catch (err) {
    // A timeout can happen after the server accepted the move. Read its position before restoring;
    // never assume the local pre-move position is still authoritative.
    try {
      const live = await api(`/api/sessions/${state.sessionId}`)
      const safeFen = parsePosition(Chess, live.board_fen || fenBefore, "Recovered lesson position").fen()
      await showPosition(safeFen, {orientation: state.step.side === "black" ? COLOR.black : COLOR.white})
      state.chess = parsePosition(Chess, safeFen, "Recovered lesson position")
      if (live.step && state.step) state.step.accepted = Boolean(live.step.accepted)
      if (state.step && state.step.accepted) {
        hideBtn("btn-hint"); hideBtn("btn-reveal"); showContinue()
      } else if (state.view === "lessons" && state.step && state.step.type === "exercise") {
        board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
      }
    } catch (_) { /* keep the last verified board when the server cannot be reached */ }
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

// A SAN move is usable as a highlight only when it is legal in the named position.
function legalSquares(fen, san) {
  try {
    const chess = parsePosition(Chess, fen, "Move position")
    const move = chess.move(san.replace(/[+#]$/, ""))
    if (!move) return null
    return {from: move.from, to: move.to, piece: {color: move.color, type: move.piece}}
  } catch (_) {
    return null
  }
}

// A spoken SAN move is highlighted only if it is legal in a position currently matching the
// actual board. A bare square is highlighted only when an actual piece occupies it.
function squaresFor(mark, element) {
  if (!mark) return {squares: [], pieces: {}, fen: null}
  const currentFen = verifiedBoard.currentFen()
  if (!currentFen) return {squares: [], pieces: {}, fen: null}
  const candidates = [currentFen]
  const holder = element && element.closest ? element.closest("[data-fens]") : null
  if (holder) {
    try {
      const fens = JSON.parse(holder.dataset.fens)
      for (const fen of fens) {
        if (fen && verifiedBoard.matches(fen)) candidates.push(fen)
      }
    } catch (_) { /* no trusted position metadata */ }
  }
  for (const fen of candidates) {
    const legal = mark.san && legalSquares(fen, mark.san)
    if (legal) {
      const actualPiece = verifiedBoard.pieceAt(legal.from)
      if (actualPiece !== `${legal.piece.color}${legal.piece.type}`) continue
      return {squares: [legal.from, legal.to], pieces: {[legal.from]: legal.piece}, fen: currentFen}
    }
  }
  // Do not point at a guessed target when SAN cannot be applied to a verified position.
  if (mark.san) return {squares: [], pieces: {}, fen: currentFen}
  const actualPiece = mark.square && verifiedBoard.pieceAt(mark.square)
  return actualPiece
    ? {squares: [mark.square], pieces: {[mark.square]: actualPiece}, fen: currentFen}
    : {squares: [], pieces: {}, fen: currentFen}
}

// ---------- lines of moves: the pieces move ----------
// In a game review, a line of moves ("the line goes Nc7+ Kd8 Nxa7") is shown by playing it
// on the board — while it's read aloud, when pointed at, or all at once on a click. Only
// single squares are highlighted. The board goes back to where it was afterwards.
const lineView = {restore: null, restoreOrientation: null, restoreInFlight: null, timer: null, token: 0, run: 0}

const placementOf = fen => (fen || "").split(" ")[0]

async function showLineMove(span) {
  if (!board || !span.chessLine) return
  const {positions, index} = span.chessLine
  clearTimeout(lineView.timer)
  const token = ++lineView.token
  if (lineView.restore === null) {
    lineView.restore = verifiedBoard.currentFen()
    lineView.restoreOrientation = board.getOrientation ? board.getOrientation() : COLOR.white
  }
  if (!lineView.restore) {
    setStatus("I couldn't verify the current board, so I skipped that move preview.")
    return
  }
  nav.lock("line-preview", "*")  // a move is shown only after its source and result are verified
  try {
    await removeMarkers(SPEECH_MARKER)
    await removeMarkers(HOVER_MARKER)
    const beforeFen = parsePosition(Chess, positions[index], "Move preview starting position").fen()
    const afterFen = parsePosition(Chess, positions[index + 1], "Move preview result").fen()
    if (!verifiedBoard.matches(beforeFen)) await showPosition(beforeFen, {animated: false})
    if (token !== lineView.token) return
    const uci = uciBetweenPositions(beforeFen, afterFen)
    if (!uci) throw new BoardStateError("The move could not be validated between the two line positions.")
    const piece = parsePosition(Chess, beforeFen, "Move preview starting position").get(uci.slice(0, 2))
    if (!piece) throw new BoardStateError(`The move's source piece is missing from ${uci.slice(0, 2)}.`)
    const transition = await verifiedMove(beforeFen, uci, {
      expectedPiece: {color: piece.color, type: piece.type}, label: "Move preview",
    })
    if (token !== lineView.token) return
    if (!verifiedBoard.matches(afterFen)) throw new BoardStateError("The preview move did not match its expected result.")
  } catch (err) {
    if (token === lineView.token) {
      setStatus(`I couldn't verify that move preview. ${err.message}`)
      endLineView(0)
    }
  }
}

function endLineView(delay) {
  if (lineView.restore === null) return
  clearTimeout(lineView.timer)
  lineView.timer = setTimeout(async () => {
    const fen = lineView.restore
    const orientation = lineView.restoreOrientation || COLOR.white
    lineView.restore = null
    lineView.restoreOrientation = null
    const token = ++lineView.token
    const restoring = {fen, orientation, token}
    lineView.restoreInFlight = restoring
    try {
      if (board && fen) await showPosition(fen, {orientation, animated: true})
    } catch (err) {
      if (token === lineView.token) setStatus(`I couldn't restore the board after that preview. ${err.message}`)
    } finally {
      if (lineView.restoreInFlight === restoring) lineView.restoreInFlight = null
      if (token === lineView.token) nav.unlock("line-preview", "*")
    }
  }, delay)
}

async function playWholeLine(span) {
  if (!board || !span.chessLine) return
  const {positions} = span.chessLine
  clearTimeout(lineView.timer)
  lineView.restore = null  // asked for: the line's final verified position stays on the board
  lineView.restoreOrientation = null
  lineView.restoreInFlight = null
  const token = ++lineView.token
  const run = ++lineView.run
  const view = state.view
  nav.unlock("line-preview", "*")
  nav.lock("line", "*")  // the line is playing by itself: ← → wait until it's done
  try {
    const firstFen = parsePosition(Chess, positions[0], "Move line starting position").fen()
    await showPosition(firstFen, {animated: false})
    let currentFen = firstFen
    for (let i = 1; i < positions.length; i++) {
      await new Promise(r => setTimeout(r, 600)) // visual pacing only; each move is explicitly verified
      if (token !== lineView.token) return
      const nextFen = parsePosition(Chess, positions[i], "Move line position").fen()
      const uci = uciBetweenPositions(currentFen, nextFen)
      if (!uci) throw new BoardStateError("The move line contains a position that does not follow legally.")
      const piece = parsePosition(Chess, currentFen, "Move line position").get(uci.slice(0, 2))
      if (!piece) throw new BoardStateError(`The line's source piece is missing from ${uci.slice(0, 2)}.`)
      await verifiedMove(currentFen, uci, {
        expectedPiece: {color: piece.color, type: piece.type}, label: "Tutor move line",
      })
      if (token !== lineView.token) return
      if (!verifiedBoard.matches(nextFen)) throw new BoardStateError("A move did not reach its expected line position.")
      currentFen = nextFen
    }
    // then it can be stepped through; "↩ Back" returns to the view's own position
    if (state.view === view) nav.overlay(view, MoveHistory.fromPositions(positions, Chess))
  } catch (err) {
    if (token === lineView.token) setStatus(`I couldn't verify that move line on the board: ${err.message}`)
  } finally {
    if (run === lineView.run) nav.unlock("line", "*")
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
    const fen = verifiedBoard.currentFen()
    removeMarkers(SPEECH_MARKER, fen)
    if (!mark) { endLineView(1200); return }  // finished reading: restore the verified position
    const result = squaresFor(mark, element)
    addMarkers(result.squares.map(square => ({marker: SPEECH_MARKER, square, piece: result.pieces[square] || null})),
      result.fen, "Speech highlight")
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
  removeMarkers(HOVER_MARKER)
  if (mv && mv.chessLine) { showLineMove(mv); return }
  if (!narrator.current) endLineView(700)  // pointer left the line: put the position back
  if (!mv) return
  const san = mv.dataset.san
  const result = squaresFor({san, square: targetSquare(san)}, mv)
  addMarkers(result.squares.map(square => ({marker: HOVER_MARKER, square, piece: result.pieces[square] || null})),
    result.fen, "Move hover highlight")
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
  if (result.teacher === "library") {
    // verified library text, shown at once; the AI teacher only when asked for more
    card.querySelector(".teacher-label").textContent = "✓ From the verified library"
    if (result.deeper) {
      const btn = document.getElementById("btn-explain-example")
      btn.textContent = "🧠 Explain deeper"
      btn.title = "Ask the AI teacher for a deeper explanation of this position"
      showBtn("btn-explain-example")
    }
  }
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

let stepRenderId = 0
function lessonStepFen(step) {
  const rawFen = step.type === "demonstrate" ? step.start_fen : step.board && step.board.fen
  return parsePosition(Chess, rawFen, "Lesson position").fen()
}

function stepSetupOptions(step) {
  const orientation = step.type === "exercise" && step.side === "black" ? COLOR.black : COLOR.white
  return {
    orientation,
    highlights: step.type === "teach" ? (step.board && step.board.highlights || []) : [],
    animated: false,
  }
}

async function renderStep(step, {positionVerified = false} = {}) {
  const renderId = ++stepRenderId
  const stepFen = lessonStepFen(step)
  const stepPosition = parsePosition(Chess, stepFen, "Lesson position")
  const setup = stepSetupOptions(step)
  try {
    if (positionVerified) {
      if (!verifiedBoard.matches(stepFen)) throw new BoardStateError("The prepared lesson position is no longer on the board.")
    } else {
      await showPosition(stepFen, setup)
    }
  } catch (err) {
    setStatus(`I couldn't verify this lesson position, so I stopped before showing its explanation. ${err.message}`)
    throw err
  }
  if (renderId !== stepRenderId) return false

  stopStream()
  narrator.stop()
  state.busy = false
  state.step = step
  state.chess = stepPosition
  hideControls()
  document.getElementById("feedback-slot").innerHTML = ""
  document.getElementById("lesson-title").textContent = step.lesson_title
  document.getElementById("step-indicator").textContent = `Step ${step.index + 1} / ${step.total_steps}`
  board.disableMoveInput()
  resetLessonHistory(stepFen)
  if (step.coach_note) showCoachNote(step.coach_note)

  if (step.type === "teach") {
    const msg = addMsg(step.text)
    showContinue()
    if (step.example && step.example.explainable) {
      const btn = document.getElementById("btn-explain-example")
      btn.textContent = "🧠 Explain this example"
      btn.title = "The AI teacher explains this verified example"
      showBtn("btn-explain-example")
    }
    setStatus("")
    narrator.auto(msg, "lessons")
  } else if (step.type === "demonstrate") {
    const msg = addMsg(step.text)
    setStatus("Watch the demonstration…")
    // Plays by itself (read aloud first when that's on); the learner can watch it again.
    await narrator.auto(msg, "lessons")
    if (state.step === step) await playDemonstration()
  } else if (step.type === "exercise") {
    const side = setup.orientation
    const msg = addMsg(`🎯 **Exercise:** ${escapeHtml(step.prompt)}`.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"), "assistant", true)
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
  return true
}

async function playDemonstration() {
  if (state.demoPlaying || !state.step || state.step.type !== "demonstrate") return
  state.demoPlaying = true
  nav.lock("demo", "lessons")  // the AI is moving the pieces: no ← → and no arrows until it's done
  board.disableMoveInput()
  hideBtn("btn-play"); hideBtn("btn-continue")
  const step = state.step
  let demoChess = parsePosition(Chess, step.start_fen, "Demonstration position")
  let completed = false
  try {
    await showPosition(demoChess.fen(), {animated: false})
    if (state.step !== step || state.view !== "lessons") return
    resetLessonHistory(demoChess.fen())
    board.clearCalculation()
    setStatus("Watch the demonstration…")
    for (let i = 0; i < step.moves.length; i++) {
      if (state.step !== step || state.view !== "lessons") return  // learner moved on
      const uci = step.moves[i]
      const piece = demoChess.get(uci.slice(0, 2))
      if (!piece) throw new BoardStateError(`The demonstrated piece is missing from ${uci.slice(0, 2)}.`)
      const transition = await verifiedBoard.move(demoChess.fen(), uci, {
        expectedPiece: {color: piece.color, type: piece.type}, animated: true, label: "Lesson demonstration",
      })
      demoChess = parsePosition(Chess, transition.afterFen, "Demonstration result")
      lessonHistory.append(transition.afterFen, uci, transition.move.san)
      nav.refresh()
      sounds.playMove(transition.move, transition.afterFen)
      const comment = step.comments && step.comments[i]
      if (comment) {
        const msg = addMsg(comment, "system")
        // Read aloud only after the moved piece has been verified at its destination.
        if (narrator.enabled && narrator.prefs.read.lessons !== false) await narrator.speak(msg)
        else await sleep(900)
      } else {
        await sleep(620)
      }
    }
    const after = step.board_after || {}
    if (step.final_fen && !verifiedBoard.matches(step.final_fen)) {
      throw new BoardStateError("The demonstrated final position disagrees with the verified move sequence.")
    }
    const markers = (after.highlights || []).map(h => ({
      marker: HIGHLIGHT_MARKERS[h.color] || MARKER_TYPE.square, square: h.square, piece: h.piece || null,
    }))
    if (!await addMarkers(markers, demoChess.fen(), "Demonstration highlight")) {
      throw new BoardStateError("The demonstration highlights could not be verified.")
    }
    completed = true
    setStatus(step.next_type === "exercise" ? "Now it's your turn." : "")
  } catch (err) {
    setStatus(`I couldn't safely complete that demonstration, so I stopped before continuing. ${err.message}`)
  } finally {
    state.demoPlaying = false
    nav.unlock("demo", "lessons")
    if (state.step === step) {
      showBtn("btn-play")
      if (completed) showContinue()
      else hideBtn("btn-continue")
    }
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// ---------- navigation ----------

async function advanceLesson() {
  if (state.busy || !state.sessionId) return null
  state.busy = true
  board.disableMoveInput()
  let result = null
  try {
    const prepared = await api(`/api/sessions/${state.sessionId}/advance/prepare`, "POST")
    if (!verifiedBoard.matches(prepared.source_fen)) {
      await showPosition(prepared.source_fen, {orientation: state.step && state.step.side === "black" ? COLOR.black : COLOR.white})
      throw new BoardStateError("The current lesson position was out of sync. I restored the server position; review it before continuing.")
    }

    if (!prepared.completed) {
      if (!prepared.step) throw new BoardStateError("The lesson service returned no next position.")
      const nextFen = lessonStepFen(prepared.step)
      await showPosition(nextFen, stepSetupOptions(prepared.step))
      if (!verifiedBoard.matches(nextFen)) throw new BoardStateError("The next lesson position was not confirmed.")
    }

    // Progress is committed only after the next board setup was rendered and verified.
    const committed = await api(`/api/sessions/${state.sessionId}/advance/confirm`, "POST", {
      source_index: prepared.source_index, source_fen: prepared.source_fen,
    })
    result = committed
    if (prepared.completed) {
      if (!committed.completed) throw new BoardStateError("The lesson completion was not confirmed.")
      state.sessionCompleted = true
      hideControls()
      board.disableMoveInput()
      renderCompletion(committed)
      setStatus("Lesson complete!")
      loadCourses()
      coach.refresh()
    } else {
      if (committed.completed || !committed.step) throw new BoardStateError("The lesson service returned an unexpected step.")
      const committedFen = lessonStepFen(committed.step)
      if (!verifiedBoard.matches(committedFen)) {
        await showPosition(committedFen, stepSetupOptions(committed.step))
      }
      if (!verifiedBoard.matches(committedFen)) throw new BoardStateError("The confirmed lesson position is not on the board.")
      await renderStep(committed.step, {positionVerified: true})
    }
  } catch (err) {
    renderError(err.message)
    // A failed render must not leave the browser on an unconfirmed next step. Query the session
    // and restore its authoritative position; only render a different step after verifying it.
    try {
      const live = await api(`/api/sessions/${state.sessionId}`)
      if (live.status === "completed") {
        state.sessionCompleted = true
        hideControls()
        setStatus("The lesson service saved completion, but its confirmation could not be loaded. Reload the lesson to continue.")
      } else if (live.step) {
        const liveFen = parsePosition(Chess, live.board_fen || lessonStepFen(live.step), "Recovered lesson position").fen()
        await showPosition(liveFen, stepSetupOptions(live.step))
        if (!verifiedBoard.matches(liveFen)) throw new BoardStateError("The recovered lesson position could not be verified.")
        if (!state.step || live.step.index !== state.step.index) {
          await renderStep(live.step, {positionVerified: true})
        } else {
          state.step.accepted = Boolean(live.step.accepted)
          if (state.step.accepted) showContinue()
          else if (state.step.type === "exercise") {
            board.enableMoveInput(moveInputHandler, state.step.side === "black" ? COLOR.black : COLOR.white)
          }
        }
      }
    } catch (syncError) {
      setStatus(`I couldn't synchronize the lesson position. Reload before continuing. ${syncError.message}`)
    }
  } finally {
    state.busy = false
  }
  return result
}

// The end of a lesson: how it went, in plain words, and where to go next.
function renderCompletion(res) {
  if (res.status === "INVALID_LESSON") {  // the server refused to count a lesson that didn't match the request
    const div = addMsg(`⚠ ${res.message || "This lesson didn't match your request, so it isn't counted."}`)
    div.classList.add("lesson-summary", "lesson-invalid")
    return
  }
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

async function startLesson(lessonId, planId = null) {
  if (state.busy) return
  state.busy = true
  board.disableMoveInput()
  try {
    const initialHistory = state.coachHistory.slice(-8)
    const res = await api(`/api/lessons/${lessonId}/start`, "POST",
      initialHistory.length ? {history: initialHistory} : undefined)
    state.sessionId = res.session_id
    state.sessionPlanId = res.plan_id ?? planId
    state.sessionCompleted = false
    state.step = null
    state.learningState = res.learning_state || null
    state.coachHistory = []
    setActivePlanId(state.sessionPlanId)
    setActiveLessonId(res.lesson_id || lessonId)
    state.lastNote = null
    await renderStep(res.step)
  } catch (err) {
    renderError(err.message)
    setStatus(`I couldn't verify the lesson setup, so I stopped before its first explanation. ${err.message}`)
  } finally {
    state.busy = false
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
  const step = state.step
  if (!step || step.type !== "exercise" || state.busy) return
  state.busy = true
  board.disableMoveInput()
  hideBtn("btn-hint"); hideBtn("btn-reveal")
  const beforeFen = parsePosition(Chess, step.board.fen, "Lesson solution position").fen()
  try {
    const res = await api(`/api/sessions/${state.sessionId}/reveal/prepare`, "POST")
    if (state.step !== step) return
    // Set up and verify the solution position before moving or asking the server to unlock it.
    await showPosition(beforeFen, {orientation: step.side === "black" ? COLOR.black : COLOR.white})
    if (!verifiedBoard.matches(beforeFen) || (res.fen && !verifiedBoard.matches(res.fen))) {
      throw new BoardStateError("The solution starting position was not confirmed.")
    }
    let transition = null
    if (res.uci) {
      const solutionPiece = parsePosition(Chess, beforeFen, "Lesson solution position").get(res.uci.slice(0, 2))
      if (!solutionPiece) throw new BoardStateError(`The solution's source piece is missing from ${res.uci.slice(0, 2)}.`)
      transition = await verifiedBoard.move(beforeFen, res.uci, {
        expectedPiece: {color: solutionPiece.color, type: solutionPiece.type},
        animated: true, label: "Lesson solution",
      })
      if (!verifiedBoard.matches(transition.afterFen)) {
        throw new BoardStateError("The solution move did not match the resulting board position.")
      }
      if (!await addMarkers([
        {marker: MARKER_TYPE.square, square: transition.from},
        {marker: MARKER_TYPE.square, square: transition.to},
      ], transition.afterFen, "Solution move highlight")) {
        throw new BoardStateError("The solution move was verified, but its board highlights were not.")
      }
    }
    const confirmed = await api(`/api/sessions/${state.sessionId}/reveal/confirm`, "POST", {expected_fen: beforeFen})
    if (!confirmed.accepted) throw new BoardStateError("The lesson service did not confirm the solution.")
    if (transition) {
      state.chess = parsePosition(Chess, transition.afterFen, "Lesson solution result")
      resetLessonHistory(beforeFen)
      lessonHistory.append(transition.afterFen, res.uci, transition.move.san)
      nav.refresh()
      sounds.playMove(transition.move, transition.afterFen)
    } else {
      state.chess = parsePosition(Chess, beforeFen, "Lesson solution position")
    }
    state.step.accepted = true
    narrator.auto(addMsg(`🔎 Solution: ${res.accepted_moves.join(" or ") || "see the engine's best move"}`, "system"), "hints")
    showContinue()
    setStatus("Solution shown — continue when ready.")
    if (confirmed.adapted && confirmed.adapted.note) showCoachNote(confirmed.adapted.note)
  } catch (err) {
    // The confirm response can time out after a successful server commit. Recover its authoritative
    // position/state before deciding whether the exercise is still active.
    let recovered = false
    try {
      const live = await api(`/api/sessions/${state.sessionId}`)
      const liveFen = parsePosition(Chess, live.board_fen || beforeFen, "Recovered lesson position").fen()
      await showPosition(liveFen, {orientation: step.side === "black" ? COLOR.black : COLOR.white})
      state.chess = parsePosition(Chess, liveFen, "Recovered lesson position")
      state.step.accepted = Boolean(live.step && live.step.accepted)
      if (state.step.accepted) showContinue()
      else {
        board.enableMoveInput(moveInputHandler, step.side === "black" ? COLOR.black : COLOR.white)
        showBtn("btn-hint"); showBtn("btn-reveal")
      }
      recovered = true
    } catch (_) { /* do not unlock the lesson when neither board nor session can be confirmed */ }
    setStatus(recovered
      ? `I couldn't confirm the solution, so I synchronized with the lesson service without showing an explanation. ${err.message}`
      : `I couldn't verify the lesson position. Reload the step before continuing. ${err.message}`)
    addMsg(`⚠ ${err.message}`, "system")
  } finally {
    state.busy = false
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

async function sendChat(forcedMessage = null) {
  const input = document.getElementById("chat-input")
  const message = (typeof forcedMessage === "string" ? forcedMessage : input.value).trim()
  if (!message) return
  if (typeof forcedMessage !== "string") input.value = ""
  await routeChatMessage(message)
}

async function routeChatMessage(message) {
  const turnStartedAt = performance.now()
  let route
  try {
    // Routing and the existing local answer classifier now share one request. The API checks
    // deterministic greetings/board routes first, then returns a verified quick answer or invokes
    // the existing semantic router—no frontend-side keyword router is introduced.
    const routeStartedAt = performance.now()
    route = await api("/api/coach/route", "POST", {
      message,
      session_id: state.sessionId,
      learning_state: state.learningState,
      history: state.coachHistory.slice(-8),
    })
    reportCoachLatency("route API", performance.now() - routeStartedAt,
      {action: route.action, source: route.source})
  } catch (err) {
    // Preserve the old local-answer escape hatch if the combined route endpoint is unavailable.
    if (await quickAnswer(message)) {
      reportCoachPaint("TTFO (local-answer fallback)", turnStartedAt, {source: "knowledge_answer"})
      reportCoachLatency("turn total", performance.now() - turnStartedAt, {source: "knowledge_answer"})
      return
    }
    // Offline fallback only for explicit, well-formed actions. In particular, a greeting or an
    // arbitrary first message is never converted into a lesson request.
    if (state.activePlanId && isRestartRequest(message)) {
      await continueActivePlan(message, {restart: true})
      return
    }
    if (state.activePlanId && isContinueRequest(message)) {
      await continueActivePlan(message)
      return
    }
    if (isLearnRequest(message)) {
      await requestPlan(message, null, {autoStart: true})
      return
    }
    if (state.sessionId) {
      await streamSessionChat(message, null)
      return
    }
    addMsg(message, "user")
    renderError(err.message)
    return
  }

  if (route.quick_answer) {
    renderQuickAnswer(message, route.quick_answer)
    reportCoachPaint("TTFO (verified local answer)", turnStartedAt, {source: route.source})
    reportCoachLatency("turn total", performance.now() - turnStartedAt, {source: route.source})
    return
  }

  const action = route.action
  if (action === "clarify") {
    addMsg(message, "user")
    renderCoachClarify(route.clarification)
    reportCoachPaint("TTFO (clarification)", turnStartedAt, {source: route.source})
    reportCoachLatency("turn total", performance.now() - turnStartedAt, {source: route.source})
    return
  }
  if (action === "start_lesson" || action === "topic_change") {
    await requestPlan(message, null, {autoStart: true, route, turnStartedAt})
    return
  }
  if (action === "resume_lesson") {
    addMsg(message, "user")
    const parked = findParkedLesson(route.topic)
    if (parked) await restoreParkedLesson(parked)
    else if (state.sessionId && (!route.topic || sameTopic(route.topic, state.learningState?.topic))) {
      addMsg(`We’re still on ${state.learningState?.topic || "your current lesson"}. I’ll keep that lesson ready.`)
    } else if (route.topic) {
      await requestPlan(`I want to learn ${route.topic}`, null, {autoStart: true, route, turnStartedAt})
    }
    return
  }
  if (action === "continue_lesson") {
    if (state.activePlanId) {
      await continueActivePlan(message, {restart: isRestartRequest(message)})
    } else if (state.sessionId) {
      addMsg(message, "user")
      await advanceLesson()
    } else {
      addMsg(message, "user")
      renderCoachClarify({question: "There isn’t an active lesson yet. What would you like to learn?", options: []})
    }
    return
  }
  if (action === "practice" || action === "puzzle" ||
      action === "mode_change" && ["practice", "puzzle"].includes(route.mode)) {
    addMsg(message, "user")
    await startCoachPractice(route)
    return
  }
  if (action === "mode_change" && ["lesson", "opening_training", "endgame_training"].includes(route.mode)) {
    const goal = route.mode === "lesson" ? message
      : route.topic ? `I want to learn ${route.topic}` : message
    await requestPlan(goal, null, {autoStart: true, route, turnStartedAt})
    return
  }

  if (state.sessionId) {
    await streamSessionChat(message, route, turnStartedAt)
  } else {
    addMsg(message, "user")
    const reply = route.reply || "I can help with chess lessons, explanations, and practice. Name a topic or ask a chess question."
    addMsg(reply, "assistant")
    state.coachHistory.push({role: "user", content: message}, {role: "assistant", content: reply})
    state.coachHistory = state.coachHistory.slice(-16)
    reportCoachPaint("TTFO (local conversation)", turnStartedAt, {action, source: route.source})
    reportCoachLatency("turn total", performance.now() - turnStartedAt, {action, source: route.source})
  }
}

async function streamSessionChat(message, intent, turnStartedAt = performance.now()) {
  addMsg(message, "user")
  const bubble = addMsg("…", "assistant")
  bubble.classList.add("typing")
  let text = ""
  let teacher = null
  let firstMeaningful = false
  try {
    await streamEvents(`/api/sessions/${state.sessionId}/chat/stream`, {message, intent}, ev => {
      if (ev.type === "start") teacher = ev.teacher || null
      if (ev.type === "delta") text += ev.text
      else if (ev.type === "replace" || ev.type === "done") text = ev.text
      if (text) {
        bubble.textContent = text
        messagesEl.scrollTop = messagesEl.scrollHeight
        if (!firstMeaningful) {
          firstMeaningful = true
          reportCoachPaint("TTFO (first rendered response text)", turnStartedAt, {teacher})
        }
      }
    })
  } catch (err) {
    bubble.remove()
    renderError(err.message)
  } finally {
    bubble.classList.remove("typing")
    if (bubble.isConnected && text) finishStreamedMsg(bubble)
    else if (bubble.isConnected) bubble.remove()
    reportCoachLatency("turn total", performance.now() - turnStartedAt,
      {teacher, meaningful_output: Boolean(text)})
  }
}

async function startCoachPractice(route) {
  if (!route.topic_id) {
    renderCoachClarify({question: `I can use a puzzle for ${route.topic || "a chess theme"}, but I couldn't match it to a verified practice theme. Which topic should I use?`, options: []})
    return
  }
  addMsg(`🧩 Opening a verified ${route.topic || "topic"} practice position.`, "system")
  try {
    await switchView("puzzles")
    await puzzles.startFromCoach({concept: route.topic_id, topic: route.topic})
  } catch (err) {
    renderError(err.message)
  }
}

function renderCoachClarify(clarification) {
  const question = clarification && clarification.question || "What would you like to do?"
  const options = clarification && Array.isArray(clarification.options) ? clarification.options : []
  const div = addMsg(escapeHtml(question), "assistant", true)
  div.classList.add("clarify-card")
  const row = document.createElement("div")
  row.className = "clarify-options no-speech"
  for (const option of options) {
    const button = document.createElement("button")
    button.className = "chip"
    button.textContent = option.label
    button.addEventListener("click", () => sendChat(option.message))
    row.appendChild(button)
  }
  div.appendChild(row)
  if (!options.length) {
    const hint = document.createElement("div")
    hint.className = "muted"
    hint.textContent = "You can reply in your own words."
    div.appendChild(hint)
  }
  narrator.auto(div, "lessons")
}

function sameTopic(a, b) {
  const normalize = value => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
  const left = normalize(a), right = normalize(b)
  return Boolean(left && right && (left === right || left.includes(right) || right.includes(left)))
}

function findParkedLesson(topic) {
  if (!topic) return null
  return [...state.parkedLessons].reverse().find(entry => sameTopic(topic, entry.learningState?.topic)) || null
}

function parkCurrentLesson() {
  if (!state.sessionId || !state.learningState) return
  const existing = state.parkedLessons.find(entry => entry.sessionId === state.sessionId)
  if (existing) {
    existing.learningState = state.learningState
    existing.planId = state.sessionPlanId
    existing.lessonId = state.activeLessonId
    return
  }
  state.parkedLessons.push({sessionId: state.sessionId, planId: state.sessionPlanId,
    lessonId: state.activeLessonId, learningState: state.learningState})
}

async function restoreParkedLesson(entry) {
  if (state.sessionId && state.sessionId !== entry.sessionId) parkCurrentLesson()
  try {
    const res = await api(`/api/sessions/${encodeURIComponent(entry.sessionId)}`)
    state.sessionId = res.session_id
    state.sessionPlanId = entry.planId
    state.sessionCompleted = res.status === "completed"
    state.activeLessonId = res.lesson_id || entry.lessonId
    state.learningState = res.learning_state || entry.learningState
    state.step = res.step
    state.parkedLessons = state.parkedLessons.filter(item => item.sessionId !== entry.sessionId)
    setActivePlanId(state.sessionPlanId)
    setActiveLessonId(state.activeLessonId)
    await renderStep(res.step)
  } catch (err) {
    renderError(`I couldn't safely resume that lesson. ${err.message}`)
  }
}

async function continueActivePlan(message = null, {restart = false} = {}) {
  if (message) addMsg(message, "user")
  const button = document.getElementById("btn-chat")
  button.disabled = true
  try {
    const query = new URLSearchParams()
    if (state.activeLessonId) query.set("current_lesson_id", state.activeLessonId)
    if (restart) query.set("restart", "true")
    const encodedQuery = query.toString()
    const suffix = encodedQuery ? `?${encodedQuery}` : ""
    const progress = await api(`/api/plans/${encodeURIComponent(state.activePlanId)}/progression${suffix}`)

    if (progress.complete) {
      renderPlanComplete(progress)
      return
    }

    const next = progress.next_lesson
    if (!next) {
      renderPlanComplete({...progress, complete: true})
      return
    }

    // If this is the lesson currently in progress, continue it rather than
    // restarting it. Once it completes, re-read the saved plan and start its next
    // incomplete lesson immediately.
    const isCurrentIncomplete = !restart && state.sessionId &&
      state.sessionPlanId === state.activePlanId && !state.sessionCompleted &&
      state.activeLessonId === next.id
    if (isCurrentIncomplete) {
      if (state.step && state.step.type === "exercise" && !state.step.accepted) {
        addMsg("Finish the current exercise first, then I'll continue your lesson plan.")
        return
      }
      const result = await advanceLesson()
      if (result && result.completed && result.status !== "INVALID_LESSON") {
        await continueActivePlan()
      }
      return
    }

    if (restart) {
      addMsg(`Starting the ${planSubject(progress.plan_title)} lesson plan again from Lesson 1: ${next.title}.`)
    } else if (progress.completed_lesson) {
      addMsg(`Excellent! You've completed "${progress.completed_lesson.title}." ` +
        `Let's move on to Lesson ${next.number}: ${next.title}.`)
    } else {
      addMsg(`Let's continue with Lesson ${next.number}: ${next.title}.`)
    }
    await startLesson(next.id, state.activePlanId)
  } catch (err) {
    if (err.status === 404) {
      setActivePlanId(null)
      setActiveLessonId(null)
    }
    renderError(err.message)
  } finally {
    button.disabled = false
  }
}

function planSubject(title) {
  return String(title || "your").replace(/^Learn:\s*/i, "").trim() || "your"
}

function renderPlanComplete(progress) {
  const subject = planSubject(progress.plan_title)
  const titles = (progress.lesson_titles || []).join(" · ")
  const count = progress.lesson_count || (progress.lesson_titles || []).length
  const list = titles ? `<br><br>Completed: ${escapeHtml(titles)}.` : ` You completed all ${count} lessons.`
  const div = addMsg(`🏆 <b>You've completed the ${escapeHtml(subject)} lesson plan!</b>` +
    `<br><br>You finished all ${count} lessons.${list}<br><br>` +
    `Want to keep training ${escapeHtml(subject)} or move on to another topic?`, "assistant", true)
  div.classList.add("lesson-summary")
  const goals = [`I want to practice ${subject}`]
  const labels = [`Keep training ${subject}`]
  for (const related of (progress.related || []).slice(0, 3)) {
    goals.push(`I want to learn ${related}`)
    labels.push(related)
  }
  div.appendChild(suggestionChips(goals, labels))
  narrator.auto(div, "lessons")
}

async function quickAnswer(message) {
  let res
  try { res = await api("/api/knowledge/answer", "POST", {message, session_id: state.sessionId}) } catch (_) { return false }
  const a = res && res.answer
  return a ? renderQuickAnswer(message, a) : false
}

function renderQuickAnswer(message, a) {
  const answerText = `${a.term}: ${a.text}`
  addMsg(message, "user")
  const div = addMsg(`📖 <b>${escapeHtml(a.term)}</b> — ${escapeHtml(a.text)}`, "assistant", true)
  if (!state.sessionId) {
    state.coachHistory.push({role: "user", content: message}, {role: "assistant", content: answerText})
    state.coachHistory = state.coachHistory.slice(-16)
  }
  div.classList.add("quick-answer")
  if (!a.verified) {
    const note = document.createElement("div")
    note.className = "muted"
    note.textContent = a.source === "basic_explanations"
      ? "Curated explanation; no direct engine-checked positions for this term are linked."
      : "From my glossary — I don't have checked example positions for this yet."
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

const LEARN_REQUEST = /^\s*(i\s*(really\s*)?(want|would like|'d like|wanna|need)\s*(to\s*)?(learn|study|practice|practise|get better at|improve|master)|(?:let's|let us)\s+(learn|study|practice|practise)\b|teach me|help me (learn|with|improve|understand)|show me how|how do i (play|learn)|can you teach me|learn\b|plan\b)/i

function isLearnRequest(text) {
  return LEARN_REQUEST.test(text)
}

// `body` overrides the request (a clarification answer or "ask me again"); the learner's
// message is only echoed for a new request.
async function requestPlan(goal, body = null, options = {}) {
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
    const planStartedAt = performance.now()
    const res = await api("/api/plans", "POST", body || {goal, library: true})
    reportCoachLatency("plan API", performance.now() - planStartedAt,
      {source: res.source || (res.clarify ? "clarify" : "unknown")})
    pending.remove()
    if (res.clarify) {  // several different readings: ask instead of guessing
      renderClarify(res.clarify, res.goal || goal, options)
      if (options.turnStartedAt != null) {
        reportCoachPaint("TTFO (plan clarification)", options.turnStartedAt, {source: "planner"})
      }
      return
    }
    if (options.autoStart) {
      parkCurrentLesson()
      state.coachHistory.push({role: "user", content: goal.slice(0, 500)})
      state.coachHistory = state.coachHistory.slice(-16)
    }
    renderPlan(res)
    if (options.turnStartedAt != null) {
      reportCoachPaint("TTFO (verified lesson plan)", options.turnStartedAt, {source: res.source || "planner"})
    }
    await loadCourses()
    if (options.autoStart && res.first_lesson_id) {
      await startLesson(res.first_lesson_id, res.plan && res.plan.id)
    }
  } catch (err) {
    pending.remove()
    const suggestions = (err.data && err.data.suggestions) || []
    const div = addMsg(escapeHtml(err.message), "assistant", true)
    if (suggestions.length) div.appendChild(suggestionChips(suggestions))
    if (err.data && err.data.debug) appendDebug(div, err.data.debug)
  } finally {
    clearTimeout(slow)
    btn.disabled = false
    if (options.turnStartedAt != null) {
      reportCoachLatency("lesson-plan turn total", performance.now() - options.turnStartedAt,
        {autoStart: Boolean(options.autoStart)})
    }
  }
}


// Developer view of how a request became a plan (only with ?debug=1 / localStorage "chessai.debug").
function appendDebug(div, debug) {
  if (!debugEnabled()) return
  const box = document.createElement("details")
  box.className = "plan-debug no-speech"
  const summary = document.createElement("summary")
  summary.textContent = "Debug: how this request was handled"
  const table = document.createElement("dl")
  for (const [k, v] of debugLines(debug)) {
    const dt = document.createElement("dt")
    dt.textContent = k
    const dd = document.createElement("dd")
    dd.textContent = v
    if (/^(PASS|FAIL|UNCERTAIN)/.test(v)) dd.className = v.startsWith("PASS") ? "ok" : "bad"
    table.append(dt, dd)
  }
  box.append(summary, table)
  div.appendChild(box)
}

// A question card: one button per reading, plus "Something else" with a text box.
function renderClarify(question, goal, options = {}) {
  // a confirmation card lists what was understood ("You have two rooks.") under the question
  const details = (question.details || []).map(d => `<li>${escapeHtml(d)}</li>`).join("")
  const icon = question.kind === "confirm" ? "✅" : "🤔"
  const div = addMsg(`${icon} <b>${escapeHtml(question.question)}</b>` +
    (details ? `<ul class="clarify-details">${details}</ul>` : ""), "assistant", true)
  div.classList.add("clarify-card")
  const row = document.createElement("div")
  row.className = "clarify-options no-speech"
  const other = document.createElement("form")
  other.className = "clarify-other no-speech"
  other.hidden = true
  const hint = question.kind === "confirm" ? "What should change? e.g. “I have a rook and a queen”"
    : "Tell me what you'd like to learn"
  other.innerHTML = `<input type="text" maxlength="200" placeholder="${escapeHtml(hint)}">` +
    `<button class="btn primary" type="submit">Send</button>`
  const note = document.createElement("div")
  note.className = "muted clarify-note"
  const answer = (choice, text = "") => {
    const got = clarifyBody(goal, question, choice, text)
    if (got.error) { note.textContent = got.error; return }
    div.querySelectorAll("button, input").forEach(el => { el.disabled = true })
    const picked = question.options.find(o => o.id === choice)
    addMsg(choice === OTHER ? text.trim() : optionLabel(picked), "user")
    requestPlan(goal, got.body, options)
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
  if (plan && plan.id) {
    setActivePlanId(plan.id)
    setActiveLessonId(null)
  }
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
  const reasons = puzzleReasons(plan)
  if (reasons.length) {
    const list = document.createElement("ol")
    list.className = "puzzle-why"
    for (const r of reasons) {
      const li = document.createElement("li")
      li.innerHTML = `<b>${escapeHtml(r.title)}</b> <span class="muted">— ${escapeHtml(r.why)}</span>`
      list.appendChild(li)
    }
    div.appendChild(list)
  }
  if (plan.debug) appendDebug(div, plan.debug)
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
  start.addEventListener("click", () => startLesson(res.first_lesson_id, plan.id))
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
  // An opening is a tree: the plan teaches one branch, the others are one click away.
  const branches = (plan.branches && plan.branches.names) || []
  if (branches.length) {
    const label = document.createElement("div")
    label.className = "muted"
    label.textContent = `Other branches of the ${plan.branches.opening} (not in this plan):`
    div.appendChild(label)
    div.appendChild(suggestionChips(branches.map(b => `I want to learn ${b}`),
      branches.map(b => {
        const short = b.includes(": ") ? b.split(": ").slice(1).join(": ") : b
        return short === plan.branches.opening ? b : short  // "Queen's Pawn Game: London System"
      })))
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
    if (state.activePlanId === planId) setActivePlanId(null)
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
    if (!plans.some(c => c.plan && c.plan.id === state.activePlanId)) {
      setActivePlanId(plans[0] && plans[0].plan ? plans[0].plan.id : null)
    }
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
        if (lesson.status === "available") btn.addEventListener("click", () => startLesson(lesson.id, course.plan.id))
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
  api, streamEvents, stopStream, board, showPosition, clearMarkers, addMarkers, verifiedMove, isPositionVerified,
  Chess, COLOR, MARKER_TYPE, escapeHtml,
  setStatus, makeSpeakable, narrator, nav, MoveHistory, sounds,
  // A training plan built from the learner's games is an ordinary plan: show it in the lessons view.
  onTraining: async res => {
    await switchView("lessons")
    renderPlan(res)
    await loadCourses()
  },
})

const puzzles = setupPuzzles({
  api, board, showPosition, clearMarkers, addMarkers, addMarker, verifiedMove, isPositionVerified,
  Chess, COLOR, MARKER_TYPE, legalInputHandler, escapeHtml, setStatus,
  nav, MoveHistory, showNode, sounds,
})

let lessonBoard = null  // what the lesson view showed, restored when coming back
let viewSwitch = 0      // a newer tab click wins: an older switch stops at its next await

const VIEWS = {  // tab, sidebar block, pane of each top-level view
  lessons: ["tab-lessons", "lessons-side", "lesson-pane"],
  games: ["tab-games", "games-side", "analysis-pane"],
  puzzles: ["tab-puzzles", "puzzles-side", "puzzles-pane"],
}

// Each view keeps its own state while hidden: a lesson, a game review and a half-solved
// puzzle are all exactly where the learner left them.
async function switchView(name) {
  if (state.view === name || !VIEWS[name]) return
  if (state.busy || state.demoPlaying) {
    setStatus(state.demoPlaying
      ? "Let the verified demonstration finish before switching views."
      : "The board is being confirmed. Please wait before switching views.")
    return
  }
  narrator.stop()
  const token = ++viewSwitch
  const current = () => token === viewSwitch && state.view === name
  const from = state.view
  const pendingLineRestore = lineView.restore
    ? {fen: lineView.restore, orientation: lineView.restoreOrientation || COLOR.white}
    : lineView.restoreInFlight
  if (from === "lessons") {
    const fen = (pendingLineRestore && pendingLineRestore.fen) || verifiedBoard.currentFen() ||
      (lessonBoard && lessonBoard.fen) || (state.chess && state.chess.fen()) || new Chess().fen()
    lessonBoard = {fen,
      orientation: pendingLineRestore ? pendingLineRestore.orientation
        : (board.getOrientation ? board.getOrientation() : COLOR.white),
      status: document.getElementById("board-status").textContent}
  }
  lineView.token++
  clearTimeout(lineView.timer)
  nav.unlock("line-preview", "*")
  let previewRestoreFen = null
  let previewRestoreOrientation = null
  if (pendingLineRestore) {
    previewRestoreFen = pendingLineRestore.fen
    previewRestoreOrientation = pendingLineRestore.orientation || COLOR.white
    lineView.restore = null
    lineView.restoreOrientation = null
    lineView.restoreInFlight = null
    try {
      await showPosition(previewRestoreFen, {orientation: previewRestoreOrientation, animated: false})
    } catch (err) {
      setStatus(`I couldn't restore the lesson board after its move preview. ${err.message}`)
    }
    if (token !== viewSwitch || state.view !== from) return
  }
  if (from === "lessons") {
    // The verified lesson snapshot was captured before any asynchronous preview restoration.
  } else if (from === "games") {
    gameAnalysis.leave()
  } else if (from === "puzzles") {
    puzzles.leave()
  }
  state.view = name
  nav.refresh()  // the ← → buttons follow the view being shown
  for (const [view, [tabId, sideId, paneId]] of Object.entries(VIEWS)) {
    const on = view === name
    const tab = document.getElementById(tabId)
    tab.classList.toggle("active", on)
    tab.setAttribute("aria-selected", String(on))
    document.getElementById(sideId).classList.toggle("hidden", !on)
    document.getElementById(paneId).classList.toggle("hidden", !on)
  }
  board.disableMoveInput()
  if (name === "games") {
    await gameAnalysis.enter()
  } else if (name === "puzzles") {
    await puzzles.enter()
  } else {
    if (lessonBoard) {
      await showPosition(lessonBoard.fen, {orientation: lessonBoard.orientation})
      if (!current()) return
      setStatus(lessonBoard.status)
    }
    const step = state.step
    if (step && step.type === "exercise" && !step.accepted) {
      state.chess = parsePosition(Chess, step.board.fen, "Lesson position")
      await showPosition(state.chess.fen(), {orientation: step.side === "black" ? COLOR.black : COLOR.white})
      if (!current()) return
      board.disableMoveInput()
      board.enableMoveInput(moveInputHandler, step.side === "black" ? COLOR.black : COLOR.white)
    }
  }
}

// ---------- wire up ----------

document.getElementById("tab-lessons").addEventListener("click", () => switchView("lessons"))
document.getElementById("tab-games").addEventListener("click", () => switchView("games"))
document.getElementById("tab-puzzles").addEventListener("click", () => switchView("puzzles"))

document.getElementById("btn-play").addEventListener("click", playDemonstration)
document.getElementById("btn-continue").addEventListener("click", () => { narrator.stop(); advanceLesson() })
document.getElementById("btn-hint").addEventListener("click", requestHint)
document.getElementById("btn-reveal").addEventListener("click", revealSolution)
document.getElementById("btn-chat").addEventListener("click", () => sendChat())
document.getElementById("btn-explain-example").addEventListener("click", explainExample)
document.getElementById("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter") sendChat()
})

{
  const welcome = document.getElementById("welcome")
  welcome.appendChild(suggestionChips(STARTERS.map(t => `I want to learn ${t.toLowerCase()}`), STARTERS))
  makeSpeakable(welcome)
}
const coach = setupCoach({api, narrator, requestPlan, addMsg, messagesEl, escapeHtml, sounds,
  // the username from onboarding: one click from "thanks" to the learner's games being analyzed
  analyzeGames: async username => { await switchView("games"); await gameAnalysis.fetchFor(username) }})
coach.init({
  readCurrent() {
    const last = state.view === "games" ? gameAnalysis.readable() : state.view === "puzzles" ? puzzles.readable()
      : [...messagesEl.querySelectorAll(".msg.assistant, .msg.system")].pop()
    if (last) narrator.speak(last)
  },
}).then(() => { if (!state.sessionId) coach.offerOnboarding() })
loadHealth()
loadCourses()
