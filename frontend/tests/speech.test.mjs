// Unit tests for the read-aloud text layer: `node --test frontend/tests`
import test from "node:test"
import assert from "node:assert/strict"
import {sanToSpeech, targetSquare, tokenize, buildSpeech, markAt} from "../speech.js"

test("SAN is spoken as words", () => {
  assert.equal(sanToSpeech("Kf5"), "king F5")
  assert.equal(sanToSpeech("Nxg5"), "knight takes G5")
  assert.equal(sanToSpeech("exd5"), "E takes D5")
  assert.equal(sanToSpeech("e4"), "E4")
  assert.equal(sanToSpeech("Rd8#"), "rook D8, checkmate")
  assert.equal(sanToSpeech("Qh5+"), "queen H5, check")
  assert.equal(sanToSpeech("e8=Q"), "E8 promotes to a queen")
  assert.equal(sanToSpeech("Nbd2"), "knight B to D2")
  assert.equal(sanToSpeech("R1e2"), "rook 1 to E2")
  assert.equal(sanToSpeech("O-O"), "castles kingside")
  assert.equal(sanToSpeech("O-O-O+"), "castles queenside, check")
})

test("target square of a move", () => {
  assert.equal(targetSquare("Nxg5+"), "g5")
  assert.equal(targetSquare("e8=Q#"), "e8")
  assert.equal(targetSquare("f5"), "f5")
  assert.equal(targetSquare("O-O"), null)
})

test("moves and squares are found in prose, but not inside words or coordinates", () => {
  const moves = text => tokenize(text).filter(s => s.san).map(s => s.san)
  assert.deepEqual(moves("Play Kf5, then the pawn on e4 falls after 4...Nxg5."), ["Kf5", "e4", "Nxg5"])
  assert.deepEqual(moves("1.e4 e5 2.Nf3 Nc6 3.Bc4"), ["e4", "e5", "Nf3", "Nc6", "Bc4"])
  assert.deepEqual(moves("Qwen3 runs on h2h3 notation, e.g. at a1-level"), [])
  assert.deepEqual(moves("Castle with O-O-O or O-O."), ["O-O-O", "O-O"])
  assert.deepEqual(moves("Checkmate with Rd8#!"), ["Rd8#"])
})

test("spoken text maps each move to its position (for highlighting)", () => {
  const {spoken, marks} = buildSpeech(tokenize("🎯 Exercise: play  Kf5 and then e4."))
  assert.equal(spoken, "Exercise: play king F5 and then E4.")
  assert.deepEqual(marks.map(m => spoken.slice(m.start, m.end)), ["king F5", "E4"])
  assert.deepEqual(marks.map(m => m.square), ["f5", "e4"])
  // word boundary events land on a word's first character
  assert.equal(markAt(marks, spoken.indexOf("king")).san, "Kf5")
  assert.equal(markAt(marks, spoken.indexOf("F5")).san, "Kf5")
  assert.equal(markAt(marks, spoken.indexOf("E4")).square, "e4")
  assert.equal(markAt(marks, spoken.indexOf("then")), null)
})

test("move numbers are shown but not spoken", () => {
  const {spoken} = buildSpeech(tokenize("After 4...Nxg5 White wins."))
  assert.equal(spoken, "After knight takes G5 White wins.")
})

test("links are not read out letter by letter", () => {
  const {spoken, marks} = buildSpeech(tokenize("From a real game: Lichess puzzle https://lichess.org/training/e4Qa5 (public domain)."))
  assert.equal(spoken, "From a real game: Lichess puzzle (public domain).")
  assert.equal(marks.length, 0)
})

test("Black's moves written '...e6' are found (the lessons use them a lot)", () => {
  const moves = text => tokenize(text).filter(s => s.san).map(s => s.san)
  assert.deepEqual(moves("Black follows up with ...Nf6, ...c6, ...Bf5 and ...e6 for a solid setup."),
    ["Nf6", "c6", "Bf5", "e6"])
  assert.deepEqual(moves("After ...cxd4 Black has an extra centre pawn; then ...Nxa1 wins the rook."), ["cxd4", "Nxa1"])
  const {spoken, marks} = buildSpeech(tokenize("Black prepares ...d5."))
  assert.equal(spoken, "Black prepares D5.")
  assert.deepEqual(marks.map(m => m.square), ["d5"])
})

test("squares chained with a dash or slash are each found, and read naturally", () => {
  const moves = text => tokenize(text).filter(s => s.san).map(s => s.san)
  assert.deepEqual(moves("the queen attacks along h4-e1, and mate on the e8-h5 diagonal"), ["h4", "e1", "e8", "h5"])
  assert.deepEqual(moves("the knight went f6-g4-f6-g8 (b6-b8-d8-d6)"), ["f6", "g4", "f6", "g8", "b6", "b8", "d8", "d6"])
  assert.deepEqual(moves("a knight trip Nbd2-f1-g3."), ["Nbd2", "f1", "g3"])
  assert.deepEqual(moves("Pawns on e4/d4 (or e5/d5); Ke4 (or Kd4/Kf4)"), ["e4", "d4", "e5", "d5", "Ke4", "Kd4", "Kf4"])
  assert.equal(buildSpeech(tokenize("along h4-e1.")).spoken, "along H4 to E1.")
  assert.equal(buildSpeech(tokenize("on e4/d4")).spoken, "on E4 or D4")
  // still not inside words, links or coordinates
  assert.deepEqual(moves("Qwen3 at a1-level, h2h3, https://x.org/a/e4Qa5, Giuoco-Piano-4.c3"), [])
})

test("the moves of a line are read with short pauses", () => {
  assert.equal(buildSpeech(tokenize("the line goes Bxf7+ Ke7 O-O Qxe5.")).spoken,
    "the line goes bishop takes F7, check, king E7, castles kingside, queen takes E5.")
  assert.equal(buildSpeech(tokenize("1.e4 e5 2.Nf3")).spoken, "E4, E5, knight F3")
})
