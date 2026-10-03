// Game Analysis screen: import Chess.com games, watch Stockfish analyze them,
// step through the moments that matter, and turn recurring weaknesses into
// training. The server decides everything about the chess; this file only shows it.

import {analysisLine, evalForLearner, gameMeta, matchup, progressText, resultLabel, reviewItems, uciSquares}
  from "./game-format.js"
import {FIRST_ANALYSIS, PRESET_COUNTS, TIER_HEADINGS, evidenceLine, foundIn, historyProgress, parseCount, patternDetail,
  patternIcon, practiceLabel, puzzleStatus, resultsLine, selectionText, trainingPlan, MAX_COUNT, MIN_COUNT} from "./history-view.js"
import {EMPTY_FILTER, filterGames, isoDate, lastGames, opponentRating, quotaLine, selectionSummary, timeClasses}
  from "./game-picker.js"

const USERNAME_KEY = "chessai.chesscomUsername"
const COUNT_KEY = "chessai.historyCount"
const CHOSEN_KEY = "chessai.chosenGames"
const FETCH_COUNT = 100  // downloading is cheap: load the last 100, then analyze the ones you choose

// Storage can be missing or throw (private browsing): remembering the name is a nicety.
const store = {
  get(key) { try { return globalThis.localStorage.getItem(key) } catch (_) { return null } },
  set(key, value) { try { globalThis.localStorage.setItem(key, value) } catch (_) { /* not remembered */ } },
}

export function setupGameAnalysis(ctx) {
  const {api, streamEvents, stopStream, board, showPosition, clearMarkers, Chess, COLOR, MARKER_TYPE, escapeHtml,
    setStatus, makeSpeakable, narrator, onTraining, nav = null, MoveHistory = null, sounds = null} = ctx

  // ← → under the board step through what the review last put on it (board-nav.js).
  let lineRuns = 0  // the newest engine line owns the navigation lock
  function track(fen, moves = []) {
    if (!nav || !MoveHistory) return null
    const h = MoveHistory.fromMoves(fen, moves, Chess)
    nav.set("games", h)
    return h
  }

  const el = {
    pgn: document.getElementById("ga-pgn"),
    username: document.getElementById("ga-username"),
    importBtn: document.getElementById("ga-import-btn"),
    fetchBtn: document.getElementById("ga-fetch-btn"),
    paste: document.getElementById("ga-paste"),
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
    history: null,      // GET /api/games/history: the last N games analyzed together
    historyCount: Number(store.get(COUNT_KEY)) || FIRST_ANALYSIS,
    chosen: readChosen(),  // ids of a hand-picked set (the report then covers exactly those games)
    quota: null,           // the analysis allowance (GET /api/games)
    picker: {filter: {...EMPTY_FILTER}, selected: new Set(), open: false},
    historyBusy: false,
    historyStatus: null,  // progress of a history batch; survives re-rendering the overview
    game: null,         // GET /api/games/{id}
    items: [],          // moments + habits of the open game
    index: 0,
    busy: false,
    token: 0,           // bumps on every board action so a running line animation stops
  }

  el.username.value = store.get(USERNAME_KEY) || ""
  updateFetchLabel()

  // Once there are games, the import form folds into one button so the overview has room.
  const importMore = button("＋ Fetch or import games", "ga-more", () => {
    el.importCard.classList.remove("collapsed")
    el.username.focus()
  }, "Fetch your latest Chess.com games, or paste PGNs")
  el.importCard.prepend(importMore)

  // ---------------------------------------------------------------- helpers
  const show = (node, on = true) => node.classList.toggle("hidden", !on)

  function readChosen() {
    try {
      const ids = JSON.parse(store.get(CHOSEN_KEY) || "null")
      return Array.isArray(ids) && ids.length ? ids : null
    } catch (_) { return null }
  }

  function setChosen(ids) {
    view.chosen = ids && ids.length ? [...ids] : null
    store.set(CHOSEN_KEY, JSON.stringify(view.chosen))
  }

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

  // The learner the history report is about: the Chess.com name they fetched or imported as.
  // Games of other players (pasted for a friend, say) stay out of their patterns.
  function historyUser() {
    return (store.get(USERNAME_KEY) || "").trim() || null
  }

  function historyQuery() {
    const user = historyUser()
    return `count=${view.historyCount}` + (user ? `&username=${encodeURIComponent(user)}` : "") +
      (view.chosen ? `&ids=${encodeURIComponent(view.chosen.join(","))}` : "")
  }

  function historyKey() {
    return view.chosen ? `ids:${view.chosen.join(",")}` : `n:${view.historyCount}`
  }

  function updateFetchLabel() {
    el.fetchBtn.textContent = `Load my last ${FETCH_COUNT} games`
  }

  function orientation() {
    return view.game && view.game.game.player_color === "black" ? COLOR.black : COLOR.white
  }

  // ---------------------------------------------------------------- fetch from Chess.com
  // The server downloads the last 100 games from Chess.com's public API and imports them like
  // pasted PGNs. Nothing is analyzed yet: the learner picks the games (last 25, or their own
  // choice) and only those cost Stockfish time and analysis allowance.
  async function fetchGames() {
    const name = el.username.value.trim()
    if (!name) {
      setImportStatus("Type your Chess.com username first.", "error")
      el.username.focus()
      return
    }
    store.set(USERNAME_KEY, name)
    const count = FETCH_COUNT
    view.busy = true
    el.fetchBtn.disabled = el.importBtn.disabled = true
    setImportStatus(`Loading ${escapeHtml(name)}'s last ${count} games from Chess.com…`)
    let res
    try {
      res = await api("/api/games/fetch", "POST", {username: name, count})
    } catch (err) {
      setImportStatus(escapeHtml(err.message), "error")
      return
    } finally {
      view.busy = false
      el.fetchBtn.disabled = el.importBtn.disabled = false
    }
    store.set(USERNAME_KEY, res.username)
    const skipped = res.errors.length
    setImportStatus(`Loaded ${res.fetched} game${res.fetched === 1 ? "" : "s"} ` +
      `(${res.new ? `${res.new} new` : "all already imported"}).` +
      (skipped ? ` ${skipped} couldn't be read and ${skipped === 1 ? "was" : "were"} skipped.` : "") +
      ` Choose which to analyze below: start with your last ${FIRST_ANALYSIS}, or pick your own.`,
      skipped ? "warn" : "")
    view.history = null
    view.picker.open = true
    await loadGames()
    await renderOverview()
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
      const player = name || (res.imported[0] && res.imported[0].player)
      if (player) store.set(USERNAME_KEY, player)
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
          view.history = null  // new analyses: the history report is rebuilt
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
      const res = await api("/api/games")
      view.games = res.games
      view.quota = res.quota || null
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
    if (nav) nav.set("games", null)  // no position under review
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
    el.overview.innerHTML = ""
    if (!view.games.length) return
    await loadHistory()
    if (view.game) return  // the learner opened a game meanwhile
    el.overview.innerHTML = ""
    const training = trainingCard()
    if (training) {
      el.overview.appendChild(training)
      makeSpeakable(training)
    }
    const card = historyCard()
    el.overview.appendChild(card)
    makeSpeakable(card)
  }

  // ---------------------------------------------------------------- game history (the last N games together)
  async function loadHistory(force = false) {
    if (view.history && !force && view.historyLoaded === historyKey()) return
    view.historyLoaded = historyKey()
    try {
      view.history = await api(`/api/games/history?${historyQuery()}`)
    } catch (_) {
      view.history = null
    }
  }

  function el_(tag, cls, text) {
    const node = document.createElement(tag)
    if (cls) node.className = cls
    if (text !== undefined) node.textContent = text
    return node
  }

  function historyCard() {
    const r = view.history
    const card = el_("div", "ga-card ga-summary ga-history")
    const n = r ? r.games_analyzed : 0
    card.appendChild(el_("h3", "", n
      ? (view.chosen ? `Your ${n} chosen game${n === 1 ? "" : "s"}, analyzed together`
        : `Your last ${n} game${n === 1 ? "" : "s"}, analyzed together`)
      : "Your game history"))
    card.appendChild(historyControls())
    if (view.quota) card.appendChild(el_("p", "quota-line muted no-speech", quotaLine(view.quota)))
    card.appendChild(pickerSection())
    if (!view.historyStatus) view.historyStatus = el_("div", "history-status no-speech")
    card.appendChild(view.historyStatus)
    if (!r) return card
    if (r.notice) card.appendChild(el_("p", "history-notice", r.notice))
    if (!r.games_analyzed) {
      card.appendChild(el_("p", "muted", "Press Analyze: Stockfish checks each game once, then looks for the " +
        "mistakes that keep coming back across them."))
      return card
    }
    const pending = r.not_analyzed.length
    if (pending && !view.historyBusy) {
      card.appendChild(el_("p", "muted no-speech", `${pending} of the selected games ${pending === 1 ? "isn't" : "aren't"} ` +
        "analyzed yet. Press Analyze to include them."))
    }
    const summary = el_("div", "history-summary")
    for (const line of r.summary) summary.appendChild(el_("p", "", line))
    card.appendChild(summary)
    card.appendChild(historyStats(r))

    const explainRow = el_("div", "history-explain-row no-speech")
    const out = el_("div", "history-explain hidden")
    explainRow.appendChild(button("💬 Explain my patterns", "", ev => explainHistory(out, ev.currentTarget),
      "The AI tutor explains what the analysis found, in plain words"))
    const recurring = r.patterns.filter(p => p.tier === "recurring")
    if (recurring.length > 1) {
      explainRow.appendChild(button("▶ Practice all recurring patterns", "primary",
        () => startTraining(recurring.map(p => p.key), r.game_ids)))
    }
    card.appendChild(explainRow)
    card.appendChild(out)

    for (const tier of ["recurring", "occasional"]) {
      const items = r.patterns.filter(p => p.tier === tier)
      if (!items.length) continue
      const section = el_("div", `history-section tier-${tier}`)
      section.appendChild(el_("h4", "", TIER_HEADINGS[tier]))
      const list = el_("div", "weakness-list")
      for (const p of items) list.appendChild(patternRow(p, r))
      section.appendChild(list)
      card.appendChild(section)
    }
    const once = r.patterns.filter(p => p.tier === "one_time")
    if (once.length) {
      const details = el_("details", "history-section tier-one_time no-speech")
      details.appendChild(el_("summary", "", `${TIER_HEADINGS.one_time}: ${once.length}`))
      const list = el_("div", "weakness-list")
      for (const p of once) list.appendChild(patternRow(p, r))
      details.appendChild(list)
      card.appendChild(details)
    }
    if (r.important_mistakes.length) card.appendChild(importantMistakes(r))
    if (r.observations.length) {
      const section = el_("div", "history-section")
      section.appendChild(el_("h4", "", "Openings and endgames"))
      const ul = el_("ul", "history-observations")
      for (const o of r.observations) ul.appendChild(el_("li", "", o))
      section.appendChild(ul)
      card.appendChild(section)
    }
    card.appendChild(scoringNote(r))
    card.appendChild(el_("p", "muted no-speech", "Pick a game on the left to step through all of its mistakes."))
    return card
  }

  function historyControls() {
    const row = el_("div", "history-controls no-speech")
    const label = el_("label", "", "Analyze my last ")
    const select = el_("select", "history-count")
    select.setAttribute("aria-label", "How many recent games")
    const option = (label, value) => {
      const o = el_("option", "", label)
      o.value = value
      select.appendChild(o)
    }
    for (const n of PRESET_COUNTS) option(`${n} games`, String(n))
    option("Custom…", "custom")
    const custom = el_("input", "history-custom")
    Object.assign(custom, {type: "number", min: MIN_COUNT, max: MAX_COUNT, step: 1, placeholder: `${MIN_COUNT}–${MAX_COUNT}`})
    custom.setAttribute("aria-label", "Number of games")
    const preset = PRESET_COUNTS.includes(view.historyCount)
    select.value = preset ? String(view.historyCount) : "custom"
    custom.value = preset ? "" : String(view.historyCount)
    show(custom, !preset)
    const run = button("▶ Analyze", "primary history-run", () => runHistory(select.value, custom.value),
      "Analyze these games (games already analyzed are reused)")
    run.disabled = view.historyBusy
    const error = el_("span", "history-error")
    select.addEventListener("change", async () => {
      show(custom, select.value === "custom")
      error.textContent = ""
      if (select.value === "custom") { custom.focus(); return }
      await chooseCount(Number(select.value))
    })
    custom.addEventListener("keydown", ev => { if (ev.key === "Enter") runHistory("custom", custom.value) })
    label.appendChild(select)
    row.append(label, custom, run, error)
    return row
  }

  // ---------------------------------------------------------------- your training (the next step)
  function trainingCard() {
    const r = view.history
    const t = trainingPlan(r)
    if (!t) return null
    const card = el_("div", "ga-card ga-training")
    card.appendChild(el_("h3", "", "Your Training"))
    if (!t.main) {
      card.appendChild(el_("p", "muted", t.text))
      return card
    }
    const main = el_("div", "training-main")
    main.append(el_("div", "training-label muted", t.heading),
      el_("div", "training-title", `${patternIcon(t.main)} ${t.main.title}`), el_("p", "", t.found))
    const go = el_("div", "training-actions no-speech")
    if (t.puzzles) {
      const start = button(`▶ Start training: ${t.puzzles} puzzles`, "primary training-start",
        () => newPuzzles(t.main, r.game_ids, start, t.puzzles),
        "Puzzles made for this weakness, each checked by Stockfish (new positions, not copies of your games)")
      go.appendChild(start)
    } else {
      go.appendChild(button("▶ Start training", "primary training-start", () => startTraining([t.main.key], r.game_ids),
        "Verified examples first, then positions from your own games"))
    }
    main.appendChild(go)
    card.appendChild(main)
    if (t.puzzles) {  // the lesson is a separate step only when puzzles are the main training
      const lesson = el_("div", "training-lesson")
      lesson.append(el_("div", "training-label muted", "Recommended lesson"), el_("p", "", t.lesson))
      const row = el_("div", "training-actions no-speech")
      row.appendChild(button("Start lesson", "training-lesson-btn", () => startTraining([t.main.key], r.game_ids)))
      lesson.appendChild(row)
      card.appendChild(lesson)
    }
    if (t.others.length) {
      const others = el_("div", "training-others")
      others.appendChild(el_("div", "training-label muted", "Other weaknesses"))
      for (const o of t.others) {
        const row = el_("div", "training-other")
        row.append(el_("span", "", `${o.title} (${o.found})`),
          button("Train", "training-other-btn no-speech", () => startTraining([o.key], r.game_ids)))
        others.appendChild(row)
      }
      card.appendChild(others)
    }
    return card
  }

  // ---------------------------------------------------------------- choose your own games
  // Filters and ticks only change this section (re-rendering the whole card would lose focus).
  function pickerSection() {
    const pk = view.picker
    const played = view.games.filter(g => g.player_color)
    const box = el_("details", "game-picker no-speech")
    box.open = pk.open
    box.addEventListener("toggle", () => { pk.open = box.open })
    box.appendChild(el_("summary", "", `Choose your own games (${played.length} loaded)`))
    if (!played.length) {
      box.appendChild(el_("p", "muted", "Load your Chess.com games (or paste PGNs) to choose from them."))
      return box
    }
    const filters = el_("div", "picker-filters")
    const input = (key, type, placeholder, label, attrs = {}) => {
      const i = el_("input", `pf-${key}`)
      Object.assign(i, {type, placeholder, value: pk.filter[key] || "", ...attrs})
      i.setAttribute("aria-label", label)
      i.addEventListener("input", () => { pk.filter[key] = i.value; drawList() })
      filters.appendChild(i)
      return i
    }
    const select = (key, label, options) => {
      const sel = el_("select", `pf-${key}`)
      sel.setAttribute("aria-label", label)
      for (const [value, text] of options) {
        const o = el_("option", "", text)
        o.value = value
        sel.appendChild(o)
      }
      sel.value = pk.filter[key] || ""
      sel.addEventListener("change", () => { pk.filter[key] = sel.value; drawList() })
      filters.appendChild(sel)
    }
    input("text", "search", "Opponent or opening", "Search by opponent or opening")
    select("result", "Result", [["", "Any result"], ["win", "Wins"], ["loss", "Losses"], ["draw", "Draws"]])
    select("color", "Your colour", [["", "White or Black"], ["white", "As White"], ["black", "As Black"]])
    select("timeClass", "Time control", [["", "Any time control"], ...timeClasses(played).map(t => [t, t[0].toUpperCase() + t.slice(1)])])
    select("analyzed", "Analyzed", [["", "Analyzed or not"], ["no", "Not analyzed yet"], ["yes", "Already analyzed"]])
    input("from", "date", "", "Played on or after")
    input("to", "date", "", "Played on or before")
    input("minRating", "number", "Opp. rating ≥", "Opponent rating at least", {min: 0, step: 50})
    input("maxRating", "number", "Opp. rating ≤", "Opponent rating at most", {min: 0, step: 50})
    input("minMoves", "number", "Moves ≥", "At least this many moves", {min: 0})
    input("maxMoves", "number", "Moves ≤", "At most this many moves", {min: 0})
    box.appendChild(filters)

    const tools = el_("div", "picker-tools")
    const shownCount = el_("span", "muted picker-shown")
    tools.append(
      button("Select all shown", "picker-all", () => { shown.forEach(g => pk.selected.add(g.id)); drawList() }),
      button(`Select my last ${FIRST_ANALYSIS}`, "picker-last", () => {
        pk.selected = new Set(lastGames(played, FIRST_ANALYSIS)); drawList()
      }),
      button("Clear", "picker-clear", () => { pk.selected.clear(); drawList() }),
      button("Reset filters", "picker-reset", () => { pk.filter = {...EMPTY_FILTER}; renderOverview() }),
      shownCount)
    box.appendChild(tools)
    const list = el_("div", "picker-list")
    box.appendChild(list)
    const actions = el_("div", "picker-actions")
    const summary = el_("span", "picker-summary")
    const run = button("▶ Analyze selected games", "primary picker-run", () => {
      const ids = played.filter(g => pk.selected.has(g.id)).map(g => g.id)
      if (ids.length) runChosen(ids)
    }, "Stockfish analyzes the ticked games, then looks for patterns across them")
    actions.append(summary, run)
    box.appendChild(actions)

    let shown = []
    function drawList() {
      shown = filterGames(played, pk.filter)
      shownCount.textContent = `Showing ${shown.length} of ${played.length}`
      list.innerHTML = ""
      for (const g of shown) {
        const row = el_("label", "picker-row" + (g.analysis ? " analyzed" : ""))
        const tick = el_("input")
        tick.type = "checkbox"
        tick.checked = pk.selected.has(g.id)
        tick.dataset.id = g.id
        tick.addEventListener("change", () => {
          tick.checked ? pk.selected.add(g.id) : pk.selected.delete(g.id)
          drawSummary()
        })
        const opp = opponentRating(g)
        row.append(tick,
          el_("span", "pr-date", isoDate(g.date) || "?"),
          el_("span", "pr-opp", `${g.opponent || "?"}${opp ? ` (${opp})` : ""}`),
          el_("span", `pr-res res ${g.learner_result || "unfinished"}`, resultLabel(g)),
          el_("span", "pr-meta muted", [g.player_color === "white" ? "White" : "Black", g.time_label || g.time_class,
            g.opening, `${g.moves} moves`].filter(Boolean).join(" · ")),
          el_("span", "pr-done muted", g.analysis ? "✓ analyzed" : ""))
        list.appendChild(row)
      }
      if (!shown.length) list.appendChild(el_("p", "muted", "No games match these filters."))
      drawSummary()
    }
    function drawSummary() {
      const ids = played.filter(g => pk.selected.has(g.id)).map(g => g.id)
      const sel = selectionSummary(ids, view.games, view.quota)
      summary.textContent = sel.text
      summary.classList.toggle("over", !sel.ok && ids.length > 0)
      run.disabled = !sel.ok || view.historyBusy
    }
    drawList()
    return box
  }

  async function chooseCount(count) {
    view.historyCount = count
    store.set(COUNT_KEY, String(count))
    setChosen(null)
    updateFetchLabel()
    await loadHistory(true)
    if (!view.game) renderOverview()
  }

  function historyStats(r) {
    const stats = el_("div", "history-stats")
    const tile = (value, label) => {
      const t = el_("div", "stat")
      t.append(el_("b", "", value), el_("span", "muted", label))
      stats.appendChild(t)
    }
    tile(String(r.games_analyzed), `game${r.games_analyzed === 1 ? "" : "s"} analyzed`)
    tile(resultsLine(r.results), "results")
    const big = r.mistakes.blunders + r.mistakes.mistakes
    tile(`${big}`, `big mistake${big === 1 ? "" : "s"} (${r.mistakes.per_game} per game)`)
    const patterns = r.patterns.filter(p => p.tier === "recurring").length
    tile(String(patterns), `recurring pattern${patterns === 1 ? "" : "s"}`)
    return stats
  }

  function patternRow(p, r) {
    const row = el_("div", `weakness pattern tier-${p.tier}` + (p.tier === "recurring" ? " recurring" : ""))
    row.dataset.key = p.key
    const main = el_("div", "pattern-main")
    const icon = el_("span", "pattern-icon", patternIcon(p))
    icon.setAttribute("aria-hidden", "true")
    const text = el_("div", "pattern-text")
    const title = el_("div", "pattern-title")
    title.append(el_("b", "", p.title), document.createTextNode(" — "), el_("span", "found-in", foundIn(p)))
    const detail = [patternDetail(p)]
    if (p.includes) detail.push(`includes ${p.includes.join(" and ").toLowerCase()}`)
    if (p.library_examples) {
      detail.push(`${p.library_examples} verified example${p.library_examples === 1 ? "" : "s"} to practise`)
    }
    text.append(title, el_("div", "muted", detail.filter(Boolean).join(" · ")))
    main.append(icon, text)
    const actions = el_("div", "weakness-actions no-speech")
    const evidence = el_("ul", "pattern-evidence hidden no-speech")
    const toggle = button(p.game_count === 1 ? "Show the game" : "Show the games", "", () => {
      const open = evidence.classList.toggle("hidden")
      toggle.textContent = open ? (p.game_count === 1 ? "Show the game" : "Show the games") : "Hide"
    }, "The games and positions where this happened")
    actions.appendChild(toggle)
    actions.appendChild(button(practiceLabel(p), p.tier === "recurring" ? "primary" : "",
      () => startTraining([p.key], r.game_ids), "Verified examples first, then positions from your own games"))
    if (p.new_puzzles) {
      const more = button("New puzzles for this", "", () => newPuzzles(p, r.game_ids, more),
        "New positions made for this skill (not from your games), each checked by Stockfish")
      actions.appendChild(more)
    }
    for (const e of p.evidence) {
      const li = el_("li")
      const b = button("", `evidence-row ${e.severity}`, () => openGame(e.game_id, e.moment_id),
        "Open this position in the game review")
      b.textContent = evidenceLine(e)
      li.appendChild(b)
      evidence.appendChild(li)
    }
    if (p.occurrences > p.evidence.length) {
      evidence.appendChild(el_("li", "muted", `…and ${p.occurrences - p.evidence.length} more`))
    }
    row.append(main, actions, evidence)
    return row
  }

  function importantMistakes(r) {
    const section = el_("div", "history-section no-speech")
    section.appendChild(el_("h4", "", "Biggest mistakes"))
    const ul = el_("ul", "pattern-evidence")
    for (const m of r.important_mistakes) {
      const li = el_("li")
      const b = button("", `evidence-row ${m.severity}`, () => openGame(m.game_id, m.moment_id))
      b.textContent = evidenceLine(m)
      li.appendChild(b)
      ul.appendChild(li)
    }
    section.appendChild(ul)
    return section
  }

  function scoringNote(r) {
    const d = el_("details", "history-scoring no-speech")
    d.appendChild(el_("summary", "", "How patterns are found and ranked"))
    const threshold = r.enough_history
      ? `In ${r.games_analyzed} games, a mistake is a recurring pattern once it shows up in ${r.recurring_threshold} different games.`
      : `Recurring patterns need at least ${r.min_games} analyzed games.`
    for (const text of [
      "Every mistake here was found by Stockfish and checked on the board; the AI tutor only explains them.",
      threshold,
      "A mistake in only one game is never called a pattern.",
      "Patterns are ranked by how often they happened and how much they cost: a blunder counts 3, " +
        "a mistake 2, an inaccuracy or habit 1, more when it lost more material; a repeat inside the same game " +
        "counts a quarter, and reaching the exact same position again counts half.",
    ]) d.appendChild(el_("p", "muted", text))
    return d
  }

  async function runHistory(choice, custom) {
    const parsed = parseCount(choice, custom)
    const status = view.historyStatus
    if (parsed.error) {
      const err = el.overview.querySelector(".history-error")
      if (err) err.textContent = parsed.error
      return
    }
    view.historyCount = parsed.count
    store.set(COUNT_KEY, String(parsed.count))
    setChosen(null)
    await runAnalysis({count: parsed.count})
  }

  // a hand-picked set: exactly these games are analyzed and reported together
  async function runChosen(ids) {
    setChosen(ids)
    view.picker.open = false
    await runAnalysis({game_ids: ids})
  }

  async function runAnalysis(selection) {
    const status = view.historyStatus
    view.historyBusy = true
    el.overview.querySelectorAll(".history-run, .picker-run").forEach(b => { b.disabled = true })
    status.innerHTML = `<div class="ga-progress"><div class="ga-progress-text">Choosing your games…</div>` +
      `<div class="ga-bar"><span></span></div></div>`
    const text = status.querySelector(".ga-progress-text")
    const fill = status.querySelector(".ga-bar span")
    let intro = ""
    const failures = []
    try {
      await streamEvents("/api/games/history/analyze", {...selection, username: historyUser()}, ev => {
        if (ev.type === "select") { intro = selectionText(ev); text.textContent = intro }
        if (ev.type === "progress") {
          const p = historyProgress(ev)
          text.textContent = p.text
          fill.style.width = `${p.pct}%`
        }
        if (ev.type === "game_done") loadGames()
        if (ev.type === "error") failures.push(ev)
        if (ev.type === "done") {
          view.history = ev.report
          view.historyLoaded = historyKey()
          if (ev.quota) view.quota = ev.quota
          fill.style.width = "100%"
          text.textContent = failures.length
            ? `Done. ${failures.length} game${failures.length === 1 ? "" : "s"} couldn't be analyzed and ` +
              `${failures.length === 1 ? "was" : "were"} left out: ${failures[0].error}`
            : `Done: ${ev.report.games_analyzed} games analyzed together.`
        }
      }, {exclusive: false})
    } catch (err) {
      text.textContent = err.message
      status.querySelector(".ga-progress").classList.add("error")
    } finally {
      view.historyBusy = false
    }
    await loadGames()
    if (!view.game) renderOverview()
  }

  async function explainHistory(target, btn) {
    target.classList.remove("hidden")
    target.textContent = "…"
    btn.disabled = true
    let text = ""
    try {
      await streamEvents("/api/games/history/explain",
        {count: view.historyCount, username: historyUser()}, ev => {
        if (ev.type === "delta") { text += ev.text; target.textContent = text }
        if (ev.type === "replace" || ev.type === "done") { text = ev.text; target.textContent = text }
      })
      makeSpeakable(target)
      narrator.auto(target, "analysis")
    } catch (err) {
      target.textContent = `Couldn't get an explanation: ${err.message}`
    } finally {
      btn.disabled = false
    }
  }

  // New puzzles for one weakness: generated on the server, each checked by Stockfish before
  // it is shown. Takes a while, so progress is streamed into the status line.
  async function newPuzzles(p, gameIds, btn, count = 3) {
    btn.disabled = true
    let made = 0
    setImportStatus(`🧩 Choosing puzzles for “${escapeHtml(p.title)}”…`, "")
    try {
      await streamEvents("/api/games/puzzles", {key: p.key, count, game_ids: gameIds}, ev => {
        if (ev.type === "puzzle") made += 1
        const text = escapeHtml(puzzleStatus(ev, made))
        if (ev.type === "error") setImportStatus(text, "error")
        else if (text) setImportStatus(`🧩 ${text}`, "")
        if (ev.type === "done") onTraining(ev)
      }, {exclusive: false})
    } catch (err) {
      setImportStatus(escapeHtml(err.message), "error")
    } finally {
      btn.disabled = false
    }
  }

  async function startTraining(keys, gameIds = null) {
    try {
      const res = await api("/api/games/training", "POST", {keys, game_ids: gameIds})
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
    if (!view.history) await loadHistory()
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

    const related = relatedPattern(m)
    if (related) {
      const note = document.createElement("div")
      note.className = "moment-related"
      const where = `${related.game_count} of your last ${related.total_games} games`
      note.innerHTML = `<span><b>${escapeHtml(related.title)}</b> happened in ${where} — ` +
        (related.tier === "recurring" ? "it's one of your recurring patterns." : "not a pattern yet, but worth a look.") +
        `</span>`
      note.appendChild(button(practiceLabel(related), related.tier === "recurring" ? "primary" : "",
        () => startTraining([related.key], view.history.game_ids)))
      card.appendChild(note)
    }

    if (m.category !== "habit") card.appendChild(engineDetails(m))
    el.review.appendChild(card)
    setActive(before)
    showBefore(m)
    el.body.scrollTop = 0
    narrator.auto(text, "analysis")
  }

  // The history pattern (seen in 2+ games) this moment is part of, if any.
  function relatedPattern(m) {
    const r = view.history
    if (!r) return null
    return r.patterns.find(p => p.tier !== "one_time" && p.evidence.some(e => e.moment_id === m.id)) || null
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
        {question: question || null}, ev => {
          if (ev.type === "delta") { text += ev.text; target.textContent = text }
          if (ev.type === "replace") { text = ev.text; target.textContent = text }
          if (ev.type === "done") { text = ev.text; target.textContent = text }
        })
      delete target.dataset.moves
      makeSpeakable(target)
      narrator.auto(target, "explanations")
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
    track(m.fen_before)
    await showPosition(m.fen_before, {orientation: orientation()})
    setStatus(`${m.side === "black" ? "Black" : "White"} to move — this is the position before ${m.san}.`)
  }

  async function showMine(m) {
    const token = ++view.token
    track(m.fen_before, [m.uci])
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
    track(m.fen_before, [m.best_move_uci])
    const chess = new Chess(m.fen_before)
    const u = m.best_move_uci
    const best = chess.move({from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] || "q"})
    if (sounds) sounds.playMove(best, chess.fen())
    await board.setPosition(chess.fen(), true)
    clearMarkers()
    mark(uciSquares(u), MARKER_TYPE.square)
    setStatus(`Stockfish's move: ${m.best_move}.`)
  }

  async function playLine(m) {
    const token = ++view.token
    const h = track(m.fen_before)
    const run = ++lineRuns
    if (nav) nav.lock("demo", "games")  // the line plays by itself: ← → once it's done
    try {
      await showPosition(m.fen_before, {orientation: orientation()})
      const chess = new Chess(m.fen_before)
      const line = (m.best_line || []).slice(0, 6)
      for (let i = 0; i < line.length; i++) {
        await new Promise(r => setTimeout(r, 650))
        if (token !== view.token) return
        let move
        try { move = chess.move(line[i]) } catch (_) { move = null }
        if (!move) break
        if (h) { h.append(chess.fen(), move.from + move.to + (move.promotion || ""), move.san); nav.refresh() }
        if (sounds) sounds.playMove(move, chess.fen())
        await board.setPosition(chess.fen(), true)
        clearMarkers()
        mark([move.from, move.to], i === 0 ? MARKER_TYPE.square : MARKER_TYPE.frame)
        setStatus(`Stockfish's line: ${line.slice(0, i + 1).join(" ")}`)
      }
    } finally {
      if (nav && run === lineRuns) nav.unlock("demo", "games")
    }
  }

  // ---------------------------------------------------------------- wiring
  el.importBtn.addEventListener("click", () => importGames())
  el.fetchBtn.addEventListener("click", () => fetchGames())
  el.username.addEventListener("keydown", ev => { if (ev.key === "Enter") fetchGames() })
  el.back.addEventListener("click", () => renderOverview())
  el.prev.addEventListener("click", () => { if (view.index > 0) { view.index--; renderMoment() } })
  el.next.addEventListener("click", () => { if (view.index < view.items.length - 1) { view.index++; renderMoment() } })

  return {
    // Called when the Game Analysis tab is shown.
    async enter() {
      view.active = true
      board.disableMoveInput()
      await loadGames()
      if (!view.active) return  // the learner already went to another tab
      if (view.game) renderMoment()
      else await renderOverview()
    },
    leave() {
      view.active = false
      view.token++  // stop board animations; a running analysis carries on in the background
    },
    // "Analyze my recent games" from the coach's onboarding: fetch + analyze for this username.
    async fetchFor(username) {
      el.username.value = username
      await fetchGames()
    },
    // What "Read aloud" should start with: the open moment, or the overview.
    readable() {
      return view.game ? view.readable : el.overview.querySelector(".ga-summary")
    },
  }
}
