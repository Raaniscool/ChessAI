// Decision Puzzles: explain a proposed move, then reveal Stockfish-backed evidence.
// The server owns task facts and assessment. This module preserves the current task/answer
// when the learner leaves the Practice tab, and never lets a language model supply chess claims.

import {parsePosition} from "./position.js"

const ASSESSMENT_LABELS = {
  correct: "Correct reasoning",
  partial: "Partly correct",
  misconception: "Needs correction",
  uncertain: "Not enough evidence to judge",
}

export function createDecisionPuzzles({api, board, showPosition, Chess, COLOR, el, onShow = () => {}, onPause = () => {}}) {
  const state = {task: null, result: null, answer: "", active: false, busy: false, positionVerified: false, token: 0}

  const show = (node, on) => node.classList.toggle("hidden", !on)
  const current = token => state.active && token === state.token

  function clearEngineEvidence() {
    el.engine.innerHTML = ""
    show(el.engine, false)
  }

  function addEvidenceRow(label, value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return
    const row = document.createElement("p")
    const strong = document.createElement("b")
    strong.textContent = `${label}: `
    row.append(strong, document.createTextNode(Array.isArray(value) ? value.join(" → ") : String(value)))
    el.engine.appendChild(row)
  }

  function renderTask() {
    const task = state.task
    if (!task) {
      show(el.task, false)
      show(el.start, true)
      el.start.textContent = "Try again"
      el.start.disabled = state.busy
      return
    }
    const retryPosition = !state.positionVerified && !state.result
    show(el.start, retryPosition)
    if (retryPosition) el.start.textContent = "Retry position"
    el.start.disabled = state.busy
    show(el.task, true)
    el.move.textContent = task.proposed_move
    el.side.textContent = `${task.side_to_move === "black" ? "Black" : "White"} to move`
    el.question.textContent = task.question
    el.answer.value = state.answer
    el.answer.disabled = Boolean(state.result) || !state.positionVerified
    el.submit.disabled = state.busy || Boolean(state.result) || !state.positionVerified
    el.next.disabled = state.busy
    show(el.submit, !state.result)
    show(el.next, Boolean(state.result))
    show(el.result, Boolean(state.result))
    if (state.result) renderResult()
    else clearEngineEvidence()
  }

  function renderResult() {
    const result = state.result
    if (!result) return
    el.assessment.textContent = ASSESSMENT_LABELS[result.assessment] || ASSESSMENT_LABELS.uncertain
    el.assessment.className = `decision-assessment ${result.assessment || "uncertain"}`
    el.feedback.textContent = result.feedback || "The engine evidence is available below."
    el.recording.textContent = result.recording_note || ""
    el.engine.innerHTML = ""
    const evidence = result.engine || {}
    addEvidenceRow("Stockfish", `${String(evidence.category || "unknown").toUpperCase()} · ${evidence.loss_cp ?? "?"} cp lost`)
    addEvidenceRow("Proposed move", evidence.proposed_move)
    addEvidenceRow("Best move", evidence.best_move)
    addEvidenceRow("Best line", evidence.best_line)
    addEvidenceRow("After the proposal", evidence.proposed_line)
    addEvidenceRow("Verified point", evidence.verified_point)
    show(el.engine, true)
  }

  async function restoreTaskPosition() {
    if (!state.task) return
    state.positionVerified = false
    const parsed = parsePosition(Chess, state.task.fen, "Decision Puzzle position")
    if ((parsed.turn() === "w" ? "white" : "black") !== state.task.side_to_move) {
      throw new Error("The proposed position has an inconsistent side to move.")
    }
    const moveProbe = parsePosition(Chess, parsed.fen(), "Decision Puzzle position")
    if (!moveProbe.move(state.task.proposed_move)) {
      throw new Error("The server proposed a move that is not legal in this position.")
    }
    board.disableMoveInput()
    await showPosition(parsed.fen(), {
      orientation: state.task.side_to_move === "black" ? COLOR.black : COLOR.white,
      animated: false,
    })
    state.positionVerified = true
  }

  async function loadNext() {
    const token = ++state.token
    state.busy = true
    el.status.textContent = "Finding a verified position and checking the move with Stockfish…"
    el.next.disabled = true
    el.submit.disabled = true
    el.start.disabled = true
    try {
      const task = await api("/api/puzzles/decision/next", "POST", {})
      if (!current(token)) return
      // Parse before replacing the active task. A bad response never silently becomes the start board.
      const parsed = parsePosition(Chess, task && task.fen, "Decision Puzzle position")
      const probe = parsePosition(Chess, parsed.fen(), "Decision Puzzle position")
      if (typeof task.proposed_move !== "string" || !probe.move(task.proposed_move)) {
        throw new Error("The server returned a proposed move that is not legal in the supplied position.")
      }
      if ((parsed.turn() === "w" ? "white" : "black") !== task.side_to_move) {
        throw new Error("The server returned an inconsistent side to move.")
      }
      state.task = task
      state.result = null
      state.answer = ""
      await restoreTaskPosition()
      if (!current(token)) return
      el.status.textContent = ""
      renderTask()
      el.answer.focus()
    } catch (err) {
      if (current(token)) {
        el.status.textContent = `Couldn't load a Decision Puzzle: ${err.message}`
        el.submit.disabled = !state.task || Boolean(state.result)
        el.next.disabled = false
        renderTask()
      }
    } finally {
      if (current(token)) {
        state.busy = false
        el.submit.disabled = Boolean(state.result) || !state.positionVerified
        el.next.disabled = false
        el.start.disabled = false
      }
    }
  }

  async function retryPosition() {
    if (!state.active || state.busy) return
    if (!state.task || state.result) return loadNext()
    const token = ++state.token
    state.busy = true
    el.status.textContent = "Rechecking the Decision Puzzle position…"
    renderTask()
    try {
      await restoreTaskPosition()
      if (!current(token)) return
      el.status.textContent = ""
      renderTask()
    } catch (err) {
      if (!current(token)) return
      el.status.textContent = `Couldn't restore this Decision Puzzle safely. ${err.message}`
      renderTask()
    } finally {
      if (current(token)) {
        state.busy = false
        renderTask()
      }
    }
  }

  async function submitAnswer() {
    if (!state.active || !state.task || state.busy || state.result || !state.positionVerified) return
    const answer = el.answer.value.trim()
    if (answer.length < 8) {
      el.status.textContent = "Add a little more explanation (at least 8 characters) before submitting."
      el.answer.focus()
      return
    }
    state.answer = answer
    const token = ++state.token
    state.busy = true
    el.status.textContent = "Checking your explanation against the verified evidence…"
    el.submit.disabled = true
    el.answer.disabled = true
    try {
      const result = await api(`/api/puzzles/decision/${encodeURIComponent(state.task.task_id)}/answer`, "POST", {answer})
      if (!current(token)) return
      state.result = result
      el.status.textContent = ""
      renderTask()
    } catch (err) {
      if (!current(token)) return
      el.status.textContent = `Couldn't assess this answer: ${err.message}`
      el.answer.disabled = false
      el.submit.disabled = false
    } finally {
      if (current(token)) {
        state.busy = false
        if (!state.result) {
          el.answer.disabled = false
          el.submit.disabled = false
        }
      }
    }
  }

  el.submit.addEventListener("click", submitAnswer)
  el.answer.addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submitAnswer()
  })
  el.next.addEventListener("click", loadNext)
  el.start.addEventListener("click", retryPosition)

  return {
    async activate() {
      state.active = true
      state.token++
      board.disableMoveInput()
      onShow()
      if (state.task) {
        try {
          await restoreTaskPosition()
          renderTask()
          el.status.textContent = ""
        } catch (err) {
          el.status.textContent = `Couldn't restore this Decision Puzzle safely: ${err.message}`
          renderTask()
        }
        return
      }
      await loadNext()
    },
    async enter() {
      return this.activate()
    },
    pause() {
      if (state.task && !state.result) state.answer = el.answer.value
      state.active = false
      state.token++
      state.busy = false
      state.positionVerified = false
      board.disableMoveInput()
      onPause()
    },
    leave() {
      this.pause()
    },
    readable() {
      return state.result ? el.result : el.panel
    },
    state: () => ({taskId: state.task && state.task.task_id, answered: Boolean(state.result),
      active: state.active, busy: state.busy, positionVerified: state.positionVerified}),
  }
}
