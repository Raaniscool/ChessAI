// node --test frontend/tests
import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {MoveHistory, createBoardNav, navKey} from "../board-nav.js"

const START = new Chess().fen()

test("a demonstrated line can be stepped back and forward without losing anything", () => {
  const h = MoveHistory.fromMoves(START, ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"], Chess)
  assert.equal(h.last, 5)
  assert.ok(h.atLatest)
  assert.equal(h.label(), "3. Bb5")
  h.back()
  assert.equal(h.label(), "2... Nc6")
  h.back(); h.back()
  assert.equal(h.label(), "1... e5")
  assert.equal(h.last, 5)                          // going back erases nothing
  h.forward()
  assert.equal(h.label(), "2. Nf3")
  h.toStart()
  assert.equal(h.label(), "Start")
  assert.equal(h.back().fen, START)                // can't go before the start
  h.toLatest()
  assert.equal(h.current.san, "Bb5")
  assert.equal(h.forward().san, "Bb5")             // nor past the end
})

test("a move at an earlier position: the same move steps forward, a new one starts a new continuation", () => {
  const h = MoveHistory.fromMoves(START, ["e2e4", "e7e5", "g1f3"], Chess)
  h.goTo(1)
  const after = h.nodes[2].fen
  const same = h.play(after, "e7e5", "e5")
  assert.deepEqual([same.branched, h.cursor, h.last], [false, 2, 3])   // Nf3 is still there
  h.goTo(1)
  const c = new Chess(h.current.fen)
  c.move("c5")
  const r = h.play(c.fen(), "c7c5", "c5")
  assert.deepEqual([r.branched, r.dropped, h.cursor, h.last], [true, 2, 2, 2])
  assert.equal(h.label(), "1... c5")
  assert.deepEqual(h.nodes.map(n => n.san), [null, "e4", "c5"])           // consistent, no leftovers
})

test("promotion uci with or without the q is the same move", () => {
  const h = new MoveHistory("8/P7/8/8/8/8/8/k6K w - - 0 1")
  h.append("Q7/8/8/8/8/8/8/k6K b - - 0 1", "a7a8q", "a8=Q")
  h.back()
  assert.equal(h.play("Q7/8/8/8/8/8/8/k6K b - - 0 1", "a7a8").branched, false)
})

test("a line from review text: moves are recovered from consecutive positions", () => {
  const c = new Chess()
  const fens = [c.fen()]
  for (const m of ["e4", "e5", "Qh5"]) { c.move(m); fens.push(c.fen()) }
  const h = MoveHistory.fromPositions(fens, Chess)
  assert.deepEqual(h.nodes.slice(1).map(n => n.san), ["e4", "e5", "Qh5"])
  assert.equal(h.nodes[3].uci, "d1h5")
})

function fakeButton() {
  const handlers = []
  return {disabled: false, classList: {set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c) },
    contains(c) { return this.set.has(c) }}, textContent: "",
  addEventListener(_t, f) { handlers.push(f) }, click() { return Promise.all(handlers.map(f => f())) }}
}

function setup() {
  const el = {prev: fakeButton(), next: fakeButton(), label: fakeButton(), back: fakeButton(), root: fakeButton()}
  const shown = []
  const ctx = {view: "lessons"}
  const nav = createBoardNav({...el, show: async node => { shown.push(node.fen) }, getView: () => ctx.view})
  return {el, shown, ctx, nav}
}

test("the buttons step through the shown view's history and are off while the AI is moving", async () => {
  const {el, shown, ctx, nav} = setup()
  const h = MoveHistory.fromMoves(START, ["e2e4", "e7e5"], Chess)
  const seen = []
  nav.set("lessons", h, {onView: hist => seen.push(hist.cursor)})
  assert.equal(el.prev.disabled, false)
  assert.equal(el.next.disabled, true)
  assert.equal(el.label.textContent, "1... e5 · 2/2")
  nav.lock("demo", "lessons")
  assert.ok(el.prev.disabled && el.next.disabled && nav.isLocked())
  assert.equal(await nav.go("back"), false)          // locked: nothing happens
  assert.equal(h.cursor, 2)
  nav.unlock("demo", "lessons")
  await el.prev.click()
  assert.equal(h.cursor, 1)
  assert.deepEqual(seen, [1])
  assert.equal(shown.at(-1), h.nodes[1].fen)
  assert.ok(el.root.classList.contains("reviewing"))
  await el.next.click()
  assert.ok(!el.root.classList.contains("reviewing"))
  // another view: its own history (none yet), the lesson's is kept
  ctx.view = "puzzles"
  nav.refresh()
  assert.ok(el.prev.disabled && el.next.disabled && el.label.textContent === "")
  nav.lock("line", "*")
  ctx.view = "lessons"
  assert.ok(nav.isLocked())                         // a "*" lock applies everywhere
  nav.unlock("line", "*")
  assert.ok(!nav.isLocked())
  assert.equal(nav.history("lessons"), h)
})

test("a played line from text is reviewable on top, and ↩ Back returns to the view's own position", async () => {
  const {el, shown, nav} = setup()
  const own = MoveHistory.fromMoves(START, ["d2d4"], Chess)
  nav.set("lessons", own)
  const line = MoveHistory.fromMoves(START, ["e2e4", "e7e5", "d1h5"], Chess)
  nav.overlay("lessons", line)
  assert.ok(!el.back.classList.contains("hidden"))
  await nav.go("back")
  assert.equal(line.cursor, 2)
  assert.equal(own.cursor, 1)
  await el.back.click()
  assert.ok(el.back.classList.contains("hidden"))
  assert.equal(shown.at(-1), own.current.fen)
  nav.overlay("lessons", line)
  nav.set("lessons", own)                           // the view moved on: the overlay is gone
  assert.ok(el.back.classList.contains("hidden"))
})

test("arrow keys navigate unless the learner is typing", () => {
  assert.equal(navKey({key: "ArrowLeft", target: {tagName: "BUTTON"}}), "back")
  assert.equal(navKey({key: "ArrowRight", target: {tagName: "DIV"}}), "forward")
  assert.equal(navKey({key: "ArrowLeft", target: {tagName: "INPUT"}}), null)
  assert.equal(navKey({key: "ArrowLeft", target: {tagName: "TEXTAREA"}}), null)
  assert.equal(navKey({key: "ArrowLeft", shiftKey: true, target: {tagName: "DIV"}}), null)
  assert.equal(navKey({key: "a", target: {tagName: "DIV"}}), null)
})
