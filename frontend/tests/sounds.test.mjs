// node --test frontend/tests
import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {createBoardSounds, soundFor} from "../sounds.js"

function memoryStorage() {
  const data = {}
  return {getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v) }, data}
}

test("the sound follows the move that was played: check beats capture beats a plain move", () => {
  const chess = new Chess()
  assert.equal(soundFor(chess.move("e4")), "move")
  chess.move("d5")
  assert.equal(soundFor(chess.move("exd5")), "capture")
  // a capture that gives check sounds as a check
  const c2 = new Chess("q3k3/8/8/8/8/8/8/R3K3 w - - 0 1")
  assert.equal(soundFor(c2.move("Rxa8+")), "check")
  const c3 = new Chess("3k4/8/8/8/8/8/8/R3K3 w Q - 0 1")
  assert.equal(soundFor(c3.move("O-O-O")), "check")   // castling with check
  const ep = new Chess("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
  assert.equal(soundFor(ep.move("exd6")), "capture")  // en passant: nothing stands on d6
  const mate = new Chess("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
  assert.equal(soundFor(mate.move("Ra8#")), "check")
})

test("an illegal move makes no sound", () => {
  const chess = new Chess()
  let move = null
  try { move = chess.move("e5") } catch (_) { move = null }
  const sounds = createBoardSounds({storage: memoryStorage(), AudioCtx: null})
  assert.equal(sounds.playMove(move), false)
  assert.equal(soundFor(null), null)
  assert.deepEqual(sounds.played, [])
})

test("the same position sounds once, even if the frontend asks twice", () => {
  let t = 1000
  const sounds = createBoardSounds({storage: memoryStorage(), AudioCtx: null, now: () => t})
  const chess = new Chess()
  const move = chess.move("e4")
  assert.ok(sounds.playMove(move, chess.fen()))
  assert.equal(sounds.playMove(move, chess.fen()), false)  // a re-render a moment later
  t += 1000
  assert.ok(sounds.playMove(move, chess.fen()))            // replayed on purpose later (watch again)
  assert.deepEqual(sounds.played, ["move", "move"])
})

test("sounds can be switched off, and the choice is remembered", () => {
  const storage = memoryStorage()
  const sounds = createBoardSounds({storage, AudioCtx: null})
  assert.ok(sounds.enabled)
  sounds.setEnabled(false)
  assert.equal(sounds.play("move", "x"), false)
  assert.equal(storage.data["chessai.boardSounds"], "off")
  assert.equal(createBoardSounds({storage, AudioCtx: null}).enabled, false)
})

test("with Web Audio the sound is synthesized (no files) and stays quiet", () => {
  const made = []
  const node = () => ({connect: n => n || node(), gain: {value: 1, setValueAtTime() {}, exponentialRampToValueAtTime() {}},
    frequency: {value: 0, setValueAtTime() {}, exponentialRampToValueAtTime() {}}, Q: {value: 0},
    start() {}, stop() {}})
  class FakeCtx {
    constructor() { this.currentTime = 0; this.sampleRate = 8000; this.state = "running"; this.destination = node() }
    createOscillator() { made.push("osc"); return node() }
    createGain() { const g = node(); made.push("gain"); return g }
    createBuffer(_c, n) { return {getChannelData: () => new Float32Array(n)} }
    createBufferSource() { made.push("noise"); return node() }
    createBiquadFilter() { return node() }
  }
  const sounds = createBoardSounds({storage: memoryStorage(), AudioCtx: FakeCtx, volume: 0.5})
  for (const kind of ["move", "capture", "check"]) assert.ok(sounds.play(kind))
  assert.ok(made.includes("osc") && made.includes("noise"))
})
