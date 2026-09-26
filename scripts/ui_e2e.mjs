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

// 1) Planner from the chat box (before any lesson)
$("chat-input").value = "I want to learn the Sicilian"
$("btn-chat").click()
await waitFor(() => [...document.querySelectorAll("#messages button")].some(b => /Start the first lesson/.test(b.textContent)), "plan message")
await waitFor(() => document.querySelector(".course.plan"), "plan course")
check(/Sicilian/.test(document.querySelector(".course.plan").textContent), "plan listed in the sidebar")
check(lastMsgs(1)[0].includes("Sicilian"), "plan message lists units")
check(![...document.querySelectorAll(".course.plan .badge")].some(b => /available/.test(b.textContent)), "no 'available' badges")

// 2) "I want to learn the Italian Game" builds the lesson on request
$("chat-input").value = "I want to learn the Italian Game"
$("btn-chat").click()
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
await finishDemo("demo complete")
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
  $("btn-chat").click()
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

// 6) Chat routing: "I want to learn ..." makes a plan even mid-lesson
$("chat-input").value = "I want to learn knight forks"
$("btn-chat").click()
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /fork/i.test(h.textContent)), "fork plan")
check(true, "chat 'I want to learn…' creates a plan")

// 7) The user's report: "I want to learn the smothered mate" must teach smothered mate
$("chat-input").value = "I want to learn the smothered mate"
$("btn-chat").click()
await waitFor(() => /Learn: Smothered mate/.test(lastMsgs(1)[0]), "smothered plan message")
const smPlan = lastMsgs(1)[0]
check(/Smothered mate/.test(smPlan) && !/Fork|Pin|Skewer/.test(smPlan), "smothered-mate plan contains only smothered mate")
const smLesson = lessonIn(/Smothered mate/, /Learn the pattern/)
check(!!smLesson, "plan has 'Smothered mate: learn the pattern'")
smLesson.click()
await waitFor(() => /Smothered/.test($("lesson-title").textContent), "smothered lesson start")
$("btn-continue").click()                                   // teach -> demo of the opponent's move
await finishDemo("puzzle demo")
$("btn-continue").click()
await waitFor(() => /smothered mate/i.test(lastMsgs(1)[0]) && /Your move/.test($("board-status").textContent), "puzzle exercise")
console.log("  prompt:", lastMsgs(1)[0])
check(move("h6", "f5") === "ok", "wrong knight move can be played")
await waitFor(() => /Try again/i.test($("board-status").textContent) || !$("btn-continue").classList.contains("hidden"), "wrong move graded", 60000)
check($("btn-continue").classList.contains("hidden"), "wrong move (Nf5) is not accepted")
await waitFor(() => board.getPiece("h6") && !board.getPiece("f5"), "knight back on h6 after wrong move", 20000)
check(move("h6", "f7") === "ok", "Nf7# playable")
await waitFor(() => !$("btn-continue").classList.contains("hidden"), "Nf7# accepted", 60000)
check(/Checkmate!/.test($("feedback-slot").textContent + lastMsgs(2).join(" ")), "Nf7# accepted as checkmate")

// 8) Unknown subject: honest answer + suggestions, no substitute plan
const plansBefore = (await planIds()).length
$("chat-input").value = "I want to learn the zorblax gambit"
$("btn-chat").click()
await waitFor(() => /don't have verified lessons/.test(lastMsgs(1)[0]), "unknown-subject answer", 90000)
check(document.querySelectorAll("#messages .suggestions").length > 0, "unknown subject: suggestion chips shown")
check((await planIds()).length === plansBefore, "unknown subject: no substitute plan created")

// 9) Knowledge Library: "Show me checkmates" mid-lesson -> verified examples as a lesson
$("chat-input").value = "Show me checkmates"
$("btn-chat").click()
await waitFor(() => /Learn: Checkmate/.test(lastMsgs(1)[0]), "library plan message", 30000)
check(/verified examples/.test([...document.querySelectorAll("#messages .msg")].pop().textContent),
      "chat 'Show me…' builds a lesson from the verified library")
const kLesson = lessonIn(/^Checkmate$/, /Learn from examples/)
check(!!kLesson, "library lesson listed in the sidebar")
kLesson.click()
await waitFor(() => /learn from examples/.test($("lesson-title").textContent), "library lesson start")
check(/verified example/.test(lastMsgs(1)[0]), "intro explains the sequence")
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
// example 2: guided — find the key move (read from the server's verified example)
for (let i = 0; i < 4 && !/Your move/.test($("board-status").textContent); i++) {
  const at = $("step-indicator").textContent
  await waitFor(() => continueShown(), "continue (example 2)", 40000)
  $("btn-continue").click()
  await waitFor(() => $("step-indicator").textContent !== at, "next step (example 2)")
  await sleep(300)
}
await waitFor(() => /Your move/.test($("board-status").textContent), "example 2 exercise", 40000)
check($("btn-explain-example").classList.contains("hidden"), "no explain button while the exercise is unsolved")
$("btn-reveal").click()
await waitFor(() => /Solution/.test(lastMsgs(1)[0]), "solution shown")
check(/Solution: \S+/.test(lastMsgs(1)[0]), "exercise solution comes from the verified example: " + lastMsgs(1)[0])
// a question starting with "show me" stays in the chat
const before = (await planIds()).length
const assistantBefore = document.querySelectorAll("#messages .msg.assistant").length
$("chat-input").value = "show me why this move is bad"
$("btn-chat").click()
await waitFor(() => { const all = [...document.querySelectorAll("#messages .msg.assistant")]; const m = all.pop(); return all.length >= assistantBefore && m && !m.classList.contains("typing") && m.textContent.length > 10 }, "chat reply", 90000)
check((await planIds()).length === before, "'show me why…' is answered in the chat, not turned into a plan")

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
check(/import a few more games|only one game/i.test($("ga-overview").textContent), "one game: no recurring weakness claimed yet")
$("ga-pgn").value = GAME2
$("ga-import-btn").click()
await waitFor(() => /Analysis done/.test($("ga-status").textContent) && document.querySelector(".weakness.recurring"), "second game analyzed", 120000)
const weakness = document.querySelector(".weakness.recurring")
check(/Missed knight fork/.test(weakness.textContent) && /2 of your|both games/.test(weakness.textContent), "recurring weakness across 2 games: " + weakness.textContent.slice(0, 80))
check(/verified example/.test(weakness.textContent), "weakness links to verified library examples")
check(document.querySelectorAll("#ga-games .game-item").length >= 2, "both games listed")
// jump to where it happened
;[...weakness.querySelectorAll("button")].find(b => /See it/.test(b.textContent)).click()
await waitFor(() => !$("ga-review").classList.contains("hidden") && document.querySelector(".moment-headline"), "moment review")
check(/You missed a knight fork/.test(document.querySelector(".moment-headline").textContent), "moment headline names the tactic")
check(/^Move \d+\.\.\./.test(document.querySelector(".ga-card.moment h3").textContent), "Black's move titled 'Move N...': " + document.querySelector(".ga-card.moment h3").textContent)
check(document.querySelector(".engine-details") && !document.querySelector(".engine-details").open, "engine numbers folded away by default")
const fenBefore = board.getPosition()
;[...document.querySelectorAll(".moment-views .btn")].find(b => b.textContent === "Best move").click()
await waitFor(() => /Stockfish's move/.test($("board-status").textContent), "best move shown")
check(board.getPosition() !== fenBefore, "best move is played on the board")
;[...document.querySelectorAll(".moment-views .btn")].find(b => b.textContent === "Your move").click()
await waitFor(() => /You played/.test($("board-status").textContent), "my move shown")
document.querySelector(".moment-ask .btn").click()
await waitFor(() => { const a = document.querySelector(".moment-ai"); return a && !a.classList.contains("hidden") && a.textContent.length > 20 && !document.querySelector(".moment-ask .btn").disabled }, "moment explanation", 120000)
check(/fork|knight/i.test(document.querySelector(".moment-ai").textContent), "explanation: " + document.querySelector(".moment-ai").textContent.slice(0, 70))
// the recurring note offers practice; start training from it
const practise = [...document.querySelectorAll(".moment-related .btn")].find(b => /Practise/.test(b.textContent))
check(!!practise, "moment says it's a recurring weakness and offers practice")
practise.click()
await waitFor(() => !document.querySelector(".lesson-pane:not(.analysis-pane)").classList.contains("hidden") &&
  /From your games/.test(lastMsgs(1)[0] || ""), "training plan in lessons view")
check(/From your games/.test(lastMsgs(1)[0]), "training plan built from your games: " + lastMsgs(1)[0].slice(0, 60))
check(document.querySelectorAll("#messages .msg").length > lessonMsgsBefore, "lesson conversation kept when switching tabs")
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /From your games/.test(h.textContent)), "training plan in the sidebar")
const own = lessonIn(/From your games/, /your own games/i)
check(!!own, "plan has a lesson with positions from your own games")
own.click()
for (let i = 0; i < 4 && !/Your move/.test($("board-status").textContent); i++) {
  const at = $("step-indicator").textContent
  await waitFor(() => continueShown() || /Your move/.test($("board-status").textContent), "continue (own games)", 40000)
  if (/Your move/.test($("board-status").textContent)) break
  $("btn-continue").click()
  await waitFor(() => $("step-indicator").textContent !== at, "next step (own games)")
  await sleep(300)
}
check(/Your game against/.test(lastMsgs(1)[0]) && board.isMoveInputEnabled(), "exercise from your own game: " + lastMsgs(1)[0].slice(0, 70))
for (const id of await gameIds()) if (!gamesBefore.has(id)) await realFetch(new URL(`api/games/${id}`, BASE), {method: "DELETE"})

check(errors.length === 0, "no unhandled errors " + errors.join("\n"))
for (const id of await planIds()) {
  if (!existingPlans.has(id)) await realFetch(new URL(`api/plans/${id}`, BASE), {method: "DELETE"})
}
console.log(process.exitCode ? "UI E2E: FAILED" : "UI E2E: all checks passed")
process.exit()
