// Puzzles tab -> Training: short games against a bot near your level, analysed afterwards.
// The board shows only the position: no theme, no hint, no source, until the segment is over.
// After each segment: accuracy, important mistakes, missed chances, concepts, what went well, what to
// work on — then Continue starts the next one at once (each mode goes on as long as you like).

import {parsePosition} from "./position.js"

export const MODE_TEXT = {
  opening: "Positions from real opening theory, a few moves in. Play on from there.",
  middlegame: "Positions from real games, in the middle of the fight.",
  endgame: "Endgame positions from real games and endgame theory.",
  mixed: "All three, mixed — beginning, middlegame and endgame in turn.",
}

export const STATUS_WORD = {needs_work: "needs practice", improving: "improving", solid: "solid", ok: "fine",
  insufficient: "not enough evidence yet"}

// One segment's info line: phase, colour, how far along (never the idea).
export function segmentLine(seg, modeLabel) {
  if (!seg) return ""
  const side = seg.side === "black" ? "Black" : "White"
  const n = seg.learner_moves
  const length = `${seg.min_moves}–${seg.max_moves} moves`
  return `${modeLabel} · You play ${side} · ${n ? `${n} played` : "Your move"} (about ${length}) · bot ~${seg.bot_rating}`
}

// The feedback card's sections as plain data (tested without a DOM).
export function reportSections(result) {
  const fb = result.feedback || {}
  const a = result.analysis || {}
  const out = []
  const stats = []
  if (a.accuracy != null) stats.push(`Accuracy ${Math.round(a.accuracy)}%`)
  stats.push(`${a.learner_moves || 0} moves`)
  if (a.avg_loss_cp != null) stats.push(`average loss ${a.avg_loss_cp} cp`)
  if (a.best_move_rate != null) stats.push(`${Math.round(a.best_move_rate * 100)}% top moves`)
  out.push({title: fb.headline || "Segment finished", lines: [stats.join(" · "), fb.ended].filter(Boolean), kind: "head"})
  const mistakes = (a.important_mistakes || []).map(m =>
    `${m.label || m.san}: ${m.category}${m.best_move ? ` — ${m.best_move} was better` : ""}`)
  if (mistakes.length) out.push({title: "Important mistakes", lines: mistakes, kind: "mistakes"})
  const missed = (a.missed_opportunities || []).map(m =>
    `${m.label || m.san || "First move"}${m.best_move ? `: ${m.best_move} was the chance` : ""}`)
  if (missed.length) out.push({title: "Missed opportunities", lines: missed, kind: "missed"})
  if ((fb.concepts || []).length) out.push({title: "Concepts detected", lines: fb.concepts.map(c => c.line), kind: "concepts"})
  out.push({title: "What went well", lines: fb.went_well || [], kind: "good"})
  out.push({title: "What to work on", lines: fb.work_on || [], kind: "work"})
  const r = fb.reveal || {}
  const reveal = []
  if (r.idea) reveal.push(`The idea in this position: ${r.idea}${r.key_move ? ` (${r.key_move})` : ""}`)
  if (r.source) reveal.push(`From: ${r.source}${r.line && r.line.length ? ` — ${r.line.join(" ")}` : ""}`)
  if (reveal.length) out.push({title: "About this position", lines: reveal, kind: "reveal", url: r.url || null})
  return out
}

export function createTrainer({api, board, showPosition, verifiedMove, addMarkers, isPositionVerified,
  Chess, COLOR, legalInputHandler, escapeHtml, setStatus,
  nav = null, MoveHistory = null, showNode = null, sounds = null, el, onShow, onExit}) {
  const st = {
    mode: null, modeLabel: "", seg: null, chess: null, busy: false, active: false, token: 0, result: null,
    home: null, history: MoveHistory ? new MoveHistory(new Chess().fen()) : null, inputOpen: false, pending: null,
  }
  const show = (node, on) => node.classList.toggle("hidden", !on)
  const orientation = () => (st.seg && st.seg.side === "black" ? COLOR.black : COLOR.white)
  const learnerTurn = () => st.chess && st.seg && st.chess.turn() === (st.seg.side === "black" ? "b" : "w")
  const sound = (move, chess) => { if (sounds && move) sounds.playMove(move, chess.fen()) }

  // ------------------------------------------------------------------ home (inside the Puzzles dashboard)
  async function loadHome(container) {
    container.innerHTML = `<p class="muted">Loading Training…</p>`
    try {
      st.home = await api("/api/puzzles/training")
    } catch (err) {
      container.innerHTML = `<p class="muted">Couldn't load Training: ${escapeHtml(err.message)}</p>`
      return
    }
    renderHome(container)
  }

  function renderHome(container) {
    const h = st.home
    container.innerHTML = ""
    const intro = document.createElement("p")
    intro.className = "muted"
    intro.textContent = "Play short games against a bot near your level. Afterwards you see how it went — "
      + "and your results decide what Practice and Personalized give you. Nothing is announced while you play."
    container.appendChild(intro)
    const grid = document.createElement("div")
    grid.className = "pz-themes tr-modes"
    for (const m of h.modes) {
      const b = document.createElement("button")
      b.className = "pz-theme tr-mode"
      b.dataset.mode = m.id
      b.disabled = !m.count
      b.title = MODE_TEXT[m.id] || ""
      b.innerHTML = `<span>${escapeHtml(m.label)}</span><span class="muted">${escapeHtml(MODE_TEXT[m.id] || "")}</span>`
      b.addEventListener("click", () => start(m.id, m.label))
      grid.appendChild(b)
    }
    container.appendChild(grid)
    container.appendChild(profileBox(h.profile))
  }

  function profileBox(p) {
    const box = document.createElement("div")
    box.className = "ga-card tr-profile"
    if (!p || !p.segments) {
      box.innerHTML = `<h3>What Training has found</h3><p class="muted">Nothing yet — play a few segments.
        One move never decides anything: the picture builds up over several games.</p>`
      return box
    }
    const phases = Object.values(p.phases).filter(x => x.segments || x.moves)
      .map(x => `<li><b>${escapeHtml(x.name[0].toUpperCase() + x.name.slice(1))}</b>: ${escapeHtml(STATUS_WORD[x.status] || x.status)}`
        + `${x.accuracy_recent != null ? ` · recent accuracy ${Math.round(x.accuracy_recent)}%` : ""}</li>`).join("")
    const needs = (p.needs || []).map(n => `<li>${escapeHtml(n.why)}</li>`).join("")
    const strengths = (p.strengths || []).map(n => escapeHtml(n.name)).join(", ")
    box.innerHTML = `<h3>What Training has found</h3>
      <p class="muted">${p.segments} ${p.segments === 1 ? "segment" : "segments"}, ${p.moves} moves analysed by Stockfish.</p>
      ${phases ? `<ul class="tr-list">${phases}</ul>` : ""}
      ${needs ? `<h4 class="pz-subhead">Worth practising</h4><ul class="tr-list">${needs}</ul>`
        : `<p class="muted">No weak spot is clear yet — ${escapeHtml(p.method)}</p>`}
      ${strengths ? `<p class="muted">Going well: ${strengths}</p>` : ""}`
    return box
  }

  // ------------------------------------------------------------------ playing
  function info() {
    el.info.textContent = segmentLine(st.seg, st.modeLabel)
  }

  function message(text, kind = "") {
    el.feedback.textContent = text
    el.feedback.className = `pz-feedback ${kind}`
  }

  function setInput(open) {
    st.inputOpen = open && st.active
    board.disableMoveInput()
    if (st.inputOpen) board.enableMoveInput(input, orientation())
  }

  function track() {
    if (nav && st.history) nav.set("puzzles", st.history, {onView})
  }

  async function onView(h) {
    if (!st.seg) return
    const playing = !st.seg.finished && !st.seg.end_reason && !st.busy
    if (h.atLatest) {
      setStatus("")
      setInput(playing && learnerTurn())
    } else {
      setInput(false)
      if (playing) setStatus("Looking at an earlier position — press → to get back to the game.")
    }
  }

  async function render() {
    if (st.history && showNode) await showNode(st.history.current, st.history, orientation())
    else await showPosition(st.chess.fen(), {orientation: orientation()})
  }

  async function start(mode, label) {
    st.token++
    const token = st.token
    setInput(false)
    st.mode = mode
    st.modeLabel = label || (st.home && (st.home.modes.find(m => m.id === mode) || {}).label) || "Training"
    st.result = null
    onShow()
    el.report.innerHTML = ""
    show(el.report, false)
    show(el.cont, false)
    show(el.end, true)
    el.end.disabled = true
    message("Finding a position…")
    let res
    try {
      res = await api("/api/puzzles/training/start", "POST", {mode})
    } catch (err) {
      message(err.message, "bad")
      return
    }
    if (token !== st.token) return
    const segment = res.segment
    let startFen
    try {
      if (!segment) throw new Error("The server did not return a Training position.")
      startFen = parsePosition(Chess, segment.start_fen || segment.fen, "Training position").fen()
      await showPosition(startFen, {orientation: segment.side === "black" ? COLOR.black : COLOR.white})
      if (!isPositionVerified(startFen)) throw new Error("The Training position was not confirmed on the board.")
    } catch (err) {
      message(`I couldn't verify that Training position. ${err.message}`, "bad")
      return
    }
    if (token !== st.token || !st.active) return
    st.seg = segment
    st.chess = parsePosition(Chess, startFen, "Training position")
    if (st.history) st.history.reset(startFen)
    track()
    el.title.textContent = st.modeLabel
    info()
    message("Your move. Play it like a real game — the analysis comes after the segment.")
    setInput(true)
  }

  const input = legalInputHandler(() => (st.inputOpen && learnerTurn() ? st.chess : null),
    (from, to, receipt) => {
      const pending = onMove(from, to, receipt)
      st.pending = pending
      pending.finally(() => { if (st.pending === pending) st.pending = null }).catch(() => {})
      return pending
    })

  function play(chess, uci) {
    return chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
  }

  async function onMove(from, to, receipt) {
    if (!st.active || st.busy || !st.seg || (st.history && !st.history.atLatest)) return
    const transition = receipt && receipt.transition
    if (!transition || receipt.beforeFen !== st.chess.fen() || transition.from !== from || transition.to !== to ||
        !isPositionVerified(transition.afterFen)) {
      setInput(false)
      message("I couldn't confirm that Training move. The segment has been paused before scoring it.", "bad")
      return
    }
    const uci = transition.uci
    const beforeFen = receipt.beforeFen
    const token = ++st.token // invalidate any re-entry sync that started before this move
    st.busy = true
    setInput(false)
    if (nav) nav.lock("auto", "puzzles")
    let canResume = true
    try {
      st.chess = parsePosition(Chess, transition.afterFen, "Training learner move")
      sound(transition.move, st.chess)
      let res = await api(`/api/puzzles/training/${st.seg.id}/move`, "POST", {uci, expected_fen: beforeFen})
      if (token !== st.token) return
      if (res.bot_move) {
        await sleep(350) // visual pacing only
        if (token !== st.token) return
        const fromSquare = res.bot_move.uci.slice(0, 2)
        const piece = st.chess.get(fromSquare)
        if (!piece) throw new Error(`The bot's source piece is missing from ${fromSquare}.`)
        const reply = await verifiedMove(st.chess.fen(), res.bot_move.uci, {
          expectedPiece: {color: piece.color, type: piece.type}, label: "Training bot move",
        })
        if (token !== st.token || !st.active) return
        st.chess = parsePosition(Chess, reply.afterFen, "Training bot result")
        sound(reply.move, st.chess)
        await addMarkers([
          {marker: {class: "marker-lastmove", slice: "markerSquare"}, square: reply.from},
          {marker: {class: "marker-lastmove", slice: "markerSquare"}, square: reply.to},
        ], reply.afterFen, "Training bot move highlight")
      }
      if (token !== st.token) return
      const serverFen = res.segment && (res.segment.current_fen || res.segment.fen)
      if (!serverFen) throw new Error("The Training service returned no current position.")
      const localFields = st.chess.fen().split(" ")
      const serverFields = parsePosition(Chess, serverFen, "Training server position").fen().split(" ")
      const sameLegalState = localFields.slice(0, 4).join(" ") === serverFields.slice(0, 4).join(" ")
      if (!sameLegalState || !isPositionVerified(serverFen)) {
        await showPosition(serverFen, {orientation: orientation()})
        if (!isPositionVerified(serverFen)) throw new Error("The server position could not be synchronized to the board.")
        message("The Training service returned a different position. I synchronized the board before continuing.", "bad")
      } else {
        message("")
      }
      st.seg = res.segment
      replay(st.seg) // rebuild the mirror/history from the server-confirmed move sequence
      if (!isPositionVerified(st.chess.fen())) throw new Error("The confirmed Training history did not match the board.")
      info()
      el.end.disabled = !st.seg.learner_moves
      if (res.ended) {
        await finish()
        return
      }
    } catch (err) {
      if (token !== st.token) return
      try {
        const live = await api(`/api/puzzles/training/${st.seg.id}`)
        const serverFen = live.current_fen || live.fen
        await showPosition(serverFen, {orientation: orientation()})
        if (!isPositionVerified(serverFen)) throw new Error("The recovered Training board could not be verified.")
        st.seg = live
        replay(live)
        info()
        message(`The Training move was not safely confirmed. I synchronized the segment. ${err.message}`, "bad")
      } catch (recoveryError) {
        canResume = false
        setInput(false)
        message(`The Training position is uncertain. Reload this segment before continuing. ${recoveryError.message}`, "bad")
      }
    } finally {
      st.busy = false
      if (nav) nav.unlock("auto", "puzzles")
      if (canResume && token === st.token && st.seg && !st.seg.end_reason && !st.result && st.active) {
        setInput(learnerTurn())
      }
    }
  }

  function replay(seg) {
    const chess = parsePosition(Chess, seg && (seg.start_fen || seg.fen), "Training position")
    if (st.history) st.history.reset(chess.fen())
    for (const uci of seg.moves_uci || []) {
      const m = play(chess, uci)
      if (!m) throw new Error("The Training line is not legal from its starting position.")
      if (st.history) st.history.append(chess.fen(), uci, m.san)
    }
    st.chess = chess
    if (nav) nav.refresh()
  }

  async function finish() {
    if (!st.seg || st.result) return
    const token = st.token
    setInput(false)
    show(el.end, false)
    message("Analysing the segment with Stockfish…")
    let res
    try {
      res = await api(`/api/puzzles/training/${st.seg.id}/finish`, "POST", {})
    } catch (err) {
      if (token !== st.token) return
      message(err.message, "bad")
      show(el.cont, true)
      return
    }
    if (token !== st.token) return
    st.result = res
    st.seg = res.segment
    if (st.home) st.home.profile = res.profile
    message("")
    renderReport(res)
    show(el.cont, true)
    if (st.active) el.cont.focus()
  }

  function renderReport(res) {
    el.report.innerHTML = ""
    for (const s of reportSections(res)) {
      const block = document.createElement("div")
      block.className = `tr-section tr-${s.kind}`
      const items = s.lines.map(l => `<li>${escapeHtml(l)}</li>`).join("")
      block.innerHTML = s.kind === "head"
        ? `<h3>${escapeHtml(s.title)}</h3>${s.lines.map(l => `<p class="muted">${escapeHtml(l)}</p>`).join("")}`
        : `<h4 class="pz-subhead">${escapeHtml(s.title)}</h4><ul class="tr-list">${items}</ul>`
      if (s.url) {
        const a = document.createElement("a")
        a.href = s.url
        a.target = "_blank"
        a.rel = "noopener"
        a.textContent = "Open the original"
        block.appendChild(a)
      }
      el.report.appendChild(block)
    }
    show(el.report, true)
  }

  el.cont.addEventListener("click", () => start(st.mode, st.modeLabel))
  el.end.addEventListener("click", () => { if (!st.busy && st.seg && st.seg.learner_moves) finish() })
  el.back.addEventListener("click", () => { st.token++; setInput(false); onExit() })

  return {
    loadHome,
    renderHome: container => { if (st.home) renderHome(container) },
    activate() { st.active = true },
    async enter() {
      st.active = true
      const token = st.token
      if (st.pending) {
        try { await st.pending } catch (_) { /* onMove reports and recovers its own failure */ }
        if (!st.active || token !== st.token) return
      }
      if (!st.seg) return
      const segmentId = st.seg.id
      try {
        // A move request can finish while this tab is hidden. Re-read the server's move list
        // before restoring the board; never trust the local mirror after an interrupted request.
        const live = await api(`/api/puzzles/training/${segmentId}`)
        if (!st.active || token !== st.token || !st.seg || st.seg.id !== segmentId) return
        st.seg = live
        replay(live)
        if (!isPositionVerified(st.chess.fen())) {
          await showPosition(live.current_fen || live.fen, {orientation: orientation()})
          if (!st.active || token !== st.token) return
          if (!isPositionVerified(st.chess.fen())) throw new Error("The current Training position was not confirmed.")
        }
      } catch (err) {
        if (st.active && token === st.token) {
          setInput(false)
          setStatus(`I couldn't synchronize Training with the saved move list. Reload this segment before continuing. ${err.message}`)
          message("The Training position is paused until it can be confirmed.", "bad")
        }
        return
      }
      if (!st.active || token !== st.token) return
      track()
      info()
      try {
        await render()
        if (!st.active || token !== st.token) return
        if (!isPositionVerified(st.chess.fen())) throw new Error("The restored Training board does not match its move history.")
      } catch (err) {
        if (st.active && token === st.token) {
          setInput(false)
          setStatus(`I couldn't restore the Training board safely. ${err.message}`)
          message("The Training position is paused until it can be confirmed.", "bad")
        }
        return
      }
      if (st.seg.end_reason && !st.result && st.seg.learner_moves > 0) {
        await finish() // a finish request may have committed while this tab was hidden
        return
      }
      if (!st.result && !st.seg.end_reason && !st.busy && (!st.history || st.history.atLatest)) setInput(learnerTurn())
    },
    leave() {
      st.active = false
      st.token++
      st.inputOpen = false
      board.disableMoveInput()
    },
    state: () => ({mode: st.mode, segment: st.seg && st.seg.id, moves: st.seg ? st.seg.learner_moves : 0,
      finished: Boolean(st.result), busy: st.busy}),
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
