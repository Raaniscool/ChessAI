// Puzzles tab: a dashboard (Personalized / Practice / Training) and a board-focused solver.
// Training (short games against a bot, analysed afterwards) lives in training.js.
// Rules live in puzzle-solver.js; the server decides which puzzles and checks every one of them.

import {createTrainer} from "./training.js"
import {parsePosition, parsePuzzlePosition} from "./position.js"
import {applyAdapt, autoNextDelay, cardLine, focusLine, header, itemLabel, judge, needsMore, newAttempt, nextHint,
  pendingIds, resultBody, retry, reveal, sameMove, shouldRecord} from "./puzzle-solver.js"

const REPLY_DELAY = 450
const REVERT_DELAY = 550
const SOLUTION_STEP = 700
const MORE_COUNT = 3      // puzzles fetched at a time in a continuous session

export function setupPuzzles({api, board, showPosition, clearMarkers, addMarkers, addMarker, verifiedMove,
  isPositionVerified, Chess, COLOR, MARKER_TYPE, legalInputHandler, escapeHtml, setStatus,
  nav = null, MoveHistory = null, showNode = null, sounds = null}) {
  const $ = id => document.getElementById(id)
  const el = {
    title: $("pz-title"), counter: $("pz-counter"), dashboard: $("pz-dashboard"), solver: $("pz-solver"),
    modePersonal: $("pz-mode-personal"), modePractice: $("pz-mode-practice"), personal: $("pz-personal"),
    practice: $("pz-practice"), status: $("pz-status"), info: $("pz-info"), feedback: $("pz-feedback"),
    explain: $("pz-explain"), hint: $("pz-hint"), solution: $("pz-solution"), retry: $("pz-retry"),
    next: $("pz-next"), back: $("pz-back"), set: $("pz-set"),
    modeTraining: $("pz-mode-training"), training: $("pz-training"), trainer: $("pz-trainer"),
  }
  const view = {
    mode: "personal", screen: "dashboard", dashboard: null, set: [], title: "", pos: 0, attempt: null,
    chess: null, inputOpen: false, token: 0, active: false, results: {}, summary: null,
    body: null, done: [], difficulty: null, adaptNote: null,
    mixed: false,    // Mixed practice: the concept stays hidden until a puzzle is over
    session: 0,      // which training session (a new start or leaving ends the old one)
    loadToken: 0,    // invalidates delayed puzzle-set requests when the tab/mode changes
    more: null,      // the pending request for the next puzzles
    auto: null,      // the pending "next puzzle by itself" {timer, tick}
    history: MoveHistory ? new MoveHistory(new Chess().fen()) : null,   // what's been played (← →)
    explore: null,   // after the puzzle: a board to try other moves on (never graded or recorded)
  }

  const puzzle = () => view.set[view.pos]
  const orientation = () => (puzzle() && puzzle().side === "black" ? COLOR.black : COLOR.white)
  const show = (node, on) => node.classList.toggle("hidden", !on)
  const sound = (move, chess) => { if (sounds && move) sounds.playMove(move, chess.fen()) }

  function checkedPuzzleFen(p, fen) {
    const chess = parsePuzzlePosition(Chess, fen, `Puzzle ${p && p.id ? p.id : "position"}`)
    return chess.fen()
  }

  async function loadPuzzleFen(p) {
    try {
      return checkedPuzzleFen(p, p && p.fen)
    } catch (firstError) {
      if (!p || !p.id) throw firstError
      // A stale/incomplete set response can omit `fen`. Re-fetch the canonical verified puzzle
      // before giving up; never let `new Chess(undefined)` silently substitute the starting board.
      try {
        const full = await api(`/api/puzzles/${encodeURIComponent(p.id)}`)
        const fen = checkedPuzzleFen(p, full && full.fen)
        p.fen = fen
        return fen
      } catch (secondError) {
        throw new Error(`Couldn't load a playable position for this puzzle. The starting position ` +
          `was not substituted. ${secondError.message || firstError.message}`)
      }
    }
  }

  // ------------------------------------------------------------------ dashboard
  function setMode(mode) {
    if (view.mode !== mode) view.loadToken++
    view.mode = mode
    for (const [btn, m] of [[el.modePersonal, "personal"], [el.modePractice, "practice"], [el.modeTraining, "training"]]) {
      btn.classList.toggle("active", m === mode)
      btn.setAttribute("aria-selected", String(m === mode))
    }
    show(el.personal, mode === "personal")
    show(el.practice, mode === "practice")
    show(el.training, mode === "training")
    if (mode === "training") trainer.loadHome(el.training)
  }

  // ------------------------------------------------------------------ Training (training.js)
  const trainer = createTrainer({api, board, showPosition, verifiedMove, addMarkers, isPositionVerified,
    Chess, COLOR, legalInputHandler, escapeHtml, setStatus,
    nav, MoveHistory, showNode, sounds,
    el: {info: $("tr-info"), feedback: $("tr-feedback"), report: $("tr-report"), end: $("tr-end"),
      cont: $("tr-continue"), back: $("tr-back"), title: el.title},
    onShow() {
      cancelAuto()
      view.screen = "trainer"
      el.counter.textContent = ""
      show(el.dashboard, false)
      show(el.solver, false)
      show(el.trainer, true)
    },
    async onExit() {
      view.screen = "dashboard"
      if (nav) nav.set("puzzles", null)
      show(el.trainer, false)
      show(el.dashboard, true)
      el.title.textContent = "Puzzles"
      board.disableMoveInput()
      await showPosition(new Chess().fen(), {orientation: COLOR.white})
      setMode("training")
      loadDashboard()
    },
  })

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
      box.appendChild(startButton(main, "Start training", true))
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
    intro.textContent = "Pick a theme. Puzzles keep coming and adapt to your level as you solve them."
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
    const token = ++view.loadToken
    el.status.textContent = body.mode === "personalized"
      ? "Finding puzzles for this weakness (new ones are checked by Stockfish first)…" : "Finding puzzles…"
    let res
    try {
      res = await api("/api/puzzles/set", "POST", body)
    } catch (err) {
      if (token === view.loadToken && view.active) el.status.textContent = err.message
      return
    }
    if (token !== view.loadToken || !view.active) return
    el.status.textContent = ""
    if (!Array.isArray(res.puzzles) || !res.puzzles.length) {
      el.status.textContent = "No playable positions were returned. Please try loading puzzles again."
      return
    }
    cancelAuto()
    view.session++
    view.more = null
    view.set = res.puzzles
    // Mixed practice is about recognising the idea yourself: a neutral heading, concepts hidden
    view.mixed = Boolean(res.mixed)
    view.title = view.mixed ? "Mixed practice" : res.title
    view.theme = res.title
    view.results = {}
    view.summary = null
    view.body = body
    view.done = []
    view.difficulty = res.difficulty || null
    view.adaptNote = null
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
      b.innerHTML = `<span>${i + 1}. ${escapeHtml(itemLabel(p, r, {mixed: view.mixed}))}</span>
        <span class="badge">${r === "solved" ? "✓" : r === "failed" ? "✗" : escapeHtml(header(p, i, 1).difficulty)}</span>`
      b.addEventListener("click", () => goTo(i))
      el.set.appendChild(b)
    })
  }

  function renderInfo() {
    const p = puzzle()
    const done = Boolean(view.attempt && view.attempt.done)
    const h = header(p, view.pos, view.set.length, done, {mixed: view.mixed, continuous: true})
    el.title.textContent = view.title || "Puzzles"
    el.counter.textContent = h.progress
    el.info.innerHTML = `<div class="pz-side ${p.side}">${escapeHtml(h.side)}</div>
      <div class="pz-objective">${escapeHtml(h.objective)}</div>
      <div class="pz-meta">${h.role ? `<span class="pz-role ${escapeHtml(p.role || "")}">${escapeHtml(h.role)}</span>` : ""}
      ${h.concept ? `<span class="pz-concept">${escapeHtml(h.concept)}</span>` : ""}<span>${escapeHtml(h.difficulty)}</span>
      ${p.steps.length > 1 ? `<span class="pz-dots">${p.steps.map((_s, i) =>
        `<i class="${i < view.attempt.index ? "on" : ""}"></i>`).join("")}</span>` : ""}</div>
      ${h.why ? `<div class="pz-why"><b>Why this puzzle:</b> ${escapeHtml(h.why)}</div>` : ""}
      ${view.adaptNote && view.adaptNote.pos === view.pos ? `<div class="pz-adapt">${escapeHtml(view.adaptNote.text)}</div>`
        : view.pos === 0 && view.difficulty && view.difficulty.summary
          ? `<div class="pz-level">${escapeHtml(view.difficulty.summary)}</div>` : ""}`
  }

  function feedback(text, kind = "") {
    el.feedback.textContent = text
    el.feedback.className = `pz-feedback ${kind}`
  }

  function setInput(open) {
    view.inputOpen = open && view.active
    board.disableMoveInput()
    // once the puzzle is over, either side may be moved to try things out
    if (view.inputOpen) board.enableMoveInput(input, exploring() ? undefined : orientation())
  }

  // ------------------------------------------------------------------ move history (← →)
  // While solving, earlier positions are for looking only: moves are played at the latest
  // position. After the puzzle, any position can be explored; a different move there starts a
  // new continuation (MoveHistory.play). Exploring never counts and never asks the engine.
  const exploring = () => Boolean(view.attempt && view.attempt.done && view.history)

  function track() {
    if (nav && view.history) nav.set("puzzles", view.history, {onView})
  }

  function lock(on) {
    if (!nav) return
    if (on) nav.lock("auto", "puzzles")
    else nav.unlock("auto", "puzzles")
  }

  async function onView(h) {
    const a = view.attempt
    if (!a || view.screen !== "solver") return
    if (a.done) {
      view.explore = new Chess(h.current.fen)
      setInput(true)
      return
    }
    if (h.atLatest) {
      setStatus("")
      setInput(true)
    } else {
      setInput(false)
      setStatus("Looking at an earlier position — press → to get back to the puzzle.")
    }
  }

  async function explore(from, to, receipt) {
    const transition = receipt && receipt.transition
    if (!transition || transition.from !== from || transition.to !== to || !isPositionVerified(transition.afterFen)) {
      throw new Error("The exploration move was not confirmed on the board.")
    }
    sound(transition.move, parsePosition(Chess, transition.afterFen, "Puzzle exploration result"))
    view.history.play(transition.afterFen, transition.uci, transition.move.san)
    view.explore = parsePosition(Chess, transition.afterFen, "Puzzle exploration result")
    nav.refresh()
    await addMarkers([
      {marker: MARKER_TYPE.square, square: from}, {marker: MARKER_TYPE.square, square: to},
    ], transition.afterFen, "Puzzle exploration move")
    setInput(true)
  }

  async function openPuzzle(i) {
    const p = view.set[i]
    const token = ++view.token
    setInput(false)
    el.status.textContent = ""
    if (!p) {
      el.status.textContent = "No puzzle position is available. Please reload the set."
      return
    }
    let fen
    try {
      fen = await loadPuzzleFen(p)
    } catch (err) {
      if (token !== view.token || !view.active) return
      el.status.textContent = err.message
      feedback("This puzzle could not be loaded, so I have not substituted the starting position.", "bad")
      if (view.attempt && !view.attempt.done && view.chess && isPositionVerified(view.chess.fen())) setInput(true)
      return
    }
    if (token !== view.token || view.set[i] !== p) return
    try {
      await showPosition(fen, {orientation: p.side === "black" ? COLOR.black : COLOR.white})
    } catch (err) {
      if (token !== view.token || !view.active) return
      el.status.textContent = `This puzzle position could not be verified. ${err.message}`
      feedback("I stopped before showing or grading this puzzle.", "bad")
      if (view.attempt && !view.attempt.done && view.chess && isPositionVerified(view.chess.fen())) setInput(true)
      return
    }
    if (token !== view.token) return
    // Commit the view/model only after the actual board setup has been confirmed.
    view.pos = i
    view.attempt = newAttempt(p)
    view.chess = parsePosition(Chess, fen, `Puzzle ${p.id}`)
    view.explore = null
    if (view.history) view.history.reset(fen)
    track()
    feedback("")
    show(el.explain, false)
    show(el.hint, true); show(el.solution, true); show(el.retry, false); show(el.next, false)
    renderInfo()
    renderSetList()
    setStatus("")
    setInput(true)
  }

  async function goTo(i) {
    if (i === view.pos && view.attempt && !view.attempt.done) return
    const token = view.token
    const loadToken = view.loadToken
    await recordIfNeeded()
    if (token !== view.token || loadToken !== view.loadToken || !view.active || view.screen !== "solver") return
    await openPuzzle(i)
  }

  const input = legalInputHandler(() => (!view.inputOpen ? null : exploring() ? view.explore : view.chess),
    (from, to, receipt) => (exploring() ? explore(from, to, receipt) : onMove(from, to, receipt)))

  function uciOf(from, to) {
    const legal = view.chess.moves({square: from, verbose: true}).filter(m => m.to === to)
    if (!legal.length) return null
    return from + to + (legal.some(m => m.promotion) ? "q" : "")
  }

  function play(chess, uci) {
    return chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
  }

  async function onMove(from, to, receipt) {
    if (!view.active || view.screen !== "solver" || (view.history && !view.history.atLatest)) return
    const chess = exploring() ? view.explore : view.chess
    const transition = receipt && receipt.transition
    if (!chess || !transition || receipt.beforeFen !== chess.fen() ||
        transition.from !== from || transition.to !== to || !isPositionVerified(transition.afterFen)) {
      setInput(false)
      const restoreFen = chess && chess.fen()
      if (restoreFen) await showPosition(restoreFen, {orientation: orientation()})
      feedback("I couldn't confirm that move, so it was not counted. The board was restored to the last verified position.", "bad")
      if (chess && view.active) setInput(true)
      return
    }
    const token = view.token
    lock(true)   // the opponent's reply / the take-back is played by the program
    try {
      await judgeMove(from, to, receipt, token)
    } catch (err) {
      feedback(`I stopped this puzzle because its board could not be verified. ${err.message}`, "bad")
      setInput(false)
    } finally {
      lock(false)
    }
  }

  async function judgeMove(from, to, receipt, token) {
    if (token !== view.token || !view.active) return
    const transition = receipt.transition
    const uci = transition.uci
    const expectedUci = uciOf(from, to)
    if (!expectedUci || expectedUci.slice(0, 4) !== uci.slice(0, 4)) return
    const p = puzzle()
    const trial = new Chess(view.chess.fen())
    const trialMove = play(trial, uci)
    if (!trialMove || trial.fen() !== transition.afterFen) throw new Error("The puzzle move result did not match the verified board.")
    sound(transition.move, parsePosition(Chess, transition.afterFen, "Puzzle move result"))
    const r = judge(p, view.attempt, uci, {mate: trial.in_checkmate()})
    if (r.verdict === "wrong" || r.verdict === "good") {
      setInput(false)
      feedback(r.message, r.verdict)
      const marker = r.verdict === "wrong" ? MARKER_TYPE.circleDanger : MARKER_TYPE.circle
      await addMarker(marker, to, transition.afterFen)
      if (token !== view.token || !view.active) return
      await sleep(REVERT_DELAY) // visual feedback only; setup below is awaited and verified
      if (token !== view.token) return
      await showPosition(view.chess.fen(), {orientation: orientation()})
      if (token !== view.token || !view.active) return
      setInput(true)
      return
    }
    view.chess = parsePosition(Chess, transition.afterFen, "Puzzle move result")
    if (view.history) view.history.append(transition.afterFen, uci, transition.move.san)
    if (nav) nav.refresh()
    await clearMarkers(transition.afterFen)
    if (token !== view.token || !view.active) return
    await addMarkers([
      {marker: MARKER_TYPE.square, square: from}, {marker: MARKER_TYPE.square, square: to},
    ], transition.afterFen, "Puzzle move highlight")
    if (token !== view.token || !view.active) return
    feedback(r.message, "correct")
    renderInfo()
    if (r.done) return finish()
    setInput(false)
    await sleep(REPLY_DELAY) // pacing only; the reply itself is serialized and verified below
    if (token !== view.token) return
    if (r.reply) {
      const replyPiece = view.chess.get(r.reply.slice(0, 2))
      if (!replyPiece) throw new Error(`The puzzle reply's source piece is missing from ${r.reply.slice(0, 2)}.`)
      const reply = await verifiedMove(view.chess.fen(), r.reply, {
        expectedPiece: {color: replyPiece.color, type: replyPiece.type}, label: "Puzzle reply",
      })
      if (token !== view.token || !view.active) return
      view.chess = parsePosition(Chess, reply.afterFen, "Puzzle reply result")
      sound(reply.move, view.chess)
      if (view.history) view.history.append(reply.afterFen, r.reply, reply.move.san)
      if (nav) nav.refresh()
      await addMarkers([
        {marker: MARKER_TYPE.square, square: r.reply.slice(0, 2)},
        {marker: MARKER_TYPE.square, square: r.reply.slice(2, 4)},
      ], reply.afterFen, "Puzzle reply highlight")
      if (token !== view.token || !view.active) return
    }
    if (token !== view.token || !view.active) return
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
      <p class="muted pz-source">${escapeHtml(p.source || "")}</p>
      ${view.history ? `<p class="muted pz-explore">← → steps through the moves. You can also try other moves on
        the board — they don't count.</p>` : ""}`
    show(el.explain, true)
    show(el.hint, false); show(el.solution, false); show(el.retry, true); show(el.next, true)
    el.next.textContent = "Next →"
    view.results[p.id] = solved ? "solved" : "failed"
    renderInfo()   // in Mixed practice, now the theme, the real objective and the named "why" are shown
    renderSetList()
    if (view.history) {   // free exploration from here (MoveHistory keeps what was played)
      view.explore = new Chess(view.history.current.fen)
      setInput(true)
    }
    const token = view.token
    await recordIfNeeded()   // record -> the difficulty profile updates -> the next selection uses it
    if (token !== view.token || view.screen !== "solver") return
    if (needsMore(view.set, view.results, view.pos)) fetchMore()   // the next puzzles, chosen now
    scheduleAuto(autoNextDelay(a))
  }

  // ------------------------------------------------------------------ continuous session
  // A training session never ends by itself: after each puzzle the next one loads (after a short
  // pause, cancelled by any interaction), and more puzzles are fetched before the queue runs
  // out — chosen with everything recorded so far (POST /api/puzzles/next: the same selection as
  // the first set, never a puzzle already in the session; generation or review when the theme's
  // fresh puzzles are used up). Only "Back to Puzzles" ends it.
  function fetchMore() {
    if (view.more) return view.more
    const session = view.session
    const b = view.body || {}
    view.more = api("/api/puzzles/next", "POST", {mode: b.mode, concept: b.concept, weakness: b.weakness,
      username: b.username, count: MORE_COUNT, exclude: view.set.map(p => p.id), done: view.done.slice(-8)})
      .then(res => {
        if (session !== view.session || !res) return 0
        const have = new Set(view.set.map(p => p.id))
        const fresh = (res.puzzles || []).filter(p => !have.has(p.id))
        if (!fresh.length) return 0
        view.set = [...view.set, ...fresh]
        if (res.difficulty) view.difficulty = res.difficulty
        if (res.session && res.session.reason) view.adaptNote = {pos: view.set.length - fresh.length, text: res.session.reason}
        renderSetList()
        return fresh.length
      })
      .catch(() => 0)
      .finally(() => { if (session === view.session) view.more = null })
    return view.more
  }

  function cancelAuto() {
    if (!view.auto) return
    clearTimeout(view.auto.timer)
    clearInterval(view.auto.tick)
    if (view.auto.node) view.auto.node.remove()
    view.auto = null
  }

  function scheduleAuto(ms) {
    cancelAuto()
    if (!view.active) return
    const node = document.createElement("div")
    node.className = "pz-auto muted"
    const left = document.createElement("span")
    const stay = document.createElement("button")
    stay.className = "btn small"
    stay.textContent = "Stay here"
    stay.addEventListener("click", cancelAuto)
    node.append("Next puzzle in ", left, " ", stay)
    el.explain.appendChild(node)
    const end = Date.now() + ms
    const update = () => { left.textContent = `${Math.max(1, Math.ceil((end - Date.now()) / 1000))}s` }
    update()
    const token = view.token
    view.auto = {node, tick: setInterval(update, 250), timer: setTimeout(() => {
      cancelAuto()
      if (token === view.token && view.active && view.screen === "solver") onNext()
    }, ms)}
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
      return
    }
    view.done.push({id: a.id, ...resultBody(a)})
    await adapt()
  }

  // Keep the rest of the set in the productive zone: two instant solves -> the remaining puzzles
  // that are now too easy are swapped for harder ones; two tough ones -> the reverse. Best effort.
  async function adapt() {
    const set = view.set
    const remaining = pendingIds(set, view.results, view.pos)
    if (!view.body || view.done.length < 2 || !remaining.length) return
    let res
    try {
      res = await api("/api/puzzles/adapt", "POST", {mode: view.body.mode, concept: view.body.concept,
        weakness: view.body.weakness, username: view.body.username, done: view.done, remaining,
        set_ids: set.map(p => p.id)})
    } catch (_) {
      return
    }
    if (set !== view.set || !res || !res.replace) return   // a different set started meanwhile
    const {set: next, changed} = applyAdapt(view.set, view.results, view.pos, res.replace)
    if (!changed) return
    view.set = next
    if (res.difficulty) view.difficulty = res.difficulty
    view.adaptNote = {pos: view.pos + 1, text: res.reason}
    renderSetList()
  }

  async function onHint() {
    if (!view.active || view.screen !== "solver" || !view.attempt || view.attempt.done) return
    const h = nextHint(puzzle(), view.attempt)
    if (!h) return
    feedback(`💡 ${h.text}`, "hint")
    if (h.square) {
      const fen = exploring() ? view.explore.fen() : view.chess.fen()
      const piece = parsePosition(Chess, fen, "Puzzle hint position").get(h.square)
      if (isPositionVerified(fen)) await addMarker(MARKER_TYPE.circlePrimary, h.square, fen,
        piece ? {color: piece.color, type: piece.type} : null)
    }
  }

  async function onSolution() {
    const a = view.attempt
    if (!a || a.done || !view.active || view.screen !== "solver") return
    const token = view.token
    setInput(false)
    try {
      if (view.history && !view.history.atLatest) {   // was looking back: continue from the latest position
        const oldCursor = view.history.cursor
        view.history.toLatest()
        try {
          await showPosition(view.chess.fen(), {orientation: orientation()})
        } catch (err) {
          view.history.cursor = oldCursor
          if (nav) nav.refresh()
          throw err
        }
      }
      if (token !== view.token || !view.active) return
    } catch (err) {
      if (token === view.token && view.active) {
        feedback(`I stopped before showing the solution because the puzzle position could not be verified. ${err.message}`, "bad")
      }
      return
    }
    // Preview on a copy. Mark the attempt as revealed only after the full solution line is verified.
    const previewAttempt = {...a, played: [...a.played]}
    const line = reveal(puzzle(), previewAttempt)
    const startFen = view.chess.fen()
    const historySnapshot = view.history && view.history.nodes.map(node => ({...node}))
    const historyCursor = view.history && view.history.cursor
    const restoreLocalSolutionStart = () => {
      view.chess = parsePosition(Chess, startFen, "Puzzle solution starting position")
      if (view.history && historySnapshot) {
        view.history.nodes = historySnapshot
        view.history.cursor = historyCursor
        if (nav) nav.refresh()
      }
    }
    lock(true)   // the solution plays by itself
    try {
      for (const uci of line) {
        await sleep(SOLUTION_STEP) // visual pacing only; every move below waits for board confirmation
        if (token !== view.token || !view.active) { restoreLocalSolutionStart(); return }
        const piece = view.chess.get(uci.slice(0, 2))
        if (!piece) throw new Error(`The solution's source piece is missing from ${uci.slice(0, 2)}.`)
        const transition = await verifiedMove(view.chess.fen(), uci, {
          expectedPiece: {color: piece.color, type: piece.type}, label: "Puzzle solution",
        })
        if (token !== view.token || !view.active) { restoreLocalSolutionStart(); return }
        view.chess = parsePosition(Chess, transition.afterFen, "Puzzle solution result")
        if (view.history) view.history.append(transition.afterFen, uci, transition.move.san)
        if (nav) nav.refresh()
        sound(transition.move, view.chess)
        if (!await addMarkers([
          {marker: MARKER_TYPE.square, square: transition.from},
          {marker: MARKER_TYPE.square, square: transition.to},
        ], transition.afterFen, "Puzzle solution move")) {
          throw new Error("A solution-move highlight could not be confirmed on the board.")
        }
        if (token !== view.token || !view.active) { restoreLocalSolutionStart(); return }
      }
      if (!isPositionVerified(view.chess.fen())) throw new Error("The final solution position could not be confirmed.")
      reveal(puzzle(), a)
    } catch (err) {
      if (token !== view.token || !view.active) { restoreLocalSolutionStart(); return }
      try {
        await showPosition(startFen, {orientation: orientation()})
        restoreLocalSolutionStart()
        setInput(true)
      } catch (_) { /* leave input closed if the solution start cannot be re-established */ }
      feedback(`I stopped before showing the puzzle result because the board could not be verified. ${err.message}`, "bad")
      return
    } finally {
      lock(false)
    }
    await finish()
  }

  async function onRetry() {
    if (!view.active || view.screen !== "solver" || !view.attempt) return
    cancelAuto()
    const p = puzzle()
    const token = ++view.token
    setInput(false)
    let fen
    try {
      fen = await loadPuzzleFen(p)
      if (token !== view.token || !view.active) return
      await showPosition(fen, {orientation: p.side === "black" ? COLOR.black : COLOR.white})
    } catch (err) {
      if (token !== view.token || !view.active) return
      el.status.textContent = err.message
      feedback("This puzzle could not be reloaded; the starting position was not substituted.", "bad")
      if (view.attempt && !view.attempt.done && isPositionVerified(view.chess.fen())) setInput(true)
      return
    }
    if (token !== view.token) return
    retry(p, view.attempt)
    view.attempt.revealed = false
    view.chess = parsePosition(Chess, fen, `Puzzle ${p.id}`)
    view.explore = null
    if (view.history) view.history.reset(fen)
    track()
    feedback("Try it again.", "")
    show(el.explain, false)
    show(el.hint, true); show(el.solution, true); show(el.retry, false); show(el.next, false)
    renderInfo()
    setInput(true)
  }

  async function onNext() {
    if (view.screen !== "solver" || !(view.attempt && view.attempt.done)) return   // only after a puzzle
    cancelAuto()
    const token = ++view.token
    await recordIfNeeded()
    if (token !== view.token || !view.active || view.screen !== "solver") return
    if (view.pos >= view.set.length - 1) {
      el.status.textContent = ""
      setStatus("Finding the next puzzle…")
      await fetchMore()
      if (token !== view.token || view.screen !== "solver") return
      setStatus("")
      if (view.pos >= view.set.length - 1) {
        feedback("Couldn't load another puzzle just now — press Next → to try again.", "")
        return
      }
    }
    return openPuzzle(view.pos + 1)
  }

  async function backToDashboard() {
    cancelAuto()
    const token = ++view.token
    view.loadToken++
    view.session++
    view.more = null
    setInput(false)
    await recordIfNeeded()
    if (token !== view.token || !view.active) return
    const finished = Object.values(view.results)
    if (view.screen === "solver" && finished.length) {
      const solved = finished.filter(r => r === "solved").length
      view.summary = `Session: ${solved} of ${finished.length} solved. Your results update what's recommended.`
    }
    if (nav) nav.set("puzzles", null)   // nothing to step through on the dashboard
    view.screen = "dashboard"
    show(el.solver, false)
    show(el.dashboard, true)
    el.title.textContent = "Puzzles"
    el.counter.textContent = ""
    await loadDashboard()
  }

  el.modePersonal.addEventListener("click", () => setMode("personal"))
  el.modePractice.addEventListener("click", () => setMode("practice"))
  el.modeTraining.addEventListener("click", () => setMode("training"))
  el.hint.addEventListener("click", onHint)
  el.solution.addEventListener("click", onSolution)
  el.retry.addEventListener("click", onRetry)
  el.next.addEventListener("click", onNext)
  el.back.addEventListener("click", backToDashboard)
  // looking around after a puzzle (← →, a move on the board, an arrow) pauses the automatic next
  for (const ev of ["mousedown", "touchstart"]) board.context.addEventListener(ev, cancelAuto, {passive: true})
  for (const id of ["nav-prev", "nav-next"]) { const b = $(id); if (b) b.addEventListener("click", cancelAuto) }
  document.addEventListener("keydown", e => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") cancelAuto() })

  return {
    // Lessons chat can open a theme practice set without replacing its server-owned lesson session.
    async startFromCoach({concept, count = 5} = {}) {
      if (!concept) throw new Error("A verified practice theme is required.")
      setMode("practice")
      return startSet({mode: "practice", concept, count, generate: true})
    },
    // Called when the Puzzles tab is shown: everything is where the learner left it.
    async enter() {
      view.active = true
      const token = view.token
      trainer.activate()
      if (view.screen === "trainer") {
        await trainer.enter()
        return
      }
      if (view.screen === "solver" && puzzle()) {
        const a = view.attempt
        try {
          const visibleFen = view.history ? view.history.current.fen : view.chess.fen()
          await showPosition(visibleFen, {orientation: orientation()})
          if (!view.active || token !== view.token) return
          const learner = puzzle().side === "black" ? "b" : "w"
          if (!a.done && (!view.history || view.history.atLatest) && view.chess.turn() !== learner && a.index > 0) {
            // If the tab was left while the opponent reply was pending, verify that reply before
            // adding it to the local history or letting the learner play again.
            const replyUci = puzzle().steps[a.index - 1].reply_uci
            if (replyUci) {
              const from = replyUci.slice(0, 2)
              const piece = view.chess.get(from)
              if (!piece) throw new Error(`The opponent's reply source piece is missing from ${from}.`)
              const reply = await verifiedMove(view.chess.fen(), replyUci, {
                expectedPiece: {color: piece.color, type: piece.type}, label: "Puzzle opponent reply",
              })
              if (!view.active || token !== view.token) return
              view.chess = parsePosition(Chess, reply.afterFen, "Puzzle reply result")
              if (view.history) view.history.append(reply.afterFen, replyUci, reply.move.san)
            }
          }
          renderInfo()
          track()
          if (view.history && showNode) await showNode(view.history.current, view.history, orientation())
          else await showPosition(view.chess.fen(), {orientation: orientation()})
          if (!view.active || token !== view.token) return
          if (a.done && !view.results[puzzle().id]) await finish()
          else if (view.history && (a.done || !view.history.atLatest)) await onView(view.history)
          else setInput(!view.attempt.done)
          setStatus("")
        } catch (err) {
          if (view.active && token === view.token) {
            setInput(false)
            setStatus(`I couldn't restore this puzzle position safely. ${err.message}`)
            feedback("The puzzle is paused until its board position can be verified.", "bad")
          }
        }
      } else {
        board.disableMoveInput()
        await showPosition(new Chess().fen(), {orientation: COLOR.white})
        if (!view.active || token !== view.token) return
        setStatus("")
        if (view.dashboard) renderDashboard()
        if (view.mode === "training") trainer.loadHome(el.training)
        await loadDashboard()
      }
    },
    leave() {
      trainer.leave()
      cancelAuto()
      view.active = false
      view.token++  // a pending reply animation stops; the attempt stays as it was
      view.loadToken++
      view.inputOpen = false
      board.disableMoveInput()
    },
    readable() {
      return view.screen === "trainer" ? $("tr-report") : view.screen === "solver" ? el.feedback : el.personal
    },
    // for the e2e test / debugging
    state: () => ({screen: view.screen, mode: view.mode, pos: view.pos, total: view.set.length,
      id: puzzle() && puzzle().id, done: Boolean(view.attempt && view.attempt.done), training: trainer.state()}),
    sameMove,
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
