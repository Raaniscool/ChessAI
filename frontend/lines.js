// Lines of moves in a game review: shown by MOVING the pieces, not by highlighting squares.
//
// Review text (the moment explanation, the AI teacher's answer) carries the positions it talks
// about in a data-fens attribute. A run of adjacent moves in that text ("the line goes Nc7+ Kd8
// Nxa7", "3. Nxe5 dxe5 4. Qh5") that plays legally from one of those positions is a line; a
// single piece move ("Nc7+ wins the queen") is a one-move line. Those move spans get a
// `chessLine` property ({positions, index}): the board can then step to the position after
// that move. Bare squares ("the f7 square", "e5") are not lines — they stay highlights.
//
// The legality check uses chess.js, passed in, so this file has no import side effects and
// is unit tested with `node --test frontend/tests`.

// Text allowed between two moves of one line: spaces and commas ("Nc7+, Kd8, Nxa7").
const LINE_GAP = /^[\s,]*$/

export function isBareSquare(san) {
  return /^[a-h][1-8]$/.test(san)
}

/**
 * Positions for `sans` played from the first FEN in `fens` where every move is legal:
 * [start, after move 1, after move 2, ...], or null.
 */
export function resolveLine(sans, fens, Chess) {
  for (const fen of fens || []) {
    let chess
    try { chess = new Chess(fen) } catch (_) { continue }
    const positions = [chess.fen()]
    let ok = true
    for (const san of sans) {
      let move = null
      try { move = chess.move(san.replace(/[+#]$/, "")) } catch (_) { move = null }
      if (!move) { ok = false; break }
      positions.push(chess.fen())
    }
    if (ok) return positions
  }
  return null
}

/** Group `spans` (in document order) into runs of moves separated only by LINE_GAP text. */
export function moveRuns(spans) {
  const runs = []
  let run = []
  for (const span of spans) {
    if (run.length && adjacent(run[run.length - 1], span)) {
      run.push(span)
    } else {
      if (run.length) runs.push(run)
      run = [span]
    }
  }
  if (run.length) runs.push(run)
  return runs
}

function adjacent(a, b) {
  let node = a.nextSibling
  while (node && node !== b) {
    if (node.nodeType !== 3 || !LINE_GAP.test(node.nodeValue)) return false
    node = node.nextSibling
  }
  return node === b
}

/**
 * Mark the lines inside `element` (only when it, or an ancestor, lists positions in
 * data-fens). Idempotent. Returns how many move spans became part of a line.
 */
export function annotateLines(element, Chess) {
  const holder = element && element.closest ? element.closest("[data-fens]") : null
  if (!holder) return 0
  let fens = []
  try { fens = JSON.parse(holder.dataset.fens) } catch (_) { return 0 }
  if (!fens.length) return 0
  const spans = [...element.querySelectorAll(".mv")].filter(s => !s.chessLine && !s.dataset.noLine)
  let marked = 0
  for (const run of moveRuns(spans)) {
    let i = 0
    while (i < run.length) {
      // the longest legal line starting here (2+ moves), else a single piece move
      let taken = 0
      for (let end = run.length; end - i >= 2 && !taken; end--) {
        const positions = resolveLine(run.slice(i, end).map(s => s.dataset.san), fens, Chess)
        if (positions) {
          run.slice(i, end).forEach((span, k) => mark(span, positions, k))
          taken = end - i
        }
      }
      if (!taken) {
        const san = run[i].dataset.san
        const positions = !isBareSquare(san) && resolveLine([san], fens, Chess)
        if (positions) { mark(run[i], positions, 0); marked++ }
        else run[i].dataset.noLine = "1"  // a square, or not playable here: stays a highlight
        i++
        continue
      }
      marked += taken
      i += taken
    }
  }
  return marked
}

function mark(span, positions, index) {
  span.chessLine = {positions, index}
  span.classList.add("mv-line")
  span.title = positions.length > 2 ? "Point to see this move on the board · click to play the whole line"
    : "Point to see this move on the board"
}
