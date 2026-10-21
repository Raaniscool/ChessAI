// Brilliant Finder: play every learner turn in an existing verified puzzle line. Each legal
// board move is sent immediately; only that line's verified legal opponent reply is automated.
import {parsePosition, parsePuzzlePosition} from "./position.js"

const DIFFICULTIES = ["adaptive", "beginner", "easy", "intermediate", "advanced", "expert"]
const LABELS = {adaptive: "Adaptive", beginner: "Beginner", easy: "Easy", intermediate: "Intermediate",
  advanced: "Advanced", expert: "Expert"}

export function createBrilliantFinder({api, board, showPosition, verifiedMove, isPositionVerified, Chess, COLOR,
  legalInputHandler, nav = null, MoveHistory = null, showNode = null, setStatus = () => {}, el,
  onShow = () => {}}) {
  const state = {
    active: false, profile: null, profileLoaded: false, preference: "adaptive", task: null,
    stepIndex: 0, currentFen: null, result: null, busy: false, preferenceBusy: false,
    exhausted: false, token: 0, sessionIds: new Set(), history: null, displayFen: null, chess: null,
  }
  const visible = (node, on) => node && node.classList.toggle("hidden", !on)
  const orientation = task => task && task.side_to_move === "black" ? COLOR.black : COLOR.white
  const apiPath = (task, action) => `/api/puzzles/brilliant/${encodeURIComponent(task.task_id)}/${action}`

  function samePosition(a, b) {
    try {
      const left = parsePosition(Chess, a, "Finder position").fen().split(/\s+/).slice(0, 4).join(" ")
      const right = parsePosition(Chess, b, "Finder position").fen().split(/\s+/).slice(0, 4).join(" ")
      return left === right
    } catch (_) { return false }
  }

  function renderProfile() {
    if (!state.profile) return
    const counts = (state.profile.bands || []).map(b => `${b.label} ${b.count}`).join(" · ")
    const adaptive = state.profile.adaptive || {}
    const summary = adaptive.summary ? `${adaptive.summary}.` : "Adaptive uses your learner profile."
    el.profile.textContent = `${state.profile.available || 0} verified positions · ${counts}. ${summary} ` +
      "Chess.com Elo, when saved, is only a weak starting reference; missing or stale ratings never prevent play."
  }

  function renderDifficultyNote() {
    if (!el.difficultyNote) return
    const manual = (state.profile && state.profile.bands || []).find(b => b.difficulty === state.preference)
    if (state.preference === "adaptive") {
      const summary = state.profile && state.profile.adaptive && state.profile.adaptive.summary
      el.difficultyNote.textContent = (summary ? `${summary}. ` : "Adaptive starts at an accessible library level. ") +
        "It uses rating only as a prior, then combines relevant game, Training, and puzzle evidence."
    } else {
      el.difficultyNote.textContent = (manual && manual.description ? `${manual.description} ` : "") +
        "These labels are relative to this verified collection, not exact human Elo promises."
    }
  }

  function renderResult() {
    const result = state.result
    if (!result) return
    const detail = result.explanation || {}
    const analysis = result.analysis || {}
    el.verdict.textContent = result.label || (result.revealed ? "Verified solution revealed" : "Combination complete")
    el.verdict.className = `brilliant-verdict ${result.revealed ? "revealed" : result.solved ? "solved" : "retry"}`

    const explanation = []
    if (result.revealed) {
      explanation.push("You requested the solution. The verified line is shown below; it was not auto-played.")
    } else if (detail.summary) {
      explanation.push(detail.summary)
    }
    if (detail.source_explanation) explanation.push(detail.source_explanation)
    if (!result.revealed && result.analysis_status === "inconclusive") {
      explanation.push("The stored puzzle line was verified, but runtime Stockfish could not establish a trustworthy interpretation. No engine verdict is claimed.")
    } else if (result.analysis_status === "complete" && detail.engine_label) {
      explanation.push(`Stockfish's bounded analysis: ${detail.engine_label.toLowerCase()}.`)
    }
    el.explanation.textContent = explanation.join(" ")

    if (el.setup) {
      el.setup.textContent = detail.preparatory_line
        ? `Verified setup before the sacrifice: ${detail.preparatory_line}.`
        : "The verified line has no player-side setup move before the sacrifice."
    }
    if (el.sacrifice) {
      const key = detail.sacrifice_move || "—"
      const played = detail.played_key_move
      el.sacrifice.textContent = played && played !== key
        ? `Key sacrifice: ${key}. Your verified alternative at that turn was ${played}.`
        : `Key sacrifice: ${key}${played ? ` — you found it` : ""}.`
    }
    if (el.defense) {
      if (detail.strongest_defense) {
        const prefix = detail.defense_source === "stockfish"
          ? "Stockfish's strongest relevant defense"
          : "The verified puzzle reply"
        el.defense.textContent = `${prefix}: ${detail.strongest_defense}.`
      } else {
        el.defense.textContent = "No separate defense was needed or reliably returned for this line."
      }
    }

    const score = result.score
    if (result.analysis_status === "complete" && score) {
      const side = score.perspective === "black" ? "Black" : "White"
      const loss = Number.isFinite(score.loss_cp) ? `${score.loss_cp} cp` : "unavailable"
      const before = score.before && score.before.label
      const after = score.after && score.after.label
      el.score.textContent = `Sacrifice-position analysis · ${loss} estimated loss from ${side}'s perspective · before: ${before || "—"} · after: ${after || "—"} · depth ${result.depth || "—"}.`
    } else if (result.revealed) {
      el.score.textContent = "Engine interpretation was not run for a requested reveal."
    } else {
      el.score.textContent = "No trustworthy runtime engine score is available. The verified puzzle completion is based on the stored solution data."
    }

    const alternative = result.stronger_alternative
    if (result.analysis_status === "complete" && alternative) {
      el.alternative.textContent = `Stockfish also found ${alternative.move}${alternative.line && alternative.line.length
        ? ` · ${alternative.line.join(" → ")}` : ""}. This is an engine comparison, not an unverified claim about a refutation.`
    } else if (result.analysis_status === "complete" && result.best_move) {
      el.alternative.textContent = `Stockfish's top move from the sacrifice position: ${result.best_move.san}.`
    } else {
      el.alternative.textContent = "No other move is described as failing unless the verified engine data establishes it."
    }

    const solutionMoves = result.verified_solution && result.verified_solution.moves || []
    const bestMoves = analysis.best_line && analysis.best_line.moves || []
    el.selectedLine.textContent = solutionMoves.length ? solutionMoves.join(" → ") : "No verified line was returned."
    el.bestLine.textContent = result.analysis_status === "complete" && bestMoves.length
      ? bestMoves.join(" → ") : "No trustworthy Stockfish line was returned."
    visible(el.showBest, result.analysis_status === "complete" && bestMoves.length > 0)
    visible(el.showSelected, solutionMoves.length > 0)

    if (result.recorded) {
      el.recording.textContent = result.revealed
        ? "The reveal was recorded as not solved. It does not count as a successful attempt."
        : result.solved
          ? "Recorded as solved in your puzzle progress and learner profile; this can inform later Adaptive selections."
          : "The verified line was completed after an incorrect try. It was recorded as a missed puzzle, following existing puzzle scoring."
    } else {
      el.recording.textContent = "The line is shown, but the learner-profile write could not be confirmed."
    }
  }

  function render() {
    if (el.difficulty) {
      el.difficulty.value = state.preference
      el.difficulty.disabled = state.preferenceBusy || state.busy
    }
    renderProfile()
    renderDifficultyNote()
    const task = state.task
    visible(el.task, Boolean(task))
    visible(el.start, !task)
    if (el.start) {
      el.start.textContent = state.profileLoaded ? "Find a position" : "Loading Finder…"
      el.start.disabled = state.busy || !state.active || !state.profileLoaded
    }
    if (!task) {
      visible(el.reveal, false); visible(el.skip, false); visible(el.retry, false)
      visible(el.next, false); visible(el.newRound, false); visible(el.result, false)
      if (el.selected) el.selected.textContent = ""
      if (el.complexity) el.complexity.textContent = ""
      return
    }
    const difficulty = task.difficulty || {}
    if (el.complexity) {
      const complexity = difficulty.complexity_band || "Verified"
      const mode = difficulty.preference === "adaptive" ? "Adaptive" : `${LABELS[difficulty.preference] || "Manual"} mode`
      el.complexity.textContent = `Position ${state.sessionIds.size} · ${mode} · ${complexity} relative complexity`
    }
    if (el.selected) {
      el.selected.textContent = state.result
        ? "Review the verified setup, sacrifice, opponent defense, and continuation below."
        : state.busy ? "Checking the move and waiting for the verified reply…"
          : "Your turn: make a legal move on the board. Each move is checked immediately; no Submit step."
    }
    const playing = !state.result
    visible(el.reveal, playing)
    visible(el.skip, playing)
    visible(el.retry, Boolean(state.result))
    visible(el.next, Boolean(state.result && !state.exhausted))
    visible(el.newRound, Boolean(state.result && state.exhausted))
    for (const button of [el.reveal, el.skip, el.retry, el.next, el.newRound]) {
      if (button) button.disabled = state.busy || state.preferenceBusy || !state.active
    }
    visible(el.result, Boolean(state.result))
    if (state.result) renderResult()
  }

  function setBoardInput(enabled) {
    board.disableMoveInput()
    if (!enabled || !state.active || !state.task || state.result || state.busy || state.preferenceBusy || !state.chess) return
    const side = state.task.side_to_move === "black" ? COLOR.black : COLOR.white
    board.enableMoveInput(input, side)
  }

  function setHistoryFromPlayed(task, moves) {
    if (!MoveHistory) return null
    const history = new MoveHistory(task.fen)
    for (const item of moves || []) {
      if (!item || !item.uci || !item.after_fen) continue
      history.append(item.after_fen, item.uci, item.san || null)
    }
    return history
  }

  function resultHistory(task, result) {
    const played = result && result.played_line
    if (!played || !Array.isArray(played.moves) || !MoveHistory) return null
    return MoveHistory.fromMoves(task.fen, played.moves, Chess)
  }

  async function onMoveSelected(_from, _to, receipt = {}) {
    const transition = receipt.transition
    const task = state.task
    if (!state.active || !task || state.busy || state.preferenceBusy || state.result || !transition) return
    const stepIndex = state.stepIndex
    const beforeFen = state.currentFen || task.fen
    const token = state.token
    if (!samePosition(beforeFen, transition.beforeFen)) {
      el.status.textContent = "The board was not on the current Finder turn. Restoring the verified position."
      await syncTask(task, true)
      return
    }
    try {
      const check = parsePosition(Chess, beforeFen, "Finder move starting position")
      const move = check.move({from: transition.uci.slice(0, 2), to: transition.uci.slice(2, 4),
        promotion: transition.uci[4] || "q"})
      if (!move || !samePosition(check.fen(), transition.afterFen)) {
        throw new Error("The board move did not match the legal chess position.")
      }
    } catch (err) {
      el.status.textContent = `That move could not be verified. ${err.message} The same puzzle turn is ready to retry.`
      await showPosition(beforeFen, {orientation: orientation(task), animated: false})
      state.chess = parsePosition(Chess, beforeFen, "Finder position")
      state.currentFen = beforeFen
      setBoardInput(true)
      return
    }

    state.busy = true
    board.disableMoveInput()
    if (nav && nav.lock) nav.lock("auto", "puzzles")
    render()
    el.status.textContent = "Checking your move against this puzzle's verified line…"
    try {
      const response = await api(apiPath(task, "move"), "POST", {
        step_index: stepIndex, move: transition.uci, before_fen: beforeFen,
      })
      if (!state.task || state.task.task_id !== task.task_id) return
      if (response.step_index !== stepIndex || !response.current_fen) {
        throw new Error("The server response did not match the active puzzle turn.")
      }
      if (["incorrect", "playable", "unverified_alternative"].includes(response.outcome)) {
        if (response.opponent_move || response.player_move || response.done ||
            !samePosition(response.current_fen, beforeFen) || response.next_step_index !== stepIndex) {
          throw new Error("A retry response tried to advance the verified puzzle line.")
        }
        state.currentFen = response.current_fen
        state.chess = parsePosition(Chess, state.currentFen, "Finder retry position")
        state.displayFen = state.currentFen
        if (state.active && token === state.token) {
          await showPosition(state.currentFen, {orientation: orientation(task), animated: false})
          el.status.textContent = `${response.message || "That move is not on the verified line."} Try again from the same position.`
          setStatus("")
        }
        return
      }

      const player = response.player_move
      if (!player || player.uci !== transition.uci || !samePosition(player.after_fen, transition.afterFen)) {
        throw new Error("The accepted player move did not match the board transition.")
      }
      if (response.opponent_move) {
        if (!samePosition(response.opponent_move.after_fen, response.current_fen)) {
          throw new Error("The automated reply did not end at the returned puzzle position.")
        }
        state.chess = parsePosition(Chess, player.after_fen, "Position after your move")
        if (state.active && token === state.token) {
          if (!isPositionVerified(player.after_fen)) {
            throw new Error("The displayed board is not at the accepted player-move position.")
          }
          const from = response.opponent_move.uci.slice(0, 2)
          const piece = state.chess.get(from)
          if (!piece) throw new Error("The verified opponent reply's source piece is missing.")
          const reply = await verifiedMove(player.after_fen, response.opponent_move.uci, {
            expectedPiece: {color: piece.color, type: piece.type}, label: "Verified Finder opponent reply",
          })
          if (reply.uci !== response.opponent_move.uci ||
              !samePosition(reply.afterFen, response.opponent_move.after_fen) || !isPositionVerified(reply.afterFen)) {
            throw new Error("The opponent reply failed board legality or position verification.")
          }
          state.chess = parsePosition(Chess, reply.afterFen, "Verified Finder reply result")
        } else if (state.active) {
          await showPosition(response.current_fen, {orientation: orientation(task), animated: false})
          state.chess = parsePosition(Chess, response.current_fen, "Finder synchronized position")
        } else {
          state.chess = parsePosition(Chess, response.current_fen, "Finder current position")
        }
      } else {
        if (!samePosition(player.after_fen, response.current_fen)) {
          throw new Error("The final player move did not match the returned position.")
        }
        state.chess = parsePosition(Chess, response.current_fen, "Finder final position")
      }

      if (MoveHistory) {
        if (!state.history) state.history = new MoveHistory(task.fen)
        state.history.append(player.after_fen, player.uci, player.san)
        if (response.opponent_move) {
          state.history.append(response.opponent_move.after_fen, response.opponent_move.uci, response.opponent_move.san)
        }
      }
      state.currentFen = response.current_fen
      state.displayFen = response.current_fen
      state.stepIndex = response.next_step_index
      if (response.done) {
        state.result = response.result || null
        const completedHistory = resultHistory(task, state.result)
        if (completedHistory) state.history = completedHistory
        if (state.active) {
          if (nav && state.history) nav.set("puzzles", state.history, {onView: onHistoryView})
          render()
          el.status.textContent = state.result && state.result.revealed
            ? "The solution is revealed below. No remaining player moves were auto-played."
            : "Verified line complete. Review the setup, sacrifice, defense, and continuation below."
          setStatus("")
        }
      } else if (state.active && token === state.token) {
        el.status.textContent = "Verified opponent reply played. Your turn — continue the combination."
        setBoardInput(true)
        setStatus("")
      }
    } catch (err) {
      if (state.task && state.task.task_id === task.task_id) {
        el.status.textContent = `Couldn't safely apply the reply: ${err.message} Syncing the verified puzzle turn…`
        try {
          await syncTask(task, state.active)
          if (state.active) el.status.textContent = `Position synchronized. ${state.result ? "Review the result." : "Your turn is ready."}`
        } catch (syncError) {
          if (state.active) {
            el.status.textContent = `The move could not be synchronized: ${syncError.message}. Board input remains disabled.`
            setBoardInput(false)
          }
        }
      }
    } finally {
      if (nav && nav.unlock) nav.unlock("auto", "puzzles")
      if (state.task && state.task.task_id === task.task_id) {
        state.busy = false
        if (state.active) {
          render()
          setBoardInput(!state.result)
        }
      }
    }
  }

  const input = legalInputHandler(() => state.chess, onMoveSelected, err => {
    if (!state.active || !state.task || state.result) return
    el.status.textContent = `The board could not confirm that move, so it was not processed. ${err.message} Try again.`
    setBoardInput(true)
  })

  async function loadProfile() {
    if (state.profileLoaded) return
    const data = await api("/api/puzzles/brilliant/profile")
    state.profile = data
    state.preference = DIFFICULTIES.includes(data.difficulty) ? data.difficulty : "adaptive"
    state.profileLoaded = true
  }

  async function setDifficulty(difficulty) {
    if (!DIFFICULTIES.includes(difficulty) || state.preferenceBusy || state.busy || !state.active) return false
    const previous = state.preference
    if (difficulty === previous) return true
    state.preferenceBusy = true
    state.preference = difficulty
    board.disableMoveInput()
    render()
    el.status.textContent = "Saving difficulty preference…"
    try {
      const saved = await api("/api/puzzles/brilliant/preferences", "POST", {difficulty})
      state.preference = DIFFICULTIES.includes(saved.difficulty) ? saved.difficulty : difficulty
      state.exhausted = false
      if (state.task) {
        el.status.textContent = `Saved ${LABELS[state.preference]}. Your current puzzle and move progress are unchanged; this applies to the next position.`
      } else {
        el.status.textContent = `Saved ${LABELS[state.preference]} for the next position.`
      }
      return true
    } catch (err) {
      state.preference = previous
      el.status.textContent = `Couldn't save difficulty: ${err.message}`
      return false
    } finally {
      state.preferenceBusy = false
      render()
      setBoardInput(!state.result)
    }
  }

  async function onHistoryView(history) {
    if (!history || !state.result || !state.task) return
    state.displayFen = history.current.fen
    state.chess = parsePosition(Chess, history.current.fen, "Brilliant Finder move history")
    board.disableMoveInput()
    if (showNode) await showNode(history.current, history, orientation(state.task))
    else await showPosition(history.current.fen, {orientation: orientation(state.task)})
  }

  async function syncTask(task, showBoard = true) {
    const snapshot = await api(apiPath(task, "state"))
    if (!state.task || state.task.task_id !== task.task_id) return
    if (snapshot.id !== task.id || !snapshot.current_fen) throw new Error("The server returned a different Finder position.")
    state.currentFen = snapshot.current_fen
    state.stepIndex = snapshot.step_index
    state.result = snapshot.status === "complete" ? snapshot.result || state.result : null
    state.chess = parsePosition(Chess, snapshot.current_fen, "Finder synchronized position")
    state.displayFen = snapshot.current_fen
    if (state.result) state.history = resultHistory(task, state.result)
    else if (Array.isArray(snapshot.played) && MoveHistory) state.history = setHistoryFromPlayed(task, snapshot.played)
    if (showBoard && state.active) {
      await showPosition(snapshot.current_fen, {orientation: orientation(task), animated: false})
      if (isPositionVerified(snapshot.current_fen) === false) throw new Error("The synchronized board was not verified.")
      if (state.result && state.history && nav) nav.set("puzzles", state.history, {onView: onHistoryView})
    }
    render()
    setBoardInput(!state.result)
  }

  async function restoreBoard(token = state.token) {
    if (!state.active || token !== state.token) return
    const task = state.task
    if (!task) {
      board.disableMoveInput()
      const chess = new Chess()
      await showPosition(chess.fen(), {orientation: COLOR.white, animated: false})
      if (!state.active || token !== state.token) return
      state.chess = chess
      state.currentFen = chess.fen()
      state.displayFen = chess.fen()
      return
    }
    if (state.result && state.history) {
      nav && nav.set("puzzles", state.history, {onView: onHistoryView})
      const node = state.history.current
      state.displayFen = node.fen
      state.chess = parsePosition(Chess, node.fen, "Brilliant Finder line")
      await showPosition(node.fen, {orientation: orientation(task)})
    } else {
      const fen = state.currentFen || task.fen
      const chess = parsePuzzlePosition(Chess, fen, "Brilliant Finder position")
      await showPosition(fen, {orientation: orientation(task), animated: false})
      if (!state.active || token !== state.token) return
      if (isPositionVerified(fen) === false) throw new Error("The board did not confirm the Finder position.")
      state.chess = chess
      state.currentFen = fen
      state.displayFen = fen
      if (nav) nav.set("puzzles", null)
    }
    setBoardInput(!state.result)
  }

  async function loadNext() {
    if (!state.active || state.busy || state.preferenceBusy) return
    if (state.task && !state.result) return
    state.busy = true
    const token = ++state.token
    render()
    el.status.textContent = "Finding a verified combination…"
    try {
      const response = await api("/api/puzzles/brilliant/next", "POST", {exclude: [...state.sessionIds]})
      if (!state.active || token !== state.token) return
      const chess = parsePuzzlePosition(Chess, response.fen, "Brilliant Finder position")
      await showPosition(response.fen, {orientation: orientation(response), animated: false})
      if (!state.active || token !== state.token) return
      if (isPositionVerified(response.fen) === false) throw new Error("The board did not confirm this verified position.")
      state.task = response
      state.sessionIds.add(response.id)
      state.exhausted = false
      state.stepIndex = 0
      state.currentFen = response.fen
      state.result = null
      state.history = MoveHistory ? new MoveHistory(response.fen) : null
      state.chess = chess
      state.displayFen = response.fen
      if (nav) nav.set("puzzles", null)
      render()
      setBoardInput(true)
      el.status.textContent = "Play every required move yourself. Only a legal opponent reply from this verified line will move automatically."
      setStatus("")
    } catch (err) {
      if (state.active && token === state.token) {
        state.exhausted = err.status === 404 && /no unseen .* positions remain/i.test(err.message)
        el.status.textContent = state.exhausted
          ? `${err.message} Start a review round or change difficulty.`
          : err.status === 404 ? `${err.message} Change difficulty or try another puzzle mode.`
            : `Couldn't load a Finder position: ${err.message}`
      }
    } finally {
      if (token === state.token) {
        state.busy = false
        render()
        if (state.active) setBoardInput(!state.result)
      }
    }
  }

  async function revealSolution() {
    const task = state.task
    if (!state.active || !task || state.result || state.busy || state.preferenceBusy) return
    state.busy = true
    board.disableMoveInput()
    render()
    el.status.textContent = "Loading the verified solution…"
    try {
      const response = await api(apiPath(task, "reveal"), "POST", {})
      if (!state.task || state.task.task_id !== task.task_id) return
      if (!response.result || !response.result.revealed || !response.result.verified_solution) {
        throw new Error("The server did not return the verified solution line.")
      }
      state.currentFen = response.current_fen || state.currentFen
      state.result = response.result
      state.history = resultHistory(task, state.result)
      state.chess = parsePosition(Chess, state.currentFen, "Finder reveal position")
      state.displayFen = state.currentFen
      if (state.active) {
        await showPosition(state.currentFen, {orientation: orientation(task), animated: false})
        if (state.history && nav) nav.set("puzzles", state.history, {onView: onHistoryView})
        render()
        el.status.textContent = "The verified solution is revealed below; the remaining player moves were not played for you."
      }
    } catch (err) {
      if (state.task && state.task.task_id === task.task_id && state.active) {
        el.status.textContent = `Couldn't reveal the verified solution: ${err.message}`
        setBoardInput(true)
      }
    } finally {
      if (state.task && state.task.task_id === task.task_id) {
        state.busy = false
        if (state.active) render()
      }
    }
  }

  async function retryPosition() {
    const task = state.task
    if (!state.active || !task || !state.result || state.busy || state.preferenceBusy) return
    state.busy = true
    board.disableMoveInput()
    render()
    el.status.textContent = "Restarting this verified position…"
    try {
      const response = await api(apiPath(task, "retry"), "POST", {})
      if (!state.task || state.task.task_id !== task.task_id) return
      const chess = parsePuzzlePosition(Chess, response.fen, "Brilliant Finder retry position")
      await showPosition(response.fen, {orientation: orientation(response), animated: false})
      if (!isPositionVerified(response.fen)) throw new Error("The retry position could not be verified.")
      state.task = response
      state.currentFen = response.fen
      state.stepIndex = 0
      state.result = null
      state.history = MoveHistory ? new MoveHistory(response.fen) : null
      state.chess = chess
      state.displayFen = response.fen
      if (nav) nav.set("puzzles", null)
      render()
      setBoardInput(true)
      el.status.textContent = "Same verified position, fresh attempt. Find the complete line."
    } catch (err) {
      if (state.active) el.status.textContent = `Couldn't retry this position: ${err.message}`
    } finally {
      state.busy = false
      if (state.active) {
        render()
        setBoardInput(!state.result)
      }
    }
  }

  async function showLine(kind) {
    if (!state.active || !state.task || !state.result || !MoveHistory) return
    const result = state.result
    const moves = kind === "best"
      ? result.analysis && result.analysis.best_line && result.analysis.best_line.moves
      : result.verified_solution && result.verified_solution.moves
    const startFen = kind === "best"
      ? result.analysis && result.analysis.position_fen
      : state.task.fen
    if (!startFen || !Array.isArray(moves) || !moves.length) {
      el.status.textContent = "No verified line is available to show."
      return
    }
    try {
      const history = MoveHistory.fromMoves(startFen, moves, Chess)
      if (history.last < 1) throw new Error("The line contains no verified moves.")
      state.history = history
      state.displayFen = history.current.fen
      state.chess = parsePosition(Chess, history.current.fen, "Brilliant Finder line")
      board.disableMoveInput()
      nav && nav.set("puzzles", history, {onView: onHistoryView})
      if (showNode) await showNode(history.current, history, orientation(state.task))
      else await showPosition(history.current.fen, {orientation: orientation(state.task)})
      el.status.textContent = kind === "best"
        ? "Showing Stockfish's line from the sacrifice position; use the board arrows to review it."
        : "Showing the full verified solution; use the board arrows to review it."
    } catch (err) {
      el.status.textContent = `Couldn't show that verified line: ${err.message}`
    }
  }

  async function skipPosition() {
    if (!state.active || !state.task || state.result || state.busy) return
    state.task = null
    state.stepIndex = 0
    state.currentFen = null
    state.result = null
    state.history = null
    state.displayFen = null
    board.disableMoveInput()
    render()
    await loadNext()
  }

  async function startReviewRound() {
    if (!state.active || state.busy || !state.result) return
    state.sessionIds = new Set(state.task && state.task.id ? [state.task.id] : [])
    state.exhausted = false
    render()
    await loadNext()
  }

  async function enter() {
    state.active = true
    const token = state.token
    onShow()
    try {
      await loadProfile()
      if (!state.active || token !== state.token) return
      render()
      if (state.task) {
        try { await syncTask(state.task, false) } catch (_) { /* keep the last verified local state */ }
      }
      await restoreBoard(token)
      if (!state.active || token !== state.token) return
      render()
    } catch (err) {
      if (state.active && token === state.token) {
        el.status.textContent = `Couldn't open Brilliant Finder: ${err.message}`
        setBoardInput(false)
      }
    }
  }

  function pause() {
    state.active = false
    state.token++
    board.disableMoveInput()
  }

  el.difficulty.addEventListener("change", () => { void setDifficulty(el.difficulty.value) })
  el.start.addEventListener("click", () => { void loadNext() })
  if (el.reveal) el.reveal.addEventListener("click", () => { void revealSolution() })
  el.skip.addEventListener("click", () => { void skipPosition() })
  el.retry.addEventListener("click", () => { void retryPosition() })
  el.next.addEventListener("click", () => { void loadNext() })
  if (el.newRound) el.newRound.addEventListener("click", () => { void startReviewRound() })
  el.showSelected.addEventListener("click", () => { void showLine("selected") })
  el.showBest.addEventListener("click", () => { void showLine("best") })

  return {
    enter,
    pause,
    readable: () => state.result ? el.result : state.task ? el.task : el.profile,
    setDifficulty,
    next: loadNext,
    reveal: revealSolution,
    retry: retryPosition,
    state: () => ({active: state.active, preference: state.preference,
      id: state.task && state.task.id, taskId: state.task && state.task.task_id,
      stepIndex: state.stepIndex, currentFen: state.currentFen,
      result: Boolean(state.result), solved: state.result && state.result.solved,
      exhausted: state.exhausted, session: state.sessionIds.size, busy: state.busy}),
  }
}
