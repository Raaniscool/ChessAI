// Check highlight: a king in check gets a red glow on its square (as on Lichess).
//
// A cm-chessboard extension, so it follows the board itself rather than any one view: every
// position shown — a lesson step, a learner move, an AI demonstration, a puzzle, game review,
// history navigation — goes through the board, and the glow is redrawn whenever the pieces
// change, the board is redrawn (resize) or turned. It lives in its own layer under the pieces,
// so clearing the lesson/puzzle markers never removes it and it never covers a piece.
// Purely visual: it reads the position, it never changes anything.

import {EXTENSION_POINT, Extension} from "./vendor/cm-chessboard/src/model/Extension.js"

const SVG_NS = "http://www.w3.org/2000/svg"
const FILES = "abcdefgh"
const KNIGHT = [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]]
const AROUND = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]]
const ROOK_DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]]
const BISHOP_DIRS = [[1, 1], [1, -1], [-1, 1], [-1, -1]]

/** The board from a FEN (or just its piece placement): grid[file][rank], 0-based, null = empty. */
export function parsePlacement(fen) {
  const grid = Array.from({length: 8}, () => Array(8).fill(null))
  const rows = String(fen || "").trim().split(/\s+/)[0].split("/")
  if (rows.length !== 8) return grid
  rows.forEach((row, i) => {
    let file = 0
    for (const ch of row) {
      if (/\d/.test(ch)) file += Number(ch)
      else if (file < 8) grid[file++][7 - i] = ch
    }
  })
  return grid
}

/** Is the square (file, rank) attacked by a piece of `by` ("w" | "b")? */
export function attacked(grid, file, rank, by) {
  const own = ch => ch && (by === "w" ? ch === ch.toUpperCase() : ch === ch.toLowerCase())
  const at = (f, r) => (f >= 0 && f < 8 && r >= 0 && r < 8 ? grid[f][r] : undefined)
  const is = (f, r, type) => { const ch = at(f, r); return own(ch) && ch.toLowerCase() === type }
  const pawnRank = by === "w" ? rank - 1 : rank + 1  // white pawns attack upwards, black down
  if (is(file - 1, pawnRank, "p") || is(file + 1, pawnRank, "p")) return true
  if (KNIGHT.some(([df, dr]) => is(file + df, rank + dr, "n"))) return true
  if (AROUND.some(([df, dr]) => is(file + df, rank + dr, "k"))) return true
  const slides = (dirs, types) => dirs.some(([df, dr]) => {
    for (let f = file + df, r = rank + dr; at(f, r) !== undefined; f += df, r += dr) {
      const ch = at(f, r)
      if (ch) return own(ch) && types.includes(ch.toLowerCase())
    }
    return false
  })
  return slides(ROOK_DIRS, "rq") || slides(BISHOP_DIRS, "bq")
}

/** Squares of the kings in check in this position (e.g. ["e1"]); [] when nobody is in check. */
export function checkedKings(fen) {
  const grid = parsePlacement(fen)
  const out = []
  for (let f = 0; f < 8; f++) {
    for (let r = 0; r < 8; r++) {
      const ch = grid[f][r]
      if (ch !== "K" && ch !== "k") continue
      if (attacked(grid, f, r, ch === "K" ? "b" : "w")) out.push(FILES[f] + (r + 1))
    }
  }
  return out
}

export class CheckHighlight extends Extension {
  constructor(chessboard, props = {}) {
    super(chessboard)
    this.props = props
    const view = chessboard.view
    const defs = document.createElementNS(SVG_NS, "defs")
    const gradient = document.createElementNS(SVG_NS, "radialGradient")
    gradient.setAttribute("id", "check-highlight-gradient")
    for (const [offset, color, opacity] of [["0%", "#ff0000", 1], ["25%", "#e70000", 1],
      ["89%", "#a90000", 0], ["100%", "#9e0000", 0]]) {
      const stop = document.createElementNS(SVG_NS, "stop")
      stop.setAttribute("offset", offset)
      stop.setAttribute("stop-color", color)
      stop.setAttribute("stop-opacity", String(opacity))
      gradient.appendChild(stop)
    }
    defs.appendChild(gradient)
    view.svg.insertBefore(defs, view.svg.firstChild)
    this.group = document.createElementNS(SVG_NS, "g")
    this.group.setAttribute("class", "check-highlight")
    view.markersLayer.appendChild(this.group)
    const redraw = () => this.redraw()
    this.registerExtensionPoint(EXTENSION_POINT.positionChanged, redraw)
    this.registerExtensionPoint(EXTENSION_POINT.afterRedrawBoard, redraw)
    this.registerExtensionPoint(EXTENSION_POINT.boardChanged, redraw)
    chessboard.getCheckedKings = () => checkedKings(chessboard.getPosition())
    this.redraw()
  }

  redraw() {
    const view = this.chessboard.view
    if (!this.group || !view || typeof view.squareToPoint !== "function") return
    while (this.group.firstChild) this.group.removeChild(this.group.firstChild)
    let fen
    try {
      fen = this.chessboard.getPosition()
    } catch (_) {
      return  // the board is still being built: no position yet
    }
    for (const square of checkedKings(fen)) {
      const point = view.squareToPoint(square)
      const rect = document.createElementNS(SVG_NS, "rect")
      rect.setAttribute("x", String(point.x))
      rect.setAttribute("y", String(point.y))
      rect.setAttribute("width", String(view.squareWidth))
      rect.setAttribute("height", String(view.squareHeight))
      rect.setAttribute("fill", "url(#check-highlight-gradient)")
      rect.setAttribute("class", "check-square")
      rect.setAttribute("data-square", square)
      this.group.appendChild(rect)
    }
  }
}
