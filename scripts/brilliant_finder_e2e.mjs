// End-to-end guided Finder test using the real app, cm-chessboard move callback, and API.
//
//   npm install --no-save --package-lock=false jsdom@24
//   node scripts/brilliant_finder_e2e.mjs http://127.0.0.1:8080/
//
// Run against an isolated DATA_DIR; the script writes Finder progress/preferences while testing.
import {JSDOM} from "jsdom"
import fs from "node:fs"
import path from "node:path"
import {fileURLToPath} from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "frontend")
const BASE = process.argv[2] || "http://127.0.0.1:8080/"
const TARGET_ID = "lichess_2gvas" // existing verified expert puzzle with two player-side setup moves
const html = fs.readFileSync(`${ROOT}/index.html`, "utf8").replace(/<script type="module".*<\/script>/, "")
const dom = new JSDOM(html, {url: BASE, pretendToBeVisual: true})
const w = dom.window
for (const key of ["window", "document", "navigator", "HTMLElement", "SVGElement", "Node", "Element",
  "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame", "DOMParser", "Event", "XMLHttpRequest"]) {
  try { globalThis[key] = key === "window" ? w : w[key] }
  catch (_) { Object.defineProperty(globalThis, key, {value: w[key]}) }
}
w.confirm = () => true
const makeTransform = () => ({setTranslate() {}, setScale() {}, setRotate() {}, matrix: {}})
w.SVGElement.prototype.createSVGTransform = makeTransform
Object.defineProperty(w.SVGElement.prototype, "transform", {get() {
  if (!this._transform) this._transform = {baseVal: {
    items: [], appendItem(item) { this.items.push(item) }, getItem(i) { return this.items[i] },
    get numberOfItems() { return this.items.length }, clear() { this.items = [] },
    consolidate() { return this.items[0] }, removeItem() {},
  }}
  return this._transform
}})
globalThis.ResizeObserver = w.ResizeObserver = class {observe() {} unobserve() {} disconnect() {}}
const realFetch = globalThis.fetch
const apiLog = []
const requestPaths = []
let delayNextMove = false
globalThis.fetch = async (input, options = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, BASE)
  requestPaths.push(url.pathname)
  if (delayNextMove && url.pathname.endsWith("/move")) {
    delayNextMove = false
    await new Promise(resolve => setTimeout(resolve, 350))
  }
  const response = await realFetch(url, options)
  if (url.pathname.startsWith("/api/puzzles/brilliant/")) {
    let body = null
    try { body = await response.clone().json() } catch (_) {}
    let request = null
    try { request = options.body ? JSON.parse(options.body) : null } catch (_) {}
    apiLog.push({path: url.pathname, status: response.status, body, request})
  }
  return response
}
const errors = []
process.on("unhandledRejection", error => errors.push(String(error && error.stack || error)))

const {Chessboard, INPUT_EVENT_TYPE} = await import(`${ROOT}/vendor/cm-chessboard/src/Chessboard.js`)
let board
const originalEnable = Chessboard.prototype.enableMoveInput
Chessboard.prototype.enableMoveInput = function (...args) {
  board = this
  return originalEnable.apply(this, args)
}
const originalSetPosition = Chessboard.prototype.setPosition
Chessboard.prototype.setPosition = function (...args) {
  board = this
  return originalSetPosition.apply(this, args)
}
await import(`${ROOT}/app.js`)
const {Chess} = await import(`${ROOT}/vendor/chess.mjs/Chess.js`)

const $ = id => document.getElementById(id)
const visible = id => Boolean($(id) && !$(id).classList.contains("hidden"))
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
const checks = []
function pass(description) {
  checks.push(description)
  console.log(`PASS ${description}`)
}
function assert(condition, description, detail = "") {
  if (!condition) throw new Error(`FAIL ${description}${detail ? `: ${detail}` : ""}`)
  pass(description)
}
async function waitFor(predicate, label, timeout = 30000) {
  const started = Date.now()
  while (Date.now() - started < timeout) {
    if (predicate()) return
    await sleep(40)
  }
  throw new Error(`Timed out waiting for ${label}; Finder status=${$("pz-brilliant-status")?.textContent || ""}; ` +
    `board status=${$("board-status")?.textContent || ""}; start-disabled=${$("pz-brilliant-start")?.disabled}; ` +
    `requests=${JSON.stringify(requestPaths.slice(-12))}`)
}
async function boardMove(from, to, fenBefore) {
  const callback = board && board.state.moveInputCallback
  if (!callback) return "input-disabled"
  const piece = board.getPiece(from)
  const started = callback({type: INPUT_EVENT_TYPE.moveInputStarted, squareFrom: from, piece, chessboard: board})
  if (!started) return "cannot-pick-up"
  const legal = callback({type: INPUT_EVENT_TYPE.validateMoveInput, squareFrom: from,
    squareTo: to, piece, chessboard: board})
  if (!legal) return "illegal"
  const preview = new Chess(fenBefore)
  const move = preview.move({from, to, promotion: "q"})
  if (!move) return "illegal"
  // The real drag updates cm-chessboard's piece model before its moveInputFinished callback.
  await board.setPosition(preview.fen(), false)
  callback({type: INPUT_EVENT_TYPE.moveInputFinished, squareFrom: from, squareTo: to,
    legalMove: true, chessboard: board})
  return "ok"
}
const pathCalls = suffix => apiLog.filter(entry => entry.path.endsWith(suffix))
const lastCall = suffix => pathCalls(suffix).at(-1)
const nextCount = () => pathCalls("/next").length
const moveCount = () => pathCalls("/move").length
const piecePlacement = fen => fen.split(/\s+/)[0]
async function puzzleData(id) {
  const response = await realFetch(new URL(`api/puzzles/${encodeURIComponent(id)}`, BASE))
  assert(response.ok, `puzzle library loaded ${id}`, `HTTP ${response.status}`)
  return response.json()
}
async function requestNext(label) {
  await waitFor(() => !$('pz-brilliant-start').disabled, `${label} start enabled`, 15000)
  const before = nextCount()
  $("pz-brilliant-start").click()
  await waitFor(() => nextCount() > before, `${label} task request`)
  const entry = lastCall("/next")
  assert(entry.status === 200, `${label} Finder API returned a position`, JSON.stringify(entry.body))
  await waitFor(() => visible("pz-brilliant-task") && board?.state.moveInputCallback,
    `${label} board position and input`, 15000)
  return entry.body
}

await waitFor(() => $("course-list") && $("health"), "application startup", 20000)
$("tab-puzzles").click()
await waitFor(() => visible("puzzles-pane") && visible("pz-dashboard"), "existing Puzzles view", 15000)
$("pz-mode-brilliant").click()
await waitFor(() => visible("pz-brilliant") && !$('pz-brilliant-start').disabled,
  "Brilliant Finder profile", 15000)
assert($("pz-brilliant").textContent.includes("Play every required move yourself"),
  "Finder opens inside the existing Puzzles view")

// Select an expert position with two prep moves before the final sacrifice.
const difficulty = $("pz-brilliant-difficulty")
const preferencesBefore = pathCalls("/preferences").length
if (difficulty.value !== "expert") {
  difficulty.value = "expert"
  difficulty.dispatchEvent(new w.Event("change", {bubbles: true}))
}
await waitFor(() => !difficulty.disabled &&
  (pathCalls("/preferences").length > preferencesBefore || difficulty.value === "expert"),
  "manual expert preference saved")
if (pathCalls("/preferences").length > preferencesBefore) {
  assert(lastCall("/preferences").body.difficulty === "expert", "manual difficulty selection reaches the existing preference API")
} else {
  assert(difficulty.value === "expert", "existing manual expert difficulty is retained")
}

let task = await requestNext("first expert selection")
let targetTask = null
for (let attempt = 1; attempt <= 38; attempt++) {
  if (task.id === TARGET_ID) {
    targetTask = task
    break
  }
  const before = nextCount()
  const movesBeforeSkip = moveCount()
  $("pz-brilliant-skip").click()
  await waitFor(() => nextCount() > before, `skip to next expert position ${attempt}`)
  const skipped = lastCall("/next")
  assert(skipped.status === 200, "skip advances to another position without recording a result", JSON.stringify(skipped.body))
  assert(moveCount() === movesBeforeSkip, "skipping a position does not submit a move or record a result")
  await waitFor(() => visible("pz-brilliant-task") && board?.state.moveInputCallback,
    "next expert board position", 15000)
  task = skipped.body
}
assert(targetTask, "expert manual difficulty exposes the verified multi-setup puzzle", `searched ${nextCount()} expert positions`)
const puzzle = await puzzleData(targetTask.id)
const sacrificeIndex = puzzle.steps.findIndex(step => step.features?.sacrifice)
assert(sacrificeIndex === 2 && puzzle.steps.length === 3,
  "selected library line has two required setup turns before its sacrifice",
  `sacrifice index=${sacrificeIndex}, steps=${puzzle.steps.length}`)
assert(puzzle.steps.every(step => step.uci.length === 4), "selected real board line needs no unsupported promotion interaction")
assert(board.getPosition() === piecePlacement(puzzle.fen), "cm-chessboard shows the selected library FEN")
assert(!visible("pz-brilliant-result") && !$("pz-brilliant-selected-line").textContent,
  "complete solution stays hidden before completion or explicit reveal")
let currentFen = targetTask.fen

// A legal but unverified move must return to the exact same puzzle turn and receive no reply.
const chessAtStart = new Chess(puzzle.fen)
const notSolution = new Set([puzzle.steps[0].uci, ...(puzzle.steps[0].accepted || []), ...(puzzle.steps[0].good || [])]
  .map(uci => uci.slice(0, 4)))
const wrong = chessAtStart.moves({verbose: true}).find(move => !notSolution.has(move.from + move.to))
assert(wrong, "the starting position has a legal move outside the verified choices")
const startPlacement = board.getPosition()
const wrongMoveStart = moveCount()
assert(await boardMove(wrong.from, wrong.to, currentFen) === "ok", "real board accepts the legal retry-test move")
await waitFor(() => moveCount() > wrongMoveStart, "incorrect move response")
const wrongResponse = lastCall("/move").body
assert(wrongResponse.outcome === "incorrect" && !wrongResponse.opponent_move && !wrongResponse.done,
  "incorrect move has feedback and triggers no scripted opponent move", JSON.stringify(wrongResponse))
assert(!visible("pz-brilliant-result") && !$("pz-brilliant-selected-line").textContent,
  "incorrect retry does not reveal the solution")
await waitFor(() => board.getPosition() === startPlacement && Boolean(board.state.moveInputCallback),
  "board restored to same retry position", 10000)
currentFen = wrongResponse.current_fen
assert(board.getPosition() === piecePlacement(targetTask.fen), "retry preserves the same starting position")
assert(/Try again from the same position/i.test($("pz-brilliant-status").textContent),
  "incorrect move feedback explicitly allows retry")

// Change difficulty during an active task. It must leave this puzzle/FEN alone and affect only next.
difficulty.value = "beginner"
difficulty.dispatchEvent(new w.Event("change", {bubbles: true}))
await waitFor(() => lastCall("/preferences")?.body?.difficulty === "beginner" && !difficulty.disabled,
  "active-task manual difficulty change saved")
assert(board.getPosition() === startPlacement && visible("pz-brilliant-task"),
  "difficulty change leaves the active puzzle and board position unchanged")

// The first correct move's network request is deliberately held. The real board callback must
// reject duplicate input while Finder waits for the one verified opponent reply.
const responseByStep = []
for (let index = 0; index < puzzle.steps.length; index++) {
  const step = puzzle.steps[index]
  const before = moveCount()
  if (index === 0) delayNextMove = true
  assert(await boardMove(step.uci.slice(0, 2), step.uci.slice(2, 4), currentFen) === "ok",
    `player personally plays verified move ${step.san}`)
  if (index === 0) {
    await waitFor(() => !board.state.moveInputCallback, "board input locked during move response", 5000)
    assert(await boardMove(step.uci.slice(0, 2), step.uci.slice(2, 4), currentFen) === "input-disabled",
      "duplicate move input is blocked while the opponent reply is pending")
  }
  await waitFor(() => moveCount() > before, `verified move response for ${step.san}`, 15000)
  const response = lastCall("/move").body
  responseByStep.push(response)
  currentFen = response.current_fen
  assert(response.player_move?.uci === step.uci, `server accepted exactly the player's ${step.san}`)
  if (!response.done) assert(!visible("pz-brilliant-result") && !$("pz-brilliant-selected-line").textContent,
    "verified solution remains hidden while required player moves are still pending")
  if (step.reply_uci) {
    assert(response.opponent_move?.uci === step.reply_uci,
      `only the stored legal opponent reply ${step.reply_san} is returned`, JSON.stringify(response.opponent_move))
    await waitFor(() => board.getPosition() === piecePlacement(response.current_fen) && Boolean(board.state.moveInputCallback),
      `verified opponent reply applied before next turn: ${step.reply_san}`, 15000)
  } else {
    assert(response.done === true, "no opponent move is invented after the verified line ends")
    await waitFor(() => visible("pz-brilliant-result"), "completed Finder explanation", 30000)
  }
}
assert(responseByStep.length === puzzle.steps.length, "each required player move was processed once")
assert(moveCount() === wrongMoveStart + 1 + puzzle.steps.length,
  "no duplicate move request or duplicate opponent reply was processed")
const completion = responseByStep.at(-1).result
assert(completion?.outcome === "guided_solution" && !completion.solved && completion.recorded && completion.mistakes === 1,
  "completed line after one wrong retry records the existing missed-puzzle outcome")
assert(completion.verified_solution?.moves?.join(" ") === puzzle.solution.join(" "),
  "completion exposes the complete verified solution only after the last player move")
assert(completion.explanation?.preparatory_moves?.join(" ") === puzzle.steps.slice(0, sacrificeIndex).map(s => s.san).join(" "),
  "completion explanation uses the actual two-move verified setup")
assert(completion.explanation?.sacrifice_move === puzzle.steps[sacrificeIndex].san,
  "completion identifies the stored sacrifice move")
assert(Boolean(completion.explanation?.source_explanation), "completion explains the tactical idea from existing puzzle data")
assert(["complete", "inconclusive"].includes(completion.analysis_status),
  "runtime engine either returns analysis or reports a safe inconclusive result", completion.analysis_status)
assert($("pz-brilliant-setup").textContent.includes(puzzle.steps[0].san) &&
  $("pz-brilliant-setup").textContent.includes(puzzle.steps[1].san),
  "result UI displays both player-side setup moves")
assert($("pz-brilliant-sacrifice").textContent.includes(puzzle.steps[sacrificeIndex].san),
  "result UI displays the personally played sacrifice")
assert($("pz-brilliant-selected-line").textContent.includes(puzzle.solution.at(-1)),
  "result UI displays the full verified continuation after completion")
assert($("pz-brilliant-defense").textContent.length > 0,
  "result UI identifies a verified or engine-derived relevant defense")

// The active task remains the same after changing difficulty; the next task uses the new setting.
const beforeNext = nextCount()
$("pz-brilliant-next").click()
await waitFor(() => nextCount() > beforeNext, "next position at the new difficulty")
const nextTask = lastCall("/next").body
currentFen = nextTask.fen
assert(nextTask.difficulty.preference === "beginner",
  "manual difficulty change applies to the next position, not the active puzzle", nextTask.difficulty.preference)
await waitFor(() => visible("pz-brilliant-task") && Boolean(board.state.moveInputCallback),
  "next manual-difficulty board position", 15000)
const revealPosition = board.getPosition()
const revealMoveCount = moveCount()
$("pz-brilliant-reveal").click()
await waitFor(() => apiLog.some(entry => entry.path.endsWith("/reveal") && entry.status === 200),
  "explicit reveal API response", 15000)
const revealResponse = apiLog.filter(entry => entry.path.endsWith("/reveal")).at(-1).body
assert(revealResponse.result?.revealed && revealResponse.result.verified_solution?.moves?.length,
  "explicit reveal returns the complete verified line")
assert(board.getPosition() === revealPosition && moveCount() === revealMoveCount,
  "explicit reveal does not auto-play any solution move")
assert($("pz-brilliant-selected-line").textContent.includes(revealResponse.result.verified_solution.moves.at(-1)),
  "explicit reveal displays the full verified line")

// Retry after reveal creates a fresh attempt for exactly this puzzle and restores its original FEN.
const retriesBefore = pathCalls("/retry").length
$("pz-brilliant-retry").click()
await waitFor(() => pathCalls("/retry").length > retriesBefore, "same-puzzle retry API response")
const retryResponse = lastCall("/retry").body
currentFen = retryResponse.fen
assert(retryResponse.id === nextTask.id && retryResponse.task_id !== nextTask.task_id,
  "retry restarts the same puzzle under a fresh task id")
await waitFor(() => board.getPosition() === piecePlacement(retryResponse.fen) && Boolean(board.state.moveInputCallback),
  "retry restores original board input", 15000)
const retryPuzzle = await puzzleData(retryResponse.id)
const cleanMoveStart = moveCount()
let cleanResult = null
for (const [index, step] of retryPuzzle.steps.entries()) {
  const before = moveCount()
  assert(await boardMove(step.uci.slice(0, 2), step.uci.slice(2, 4), currentFen) === "ok",
    `retry allows a fresh player attempt: ${step.san}`)
  await waitFor(() => moveCount() > before, `retried move response ${step.san}`, 15000)
  const response = lastCall("/move").body
  assert(response.player_move?.uci === step.uci, `retry accepts verified player move ${step.san}`)
  currentFen = response.current_fen
  if (step.reply_uci) {
    assert(response.opponent_move?.uci === step.reply_uci, `retry plays only verified reply ${step.reply_san}`)
    await waitFor(() => board.getPosition() === piecePlacement(response.current_fen) && Boolean(board.state.moveInputCallback),
      `retry verified reply ${step.reply_san}`, 15000)
  } else {
    assert(response.done && response.result, "clean retry completes the stored line")
    cleanResult = response.result
    await waitFor(() => visible("pz-brilliant-result"), "clean retry result UI", 30000)
  }
}
assert(cleanResult?.solved && cleanResult.recorded && cleanResult.mistakes === 0,
  "correct retry records a solved, mistake-free Finder attempt")
assert(moveCount() === cleanMoveStart + retryPuzzle.steps.length,
  "retried player line is processed once per required move")

// Find a final-turn alternative that the existing library has explicitly verified.
const altPreferenceCount = pathCalls("/preferences").length
difficulty.value = "expert"
difficulty.dispatchEvent(new w.Event("change", {bubbles: true}))
await waitFor(() => pathCalls("/preferences").length > altPreferenceCount && !difficulty.disabled,
  "expert difficulty restored for verified-alternative test")
assert(lastCall("/preferences").body.difficulty === "expert", "expert manual selection is saved for the next position")
const beforeExpert = nextCount()
$("pz-brilliant-next").click()
await waitFor(() => nextCount() > beforeExpert, "first expert alternative position")
let altTask = lastCall("/next").body
assert(altTask.difficulty.preference === "expert", "alternative test uses the selected expert band")
await waitFor(() => visible("pz-brilliant-task") && Boolean(board.state.moveInputCallback),
  "first expert alternative board", 15000)
for (let attempt = 1; attempt <= 50 && altTask.id !== "lichess_r8rbc"; attempt++) {
  const before = nextCount()
  $("pz-brilliant-skip").click()
  await waitFor(() => nextCount() > before, `skip to verified-alternative position ${attempt}`)
  const skipped = lastCall("/next")
  assert(skipped.status === 200, "skip keeps browsing the same expert collection", JSON.stringify(skipped.body))
  altTask = skipped.body
  await waitFor(() => visible("pz-brilliant-task") && Boolean(board.state.moveInputCallback),
    "expert alternative board position", 15000)
}
assert(altTask.id === "lichess_r8rbc", "expert collection yields a puzzle with an explicitly verified terminal alternative")
const altPuzzle = await puzzleData(altTask.id)
const altIndex = altPuzzle.steps.length - 1
const altStep = altPuzzle.steps[altIndex]
const verifiedAlternative = altStep.accepted?.[0]
assert(altStep.kind === "critical" && !altStep.reply_uci && verifiedAlternative &&
  !altStep.accepted.includes(altStep.uci), "alternative is stored on the terminal step and has no unverified reply")
currentFen = altTask.fen
const alternativeMoveStart = moveCount()
let alternativeResult = null
for (const [index, step] of altPuzzle.steps.entries()) {
  const uci = index === altIndex ? verifiedAlternative : step.uci
  const before = moveCount()
  assert(await boardMove(uci.slice(0, 2), uci.slice(2, 4), currentFen) === "ok",
    `player plays ${index === altIndex ? "stored verified alternative" : "required setup/line move"} ${uci}`)
  await waitFor(() => moveCount() > before, `alternative-line move ${uci}`, 15000)
  const response = lastCall("/move").body
  assert(response.player_move?.uci === uci, `server recognizes played move ${uci}`)
  currentFen = response.current_fen
  if (index < altIndex) {
    assert(response.opponent_move?.uci === step.reply_uci,
      `preparation follows only stored reply ${step.reply_san}`)
    await waitFor(() => board.getPosition() === piecePlacement(response.current_fen) && Boolean(board.state.moveInputCallback),
      `alternative line verified reply ${step.reply_san}`, 15000)
  } else {
    assert(response.done && response.result?.outcome === "verified_alternative" && response.result.solved,
      "only the stored terminal alternative completes the puzzle", JSON.stringify(response.result))
    assert(!response.opponent_move && response.result.recorded,
      "terminal alternative invents no opponent response and records the accepted solution")
    alternativeResult = response.result
    await waitFor(() => visible("pz-brilliant-result"), "verified alternative result UI", 30000)
  }
}
assert(moveCount() === alternativeMoveStart + altPuzzle.steps.length,
  "verified alternative is processed once without a duplicate response")
assert($("pz-brilliant-verdict").textContent.includes("Verified alternative"),
  "UI labels the explicitly verified alternative without claiming an unverified continuation")
assert(alternativeResult?.verified_solution?.moves?.join(" ") === altPuzzle.solution.join(" "),
  "alternative completion retains the complete canonical verified solution for review")
assert(!requestPaths.some(path => /\/(qwen|chat\/complete|teacher\/ask)(\/|$)/i.test(path)),
  "guided Finder flow makes no Qwen/model-inference request")
await waitFor(() => errors.length === 0, "no unhandled frontend errors", 2000)
console.log(`\n${checks.length} end-to-end assertions passed; 0 failed.`)
dom.window.close()
