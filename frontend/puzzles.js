// Puzzles tab: a dashboard (Personalized / Practice) and a board-focused solver.
// Rules live in puzzle-solver.js; the server decides which puzzles and checks every one of them.

import {cardLine, focusLine, header, itemLabel, judge, newAttempt, nextHint, resultBody, retry, reveal, sameMove,
  shouldRecord} from "./puzzle-solver.js"

const REPLY_DELAY = 450
const REVERT_DELAY = 550
const SOLUTION_STEP = 700

export function setupPuzzles({api, board, showPosition, clearMarkers, Chess, COLOR, MARKER_TYPE, legalInputHandler,
  escapeHtml, setStatus}) {
  const $ = id => document.getElementById(id)
  const el = {
    title: $("pz-title"), counter: $("pz-counter"), dashboard: $("pz-dashboard"), solver: $("pz-solver"),
    modePersonal: $("pz-mode-personal"), modePractice: $("pz-mode-practice"), personal: $("pz-personal"),
    practice: $("pz-practice"), status: $("pz-status"), info: $("pz-info"), feedback: $("pz-feedback"),
    explain: $("pz-explain"), hint: $("pz-hint"), solution: $("pz-solution"), retry: $("pz-retry"),
    next: $("pz-next"), back: $("pz-back"), set: $("pz-set"),
  }
  const view = {
    mode: "personal", screen: "dashboard", dashboard: null, set: [], title: "", pos: 0, attempt: null,
    chess: null, inputOpen: false, token: 0, active: false, results: {}, summary: null,
  }

  const puzzle = () => view.set[view.pos]
  const orientation = () => (puzzle() && puzzle().side === "black" ? COLOR.black : COLOR.white)
  const show = (node, on) => node.classList.toggle("hidden", !on)

  // ------------------------------------------------------------------ dashboard
  function setMode(mode) {
    view.mode = mode
    for (const [btn, m] of [[el.modePersonal, "personal"], [el.modePractice, "practice"]]) {
      btn.classList.toggle("active", m === mode)
      btn.setAttribute("aria-selected", String(m === mode))
    }
    show(el.personal, mode === "personal")
    show(el.practice, mode === "practice")
  }

  async function loadDashboard() {
    el.status.textContent = ""
    try {
      view.dashboard = await api("/api/puzzles/dashboard")
    } catch (err) {
      el.status.textContent = `Couldn't load puzzles: ${err.message}`
      return
    }
    renderDashboard()
  }

  function renderDashboard() {
    const d = view.dashboard
    if (!d) return
    const p = d.personalized
    el.personal.innerHTML = ""
    if (view.summary) {
      const s = document.createElement("div")
      s.className = "ga-card pz-summary"
      s.textContent = view.summary
      el.personal.appendChild(s)
    }
    if (!p.has_data) {
      const box = document.createElement("div")
      box.className = "ga-card pz-empty"
      box.innerHTML = `<h3>Puzzles for your weaknesses</h3>
        <p>Nothing to personalize yet. Analyze some of your games in <b>Game Analysis</b> — or just solve puzzles:
        your results here shape what comes next.</p>`
      const go = document.createElement("button")
      go.className = "btn primary"
      go.textContent = "Practice a theme →"
      go.addEventListener("click", () => setMode("practice"))
      box.appendChild(go)
      el.personal.appendChild(box)
    } else {
      const main = p.main
      const box = document.createElement("div")
      box.className = "ga-card pz-profile"
      box.innerHTML = `<div class="pz-label">Your main weakness</div>
        <h3>${escapeHtml(main.title)}</h3>
        <p class="pz-evidence">${escapeHtml(cardLine(main))}</p>
        ${focusLine(main) ? `<p class="pz-focus">${escapeHtml(focusLine(main))}</p>` : ""}
        <p class="muted">Recommended: ${escapeHtml(p.profile.recommendation || "")}</p>`
      box.appendChild(startButton(main, `Start ${main.recommended} puzzles`, true))
      el.personal.appendChild(box)
      const others = p.weaknesses.slice(1)
      if (others.length) {
        const h = document.createElement("h4")
        h.className = "pz-subhead"
        h.textContent = "Other weaknesses"
        el.personal.appendChild(h)
        for (const card of others) {
          const row = document.createElement("div")
          row.className = "pz-card"
          row.dataset.key = card.key
          row.innerHTML = `<div class="pz-card-text"><b>${escapeHtml(card.title)}</b>
            <span class="muted">${escapeHtml(cardLine(card))}</span></div>`
          row.appendChild(startButton(card, "Start", false))
          el.personal.appendChild(row)
        }
      }
    }
    el.practice.innerHTML = ""
    const grid = document.createElement("div")
    grid.className = "pz-themes"
    for (const t of d.practice) {
      const b = document.createElement("button")
      b.className = "pz-theme"
      b.dataset.concept = t.concept
      b.innerHTML = `<span>${escapeHtml(t.label)}</span><span class="muted">${t.count}</span>`
      b.addEventListener("click", () => startSet({mode: "practice", concept: t.concept, count: 5}))
      grid.appendChild(b)
    }
    const intro = document.createElement("p")
    intro.className = "muted"
    intro.textContent = "Pick a theme. Puzzles adapt to your level as you solve them."
    el.practice.append(intro, grid)
  }

  function startButton(card, label, primary) {
    const b = document.createElement("button")
    b.className = "btn" + (primary ? " primary" : "")
    b.textContent = label
    b.dataset.weakness = card.key
    b.addEventListener("click", () => startSet({mode: "personalized", weakness: card.key, count: card.recommended || 5}))
    return b
  }

  async function startSet(body) {
    el.status.textContent = body.mode === "personalized"
      ? "Finding puzzles for this weakness (new ones are checked by Stockfish first)…" : "Finding puzzles…"
    let res
    try {
      res = await api("/api/puzzles/set", "POST", body)
    } catch (err) {
      el.status.textContent = err.message
      return
    }
    el.status.textContent = ""
    view.set = res.puzzles
    view.title = res.title
    view.results = {}
    view.summary = null
    view.pos = 0
    view.screen = "solver"
    show(el.dashboard, false)
    show(el.solver, true)
    renderSetList()
    await openPuzzle(0)
  }

  // ------------------------------------------------------------------ solver
  function renderSetList() {
    el.set.innerHTML = ""
    view.set.forEach((p, i) => {
      const b = document.createElement("button")
      const r = view.results[p.id]
      b.className = "lesson-item pz-item" + (i === view.pos ? " active" : "") + (r ? ` ${r}` : "")
      b.dataset.id = p.id
      b.innerHTML = `<span>${i + 1}. ${escapeHtml(itemLabel(p))}</span>
        <span class="badge">${r === "solved" ? "✓" : r === "failed" ? "✗" : escapeHtml(header(p, i, 1).difficulty)}</span>`
      b.addEventListener("click", () => goTo(i))
      el.set.appendChild(b)
    })
  }

  function renderInfo() {
    const p = puzzle()
    const h = header(p, view.pos, view.set.length)
    el.title.textContent = view.title || "Puzzles"
    el.counter.textContent = h.progress
    el.info.innerHTML = `<div class="pz-side ${p.side}">${escapeHtml(h.side)}</div>
      <div class="pz-objective">${escapeHtml(h.objective)}</div>
      <div class="pz-meta">${h.role ? `<span class="pz-role ${escapeHtml(p.role || "")}">${escapeHtml(h.role)}</span>` : ""}
      <span>${escapeHtml(h.concept)}</span><span>${escapeHtml(h.difficulty)}</span>
      ${p.steps.length > 1 ? `<span class="pz-dots">${p.steps.map((_s, i) =>
        `<i class="${i < view.attempt.index ? "on" : ""}"></i>`).join("")}</span>` : ""}</div>
      ${h.why ? `<div class="pz-why"><b>Why this puzzle:</b> ${escapeHtml(h.why)}</div>` : ""}`
  }

  function feedback(text, kind = "") {
    el.feedback.textContent = text
    el.feedback.className = `pz-feedback ${kind}`
  }

  function setInput(open) {
    view.inputOpen = open && view.active
    board.disableMoveInput()
    if (view.inputOpen) board.enableMoveInput(input, orientation())
  }

  async function openPuzzle(i) {
    view.pos = i
    const p = puzzle()
    view.attempt = newAttempt(p)
    view.chess = new Chess(p.fen)
    view.token++
    feedback("")
    show(el.explain, false)
    show(el.hint, true); show(el.solution, true); show(el.retry, false); show(el.next, false)
    renderInfo()
    renderSetList()
    await showPosition(p.fen, {orientation: orientation()})
    setStatus("")
    setInput(true)
  }

  async function goTo(i) {
    if (i === view.pos && view.attempt && !view.attempt.done) return
    await recordIfNeeded()
    await openPuzzle(i)
  }

  const input = legalInputHandler(() => (view.inputOpen ? view.chess : null), (from, to) => onMove(from, to))

  function uciOf(from, to) {
    const legal = view.chess.moves({square: from, verbose: true}).filter(m => m.to === to)
    if (!legal.length) return null
    return from + to + (legal.some(m => m.promotion) ? "q" : "")
  }

  function play(chess, uci) {
    return chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
  }

  async function onMove(from, to) {
    const uci = uciOf(from, to)
    if (!uci) return
    const p = puzzle()
    const token = view.token
    const trial = new Chess(view.chess.fen())
    play(trial, uci)
    const r = judge(p, view.attempt, uci, {mate: trial.in_checkmate()})
    if (r.verdict === "wrong" || r.verdict === "good") {
      setInput(false)
      feedback(r.message, r.verdict)
      board.addMarker(r.verdict === "wrong" ? MARKER_TYPE.circleDanger : MARKER_TYPE.circle, to)
      await sleep(REVERT_DELAY)
      if (token !== view.token) return
      await showPosition(view.chess.fen(), {orientation: orientation()})
      setInput(true)
      return
    }
    play(view.chess, uci)
    clearMarkers()
    board.addMarker(MARKER_TYPE.square, from); board.addMarker(MARKER_TYPE.square, to)
    feedback(r.message, "correct")
    renderInfo()
    if (r.done) {
      await showPosition(view.chess.fen(), {orientation: orientation(), highlights: [{square: from, color: "green"},
        {square: to, color: "green"}]})
      return finish()
    }
    setInput(false)
    await sleep(REPLY_DELAY)
    if (token !== view.token) return
    if (r.reply) {
      play(view.chess, r.reply)
      await showPosition(view.chess.fen(), {orientation: orientation(), animated: true,
        highlights: [{square: r.reply.slice(0, 2), color: "grey"}, {square: r.reply.slice(2, 4), color: "grey"}]})
    }
    if (token !== view.token) return
    feedback(view.attempt.failed ? "Keep going." : "Best move! Keep going.", "correct")
    setInput(true)
  }

  async function finish() {
    setInput(false)
    const a = view.attempt
    const p = puzzle()
    const solved = a.solved && !a.failed
    if (a.revealed) feedback("Here's the solution.", "revealed")
    else if (solved) feedback(a.hints ? "✓ Solved (with a hint)" : "✓ Solved!", "solved")
    else feedback("Finished — but with a mistake, so it counts as missed. Try it again later.", "failed")
    el.explain.innerHTML = `<div class="pz-line"><b>Solution:</b> ${escapeHtml(p.solution.join(" "))}</div>
      ${p.explanation ? `<p>${escapeHtml(p.explanation)}</p>` : ""}
      <p class="muted pz-source">${escapeHtml(p.source || "")}</p>`
    show(el.explain, true)
    show(el.hint, false); show(el.solution, false); show(el.retry, true); show(el.next, true)
    el.next.textContent = view.pos < view.set.length - 1 ? "Next →" : "Finish set"
    view.results[p.id] = solved ? "solved" : "failed"
    renderSetList()
    await recordIfNeeded()
  }

  async function recordIfNeeded() {
    const a = view.attempt
    if (!a || !shouldRecord(a)) return
    a.recorded = true
    if (!a.done) view.results[a.id] = "failed"
    try {
      await api(`/api/puzzles/${encodeURIComponent(a.id)}/result`, "POST", resultBody(a))
    } catch (_) {
      a.recorded = false  // try again on the next chance
    }
  }

  function onHint() {
    const h = nextHint(puzzle(), view.attempt)
    if (!h) return
    feedback(`💡 ${h.text}`, "hint")
    if (h.square) board.addMarker(MARKER_TYPE.circlePrimary, h.square)
  }

  async function onSolution() {
    const a = view.attempt
    const token = view.token
    setInput(false)
    const line = reveal(puzzle(), a)
    for (const uci of line) {
      await sleep(SOLUTION_STEP)
      if (token !== view.token) return
      play(view.chess, uci)
      await showPosition(view.chess.fen(), {orientation: orientation(), animated: true,
        highlights: [{square: uci.slice(0, 2), color: "green"}, {square: uci.slice(2, 4), color: "green"}]})
    }
    await finish()
  }

  async function onRetry() {
    const p = puzzle()
    retry(p, view.attempt)
    view.attempt.revealed = false
    view.chess = new Chess(p.fen)
    view.token++
    feedback("Try it again.", "")
    show(el.explain, false)
    show(el.hint, true); show(el.solution, true); show(el.retry, false); show(el.next, false)
    renderInfo()
    await showPosition(p.fen, {orientation: orientation()})
    setInput(true)
  }

  async function onNext() {
    await recordIfNeeded()
    if (view.pos < view.set.length - 1) return openPuzzle(view.pos + 1)
    const solved = Object.values(view.results).filter(r => r === "solved").length
    view.summary = `Set complete: ${solved} of ${view.set.length} solved. Your results update what's recommended.`
    return backToDashboard()
  }

  async function backToDashboard() {
    await recordIfNeeded()
    view.token++
    setInput(false)
    view.screen = "dashboard"
    show(el.solver, false)
    show(el.dashboard, true)
    el.title.textContent = "Puzzles"
    el.counter.textContent = ""
    await loadDashboard()
  }

  el.modePersonal.addEventListener("click", () => setMode("personal"))
  el.modePractice.addEventListener("click", () => setMode("practice"))
  el.hint.addEventListener("click", onHint)
  el.solution.addEventListener("click", onSolution)
  el.retry.addEventListener("click", onRetry)
  el.next.addEventListener("click", onNext)
  el.back.addEventListener("click", backToDashboard)

  return {
    // Called when the Puzzles tab is shown: everything is where the learner left it.
    async enter() {
      view.active = true
      if (view.screen === "solver" && puzzle()) {
        const a = view.attempt
        const learner = puzzle().side === "black" ? "b" : "w"
        if (!a.done && view.chess.turn() !== learner && a.index > 0) {
          // left while the opponent's reply was on its way: play it now
          const reply = puzzle().steps[a.index - 1].reply_uci
          if (reply) play(view.chess, reply)
        }
        renderInfo()
        await showPosition(view.chess.fen(), {orientation: orientation()})
        if (!view.active) return
        setStatus("")
        setInput(!view.attempt.done)
      } else {
        board.disableMoveInput()
        await showPosition(new Chess().fen(), {orientation: COLOR.white})
        setStatus("")
        if (view.dashboard) renderDashboard()
        await loadDashboard()
      }
    },
    leave() {
      view.active = false
      view.token++  // a pending reply animation stops; the attempt stays as it was
      view.inputOpen = false
      board.disableMoveInput()
    },
    readable() {
      return view.screen === "solver" ? el.feedback : el.personal
    },
    // for the e2e test / debugging
    state: () => ({screen: view.screen, mode: view.mode, pos: view.pos, total: view.set.length,
      id: puzzle() && puzzle().id, done: Boolean(view.attempt && view.attempt.done)}),
    sameMove,
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
