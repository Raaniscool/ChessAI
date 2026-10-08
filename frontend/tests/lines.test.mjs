import {test} from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {annotateLines, isBareSquare, moveRuns, resolveLine} from "../lines.js"
import {tokenize} from "../speech.js"

const FORK = "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"  // Nc7+ forks king and queen
const AFTER_KD2 = "q3k3/8/8/1N6/8/8/3K4/8 b - - 1 1"

// A tiny stand-in for the DOM decorateMoves() produces: text nodes and <span class="mv">.
function fakeElement(text, fens) {
  const children = tokenize(text).map(p => p.san
    ? {nodeType: 1, san: p.san, dataset: {san: p.san}, textContent: p.text,
       classList: {items: new Set(), add(c) { this.items.add(c) }, contains(c) { return this.items.has(c) }}}
    : {nodeType: 3, nodeValue: p.text})
  children.forEach((node, i) => { node.nextSibling = children[i + 1] || null })
  const element = {
    dataset: fens ? {fens: JSON.stringify(fens)} : {},
    closest(sel) { return sel === "[data-fens]" && this.dataset.fens ? this : null },
    querySelectorAll(sel) { return sel === ".mv" ? children.filter(n => n.nodeType === 1) : [] },
  }
  return {element, moves: children.filter(n => n.nodeType === 1)}
}

test("resolveLine plays the moves from the first position where they are legal", () => {
  const positions = resolveLine(["Nc7+", "Kd8", "Nxa8"], [AFTER_KD2, FORK], Chess)
  assert.equal(positions.length, 4)
  assert.equal(positions[0].split(" ")[0], FORK.split(" ")[0])
  assert.match(positions[3], /^N2k4/)  // the knight took the queen on a8
  assert.equal(resolveLine(["Nc7+", "Kf8", "Nxa8", "Qh1"], [FORK], Chess), null)
})

test("a missing line FEN never makes the line play from the standard starting board", () => {
  assert.equal(resolveLine(["e4"], [undefined, null, ""], Chess), null)
})

test("bare squares are not moves to play", () => {
  assert.ok(isBareSquare("f7") && isBareSquare("e5"))
  assert.ok(!isBareSquare("Nc7+") && !isBareSquare("exd5") && !isBareSquare("O-O"))
})

test("moves separated only by spaces or commas form one run", () => {
  const {moves} = fakeElement("the line goes Nc7+ Kd8, Nxa8 and then Kd2 wins", [FORK])
  const runs = moveRuns(moves)
  assert.deepEqual(runs.map(r => r.map(s => s.san)), [["Nc7+", "Kd8", "Nxa8"], ["Kd2"]])
})

test("a line in review text becomes moves on the board, a square stays a highlight", () => {
  const {element, moves} = fakeElement("You played Kd2, but the line goes 1. Nc7+ Kd8 2. Nxa8 — watch f7.", [FORK, AFTER_KD2])
  assert.equal(annotateLines(element, Chess), 4)
  const [kd2, nc7, kd8, nxa8, f7] = moves
  assert.deepEqual(kd2.chessLine.positions.length, 2)  // a single piece move: one move to play
  assert.equal(nc7.chessLine.index, 0)
  assert.equal(kd8.chessLine.index, 1)
  assert.equal(nxa8.chessLine.index, 2)
  assert.equal(nc7.chessLine.positions, nxa8.chessLine.positions)  // one shared line
  assert.ok(nxa8.classList.contains("mv-line"))
  assert.equal(f7.chessLine, undefined)  // "f7" is a square: highlighted, not played
  assert.equal(annotateLines(element, Chess), 0)  // idempotent
})

test("an illegal run is split into what can be played", () => {
  const {element, moves} = fakeElement("Nc7+ Qh8 Kd2", [FORK])
  annotateLines(element, Chess)
  assert.equal(moves[0].chessLine.positions.length, 2)  // Nc7+ alone (Qh8 isn't legal after it)
  assert.equal(moves[1].chessLine, undefined)
})

test("text without positions (lesson chat) is left alone", () => {
  const {element, moves} = fakeElement("Play Nc7+ Kd8 Nxa8", null)
  assert.equal(annotateLines(element, Chess), 0)
  assert.ok(moves.every(m => m.chessLine === undefined))
})
