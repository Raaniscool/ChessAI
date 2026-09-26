// Game Analysis screen: import Chess.com games, watch Stockfish analyze them,
// step through the moments that matter, and turn recurring weaknesses into
// training. The server decides everything about the chess; this file only shows it.

import {analysisLine, evalForLearner, gameMeta, matchup, progressText, resultLabel, reviewItems, uciSquares,
  weaknessLine} from "./game-format.js"

const USERNAME_KEY = "chessai.chesscomUsername"

// Storage can be missing or throw (private browsing): remembering the name is a nicety.
const store = {
  get(key) { try { return globalThis.localStorage.getItem(key) } catch (_) { return null } },
  set(key, value) { try { globalThis.localStorage.setItem(key, value) } catch (_) { /* not remembered */ } },
}

export function setupGameAnalysis(ctx) {
  const {api, streamEvents, stopStream, board, showPosition, clearMarkers, Chess, COLOR, MARKER_TYPE, escapeHtml,
    setStatus, makeSpeakable, narrator, onTraining} = ctx

  const el = {
    pgn: document.getElementById("ga-pgn"),
    username: document.getElementById("ga-username"),
    importBtn: document.getElementById("ga-import-btn"),
    status: document.getElementById("ga-status"),
    importCard: document.getElementById("ga-import"),
    overview: document.getElementById("ga-overview"),
    review: document.getElementById("ga-review"),
    games: document.getElementById("ga-games"),
    title: document.getElementById("ga-title"),
    counter: document.getElementById("ga-counter"),
    back: document.getElementById("ga-back"),
    prev: document.getElementById("ga-prev"),
    next: document.getElementById("ga-next"),
    body: document.getElementById("ga-body"),
  }

  const view = {
    games: [],          // summaries from GET /api/games
    weaknesses: null,   // GET /api/games/weaknesses
    game: null,         // GET /api/games/{id}
    items: [],          // moments + habits of the open game
    index: 0,
    busy: false,
    token: 0,           // bumps on every board action so a running line animation stops
  }

  el.username.value = store.get(USERNAME_KEY) || ""

  // Once there are games, the import form folds into one button so the overview has room.
  const importMore = button("＋ Import more games", "ga-more", () => {
    el.importCard.classList.remove("collapsed")
    el.pgn.focus()
  }, "Paste more Chess.com games")
  el.importCard.prepend(importMore)

  // ---------------------------------------------------------------- helpers
  const show = (node, on = true) => node.classList.toggle("hidden", !on)

  function setImportStatus(html, kind = "") {
    el.status.className = `ga-status ${kind}`
    el.status.innerHTML = html
  }

  function button(label, cls, onClick, title = "") {
    const b = document.createElement("button")
    b.className = `btn ${cls || ""}`.trim()
    b.textContent = label
    if (title) b.title = title
    b.addEventListener("click", onClick)
    return b
  }

  function orientation() {
    return view.game && view.game.game.player_color === "black" ? COLOR.black : COLOR.white
  }

  // ---------------------------------------------------------------- import + analyze
  async function importGames(username) {
    const pgn = el.pgn.value.trim()
    if (!pgn) {
      setImportStatus("Paste at least one PGN first.", "error")
      return
    }
    if (username !== undefined) el.username.value = username
    const name = el.username.value.trim()
    if (name) store.set(USERNAME_KEY, name)
    view.busy = true
    el.importBtn.disabled = true
    setImportStatus("Checking the games…")
    try {
      const res = await api("/api/games/import", "POST", {pgn, username: name || null})
      const skipped = res.errors.map(e => `<li>Game ${e.index}${e.white ? ` (${escapeHtml(e.white)} vs ${escapeHtml(e.black || "?")})` : ""}: ${escapeHtml(e.error)}</li>`).join("")
      const got = res.imported.length
      setImportStatus(`Imported ${got} game${got === 1 ? "" : "s"}.` +
        (skipped ? `<div class="muted">Skipped:</div><ul class="ga-errors">${skipped}</ul>` : ""), skipped ? "warn" : "")
      el.pgn.value = ""
      await analyze(res.imported.map(g => g.id))
    } catch (err) {
      if (err.data && err.data.needs_player) {
        askWhichPlayer(err.data.players)
      } else {
        setImportStatus(escapeHtml(err.message), "error")
      }
    } finally {
      view.busy = false
      el.importBtn.disabled = false
    }
  }

  function askWhichPlayer(players) {
    setImportStatus("Which player are you? (Next time, type your Chess.com username above.)", "warn")
    const row = document.createElement("div")
    row.className = "suggestions"
    for (const name of players) {
      const chip = document.createElement("button")
      chip.className = "chip"
      chip.textContent = name
      chip.addEventListener("click", () => importGames(name))
      row.appendChild(chip)
    }
    el.status.appendChild(row)
  }

  async function analyze(gameIds) {
    const bar = document.createElement("div")
    bar.className = "ga-progress"
    bar.innerHTML = `<div class="ga-progress-text">Starting Stockfish…</div><div class="ga-bar"><span></span></div>`
    el.status.appendChild(bar)
    const text = bar.querySelector(".ga-progress-text")
    const fill = bar.querySelector(".ga-bar span")
    const failures = []
    try {
      await streamEvents("/api/games/analyze", {game_ids: gameIds}, ev => {
        if (ev.type === "start" && !ev.count) text.textContent = "Already analyzed."
        if (ev.type === "progress") {
          const p = progressText(ev)
          text.textContent = p.text
          fill.style.width = `${p.pct}%`
        }
        if (ev.type === "game_done") loadGames()
        if (ev.type === "error") failures.push(ev.error)
        if (ev.type === "done") {
          view.weaknesses = ev.weaknesses
          text.textContent = failures.length ? `Done, but ${failures.length} game(s) failed: ${failures[0]}` : "Analysis done."
          fill.style.width = "100%"
        }
      }, {exclusive: false})
    } catch (err) {
      text.textContent = err.message
      bar.classList.add("error")
    }
    await loadGames()
    renderOverview()
  }

  // ---------------------------------------------------------------- overview
  async function loadGames() {
    try {
      view.games = (await api("/api/games")).games
    } catch (err) {
      el.games.textContent = `Couldn't load your games: ${err.message}`
      return
    }
    renderGameList()
  }

  function renderGameList() {
    el.games.innerHTML = ""
    if (!view.games.length) {
      const p = document.createElement("p")
      p.className = "muted empty"
      p.textContent = "No games yet — paste a Chess.com PGN to start."
      el.games.appendChild(p)
      return
    }
    for (const g of view.games) {
      const row = document.createElement("button")
      row.className = "game-item" + (view.game && view.game.game.id === g.id ? " active" : "")
      row.innerHTML = `<span class="game-vs">${escapeHtml(g.opponent || "?")} <span class="res ${g.learner_result || "unfinished"}">${escapeHtml(resultLabel(g))}</span></span>` +
        `<span class="muted">${escapeHtml([g.opening, g.date].filter(Boolean).join(" · "))}</span>` +
        `<span class="muted">${escapeHtml(analysisLine(g.analysis))}</span>`
      row.addEventListener("click", () => openGame(g.id))
      el.games.appendChild(row)
    }
  }

  async function renderOverview() {
    view.token++
    view.game = null
    view.items = []
    stopStream()
    show(el.review, false)
    show(el.importCard, true)
    show(el.overview, true)
    el.importCard.classList.toggle("collapsed", view.games.length > 0 && !el.pgn.value.trim())
    ;[el.back, el.prev, el.next].forEach(b => show(b, false))
    el.title.textContent = "Game Analysis"
    el.counter.textContent = ""
    renderGameList()
    if (!view.weaknesses && view.games.some(g => g.analysis)) {
      try { view.weaknesses = await api("/api/games/weaknesses") } catch (_) { /* shown as empty */ }
    }
    el.overview.innerHTML = ""
    const analyzed = view.games.filter(g => g.analysis)
    if (!analyzed.length) return
    const w = view.weaknesses || {weaknesses: [], seen_once: [], total_games: analyzed.length, min_games: 2}
    const card = document.createElement("div")
    card.className = "ga-card ga-summary"
    const h = document.createElement("h3")
    h.textContent = `What I noticed in your ${w.total_games} analyzed game${w.total_games === 1 ? "" : "s"}`
    card.appendChild(h)
    if (w.weaknesses.length) {
      const list = document.createElement("div")
      list.className = "weakness-list"
      for (const item of w.weaknesses) list.appendChild(weaknessRow(item, true))
      card.appendChild(list)
      if (w.weaknesses.length > 1) {
        card.appendChild(button("▶ Train all of these", "primary", () => startTraining(w.weaknesses.map(x => x.key))))
      }
    } else {
      const p = document.createElement("p")
      p.className = "muted"
      p.textContent = w.total_games < w.min_games
        ? `A weakness only counts when it shows up in at least ${w.min_games} different games — import a few more games.`
        : "No mistake repeats across your games yet — nice. Review the games below."
      card.appendChild(p)
    }
    if (w.seen_once.length) {
      const details = document.createElement("details")
      details.className = "no-speech"
      details.innerHTML = `<summary>Seen in only one game (${w.seen_once.length}) — not a pattern yet</summary>`
      for (const item of w.seen_once) details.appendChild(weaknessRow(item, false))
      card.appendChild(details)
    }
    const tip = document.createElement("p")
    tip.className = "muted"
    tip.textContent = "Pick a game on the left to step through its mistakes."
    card.appendChild(tip)
    el.overview.appendChild(card)
    makeSpeakable(card)

  }

  function weaknessRow(w, recurring) {
    const row = document.createElement("div")
    row.className = "weakness" + (recurring ? " recurring" : "")
    const text = document.createElement("div")
    text.innerHTML = `<b>${escapeHtml(w.title)}</b><div class="muted">${escapeHtml(weaknessLine(w))}` +
      (w.library_examples ? ` · ${w.library_examples} verified example${w.library_examples === 1 ? "" : "s"} to practise` : "") +
      `</div>`
    row.appendChild(text)
    const actions = document.createElement("div")
    actions.className = "weakness-actions"
    const first = w.evidence && w.evidence[0]
    if (first) {
      actions.appendChild(button("See it", "", () => openGame(first.game_id, first.moment_id),
        "Jump to where it happened"))
    }
    actions.appendChild(button("▶ Start training", recurring ? "primary" : "", () => startTraining([w.key])))
    row.appendChild(actions)
    return row
  }

  async function startTraining(keys) {
    try {
      const res = await api("/api/games/training", "POST", {keys})
      onTraining(res)
    } catch (err) {
      setImportStatus(escapeHtml(err.message), "error")
    }
  }

  // ---------------------------------------------------------------- review
  async function openGame(gameId, momentId = null) {
    let game
    try {
      game = await api(`/api/games/${encodeURIComponent(gameId)}`)
    } catch (err) {
      setImportStatus(escapeHtml(err.message), "error")
      return
    }
    view.game = game
    view.items = reviewItems(game.analysis)
    const at = momentId ? view.items.findIndex(m => m.id === momentId) : 0
    view.index = Math.max(0, at)
    show(el.importCard, false)
    show(el.overview, false)
    show(el.review, true)
    show(el.back, true)
    el.title.textContent = matchup(game.game)
    renderGameList()
    renderMoment()
  }

  function gameHeader(summary) {
    const div = document.createElement("div")
    div.className = "ga-game-head"
    const meta = document.createElement("span")
    meta.className = "muted"
    meta.textContent = gameMeta(summary)
    div.appendChild(meta)
    if (summary.url) {
      const a = document.createElement("a")
      a.href = summary.url
      a.target = "_blank"
      a.rel = "noopener"
      a.textContent = "View on Chess.com ↗"
      div.appendChild(a)
    }
    return div
  }

  // Every position a moment's text can talk about (before, after, Stockfish's line, the
  // reply line), so a spoken or pointed-at move lights up the right squares.
  function positionsOf(m) {
    const fens = [m.fen_before]
    if (m.fen_after) fens.push(m.fen_after)
    for (const [start, line] of [[m.fen_before, m.best_line], [m.fen_after, m.reply_line]]) {
      if (!start || !line) continue
      const chess = new Chess(start)
      for (const san of line.slice(0, 8)) {
        try { if (!chess.move(san)) break } catch (_) { break }
        fens.push(chess.fen())
      }
    }
    return JSON.stringify(fens)
  }

  function renderMoment() {
    view.token++
    view.readable = null
    stopStream()
    narrator.stop()
    const g = view.game
    el.review.innerHTML = ""
    el.review.appendChild(gameHeader(g.game))
    const n = view.items.length
    show(el.prev, n > 1)
    show(el.next, n > 1)
    if (!g.analysis) {
      el.review.appendChild(Object.assign(document.createElement("p"), {className: "muted",
        textContent: "This game hasn't been analyzed yet."}))
      el.review.appendChild(button("Analyze it now", "primary", async () => {
        show(el.importCard, true)
        await analyze([g.game.id])
        openGame(g.game.id)
      }))
      el.counter.textContent = ""
      return
    }
    if (!n) {
      el.counter.textContent = ""
      const p = document.createElement("div")
      p.className = "ga-card"
      p.textContent = "Stockfish found no big mistakes in this game — well played! 🎉"
      el.review.appendChild(p)
      return
    }
    const m = view.items[view.index]
    el.counter.textContent = `${view.index + 1} / ${n}`
    el.prev.disabled = view.index === 0
    el.next.disabled = view.index === n - 1
    el.next.textContent = view.index === n - 1 ? "Last one" : "Next →"

    // chips: every moment of the game, so the learner can jump around
    const chips = document.createElement("div")
    chips.className = "moment-chips"
    view.items.forEach((item, i) => {
      const chip = document.createElement("button")
      chip.className = `chip ${item.category}` + (i === view.index ? " active" : "")
      chip.textContent = item.category === "habit" ? item.review.title : item.review.title.replace(/^Move /, "")
      chip.addEventListener("click", () => { view.index = i; renderMoment() })
      chips.appendChild(chip)
    })
    el.review.appendChild(chips)

    const card = document.createElement("div")
    card.className = `ga-card moment ${m.category}`
    const title = document.createElement("h3")
    title.textContent = m.review.title
    card.appendChild(title)
    // What gets read aloud: the headline and the explanation (with the 🔊 button).
    const text = document.createElement("div")
    text.className = "moment-text"
    text.dataset.fens = positionsOf(m)
    const headline = document.createElement("div")
    headline.className = "moment-headline"
    headline.textContent = m.review.headline
    text.appendChild(headline)

    const views = document.createElement("div")
    views.className = "moment-views"
    const setActive = b => views.querySelectorAll(".btn").forEach(x => x.classList.toggle("active", x === b))
    const addView = (label, fn, titleText) => {
      const b = button(label, "", () => { setActive(b); fn() }, titleText)
      views.appendChild(b)
      return b
    }
    const before = addView("Position before", () => showBefore(m), "The position before your move")
    if (m.category !== "habit") {
      addView("Your move", () => showMine(m), `What you played: ${m.san}`)
      if (m.best_move) addView("Best move", () => showBest(m), "Stockfish's move")
      if ((m.best_line || []).length > 1) addView("▶ Best line", () => playLine(m), "Watch how Stockfish's line continues")
    }
    const why = document.createElement("div")
    why.className = "moment-why"
    why.textContent = m.review.why
    text.appendChild(why)
    card.append(text, views)
    makeSpeakable(text)
    view.readable = text

    const explanation = document.createElement("div")
    explanation.className = "moment-ai hidden"
    explanation.dataset.fens = text.dataset.fens
    if (m.category !== "habit") {
      const ask = document.createElement("div")
      ask.className = "moment-ask"
      const input = document.createElement("input")
      input.type = "text"
      input.placeholder = "Ask about this move (optional)…"
      const go = button("🧠 Explain", "primary", () => explain(m, input.value, explanation, go),
        "The AI teacher explains this moment using Stockfish's verified facts")
      input.addEventListener("keydown", e => { if (e.key === "Enter") go.click() })
      ask.append(input, go)
      card.appendChild(ask)
    }
    card.appendChild(explanation)

    const related = relatedWeakness(m)
    if (related) {
      const note = document.createElement("div")
      note.className = "moment-related"
      note.innerHTML = `<span><b>${escapeHtml(related.title)}</b> happened in ${related.game_count} of your games — ` +
        `it's one of your recurring weaknesses.</span>`
      note.appendChild(button("▶ Practise it", "primary", () => startTraining([related.key])))
      card.appendChild(note)
    }

    if (m.category !== "habit") card.appendChild(engineDetails(m))
    el.review.appendChild(card)
    setActive(before)
    showBefore(m)
    el.body.scrollTop = 0
    narrator.auto(text)
  }

  function relatedWeakness(m) {
    const w = view.weaknesses
    if (!w) return null
    return w.weaknesses.find(x => (x.evidence || []).some(e => e.moment_id === m.id)) || null
  }

  // Engine numbers exist, but stay folded away: beginners get words first.
  function engineDetails(m) {
    const color = view.game.game.player_color
    const d = document.createElement("details")
    d.className = "engine-details no-speech"
    const rows = [
      ["With the best move", evalForLearner(m.eval_before, color)],
      [`After ${m.san}`, evalForLearner(m.eval_after, color)],
    ]
    if ((m.alternatives || []).length) rows.push(["Also good", m.alternatives.join(", ")])
    if ((m.best_line || []).length) rows.push(["Stockfish's line", m.best_line.slice(0, 6).join(" ")])
    if ((m.reply_line || []).length) rows.push([`What follows ${m.san}`, m.reply_line.slice(0, 6).join(" ")])
    d.innerHTML = `<summary>Engine details</summary><table>${rows.map(([k, v]) =>
      `<tr><th>${escapeHtml(k)}</th><td>${escapeHtml(v)}</td></tr>`).join("")}</table>`
    return d
  }

  async function explain(m, question, target, btn) {
    target.classList.remove("hidden")
    target.textContent = "…"
    btn.disabled = true
    let text = ""
    try {
      await streamEvents(`/api/games/${encodeURIComponent(view.game.game.id)}/moments/${m.ply}/explain`,
        {question: question || null, level: "beginner"}, ev => {
          if (ev.type === "delta") { text += ev.text; target.textContent = text }
          if (ev.type === "replace") { text = ev.text; target.textContent = text }
          if (ev.type === "done") { text = ev.text; target.textContent = text }
        })
      delete target.dataset.moves
      makeSpeakable(target)
      narrator.auto(target)
    } catch (err) {
      target.textContent = `Couldn't get an explanation: ${err.message}`
    } finally {
      btn.disabled = false
    }
  }

  // ---------------------------------------------------------------- board views
  function mark(squares, type) {
    for (const sq of squares) board.addMarker(type, sq)
  }

  async function showBefore(m) {
    view.token++
    await showPosition(m.fen_before, {orientation: orientation()})
    setStatus(`${m.side === "black" ? "Black" : "White"} to move — this is the position before ${m.san}.`)
  }

  async function showMine(m) {
    const token = ++view.token
    await showPosition(m.fen_before, {orientation: orientation()})
    if (token !== view.token) return
    await board.setPosition(m.fen_after, true)
    clearMarkers()
    mark(uciSquares(m.uci), MARKER_TYPE.circleDanger)
    setStatus(`You played ${m.san}.`)
  }

  async function showBest(m) {
    const token = ++view.token
    await showPosition(m.fen_before, {orientation: orientation()})
    if (token !== view.token || !m.best_move_uci) return
    const chess = new Chess(m.fen_before)
    const u = m.best_move_uci
    chess.move({from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] || "q"})
    await board.setPosition(chess.fen(), true)
    clearMarkers()
    mark(uciSquares(u), MARKER_TYPE.square)
    setStatus(`Stockfish's move: ${m.best_move}.`)
  }

  async function playLine(m) {
    const token = ++view.token
    await showPosition(m.fen_before, {orientation: orientation()})
    const chess = new Chess(m.fen_before)
    const line = (m.best_line || []).slice(0, 6)
    for (let i = 0; i < line.length; i++) {
      await new Promise(r => setTimeout(r, 650))
      if (token !== view.token) return
      let move
      try { move = chess.move(line[i]) } catch (_) { move = null }
      if (!move) break
      await board.setPosition(chess.fen(), true)
      clearMarkers()
      mark([move.from, move.to], i === 0 ? MARKER_TYPE.square : MARKER_TYPE.frame)
      setStatus(`Stockfish's line: ${line.slice(0, i + 1).join(" ")}`)
    }
  }

  // ---------------------------------------------------------------- wiring
  el.importBtn.addEventListener("click", () => importGames())
  el.back.addEventListener("click", () => renderOverview())
  el.prev.addEventListener("click", () => { if (view.index > 0) { view.index--; renderMoment() } })
  el.next.addEventListener("click", () => { if (view.index < view.items.length - 1) { view.index++; renderMoment() } })

  return {
    // Called when the Game Analysis tab is shown.
    async enter() {
      board.disableMoveInput()
      await loadGames()
      if (view.game) renderMoment()
      else await renderOverview()
    },
    leave() {
      view.token++  // stop board animations; a running analysis carries on in the background
    },
    // What "Read aloud" should start with: the open moment, or the overview.
    readable() {
      return view.game ? view.readable : el.overview.querySelector(".ga-summary")
    },
  }
}
