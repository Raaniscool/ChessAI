// Browser-level smoke test of the real frontend code against a running server.
//
//   npm install --no-save jsdom@24          (one-time; not a project dependency)
//   node scripts/ui_e2e.mjs [http://127.0.0.1:8000/]
//
// Drives app.js through cm-chessboard's own move-input callback, so it tests
// the exact code paths a mouse/touch move goes through. Needs the engine.
// Only deletes the plans it creates itself.
import {JSDOM} from "jsdom"
import fs from "node:fs"
import path from "node:path"
import {fileURLToPath} from "node:url"
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "frontend")
const BASE = process.argv[2] || "http://127.0.0.1:8000/"
const html = fs.readFileSync(`${ROOT}/index.html`, "utf8").replace(/<script type="module".*<\/script>/, "")
const dom = new JSDOM(html, {url: BASE, pretendToBeVisual: true})
const w = dom.window
for (const k of ["window", "document", "navigator", "HTMLElement", "SVGElement", "Node", "Element",
                 "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame", "DOMParser", "Event", "XMLHttpRequest"]) {
  try { globalThis[k] = k === "window" ? w : w[k] } catch (_) { Object.defineProperty(globalThis, k, {value: w[k]}) }
}
w.confirm = () => true
// Minimal SVG geometry stubs (jsdom has no layout engine)
const mkT = () => ({setTranslate() {}, setScale() {}, setRotate() {}, matrix: {}})
w.SVGElement.prototype.createSVGTransform = mkT
Object.defineProperty(w.SVGElement.prototype, "transform", {get() {
  if (!this._t) this._t = {baseVal: {items: [], appendItem(t) { this.items.push(t) }, getItem(i) { return this.items[i] }, get numberOfItems() { return this.items.length }, clear() { this.items = [] }, consolidate() { return this.items[0] }, removeItem() {}}}
  return this._t
}})
globalThis.ResizeObserver = w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
const realFetch = globalThis.fetch
globalThis.fetch = (p, o) => realFetch(new URL(p, BASE), o)
const errors = []
process.on("unhandledRejection", e => errors.push(String(e && e.stack || e)))

const planIds = async () => (await (await realFetch(new URL("api/plans", BASE))).json()).plans.map(p => p.id)
const existingPlans = new Set(await planIds())
const {Chessboard, INPUT_EVENT_TYPE} = await import(`${ROOT}/vendor/cm-chessboard/src/Chessboard.js`)
let board
const orig = Chessboard.prototype.enableMoveInput
Chessboard.prototype.enableMoveInput = function (...a) { board = this; return orig.apply(this, a) }
const origSet = Chessboard.prototype.setPosition
Chessboard.prototype.setPosition = function (...a) { board = this; return origSet.apply(this, a) }
await import(`${ROOT}/app.js`)
const {Chess} = await import(`${ROOT}/vendor/chess.mjs/Chess.js`)

const $ = id => document.getElementById(id)
const sleep = ms => new Promise(r => setTimeout(r, ms))
const lastMsgs = n => [...document.querySelectorAll("#messages .msg")].slice(-n).map(e => e.textContent.trim().slice(0, 160))
async function waitFor(fn, label, ms = 30000) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100) }
  throw new Error("timeout waiting for " + label + " | status=" + $("board-status").textContent + " | msgs=" + JSON.stringify(lastMsgs(3)))
}
function move(from, to) {
  const cb = board.state.moveInputCallback
  if (!cb) return "input-disabled"
  const started = cb({type: INPUT_EVENT_TYPE.moveInputStarted, squareFrom: from, chessboard: board})
  if (!started) return "cannot-pick-up"
  const valid = cb({type: INPUT_EVENT_TYPE.validateMoveInput, squareFrom: from, squareTo: to, chessboard: board})
  if (!valid) return "illegal"
  cb({type: INPUT_EVENT_TYPE.moveInputFinished, squareFrom: from, squareTo: to, legalMove: true, chessboard: board})
  return "ok"
}
const check = (cond, msg) => { console.log((cond ? "PASS " : "FAIL ") + msg); if (!cond) process.exitCode = 1 }

await waitFor(() => document.querySelector("#course-list .lesson-item, #course-list .empty"), "sidebar")
await waitFor(() => /\b(ok|partial|bad)\b/.test($("health").className), "health")
console.log("health:", $("health").className, "|", $("health").title)

// 0) No leftovers: no built-in course in the sidebar, one text box, no technical status text
check(!document.querySelector(".course:not(.plan)"), "no built-in course (Italian Game) advertised in the sidebar")
check(!/Italian Game/.test($("course-list").textContent), "sidebar doesn't mention the Italian Game")
check(!$("plan-input") && !$("btn-reset"), "no duplicate plan box, no free-move reset button")
check(!/engine|plies|teacher/i.test($("health").textContent), "header status has no technical text when all is well")
check(document.querySelectorAll("#welcome .chip").length >= 3, "welcome offers starter topics")


// ---- Puzzles tab helpers: the solution comes from the API, the moves go through the board
const visible = id => !$(id).classList.contains("hidden")
const puzzleNow = () => { const a = $("pz-set").querySelector(".pz-item.active"); return a && a.dataset.id }
const puzzleData = async id => (await realFetch(new URL(`api/puzzles/${id}`, BASE))).json()
async function playStep(step, last) {
  check(move(step.uci.slice(0, 2), step.uci.slice(2, 4)) === "ok", `puzzle move ${step.san} accepted by the board`)
  if (last) await waitFor(() => visible("pz-next"), "puzzle finished", 10000)
  else await waitFor(() => board.state.moveInputCallback && /Keep going/.test($("pz-feedback").textContent), "opponent replied", 10000)
}
async function wrongMove(p) {
  const chess = new Chess(p.fen)
  const avoid = new Set([p.steps[0].uci, ...p.steps[0].accepted, ...p.steps[0].good].map(u => u.slice(0, 4)))
  const m = chess.moves({verbose: true}).find(x => { const c = new Chess(p.fen); c.move(x); return !avoid.has(x.from + x.to) && !c.in_checkmate() })
  const before = board.getPosition()
  check(move(m.from, m.to) === "ok", "a wrong move can be played")
  await waitFor(() => /✗/.test($("pz-feedback").textContent), "wrong-move feedback", 5000)
  await waitFor(() => board.getPosition() === before && board.state.moveInputCallback, "wrong move taken back", 5000)
}

// While solving, earlier positions are for looking: no moves there; → gets back to the puzzle.
let midReviewed = false
async function reviewMidPuzzle() {
  const latest = board.getPosition()
  $("nav-prev").click()
  await waitFor(() => board.getPosition() !== latest && !board.isMoveInputEnabled(), "nav: mid-puzzle review", 5000)
  check(/earlier position/.test($("board-status").textContent), "nav: mid-puzzle ← shows an earlier position, moves paused: " +
    $("board-status").textContent)
  $("nav-next").click()
  await waitFor(() => board.getPosition() === latest && board.isMoveInputEnabled(), "nav: back to the puzzle", 5000)
  midReviewed = true
}

// P) Puzzles tab — usable on its own, before any game analysis or lesson
check(!!$("tab-puzzles") && /Puzzles/.test($("tab-puzzles").textContent) && $("tab-puzzles").parentElement === document.querySelector("nav.tabs"),
  "puzzles 1: Puzzles is a top-level tab")
$("tab-puzzles").click()
await waitFor(() => visible("puzzles-pane") && $("pz-practice").querySelector(".pz-theme"), "puzzle dashboard")
check(!visible("lesson-pane") && visible("pz-dashboard") && $("tab-puzzles").classList.contains("active"),
  "puzzles 2: clicking the tab opens the puzzle dashboard")
check(/Nothing to personalize yet/.test($("pz-personal").textContent), "no analysis yet: Personalized says so and points to Practice")
$("pz-mode-practice").click()
{
  const pins = $("pz-practice").querySelector('.pz-theme[data-concept="pin"]')
  check(visible("pz-practice") && pins && /Pins/.test(pins.textContent), "Practice lists themes (Pins: " + (pins && pins.textContent) + ")")
  pins.click()
  await waitFor(() => visible("pz-solver") && board.state.moveInputCallback, "solver open", 20000)
  // Check highlight: a king in check glows red on its square, in its own layer (markers can't erase it)
  {
    const svg = document.querySelector("#board svg")
    const layer = svg && svg.querySelector(".markers-layer .check-highlight")
    check(!!layer && !layer.querySelector(".check-square"), "check highlight: layer present, nothing lit at the start")
    const before = board.getPosition()
    {
      await board.setPosition("4k3/8/8/8/8/8/8/4R1K1 b - - 0 1", false)
      const lit = [...layer.querySelectorAll(".check-square")].map(r => r.dataset.square)
      if (board.removeMarkers) board.removeMarkers()
      check(lit.join() === "e8" && layer.querySelectorAll(".check-square").length === 1,
        "check highlight: the checked king's square (e8) is lit and survives clearing markers: " + lit.join())
      await board.setPosition(before, false)
      check(!layer.querySelector(".check-square"), "check highlight: gone once the king is out of check")
    }
  }
  check(/(White|Black) to move/.test($("pz-info").textContent) && /^Puzzle 1$/.test($("pz-counter").textContent) &&
    $("pz-set").querySelectorAll(".pz-item").length >= 3, "puzzles 4+5: general practice works without analysis and opens the solver: " +
    $("pz-info").textContent.replace(/\s+/g, " ").trim())
  // Practice → a chosen theme names the type before solving (only Mixed practice hides it)
  {
    const c = $("pz-info").querySelector(".pz-concept")
    check(!!c && /pin/i.test(c.textContent) && !/Mixed/.test($("pz-title").textContent) &&
      /pin/i.test($("pz-set").querySelector(".pz-item.active").textContent),
      "Practice → Pins shows the type before solving: " + (c && c.textContent))
  }
  const level = $("pz-info").querySelector(".pz-level")
  check(level && /^Aimed at your pins? level|^Aimed at your (tactics|pattern recognition|calculation) level/.test(level.textContent),
    "calibration: the first card says which level the set is aimed at: " + (level && level.textContent))
  const id = puzzleNow()
  const p = await puzzleData(id)
  check(p.critical_moves.length >= 1 && p.expected_solution_length === p.steps.length, `puzzle ${id}: ${p.critical_moves.length} critical of ${p.steps.length} moves`)
  await wrongMove(p)
  $("pz-hint").click()
  check(/💡/.test($("pz-feedback").textContent), "hint shows the idea: " + $("pz-feedback").textContent)
  for (const [i, step] of p.steps.entries()) {
    await playStep(step, i === p.steps.length - 1)
    if (i === 0 && p.steps.length > 1) await reviewMidPuzzle()
  }
  check(/counts as missed/.test($("pz-feedback").textContent) && visible("pz-explain") && /Solution:/.test($("pz-explain").textContent),
    "after a mistake the finished puzzle counts as missed and shows the solution + explanation")
  await sleep(300)
  const stats = (await puzzleData(id)).stats
  check(stats.attempts === 1 && stats.last_result === "failed", "puzzles 6: completing a puzzle records the result (" + JSON.stringify(stats).slice(0, 90) + ")")
  // the next one, solved cleanly
  $("pz-next").click()
  await waitFor(() => /^Puzzle 2$/.test($("pz-counter").textContent) && board.state.moveInputCallback, "second puzzle")
  const p2 = await puzzleData(puzzleNow())
  for (const [i, step] of p2.steps.entries()) {
    await playStep(step, i === p2.steps.length - 1)
    if (i === 0 && p2.steps.length > 1) await reviewMidPuzzle()
  }
  check(/✓ Solved/.test($("pz-feedback").textContent), "a clean solve: " + $("pz-feedback").textContent)
  // ← → review what was played (nothing is erased); afterwards other moves can be tried
  await sleep(300)
  const endFen = board.getPosition()
  check(!$("nav-prev").disabled && $("nav-next").disabled && /\d+\/\d+/.test($("nav-label").textContent),
    "nav: ← is available once puzzle moves were played: " + $("nav-label").textContent)
  $("nav-prev").click()
  await waitFor(() => board.getPosition() !== endFen, "nav: ← shows the previous puzzle position", 5000)
  check($("board-nav").classList.contains("reviewing") && !$("nav-next").disabled, "nav: reviewing an earlier position, → available")
  $("nav-next").click()
  await waitFor(() => board.getPosition() === endFen, "nav: → returns to the latest position", 5000)
  check(board.isMoveInputEnabled() && /try other moves/.test($("pz-explain").textContent),
    "nav: after the puzzle the board can be explored (moves don't count)")
  // right-click + drag: a calculation arrow, never a move
  const sq = s => board.context.querySelector(`[data-square="${s}"]`)
  const fire = (s, type, button) => sq(s).dispatchEvent(new w.MouseEvent(type, {button, bubbles: true, cancelable: true}))
  const files = "abcdefgh"
  const all = [...files].flatMap(f => [1, 2, 3, 4, 5, 6, 7, 8].map(r => f + r))
  const occupied = all.find(s => board.getPiece(s))
  const empty = all.filter(s => !board.getPiece(s) && s !== occupied)
  const feedbackBefore = $("pz-feedback").textContent
  fire(occupied, "mousedown", 2); fire(empty[0], "mousemove", 2); fire(empty[0], "mouseup", 2)
  fire(empty[1], "mousedown", 2); fire(empty[2], "mousemove", 2); fire(empty[2], "mouseup", 2)
  check(board.getArrows().length === 2 && board.getPosition() === endFen && $("pz-feedback").textContent === feedbackBefore,
    `arrows: right-drag draws calculation arrows (${occupied}->${empty[0]}) and moves nothing`)
  fire(empty[1], "mousedown", 2); fire(empty[2], "mouseup", 2)
  check(board.getArrows().length === 1, "arrows: drawing the same arrow again removes it")
  fire(empty[3], "mousedown", 0); fire(empty[3], "mouseup", 0)
  check(board.getArrows().length === 0 && board.getPosition() === endFen, "arrows: a left click clears them")
  // The mid-puzzle ← check needs a puzzle of 2+ moves. Which puzzles come first depends on the
  // library, so when both above were one-movers, open a longer one from the same set.
  if (!midReviewed) {
    let multi = null
    for (const item of $("pz-set").querySelectorAll(".pz-item")) {
      if (item.classList.contains("solved") || item.classList.contains("failed")) continue
      const d = await puzzleData(item.dataset.id)
      if (d.steps.length > 1) { multi = {item, d}; break }
    }
    check(!!multi, "nav: the set has a puzzle of 2+ moves for the mid-puzzle check")
    multi.item.click()
    await waitFor(() => puzzleNow() === multi.d.id && board.state.moveInputCallback, "longer puzzle open", 10000)
    await playStep(multi.d.steps[0], false)
    await reviewMidPuzzle()
  }
  check(midReviewed, "nav: mid-puzzle review was exercised")
  $("pz-back").click()
  await waitFor(() => visible("pz-dashboard"), "back to the dashboard")
}
// T) Training subtab: short games against a bot; the idea stays hidden until the report
{
  let lastSeg = null
  const appFetch = globalThis.fetch
  globalThis.fetch = async (...a) => {
    const r = await appFetch(...a)
    if (String(a[0]).includes("puzzles/training")) r.clone().json().then(j => { if (j && j.segment) lastSeg = j.segment }).catch(() => {})
    return r
  }
  const IDEA = /fork|\bpins?\b|skewer|mate\b|sacrific|deflect|discover|smother|back.rank|opposition|zwischenzug|desperado|hint/i
  $("pz-mode-training").click()
  await waitFor(() => visible("pz-training") && $("pz-training").querySelector(".tr-mode"), "training home", 20000)
  check([...$("pz-training").querySelectorAll(".tr-mode")].map(b => b.dataset.mode).join() === "opening,middlegame,endgame,mixed",
    "training 1: Beginning Game, Middlegame, Endgame and Mixed Games")
  check(/Nothing yet/.test($("pz-training").textContent), "training 2: a new learner's profile says there is no evidence yet")
  $("pz-training").querySelector('.tr-mode[data-mode="mixed"]').click()
  await waitFor(() => visible("pz-trainer") && lastSeg && board.state.moveInputCallback, "training segment open", 20000)
  check(!visible("pz-dashboard") && !visible("pz-solver") && /You play (White|Black)/.test($("tr-info").textContent),
    "training 3: the board with an info line (" + $("tr-info").textContent + ")")
  check(!IDEA.test($("tr-info").textContent + $("tr-feedback").textContent) && !visible("tr-report"),
    "training 4: nothing names the position's idea while playing")
  const firstSeg = lastSeg.id
  let plies = 0
  while (!visible("tr-continue") && plies < 12) {
    await waitFor(() => (board.state.moveInputCallback && lastSeg && !lastSeg.end_reason) || visible("tr-continue")
      || visible("tr-report"), "training: your move", 30000)
    if (visible("tr-continue") || visible("tr-report")) break
    const chess = new Chess(lastSeg.fen)
    const m = chess.moves({verbose: true})[0]
    const before = lastSeg.learner_moves
    check(move(m.from, m.to) === "ok", `training move ${m.san} accepted by the board`)
    await waitFor(() => (lastSeg && lastSeg.learner_moves > before) || visible("tr-continue"), "training: move sent", 20000)
    if (plies === 1) check(!IDEA.test($("tr-info").textContent + $("tr-feedback").textContent), "training 5: still no idea mid-segment")
    plies++
  }
  await waitFor(() => visible("tr-continue") && visible("tr-report"), "training report", 60000)
  const report = $("tr-report").textContent
  check(/Accuracy|moves played/.test(report) && /What went well/.test(report) && /What to work on/.test(report),
    "training 6: the report shows accuracy, what went well and what to work on")
  check(lastSeg.learner_moves >= 1 && lastSeg.learner_moves <= 10, `training 7: a segment of ${lastSeg.learner_moves} moves (5–10, or the game ended)`)
  $("tr-continue").click()
  await waitFor(() => lastSeg && lastSeg.id !== firstSeg && !lastSeg.finished && board.state.moveInputCallback, "training: next segment", 20000)
  check(!visible("tr-report") && visible("pz-trainer"), "training 8: Continue starts the next segment at once")
  $("tr-back").click()
  await waitFor(() => visible("pz-dashboard") && visible("pz-training") && /moves analysed/.test($("pz-training").textContent), "training: back home", 20000)
  check(/\b1 segment\b/.test($("pz-training").textContent), "training 9: the profile counts the finished segment (not the open one)")
  globalThis.fetch = appFetch
}
$("tab-lessons").click()
await waitFor(() => visible("lesson-pane") && !visible("puzzles-pane"), "lessons again")

// 0b) Onboarding: a short optional card for a new learner, never a wall
await waitFor(() => document.querySelector(".onboarding-card"), "onboarding card", 10000)
const onb = document.querySelector(".onboarding-card")
check(onb.querySelector(".onb-rating") && onb.querySelectorAll(".onb-experience .chip").length === 5 &&
  onb.querySelector(".onb-username") && onb.querySelectorAll(".onb-goals .chip").length >= 6,
  "onboarding asks rating or experience, goals, optional Chess.com username")
onb.querySelector(".onb-save").click()
check(/rating|description/.test(onb.querySelector(".onb-error").textContent), "onboarding: nothing chosen → a clear hint")
onb.querySelector(".onb-skip").click()
await waitFor(() => !document.querySelector(".onboarding-card"), "onboarding skipped")
check(/No problem/.test(lastMsgs(1)[0]), "skipping onboarding is fine: " + lastMsgs(1)[0])
// Settings: voice, speed, what's read aloud automatically; without any voice the text just stays
$("btn-settings").click()
check(!$("settings-panel").classList.contains("hidden"), "settings panel opens")
check([...$("speech-rate").options].map(o => o.value).join() === "slow,normal,fast", "speed: slow / normal / fast")
check(document.querySelectorAll("#settings-panel [data-kind]").length === 5, "five kinds of auto-read to choose from")
check(/docs\/TTS\.md|browser/.test($("voice-status").textContent), "voice status explains the voice: " + $("voice-status").textContent.slice(0, 90))
$("btn-settings").click()
check($("settings-panel").classList.contains("hidden"), "settings panel closes")

// A lesson button inside the sidebar card of a plan (titles are shown without the plan name)
const lessonIn = (plan, lesson) => {
  const card = [...document.querySelectorAll(".course.plan")].find(c => plan.test(c.querySelector("h3").textContent))
  return card && [...card.querySelectorAll(".lesson-item")].find(b => lesson.test(b.textContent))
}

// Helpers for the demonstrate → practise flow
const continueShown = () => !$("btn-continue").classList.contains("hidden")
async function finishDemo(label) {
  // demonstrations play by themselves; "Watch again" + Continue appear when done
  await waitFor(() => !$("btn-play").classList.contains("hidden") && continueShown(), label, 40000)
}

// 0c) "What's a fork?" is answered at once from the library — no plan built
{
  const plansBefore = (await planIds()).length
  $("chat-input").value = "What is a fork?"
  await sendChat()
  await waitFor(() => document.querySelector("#messages .quick-answer"), "quick answer", 10000)
  const qa = document.querySelector("#messages .quick-answer")
  check(/Fork/.test(qa.textContent) && /attacking two/.test(qa.textContent) && qa.querySelector(".chip"),
    "a definition question gets an instant library answer with a lesson offer: " + qa.textContent.slice(0, 70))
  check((await planIds()).length === plansBefore, "a definition question doesn't build a plan")
}

// 1) Planner from the chat box (before any lesson)
$("chat-input").value = "I want to learn the Sicilian"
await sendChat()
await waitFor(() => [...document.querySelectorAll("#messages button")].some(b => /Start the first lesson/.test(b.textContent)), "plan message")
await waitFor(() => document.querySelector(".course.plan"), "plan course")
check(/Sicilian/.test(document.querySelector(".course.plan").textContent), "plan listed in the sidebar")
check(lastMsgs(1)[0].includes("Sicilian"), "plan message lists units")
check(![...document.querySelectorAll(".course.plan .badge")].some(b => /available/.test(b.textContent)), "no 'available' badges")

// 2) "I want to learn the Italian Game" builds the lesson on request
$("chat-input").value = "I want to learn the Italian Game"
await sendChat()
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /Italian/.test(h.textContent)), "Italian plan")
const itLesson = lessonIn(/Italian/, /Moves and ideas/)
check(!!itLesson, "Italian plan has 'moves and ideas'")
itLesson.click()
await waitFor(() => /Italian/.test($("lesson-title").textContent) && $("step-indicator").textContent.startsWith("Step 1"), "Italian lesson start")
await sleep(300)
check(!board.isMoveInputEnabled(), "teach step: the board is locked (pieces can't be dragged)")
check(move("e2", "e4") === "input-disabled", "teach step: no silent ungraded moves")
board.context.dispatchEvent(new w.Event("pointerdown", {bubbles: true}))
check(/press/i.test($("board-status").textContent), "clicking the locked board says what to do: " + $("board-status").textContent)

// 3) Demonstration plays by itself, then leads straight into practice (the reported bug)
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 2"), "step 2")
check(!board.isMoveInputEnabled(), "during the demonstration the board is locked")
await waitFor(() => $("board-nav").classList.contains("locked"), "navigation locked while the AI demonstrates", 20000)
check($("nav-prev").disabled && $("nav-next").disabled, "nav: during the demonstration ← → are disabled")
await finishDemo("demo complete")
{
  await sleep(200)
  const demoEnd = board.getPosition()
  check(!$("board-nav").classList.contains("locked") && !$("nav-prev").disabled,
    "nav: after the demonstration ← is enabled: " + $("nav-label").textContent)
  const label = $("nav-label").textContent
  let steps = 0
  while (!$("nav-prev").disabled && steps < 40) {
    const before = board.getPosition()
    $("nav-prev").click()
    await waitFor(() => board.getPosition() !== before, "nav: ← steps back", 5000)
    steps++
  }
  check(steps >= 2 && /^Start/.test($("nav-label").textContent), `nav: ← walks back through the demonstrated line (${steps} moves, from "${label}")`)
  while (!$("nav-next").disabled) { $("nav-next").click(); await sleep(50) }
  await waitFor(() => board.getPosition() === demoEnd, "nav: → returns to the end of the line", 5000)
  check($("nav-label").textContent === label, "nav: → walks forward to the same final move")
  board.context.dispatchEvent(new w.KeyboardEvent("keydown", {key: "ArrowLeft", bubbles: true}))
  await waitFor(() => board.getPosition() !== demoEnd, "nav: the ← key works too", 5000)
  document.dispatchEvent(new w.KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}))
  await waitFor(() => board.getPosition() === demoEnd, "nav: the → key works too", 5000)
}
check(!board.isMoveInputEnabled(), "after the demonstration the board stays locked (no dead moves)")
check(/Your turn/.test($("btn-continue").textContent), "Continue says 'Your turn — practise it': " + $("btn-continue").textContent)
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 3") && /Your move/.test($("board-status").textContent), "step 3 (exercise)")
check(board.isMoveInputEnabled(), "exercise: the board takes moves")
const r = move("e2", "e4"); console.log("  e2e4 ->", r)
check(r === "ok", "exercise: 1.e4 playable")
await waitFor(() => continueShown() || /Try again/.test($("board-status").textContent), "e4 graded", 60000)
check(continueShown(), "1.e4 accepted")
console.log("  feedback:", $("feedback-slot").textContent.trim().slice(0, 140))
check(!/plies|teacher:/.test($("feedback-slot").textContent), "feedback card has no engine internals")
const qwenOn = /AI teacher:/.test($("health").title)
if (qwenOn) {
  await waitFor(() => !document.querySelector(".teacher-label.typing"), "explanation stream done", 90000)
  const expl = document.querySelector(".explanation").textContent
  check(expl.length > 20 && !/<think>|<\/think>/.test(expl), "explanation shown without <think> tags: " + expl.slice(0, 60))
  // Chat answer streams into a bubble.
  $("chat-input").value = "Why is the pawn good on e4?"
  await sendChat()
  await waitFor(() => { const m = [...document.querySelectorAll("#messages .msg.assistant")].pop(); return m && !m.classList.contains("typing") && m.textContent.length > 20 }, "chat stream", 90000)
  check(true, "chat reply streamed: " + [...document.querySelectorAll("#messages .msg.assistant")].pop().textContent.slice(0, 50))
}
// Moves in the tutor's text are marked for read-aloud highlighting
check(document.querySelectorAll("#messages .mv").length > 0, "moves/squares in messages are marked for read-aloud")

// 5) Start the plan's opening lesson and play the first exercise as Black
const sicilianLesson = lessonIn(/Sicilian/, /Moves and ideas/)
sicilianLesson.click()
await waitFor(() => /Sicilian/.test($("lesson-title").textContent), "plan lesson start")
$("btn-continue").click()
await finishDemo("plan demo")
$("btn-continue").click()
await waitFor(() => /Exercise/.test(lastMsgs(1)[0]) && /Your move/.test($("board-status").textContent), "plan exercise")
console.log("  prompt:", lastMsgs(1)[0])
check(move("c7", "c5") === "ok", "plan exercise: 1...c5 playable")
await waitFor(() => !$("btn-continue").classList.contains("hidden"), "c5 accepted", 60000)
check(true, "1...c5 accepted")

// The chat button is disabled while a plan is being built; wait for it like a user would.
async function sendChat() {
  await waitFor(() => !$("btn-chat").disabled, "chat button enabled")
  $("btn-chat").click()
}

// 6) Chat routing: "I want to learn ..." makes a plan even mid-lesson
$("chat-input").value = "I want to learn knight forks"
await sendChat()
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /fork/i.test(h.textContent)), "fork plan")
check(true, "chat 'I want to learn…' creates a plan")

// 7) The user's report: "I want to learn the smothered mate" must teach smothered mate
$("chat-input").value = "I want to learn the smothered mate"
await sendChat()
await waitFor(() => /Learn: Smothered mate/.test(lastMsgs(1)[0]), "smothered plan message")
const smPlan = lastMsgs(1)[0]
check(/Smothered mate/.test(smPlan) && !/Fork|Pin|Skewer/.test(smPlan), "smothered-mate plan contains only smothered mate")
// the sidebar course list refreshes after the chat reply: wait for it (a race, not a missing lesson)
await waitFor(() => lessonIn(/Smothered mate/, /Learn the pattern/), "smothered-mate lesson in the sidebar", 15000).catch(() => null)
const smLesson = lessonIn(/Smothered mate/, /Learn the pattern/)
check(!!smLesson, "plan has 'Smothered mate: learn the pattern'")
smLesson.click()
await waitFor(() => /Smothered/.test($("lesson-title").textContent), "smothered lesson start")
$("btn-continue").click()                                   // teach -> demo of the opponent's move
await finishDemo("puzzle demo")
$("btn-continue").click()
await waitFor(() => /Exercise/.test(lastMsgs(1)[0]) && /Your move/.test($("board-status").textContent), "puzzle exercise")
console.log("  prompt:", lastMsgs(1)[0])
check(/find the best move/i.test(lastMsgs(1)[0]) && !/smother/i.test(lastMsgs(1)[0]),
  "no spoiler: the exercise asks for the best move, not for 'the smothered mate'")
check(move("h6", "f5") === "ok", "wrong knight move can be played")
await waitFor(() => /Try again/i.test($("board-status").textContent) || !$("btn-continue").classList.contains("hidden"), "wrong move graded", 60000)
check($("btn-continue").classList.contains("hidden"), "wrong move (Nf5) is not accepted")
await waitFor(() => board.getPiece("h6") && !board.getPiece("f5"), "knight back on h6 after wrong move", 20000)
check(move("h6", "f7") === "ok", "Nf7# playable")
await waitFor(() => !$("btn-continue").classList.contains("hidden"), "Nf7# accepted", 60000)
check(/Checkmate!/.test($("feedback-slot").textContent + lastMsgs(2).join(" ")), "Nf7# accepted as checkmate")

// 7b) An opening is a tree: the plan teaches ONE verified branch; the other branches are chips
$("chat-input").value = "I want to learn the Dutch Defense"
await sendChat()
await waitFor(() => /Learn: Dutch Defense/.test(lastMsgs(1)[0]), "Dutch plan message", 30000)
{
  const msg = [...document.querySelectorAll("#messages .msg")].pop()
  check(/one branch of the Dutch Defense tree/.test(msg.textContent), "opening plan names the branch it teaches")
  check(/Other branches of the Dutch Defense/.test(msg.textContent), "opening plan offers the other branches")
  const chips = [...msg.querySelectorAll(".suggestions .chip")]
  const stonewall = chips.find(c => /Stonewall/.test(c.textContent))
  check(chips.length >= 3 && !!stonewall && !/Dutch Defense:/.test(stonewall.textContent),
    "branch chips use the variation name: " + chips.map(c => c.textContent).join(" | "))
  stonewall.click()
  await waitFor(() => /Learn: Dutch Defense: Stonewall/.test(lastMsgs(1)[0]), "Stonewall plan from the chip", 30000)
  check(true, "a branch chip plans that branch")
}

// 8) Unknown subject: honest answer + suggestions, no substitute plan
const plansBefore = (await planIds()).length
$("chat-input").value = "I want to learn the zorblax gambit"
await sendChat()
await waitFor(() => /don't have verified lessons/.test(lastMsgs(1)[0]), "unknown-subject answer", 90000)
check(document.querySelectorAll("#messages .suggestions").length > 0, "unknown subject: suggestion chips shown")
check((await planIds()).length === plansBefore, "unknown subject: no substitute plan created")

// 8b) Ambiguous request: a question with genuinely different readings, never a guess
$("chat-input").value = "I want to learn knight and bishop endgames"
await sendChat()
await waitFor(() => document.querySelector(".clarify-card"), "clarification question", 30000)
const card = [...document.querySelectorAll(".clarify-card")].pop()
check(/What do you mean by “knight and bishop endgames”\?/.test(card.textContent), "question names the ambiguous words")
const opts = [...card.querySelectorAll(".clarify-options .chip")]
check(opts.length >= 3 && /Something else/.test(opts[opts.length - 1].textContent), "2+ readings plus 'Something else'")
opts[opts.length - 1].click()
card.querySelector(".clarify-other button").click()               // nothing typed
check(/few words/.test(card.querySelector(".clarify-note").textContent), "'Something else' needs a few words")
const versus = opts.find(b => /against/.test(b.textContent))
check(!!versus, "a 'knight against bishop' reading is offered")
versus.click()
await waitFor(() => [...document.querySelectorAll("#messages .plan-badge")].length, "custom plan", 60000)
const planMsg = [...document.querySelectorAll("#messages .msg.assistant")].pop()
check(/against/i.test(planMsg.textContent) && /Understood as/.test(planMsg.textContent), "plan follows the chosen reading")
check(/verified/.test(planMsg.querySelector(".plan-badge").textContent), "custom plan shows it was verified")
planMsg.querySelector(".understood .link-btn").click()             // "Ask me again"
await waitFor(() => document.querySelectorAll(".clarify-card").length === 2, "asked again on request", 30000)

// 9) Knowledge Library: "Show me checkmates" mid-lesson -> verified examples as a lesson
$("chat-input").value = "Show me checkmates"
await sendChat()
await waitFor(() => /Learn: Checkmate/.test(lastMsgs(1)[0]), "library plan message", 30000)
check(/verified examples/.test([...document.querySelectorAll("#messages .msg")].pop().textContent),
      "chat 'Show me…' builds a lesson from the verified library")
await waitFor(() => lessonIn(/^Checkmate$/, /understand the idea/i), "library lesson in the sidebar", 15000)
const kLesson = lessonIn(/^Checkmate$/, /understand the idea/i)
check(!!kLesson, "library lesson listed in the sidebar")
kLesson.click()
await waitFor(() => /understand the idea/.test($("lesson-title").textContent), "library lesson start")
{
  const intro = [...document.querySelectorAll("#messages .msg")].pop().textContent
  check(/verified example/.test(intro), "intro explains the sequence: " + intro.slice(0, 90))
}
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 2"), "example 1 step")
check(/Example 1 of/.test(document.querySelectorAll("#messages .msg")[document.querySelectorAll("#messages .msg").length - 1].textContent) ||
      [...document.querySelectorAll("#messages .msg")].slice(-4).some(m => /Example 1 of/.test(m.textContent)), "example 1 is demonstrated first")
await finishDemo("example 1 demo")
$("btn-continue").click()
await waitFor(() => !$("btn-explain-example").classList.contains("hidden"), "explain-example button")
$("btn-explain-example").click()
await waitFor(() => { const m = [...document.querySelectorAll("#messages .msg.assistant")].pop(); return m && /^🧠/.test(m.textContent) && !m.classList.contains("typing") }, "example explanation", 90000)
check(true, "example explained: " + [...document.querySelectorAll("#messages .msg.assistant")].pop().textContent.slice(0, 60))
// the guided example — find the key move (read from the server's verified example). How many
// demonstrations come first depends on the learner (a beginner meeting a new idea sees two).
for (let i = 0; i < 10 && !/Your move/.test($("board-status").textContent); i++) {
  const at = $("step-indicator").textContent
  await waitFor(() => continueShown(), "continue (example 2)", 40000)
  $("btn-continue").click()
  await waitFor(() => $("step-indicator").textContent !== at, "next step (example 2)")
  await sleep(300)
}
await waitFor(() => /Your move/.test($("board-status").textContent), "example 2 exercise (at " + $("step-indicator").textContent + ")", 40000)
check($("btn-explain-example").classList.contains("hidden"), "no explain button while the exercise is unsolved")
$("btn-reveal").click()
await waitFor(() => /Solution/.test(lastMsgs(1)[0]), "solution shown")
check(/Solution: \S+/.test(lastMsgs(1)[0]), "exercise solution comes from the verified example: " + lastMsgs(1)[0])
// a question starting with "show me" stays in the chat
const before = (await planIds()).length
const assistantBefore = document.querySelectorAll("#messages .msg.assistant").length
$("chat-input").value = "show me why this move is bad"
await sendChat()
await waitFor(() => { const all = [...document.querySelectorAll("#messages .msg.assistant")]; const m = all.pop(); return all.length >= assistantBefore && m && !m.classList.contains("typing") && m.textContent.length > 10 }, "chat reply", 90000)
check((await planIds()).length === before, "'show me why…' is answered in the chat, not turned into a plan")
// Explanation requests (learner.help): only the explicit asks count. That is the one "Explain this
// example" click and the chat questions ("show me why…"; with Qwen on also "Why is the pawn good on
// e4?"). Instant feedback, automatic AI explanations, hints and revealed solutions never count.
{
  const prof = (await (await realFetch(new URL("api/profile", BASE))).json()).profile
  const ex = prof.stats.explanations || {}
  check(ex.explain === 1 && ex.question === (qwenOn ? 2 : 1) && ex.deeper === 0,
    "explanation requests: only the explicit asks are counted: " + JSON.stringify(ex))
}

// 10) Game Analysis: import Chess.com games, analyze with Stockfish, review, explain, train
const pgnHead = (white, link, fen) => `[Event "Live Chess"]\n[Site "Chess.com"]\n[Date "2026.09.20"]\n[White "${white}"]\n` +
  `[Black "RaanTest"]\n[Result "*"]\n[SetUp "1"]\n[FEN "${fen}"]\n[TimeControl "600"]\n[Link "https://www.chess.com/game/live/${link}"]\n\n`
const GAME1 = pgnHead("endgamer", 900000001, "8/8/8/3n3R/1pk3p1/6K1/8/8 w - - 0 53") + "53. Kxg4 b3 54. Rh1 b2 55. Rb1 Kc3 56. Kf3 Nc7 *\n"
const GAME2 = pgnHead("rookie", 900000002, "8/4R3/4p3/2kpP3/3n1K2/8/8/8 w - - 4 69") + "69. Ke3 Nc6 70. Rc7+ Kb6 71. Rxc6+ Kxc6 72. Kd4 *\n"
const gameIds = async () => (await (await realFetch(new URL("api/games", BASE))).json()).games.map(g => g.id)
const gamesBefore = new Set(await gameIds())
for (const id of ["chesscom-900000001", "chesscom-900000002"]) {
  if (gamesBefore.has(id)) await realFetch(new URL(`api/games/${id}`, BASE), {method: "DELETE"})
}
const lessonMsgsBefore = document.querySelectorAll("#messages .msg").length
$("tab-games").click()
await waitFor(() => !$("analysis-pane").classList.contains("hidden"), "Game Analysis tab")
check($("lessons-side").classList.contains("hidden") && !$("games-side").classList.contains("hidden"), "sidebar shows your games")
check(document.querySelector(".lesson-pane:not(.analysis-pane)").classList.contains("hidden"), "lesson pane hidden while analyzing games")
check(!board || !board.isMoveInputEnabled(), "board is locked in Game Analysis")
// fetch by username: the sandbox can't reach api.chess.com, so this checks the flow reports it clearly
check(/Load my last 100 games/.test($("ga-fetch-btn").textContent), "fetch loads the last 100 games: " + $("ga-fetch-btn").textContent)
check(!$("ga-paste").open, "pasting PGN is the fallback, folded away")
$("ga-username").value = ""
$("ga-fetch-btn").click()
await waitFor(() => /username first/.test($("ga-status").textContent), "asks for a username")
$("ga-username").value = "RaanTest"
$("ga-fetch-btn").click()
await waitFor(() => $("ga-status").classList.contains("error") && /Chess\.com/.test($("ga-status").textContent) &&
  !$("ga-fetch-btn").disabled, "fetch error shown", 60000)
check(/Couldn't reach Chess\.com|didn't answer|no Chess\.com player|rate-limiting/.test($("ga-status").textContent),
  "a failed fetch explains why: " + $("ga-status").textContent.slice(0, 90))
try { w.localStorage.removeItem("chessai.chesscomUsername") } catch (_) { /* none */ }
$("ga-username").value = ""
$("ga-pgn").value = GAME1
$("ga-import-btn").click()
await waitFor(() => $("ga-status").querySelector(".chip"), "which-player question")
const chips = [...$("ga-status").querySelectorAll(".chip")].map(c => c.textContent)
check(chips.includes("RaanTest") && chips.includes("endgamer"), "one game without a username: asks which player you are")
$("ga-status").querySelectorAll(".chip")[chips.indexOf("RaanTest")].click()
await waitFor(() => /Analysis done/.test($("ga-status").textContent), "first game analyzed", 120000)
check($("ga-username").value === "RaanTest", "the chosen player is remembered")
await waitFor(() => document.querySelector(".ga-history"), "history card")
check(/isn't enough game history/.test($("ga-overview").textContent) && !document.querySelector(".weakness.recurring"),
  "one game: explains there isn't enough history, no pattern claimed")
$("ga-pgn").value = GAME2
$("ga-import-btn").click()
await waitFor(() => /Analysis done/.test($("ga-status").textContent) && document.querySelector(".weakness.pattern.tier-occasional"), "second game analyzed", 120000)
const weakness = document.querySelector(".weakness.pattern.tier-occasional")
check(/Missed knight fork/.test(weakness.textContent) && /Found in both games/.test(weakness.textContent), "knight fork in 2 games: " + weakness.textContent.slice(0, 80))
check(!document.querySelector(".weakness.recurring") && /not a pattern yet/i.test($("ga-overview").textContent) &&
  /isn't enough game history/.test($("ga-overview").textContent), "2 games: seen twice, but not called a recurring pattern")
check(/verified example/.test(weakness.textContent), "pattern links to verified library examples")
check(document.querySelectorAll("#ga-games .game-item").length >= 2, "both games listed")
check($("ga-import").classList.contains("collapsed") && /Fetch or import games/.test($("ga-import").textContent),
  "the import form folds into one button once there are games")
// jump to where it happened
;[...weakness.querySelectorAll("button")].find(b => /Show the games/.test(b.textContent)).click()
const evidenceRows = weakness.querySelectorAll(".pattern-evidence:not(.hidden) .evidence-row")
check(evidenceRows.length === 2 && /move \d+: you played/.test(evidenceRows[0].textContent), "the pattern lists the games and moves where it happened")
evidenceRows[0].click()
await waitFor(() => !$("ga-review").classList.contains("hidden") && document.querySelector(".moment-headline"), "moment review")
check(/You missed a knight fork/.test(document.querySelector(".moment-headline").textContent), "moment headline names the tactic")
check(/^Move \d+\.\.\./.test(document.querySelector(".ga-card.moment h3").textContent), "Black's move titled 'Move N...': " + document.querySelector(".ga-card.moment h3").textContent)
check(document.querySelector(".engine-details") && !document.querySelector(".engine-details").open, "engine numbers folded away by default")
// the explanation can be read aloud, and its moves are tied to the positions they're about
const momentText = document.querySelector(".moment-text")
const momentMove = momentText && momentText.querySelector(".moment-why .mv")
check(!!momentMove && JSON.parse(momentText.dataset.fens).length > 2, "moment text: moves marked and linked to their positions")
await new Promise(r => setTimeout(r, 300))  // let "Position before" finish drawing
// a move (or a line of moves) in the review is shown by moving the pieces, not by highlighting
const placement = fen => fen.split(" ")[0]
const lineMoves = [...momentText.querySelectorAll(".mv-line")]
check(lineMoves.length > 0, "review moves are playable on the board: " + lineMoves.map(m => m.textContent).join(" "))
const reviewPos = board.getPosition()
const pointed = lineMoves[lineMoves.length - 1]
pointed.dispatchEvent(new w.MouseEvent("mouseover", {bubbles: true}))
const target = placement(pointed.chessLine.positions[pointed.chessLine.index + 1])
await waitFor(() => placement(board.getPosition()) === target, "pointing at a move plays it on the board")
check(!board.getMarkers().some(m => m.type.class === "marker-speech"), "pointing at " + pointed.textContent + " moves the piece instead of highlighting squares")
$("ga-title").dispatchEvent(new w.MouseEvent("mouseover", {bubbles: true}))
await waitFor(() => board.getPosition() === reviewPos, "moving away puts the reviewed position back", 5000)
check(true, "moving away puts the reviewed position back")
const squareRef = momentText.querySelector(".mv:not(.mv-line)")
if (squareRef) {
  squareRef.dispatchEvent(new w.MouseEvent("mouseover", {bubbles: true}))
  check(board.getMarkers().some(m => m.type.class === "marker-speech") && board.getPosition() === reviewPos,
    "a single square (" + squareRef.textContent + ") is still highlighted, nothing moves")
  $("ga-title").dispatchEvent(new w.MouseEvent("mouseover", {bubbles: true}))
}
const longest = lineMoves.find(m => m.chessLine.positions.length > 2)
if (longest) {
  longest.click()
  const end = longest.chessLine.positions
  await waitFor(() => placement(board.getPosition()) === placement(end[end.length - 1]), "clicking a line plays all of it", 10000)
  check(true, "clicking a line plays all " + (end.length - 1) + " moves on the board")
}
const fenBefore = board.getPosition()
;[...document.querySelectorAll(".moment-views .btn")].find(b => b.textContent === "Best move").click()
await waitFor(() => /Stockfish's move/.test($("board-status").textContent), "best move shown")
check(board.getPosition() !== fenBefore, "best move is played on the board")
;[...document.querySelectorAll(".moment-views .btn")].find(b => b.textContent === "Your move").click()
await waitFor(() => /You played/.test($("board-status").textContent), "my move shown")
document.querySelector(".moment-ask .btn").click()
await waitFor(() => { const a = document.querySelector(".moment-ai"); return a && !a.classList.contains("hidden") && a.textContent.length > 20 && !document.querySelector(".moment-ask .btn").disabled }, "moment explanation", 120000)
check(/fork|knight/i.test(document.querySelector(".moment-ai").textContent), "explanation: " + document.querySelector(".moment-ai").textContent.slice(0, 70))
// the moment says it happened in another game too (not a pattern yet) and offers practice
const related = document.querySelector(".moment-related")
check(!!related && /2 of your last 2 games/.test(related.textContent) && /not a pattern yet/.test(related.textContent),
  "moment: seen in 2 games, not called a pattern: " + (related ? related.textContent.slice(0, 80) : "none"))
const practise = [...document.querySelectorAll(".moment-related .btn")].find(b => /Practice Knight fork/.test(b.textContent))
check(!!practise, "the moment offers practice")
practise.click()
await waitFor(() => !document.querySelector(".lesson-pane:not(.analysis-pane)").classList.contains("hidden") &&
  /From your games/.test(lastMsgs(1)[0] || ""), "training plan in lessons view")
check(/From your games/.test(lastMsgs(1)[0]), "training plan built from your games: " + lastMsgs(1)[0].slice(0, 60))
check(document.querySelectorAll("#messages .msg").length > lessonMsgsBefore, "lesson conversation kept when switching tabs")
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /From your games/.test(h.textContent)), "training plan in the sidebar")
const own = lessonIn(/From your games/, /your own games/i)
check(!!own, "plan has a lesson with positions from your own games")
own.click()
await waitFor(() => /your own games/i.test($("lesson-title").textContent), "own-games lesson starts")
await sleep(300)
for (let i = 0; i < 4 && !/Your move/.test($("board-status").textContent); i++) {
  const at = $("step-indicator").textContent
  await waitFor(() => continueShown() || /Your move/.test($("board-status").textContent), "continue (own games)", 40000)
  if (/Your move/.test($("board-status").textContent)) break
  $("btn-continue").click()
  await waitFor(() => $("step-indicator").textContent !== at, "next step (own games)")
  await sleep(300)
}
check(/Your game against/.test(lastMsgs(1)[0]) && board.isMoveInputEnabled(), "exercise from your own game: " + lastMsgs(1)[0].slice(0, 70))

// 11) Game history: the last 10 games analyzed together
const forkAgain = (base, link) => base.replace(/game\/live\/\d+/, `game/live/${link}`)
const quiet = link => pgnHead("quietplayer", link, "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1") + "1... e5 2. Nf3 Nc6 *\n"
const MORE = [forkAgain(GAME1, 900000003), forkAgain(GAME2, 900000004),
  ...[5, 6, 7, 8, 9, 10].map(i => quiet(900000000 + i))].join("\n")
$("tab-games").click()
await waitFor(() => document.querySelector(".ga-history"), "history card again")
$("ga-import").querySelector(".ga-more").click()
$("ga-pgn").value = MORE
$("ga-import-btn").click()
await waitFor(() => /Imported 8 games/.test($("ga-status").textContent) && /Analysis done/.test($("ga-status").textContent), "8 more games analyzed", 300000)
await waitFor(() => document.querySelector(".history-count"), "history controls")
check(document.querySelector(".history-count").value === "25", "the last 25 games is the default first analysis")
check(/You can analyze \d+ more games? now/.test(document.querySelector(".quota-line").textContent),
  "the analysis allowance is shown: " + document.querySelector(".quota-line").textContent.slice(0, 70))
{
  const pick = document.querySelector(".history-count")
  pick.value = "10"
  pick.dispatchEvent(new w.Event("change"))
  await waitFor(() => document.querySelector(".history-count") && document.querySelector(".history-count").value === "10", "10 chosen")
}
document.querySelector(".history-run").click()
await waitFor(() => /Done: 10 games analyzed together/.test(document.querySelector(".history-status").textContent) &&
  document.querySelector(".weakness.recurring"), "history of 10 games", 120000)
const hist = $("ga-overview")
check(/Your last 10 games, analyzed together/.test(hist.querySelector(".ga-history h3").textContent), "history covers the last 10 games")
{
  const tc = hist.querySelector(".ga-training")
  check(tc && /Your Training/.test(tc.textContent) && /Main weakness/.test(tc.textContent) &&
    /Missed knight fork/.test(tc.querySelector(".training-title").textContent) &&
    /Found in 4 of your 10 analyzed games/.test(tc.textContent), "Your Training names the main weakness and why")
  check(tc && tc.querySelector(".training-start") && /Start training/.test(tc.querySelector(".training-start").textContent),
    "Your Training offers one clear next step")
}
check(!/isn't enough game history/.test(hist.textContent), "10 games: enough history")
const rec = hist.querySelector(".weakness.recurring")
check(/Missed knight fork/.test(rec.textContent) && /Found in 4 of 10 games/.test(rec.textContent),
  "recurring pattern across games: " + rec.textContent.slice(0, 80))
check(/Patterns that keep showing up/.test(hist.textContent) && /I found 1 recurring pattern in your last 10 games/.test(hist.textContent),
  "summary names the recurring pattern")
check(/10\s*games analyzed/.test(hist.querySelector(".history-stats").textContent), "overview: games analyzed")
check([...rec.querySelectorAll("button")].some(b => /Practice Knight fork/.test(b.textContent)), "pattern offers practice")
// a second run reuses every analysis
const rerun = (await (await realFetch(new URL("api/games/history/analyze", BASE), {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify({count: 10})})).text()).trim().split("\n").map(l => JSON.parse(l))
check(rerun[0].type === "select" && rerun[0].cached === 10 && rerun[0].to_analyze === 0 &&
  rerun.at(-1).report.recurring.includes("knight_fork"), "running it again reuses all 10 analyses")
// the Puzzle Library and targeted training: library puzzles first, easy -> hard, each with a reason
{
  const lib = await (await realFetch(new URL("api/puzzles", BASE))).json()
  check(lib.total > 300 && lib.by_type.tactic > 0 && lib.by_type.checkmate > 0, `puzzle library: ${lib.total} verified puzzles`)
  hist.querySelector(".ga-training .training-start").click()
  await waitFor(() => document.querySelector(".puzzle-why"), "targeted training plan", 180000)
  const why = [...document.querySelectorAll(".puzzle-why")].at(-1)
  const card = why.closest(".msg") || why.parentElement
  check(why.querySelectorAll("li").length === 5 && /Training: Missed knight fork/.test(card.textContent),
    "Start training: 5 targeted puzzles for the main weakness")
  check([...why.querySelectorAll("li")].every(li => /—/.test(li.textContent)) && /New to you/.test(why.textContent),
    "every puzzle says why it was chosen")
  check(/puzzle library/.test(card.textContent) && /not positions from your games/.test(card.textContent),
    "the plan says where the puzzles came from: " + card.textContent.slice(0, 160))
  // back to the games view: it re-renders the overview, so wait for the new history card
  const oldCount = document.querySelector(".history-count")
  $("tab-games").click()
  await waitFor(() => { const n = document.querySelector(".history-count"); const h = hist.querySelector(".ga-history h3")
    return n && n !== oldCount && h && /Your last 10 games/.test(h.textContent) &&
      [...hist.querySelectorAll("button")].some(b => /Explain my patterns/.test(b.textContent)) }, "back to the games view", 60000)
  await sleep(300)
}
// Puzzles tab, personalized: the weakness found in the games, results feeding back
{
  const msgCount = document.querySelectorAll("#messages .msg").length
  $("tab-puzzles").click()
  await waitFor(() => visible("pz-dashboard") && /Your main weakness/.test($("pz-personal").textContent), "personalized dashboard", 30000)
  $("pz-mode-personal").click()
  const profileBox = $("pz-personal").querySelector(".pz-profile")
  check(/Missed knight fork/.test(profileBox.textContent) && /of your last 10 analyzed games/.test(profileBox.textContent) &&
    /Recommended: 5 puzzles/.test(profileBox.textContent), "puzzles 3: personalized puzzles from the weakness data: " +
    profileBox.textContent.replace(/\s+/g, " ").trim().slice(0, 140))
  const before = (await (await realFetch(new URL("api/puzzles/dashboard", BASE))).json()).personalized.main
  profileBox.querySelector("button").click()
  await waitFor(() => visible("pz-solver") && board.state.moveInputCallback, "personalized set", 180000)
  {
    // a personalized set keeps naming the identified type (only Mixed practice hides it)
    check(/Missed knight fork/.test($("pz-title").textContent) && /Knight fork/.test($("pz-info").textContent),
      "the personalized set names the weakness it trains: " + $("pz-title").textContent)
    const ids = [...$("pz-set").querySelectorAll(".pz-item")].map(b => b.dataset.id)
    const data = await Promise.all(ids.map(puzzleData))
    check(data.filter(d => d.concept).every(d => /fork/.test(d.concept) || d.role === "defend"),
      "the set trains the weakness: " + data.map(d => d.concept).join(", "))
  }
  {
    const info = $("pz-info").textContent.replace(/\s+/g, " ").trim()
    const items = [...$("pz-set").querySelectorAll(".pz-item")].map(b => b.textContent.replace(/\s+/g, " ").trim())
    check(/Why this puzzle:/.test(info) && $("pz-info").querySelectorAll(".pz-why").length === 1,
      "every personalized puzzle says why, in one line: " + info.slice(info.indexOf("Why")))
    const mine = /Your game/.test(items[0])
    check(!mine || /Your own game/.test(info), "set order: " + items.join(" | ") +
      (mine ? " (opens with the position from your own game)" : " (no game moment passed the Stockfish re-check)"))
  }
  // half-way through the first puzzle: go to Analysis and Lessons, then come back
  const id = puzzleNow()
  const p = await puzzleData(id)
  if (p.steps.length > 1) await playStep(p.steps[0], false)
  const fen = board.getPosition()
  $("tab-games").click()
  await waitFor(() => visible("analysis-pane"), "analysis tab")
  await sleep(500)
  $("tab-lessons").click()
  await waitFor(() => visible("lesson-pane"), "lessons tab")
  check(document.querySelectorAll("#messages .msg").length === msgCount, "the lesson is untouched by the other tabs")
  $("tab-puzzles").click()
  await waitFor(() => visible("pz-solver") && board.state.moveInputCallback, "puzzle again")
  await sleep(300)
  check(puzzleNow() === id && board.getPosition() === fen && /^Puzzle 1$/.test($("pz-counter").textContent),
    "puzzles 8: switching between Analysis, Lessons and Puzzles keeps the half-solved puzzle")
  // two missed puzzles -> the weakness climbs and says how puzzles are going
  $("pz-solution").click()
  await waitFor(() => visible("pz-next"), "solution shown", 20000)
  $("pz-next").click()
  await waitFor(() => /^Puzzle 2$/.test($("pz-counter").textContent) && board.state.moveInputCallback, "second personalized puzzle")
  $("pz-solution").click()
  await waitFor(() => visible("pz-next"), "solution shown", 20000)
  $("pz-back").click()
  await waitFor(() => visible("pz-dashboard") && /Puzzles lately: 0 of 2 solved/.test($("pz-personal").textContent), "dashboard updated", 20000)
  const after = (await (await realFetch(new URL("api/puzzles/dashboard", BASE))).json()).personalized.main
  check(after.key === before.key && after.priority > before.priority && /Puzzles lately: 0 of 2/.test(after.progress),
    `puzzles 7: results change personalization (priority ${before.priority} -> ${after.priority})`)
  $("tab-games").click()
  await waitFor(() => visible("analysis-pane") && document.querySelector(".history-count"), "games again", 60000)
  await sleep(500)
}
// custom count: fewer than 10 is refused in the browser
const sel = document.querySelector(".history-count")
sel.value = "custom"
sel.dispatchEvent(new w.Event("change"))
document.querySelector(".history-custom").value = "9"
document.querySelector(".history-run").click()
check(/at least 10/.test(document.querySelector(".history-error").textContent), "custom count below 10 is refused")
// the tutor explains the findings (Qwen if running, else the verified summary)
const explainBtn = [...hist.querySelectorAll("button")].find(b => /Explain my patterns/.test(b.textContent))
explainBtn.click()
await waitFor(() => { const o = hist.querySelector(".history-explain"); return o && o.textContent.length > 30 && !explainBtn.disabled }, "history explanation", 120000)
check(/knight fork/i.test(hist.querySelector(".history-explain").textContent), "explanation: " + hist.querySelector(".history-explain").textContent.slice(0, 80))
// choose your own games: filter, tick, and exactly those are analyzed together
{
  const picker = () => document.querySelector(".game-picker")
  picker().open = true
  const search = picker().querySelector(".pf-text")
  search.value = "quietplayer"
  search.dispatchEvent(new w.Event("input"))
  check(picker().querySelectorAll(".picker-row").length === 6 && /Showing 6 of 10/.test(picker().textContent),
    "filtering by opponent shows their 6 games")
  for (const box of [...picker().querySelectorAll(".picker-row input")].slice(0, 3)) {
    box.checked = true
    box.dispatchEvent(new w.Event("change"))
  }
  check(/3 games selected \(3 already analyzed\)\. Nothing new to analyze/.test(picker().querySelector(".picker-summary").textContent),
    "selection summary: " + picker().querySelector(".picker-summary").textContent)
  check(!picker().querySelector(".picker-run").disabled, "analyze selected is enabled")
  picker().querySelector(".picker-run").click()
  await waitFor(() => /Your 3 chosen games, analyzed together/.test($("ga-overview").querySelector(".ga-history h3").textContent),
    "report covers exactly the chosen games", 60000)
  check(/3\s*games analyzed/.test($("ga-overview").querySelector(".history-stats").textContent), "chosen set: 3 games analyzed")
}
try { w.localStorage.removeItem("chessai.historyCount"); w.localStorage.removeItem("chessai.chosenGames") } catch (_) { /* none */ }
for (const id of await gameIds()) if (!gamesBefore.has(id)) await realFetch(new URL(`api/games/${id}`, BASE), {method: "DELETE"})

// Mixed practice (near the end: revealing solutions adds puzzle-based weaknesses): the idea stays hidden until the puzzle is over; the session never ends by itself
{
  $("tab-puzzles").click()
  await waitFor(() => visible("pz-dashboard") || visible("pz-solver"), "puzzles tab")
  if (visible("pz-solver")) { $("pz-back").click(); await waitFor(() => visible("pz-dashboard"), "dashboard") }
  $("pz-mode-practice").click()
  const mixed = $("pz-practice").querySelector('.pz-theme[data-concept="tactics"]')
  check(!!mixed, "Practice offers Mixed tactics")
  mixed.click()
  await waitFor(() => visible("pz-solver") && board.state.moveInputCallback && /^Puzzle 1$/.test($("pz-counter").textContent),
    "mixed set", 60000)
  const before = $("pz-title").textContent + " " + $("pz-info").textContent + " " + $("pz-set").textContent
  check(/Mixed practice/.test($("pz-title").textContent) && !$("pz-info").querySelector(".pz-concept") &&
    /Find the best move/.test($("pz-info").textContent) && $("pz-info").querySelector(".pz-meta span"),
    "mixed: no concept before solving, difficulty still shown: " + before.replace(/\s+/g, " ").trim().slice(0, 100))
  $("pz-solution").click()
  await waitFor(() => visible("pz-next") && $("pz-info").querySelector(".pz-concept"), "mixed: solution shown", 20000)
  const concept = $("pz-info").querySelector(".pz-concept").textContent
  check(concept && !before.includes(concept) && $("pz-set").querySelector(".pz-item.active").textContent.includes(concept),
    `mixed: the concept (${concept}) is revealed only after the puzzle`)
  check(!!$("pz-explain").querySelector(".pz-auto"), "continuous: a countdown to the next puzzle: " +
    ($("pz-explain").querySelector(".pz-auto") || {}).textContent)
  // no click: the next puzzle loads by itself
  await waitFor(() => /^Puzzle 2$/.test($("pz-counter").textContent) && board.state.moveInputCallback, "next puzzle by itself", 15000)
  check(true, "continuous: after a puzzle the next one loads automatically")
  const first = $("pz-set").querySelectorAll(".pz-item").length
  for (let n = 2; n <= first + 2; n++) {
    $("pz-solution").click()
    await waitFor(() => visible("pz-next"), `mixed puzzle ${n} solved`, 20000)
    $("pz-next").click()
    await waitFor(() => $("pz-counter").textContent === `Puzzle ${n + 1}` && board.state.moveInputCallback, `puzzle ${n + 1}`, 120000)
  }
  check(visible("pz-solver") && $("pz-set").querySelectorAll(".pz-item").length > first,
    `continuous: past the first ${first} puzzles the session goes on (${$("pz-counter").textContent}, ` +
    `${$("pz-set").querySelectorAll(".pz-item").length} in the session)`)
  const ids = [...$("pz-set").querySelectorAll(".pz-item")].map(b => b.dataset.id)
  check(new Set(ids).size === ids.length, "continuous: no puzzle served twice in a session")
  $("pz-back").click()
  await waitFor(() => visible("pz-dashboard") && /Session: 0 of \d+ solved/.test($("pz-personal").textContent + $("pz-dashboard").textContent),
    "back to Puzzles ends the session", 20000)
  check(true, "continuous: only Back to Puzzles ends the session")
}

// Coach panel: what the coach knows, and editing my details (at the end: it changes the level)
$("tab-lessons").click()
await sleep(300)
$("btn-coach").click()
await waitFor(() => /Level/.test($("coach-panel").textContent), "coach panel")
check(/Level/.test($("coach-panel").textContent) && [...$("coach-panel").querySelectorAll("button")].some(b => /My details/.test(b.textContent)),
  "coach panel shows the level and lets me edit my details")
check(/From your games/.test($("coach-panel").textContent) && /Missed knight fork/.test($("coach-panel").textContent),
  "the game-history pattern is part of what the coach knows")
;[...$("coach-panel").querySelectorAll("button")].find(b => /My details/.test(b.textContent)).click()
await waitFor(() => document.querySelector(".onboarding-card"), "details card")
const details = document.querySelector(".onboarding-card")
details.querySelector(".onb-rating").value = "1450"
details.querySelector(".onb-save").click()
await waitFor(() => !document.querySelector(".onboarding-card"), "details saved")
check(/Thanks! I'll start around/.test(lastMsgs(1)[0]), "saving my details: " + lastMsgs(1)[0])
const prof = await (await realFetch(new URL("api/profile", BASE))).json()
check(prof.profile.onboarding.rating === 1450 && prof.profile.onboarding.done, "onboarding answer is stored in the learner profile")

check(errors.length === 0, "no unhandled errors " + errors.join("\n"))
for (const id of await planIds()) {
  if (!existingPlans.has(id)) await realFetch(new URL(`api/plans/${id}`, BASE), {method: "DELETE"})
}
console.log(process.exitCode ? "UI E2E: FAILED" : "UI E2E: all checks passed")
process.exit()
