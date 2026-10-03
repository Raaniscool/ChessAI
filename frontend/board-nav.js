// Board move history and the ← → controls under the board (Lichess-style).
//
// Every view that puts a sequence of moves on the board (a lesson demonstration, a puzzle being
// solved, an engine line in a game review) keeps one MoveHistory: the positions in order plus a
// cursor for the one being looked at. Going back never erases anything. A move made at an
// earlier position:
//   - is the next move already in the history  -> just steps forward;
//   - is different                             -> becomes the new continuation (the old moves
//                                                 after that point are replaced).
// Moves the program plays itself (a demonstration, the opponent's reply in a puzzle) are
// appended at the end of the line.
//
// createBoardNav wires the buttons: one shared board, so each view registers its history and
// the buttons follow whichever view is shown. While the AI is moving pieces by itself, the
// view locks navigation (lock/unlock), and the buttons are disabled.
//
// No DOM or chess.js imports here: unit tested with `node --test frontend/tests`.

const norm = uci => {
  const m = String(uci || "").toLowerCase()
  return m.length === 5 && m[4] === "q" ? m.slice(0, 4) : m
}

export class MoveHistory {
  constructor(fen) {
    this.reset(fen)
  }

  reset(fen) {
    this.nodes = [{fen, uci: null, san: null}]
    this.cursor = 0
    return this
  }

  get current() { return this.nodes[this.cursor] }
  get last() { return this.nodes.length - 1 }
  get atLatest() { return this.cursor === this.last }
  get canBack() { return this.cursor > 0 }
  get canForward() { return this.cursor < this.last }
  get latest() { return this.nodes[this.last] }

  back() {
    if (this.canBack) this.cursor -= 1
    return this.current
  }

  forward() {
    if (this.canForward) this.cursor += 1
    return this.current
  }

  goTo(index) {
    this.cursor = Math.max(0, Math.min(this.last, index))
    return this.current
  }

  toStart() { return this.goTo(0) }
  toLatest() { return this.goTo(this.last) }

  // The program's own move (demonstration, puzzle reply): always the end of the line.
  append(fen, uci = null, san = null) {
    this.nodes.push({fen, uci, san})
    this.cursor = this.last
    return this.current
  }

  // The learner's move at the position being viewed.
  play(fen, uci, san = null) {
    const next = this.nodes[this.cursor + 1]
    if (next && norm(next.uci) === norm(uci)) {
      this.cursor += 1
      return {node: next, branched: false, dropped: 0}
    }
    const dropped = this.last - this.cursor
    this.nodes = this.nodes.slice(0, this.cursor + 1)
    this.append(fen, uci, san)
    return {node: this.current, branched: dropped > 0, dropped}
  }

  // "Start", "3. Bb5", "3... Nc6" — the move that led to the position being viewed.
  label(index = this.cursor) {
    if (index <= 0) return "Start"
    const node = this.nodes[index]
    const before = String(this.nodes[index - 1].fen || "").split(" ")
    const number = parseInt(before[5], 10) || 1
    const san = node.san || node.uci || "…"
    return before[1] === "b" ? `${number}... ${san}` : `${number}. ${san}`
  }

  // A history from a start position and moves (uci or san), using chess.js (passed in).
  static fromMoves(fen, moves, Chess) {
    const h = new MoveHistory(fen)
    const chess = new Chess(fen)
    for (const m of moves) {
      let move = null
      try {
        move = /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(m)
          ? chess.move({from: m.slice(0, 2), to: m.slice(2, 4), promotion: m[4] || "q"})
          : chess.move(m)
      } catch (_) { move = null }
      if (!move) break
      h.append(chess.fen(), move.from + move.to + (move.promotion || ""), move.san)
    }
    return h
  }

  // A history from consecutive positions (a line in review text): each move is found by
  // trying the legal moves of the previous position.
  static fromPositions(fens, Chess) {
    const placement = f => String(f || "").split(" ")[0]
    const h = new MoveHistory(fens[0])
    for (let i = 1; i < fens.length; i++) {
      let found = null
      try {
        const chess = new Chess(fens[i - 1])
        for (const m of chess.moves({verbose: true})) {
          const trial = new Chess(fens[i - 1])
          trial.move(m)
          if (placement(trial.fen()) === placement(fens[i])) { found = m; break }
        }
      } catch (_) { found = null }
      h.append(fens[i], found ? found.from + found.to + (found.promotion || "") : null, found ? found.san : null)
    }
    return h
  }
}

// The ← → controls. `show(node, history)` puts a position on the board; `getView()` names the
// view currently shown. Each view registers its history with set(view, history, {onView}).
// onView(history) runs after the learner navigated (e.g. a puzzle turns move input off while an
// earlier position is shown).
export function createBoardNav({prev, next, label, back, root = null, show, getView}) {
  const entries = {}
  const overlays = {}
  const locks = new Set()   // "view:key"; "*:key" locks every view

  const entry = () => {
    const v = getView()
    return overlays[v] ? {history: overlays[v], overlay: true} : entries[v] || null
  }

  function isLocked(view = getView()) {
    for (const k of locks) {
      if (k.startsWith(`${view}:`) || k.startsWith("*:")) return true
    }
    return false
  }

  function render() {
    const e = entry()
    const h = e && e.history
    const locked = isLocked()
    prev.disabled = locked || !h || !h.canBack
    next.disabled = locked || !h || !h.canForward
    const moves = h ? h.last : 0
    if (label) label.textContent = h && moves > 0 ? `${h.label()} · ${h.cursor}/${moves}` : ""
    if (back) back.classList.toggle("hidden", !(e && e.overlay))
    if (root) {
      root.classList.toggle("reviewing", Boolean(h && !h.atLatest))
      root.classList.toggle("locked", locked)
    }
  }

  async function go(step) {
    const e = entry()
    if (!e || isLocked()) return false
    const h = e.history
    const before = h.cursor
    if (step === "back") h.back()
    else if (step === "forward") h.forward()
    else if (step === "start") h.toStart()
    else if (step === "end") h.toLatest()
    if (h.cursor === before) return false
    render()
    await show(h.current, h)
    if (!e.overlay && e.onView) await e.onView(h)
    render()
    return true
  }

  async function closeOverlay(view = getView()) {
    if (!overlays[view]) return
    delete overlays[view]
    render()
    const e = entries[view]
    if (e) {
      await show(e.history.current, e.history)
      if (e.onView) await e.onView(e.history)
    }
  }

  prev.addEventListener("click", () => go("back"))
  next.addEventListener("click", () => go("forward"))
  if (back) back.addEventListener("click", () => closeOverlay())

  return {
    set(view, history, {onView = null} = {}) {
      entries[view] = {history, onView}
      delete overlays[view]
      render()
    },
    history: view => (entries[view] ? entries[view].history : null),
    // A line played from text (not the view's own sequence): reviewable until the view moves on.
    overlay(view, history) {
      overlays[view] = history
      render()
    },
    dropOverlay(view) {
      delete overlays[view]
      render()
    },
    closeOverlay,
    lock(key, view = getView()) {
      locks.add(`${view}:${key}`)
      render()
    },
    unlock(key, view = getView()) {
      locks.delete(`${view}:${key}`)
      render()
    },
    isLocked,
    refresh: render,
    go,
  }
}

// ← / → (and Home / End) from the keyboard, unless the learner is typing.
export function navKey(event) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null
  const t = event.target
  const tag = t && t.tagName ? t.tagName.toLowerCase() : ""
  if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) return null
  return {ArrowLeft: "back", ArrowRight: "forward", Home: "start", End: "end"}[event.key] || null
}
