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

await waitFor(() => document.querySelectorAll(".lesson-item").length > 0, "courses")
await waitFor(() => /engine/.test($("health").textContent), "health")
console.log("health:", $("health").textContent)

// 1) Planner from the sidebar box
$("plan-input").value = "I want to learn the Sicilian"
$("plan-form").dispatchEvent(new w.Event("submit", {cancelable: true}))
await waitFor(() => [...document.querySelectorAll("#messages button")].some(b => /Start the first lesson/.test(b.textContent)), "plan message")
await waitFor(() => document.querySelector(".course.plan"), "plan course")
check(/Sicilian/.test(document.querySelector(".course.plan").textContent), "plan course listed first in sidebar")
check(lastMsgs(1)[0].includes("Opening principles") && lastMsgs(1)[0].includes("Sicilian"), "plan message lists units")

// 2) Italian lesson 1: explore on teach step
const italian = [...document.querySelectorAll(".course:not(.plan) .lesson-item")].find(b => !b.disabled)
italian.click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 1") && board && board.state.moveInputCallback, "step 1")
check(move("e2", "e4") === "ok", "teach step: can move a white pawn (explore)")
check(move("e7", "e5") === "ok", "teach step: then a black pawn (turns alternate)")
check(move("g1", "f3") === "ok", "teach step: knight develops")
await sleep(1200)
check(/Exploring/.test($("board-status").textContent), "status says exploring/ungraded")
$("btn-reset").click(); await sleep(400)
check(board.getPosition().startsWith("rnbqkbnr/pppppppp"), "reset restores the position")

// 3) Demonstration, then exercise Bc4 (the reported bug)
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 2"), "step 2")
$("btn-play").click()
await waitFor(() => /Demonstration complete/.test($("board-status").textContent), "demo complete", 20000)
check(move("g8", "f6") === "ok", "after demo: can explore from the final position")
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 3") && /Your move/.test($("board-status").textContent), "step 3 (exercise)")
const r = move("f1", "c4"); console.log("  f1c4 ->", r)
check(r === "ok", "exercise: bishop f1 can be picked up and moved to c4 (was the bug)")
await waitFor(() => !$("btn-continue").classList.contains("hidden") || /Try again/.test($("board-status").textContent), "Bc4 graded", 60000)
check(!$("btn-continue").classList.contains("hidden"), "Bc4 accepted")
console.log("  feedback:", $("feedback-slot").textContent.trim().slice(0, 140))
const qwenOn = /teacher: qwen/.test($("health").textContent)
if (qwenOn) {
  // Engine verdict is instant; Qwen's explanation must stream into the same card.
  check(/Qwen is writing/.test($("feedback-slot").textContent) || /teacher: qwen/.test($("feedback-slot").textContent),
        "AI explanation starts streaming right after the instant verdict")
  await waitFor(() => /teacher: (qwen|fallback)/.test(document.querySelector(".teacher-label").textContent), "explanation stream done", 90000)
  const label = document.querySelector(".teacher-label").textContent
  check(label === "teacher: qwen", "streamed explanation finished (" + label + ")")
  const expl = document.querySelector(".explanation").textContent
  check(expl.length > 20 && !/<think>|<\/think>/.test(expl), "explanation shown without <think> tags: " + expl.slice(0, 60))
}

// 4) Black exercise: Nf6
$("btn-continue").click()
await waitFor(() => $("step-indicator").textContent.startsWith("Step 4") && /Your move/.test($("board-status").textContent), "step 4")
check(move("e2", "e4") === "cannot-pick-up", "black exercise: white pieces can't be moved")
check(move("g8", "f6") === "ok", "black exercise: knight g8 can move")
await waitFor(() => !$("btn-continue").classList.contains("hidden"), "Nf6 accepted", 60000)
check(true, "Nf6 accepted")

if (qwenOn) {
  // Chat answer streams into a bubble.
  $("chat-input").value = "Why is the bishop good on c4?"
  $("btn-chat").click()
  await waitFor(() => { const m = [...document.querySelectorAll("#messages .msg.assistant")].pop(); return m && !m.classList.contains("typing") && m.textContent.length > 20 }, "chat stream", 90000)
  check(true, "chat reply streamed: " + [...document.querySelectorAll("#messages .msg.assistant")].pop().textContent.slice(0, 50))
}

// 5) Start the plan's opening lesson and play the first exercise as Black
const sicilianLesson = [...document.querySelectorAll(".course.plan .lesson-item")].find(b => /moves and ideas/.test(b.textContent))
sicilianLesson.click()
await waitFor(() => /Sicilian/.test($("lesson-title").textContent), "plan lesson start")
$("btn-continue").click()
await waitFor(() => !$("btn-play").classList.contains("hidden"), "demo button")
$("btn-play").click()
await waitFor(() => /Demonstration complete/.test($("board-status").textContent), "plan demo", 30000)
$("btn-continue").click()
await waitFor(() => /Exercise/.test(lastMsgs(1)[0]) && /Your move/.test($("board-status").textContent), "plan exercise")
console.log("  prompt:", lastMsgs(1)[0])
check(move("c7", "c5") === "ok", "plan exercise: 1...c5 playable")
await waitFor(() => !$("btn-continue").classList.contains("hidden"), "c5 accepted", 60000)
check(true, "1...c5 accepted")

// 6) Chat routing: "I want to learn ..." makes a plan even mid-lesson
$("chat-input").value = "I want to learn knight forks"
$("btn-chat").click()
await waitFor(() => [...document.querySelectorAll(".course.plan h3")].some(h => /Fork/.test(h.textContent)), "fork plan")
check(true, "chat 'I want to learn…' creates a plan")

// 7) The user's report: "I want to learn the smothered mate" must teach smothered mate
$("chat-input").value = "I want to learn the smothered mate"
$("btn-chat").click()
await waitFor(() => /Learn: Smothered mate/.test(lastMsgs(1)[0]), "smothered plan message")
const smPlan = lastMsgs(1)[0]
check(/Smothered mate/.test(smPlan) && !/Fork|Pin|Skewer/.test(smPlan), "smothered-mate plan contains only smothered mate")
const smLesson = [...document.querySelectorAll(".course.plan .lesson-item")].find(b => /Smothered mate: learn the pattern/.test(b.textContent))
check(!!smLesson, "plan has 'Smothered mate: learn the pattern'")
smLesson.click()
await waitFor(() => /Smothered/.test($("lesson-title").textContent), "smothered lesson start")
$("btn-continue").click()                                   // teach -> demo of the opponent's move
await waitFor(() => !$("btn-play").classList.contains("hidden"), "puzzle demo button")
$("btn-play").click()
await waitFor(() => /Demonstration complete/.test($("board-status").textContent), "puzzle demo", 30000)
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

check(errors.length === 0, "no unhandled errors " + errors.join("\n"))
for (const id of await planIds()) {
  if (!existingPlans.has(id)) await realFetch(new URL(`api/plans/${id}`, BASE), {method: "DELETE"})
}
console.log(process.exitCode ? "UI E2E: FAILED" : "UI E2E: all checks passed")
process.exit()
