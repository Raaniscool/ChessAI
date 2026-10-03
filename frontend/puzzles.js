// Puzzles tab: a dashboard (Personalized / Practice) and a board-focused solver.
// Rules live in puzzle-solver.js; the server decides which puzzles and checks every one of them.

import {applyAdapt, autoNextDelay, cardLine, focusLine, header, itemLabel, judge, needsMore, newAttempt, nextHint,
  pendingIds, resultBody, retry, reveal, sameMove, shouldRecord} from "./puzzle-solver.js"

const REPLY_DELAY = 450
const REVERT_DELAY = 550
const SOLUTION_STEP = 700
const MORE_COUNT = 3      // puzzles fetched at a time in a continuous session

export function setupPuzzles({api, board, showPosition, clearMarkers, Chess, COLOR, MARKER_TYPE, legalInputHandler,
  escapeHtml, setStatus, nav = null, MoveHistory = null, showNode = null, sounds = null}) {
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
    body: null, done: [], difficulty: null, adaptNote: null,
    mixed: false,    // Mixed practice: the concept stays hidden until a puzzle is over
    session: 0,      // which training session (a new start or leaving ends the old one)
    more: null,      // the pending request for the next puzzles
    auto: null,      // the pending "next puzzle by itself" {timer, tick}
    history: MoveHistory ? new MoveHistory(new Chess().fen()) : null,   // what's been played (← →)
    explore: null,   // after the puzzle: a board to try other moves on (never graded or recorded)
  }

  const puzzle = () => view.set[view.pos]
  const orientation = () => (puzzle() && puzzle().side === "black" ? COLOR.black : COLOR.white)
  const show = (node, on) => node.classList.toggle("hidden", !on)
  const sound = (move, chess) => { if (sounds && move) sounds.playMove(move, chess.fen()) }

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

  async function explore(from, to) {
    const chess = new Chess(view.history.current.fen)
    const legal = chess.moves({square: from, verbose: true}).filter(m => m.to === to)
    if (!legal.length) return
    const move = chess.move({from, to, promotion: legal.some(m => m.promotion) ? "q" : undefined})
    sound(move, chess)
    view.history.play(chess.fen(), move.from + move.to + (move.promotion || ""), move.san)
    view.explore = new Chess(chess.fen())
    nav.refresh()
    await showNode(view.history.current)
    setInput(true)
  }

  async function openPuzzle(i) {
    view.pos = i
    const p = puzzle()
    view.attempt = newAttempt(p)
    view.chess = new Chess(p.fen)
    if (view.history) view.history.reset(p.fen)
    track()
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

  const input = legalInputHandler(() => (!view.inputOpen ? null : exploring() ? view.explore : view.chess),
    (from, to) => (exploring() ? explore(from, to) : onMove(from, to)))

  function uciOf(from, to) {
    const legal = view.chess.moves({square: from, verbose: true}).filter(m => m.to === to)
    if (!legal.length) return null
    return from + to + (legal.some(m => m.promotion) ? "q" : "")
  }

  function play(chess, uci) {
    return chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
  }

  async function onMove(from, to) {
    if (view.history && !view.history.atLatest) return   // earlier positions are for looking
    lock(true)   // the opponent's reply / the take-back is played by the program
    try {
      await judgeMove(from, to)
    } finally {
      lock(false)
    }
  }

  async function judgeMove(from, to) {
    const uci = uciOf(from, to)
    if (!uci) return
    const p = puzzle()
    const token = view.token
    const trial = new Chess(view.chess.fen())
    sound(play(trial, uci), trial)   // the learner's move sounds once, right away (also when it's wrong)
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
    const mine = play(view.chess, uci)
    if (view.history) view.history.append(view.chess.fen(), uci, mine && mine.san)
    if (nav) nav.refresh()
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
      const reply = play(view.chess, r.reply)
      sound(reply, view.chess)
      if (view.history) view.history.append(view.chess.fen(), r.reply, reply && reply.san)
      if (nav) nav.refresh()
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
    if (view.history && !view.history.atLatest) {   // was looking back: continue from the latest position
      view.history.toLatest()
      await showPosition(view.chess.fen(), {orientation: orientation()})
    }
    const line = reveal(puzzle(), a)
    lock(true)   // the solution plays by itself
    try {
      for (const uci of line) {
        await sleep(SOLUTION_STEP)
        if (token !== view.token) return
        const move = play(view.chess, uci)
        if (view.history) view.history.append(view.chess.fen(), uci, move && move.san)
        if (nav) nav.refresh()
        sound(move, view.chess)
        await showPosition(view.chess.fen(), {orientation: orientation(), animated: true,
          highlights: [{square: uci.slice(0, 2), color: "green"}, {square: uci.slice(2, 4), color: "green"}]})
      }
    } finally {
      lock(false)
    }
    await finish()
  }

  async function onRetry() {
    cancelAuto()
    const p = puzzle()
    retry(p, view.attempt)
    view.attempt.revealed = false
    view.chess = new Chess(p.fen)
    if (view.history) view.history.reset(p.fen)
    track()
    view.token++
    feedback("Try it again.", "")
    show(el.explain, false)
    show(el.hint, true); show(el.solution, true); show(el.retry, false); show(el.next, false)
    renderInfo()
    await showPosition(p.fen, {orientation: orientation()})
    setInput(true)
  }

  async function onNext() {
    if (view.screen !== "solver" || !(view.attempt && view.attempt.done)) return   // only after a puzzle
    cancelAuto()
    const token = ++view.token
    await recordIfNeeded()
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
    await recordIfNeeded()
    const finished = Object.values(view.results)
    if (view.screen === "solver" && finished.length) {
      const solved = finished.filter(r => r === "solved").length
      view.summary = `Session: ${solved} of ${finished.length} solved. Your results update what's recommended.`
    }
    view.session++
    view.more = null
    view.token++
    setInput(false)
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
    // Called when the Puzzles tab is shown: everything is where the learner left it.
    async enter() {
      view.active = true
      if (view.screen === "solver" && puzzle()) {
        const a = view.attempt
        const learner = puzzle().side === "black" ? "b" : "w"
        if (!a.done && view.chess.turn() !== learner && a.index > 0) {
          // left while the opponent's reply was on its way: play it now
          const reply = puzzle().steps[a.index - 1].reply_uci
          if (reply) {
            const move = play(view.chess, reply)
            if (view.history) view.history.append(view.chess.fen(), reply, move && move.san)
          }
        }
        renderInfo()
        track()
        if (view.history && showNode) await showNode(view.history.current, view.history, orientation())
        else await showPosition(view.chess.fen(), {orientation: orientation()})
        if (!view.active) return
        setStatus("")
        if (view.history && (a.done || !view.history.atLatest)) await onView(view.history)
        else setInput(!view.attempt.done)
      } else {
        board.disableMoveInput()
        await showPosition(new Chess().fen(), {orientation: COLOR.white})
        setStatus("")
        if (view.dashboard) renderDashboard()
        await loadDashboard()
      }
    },
    leave() {
      cancelAuto()
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
