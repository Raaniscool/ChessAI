// Decision Puzzles: a mutually exclusive move verdict plus a separate required explanation.
// Stockfish owns move quality; the server assesses reasoning only against supplied evidence.
// This module preserves both controls and the active task across Practice navigation.

import {parsePosition} from "./position.js"

const ASSESSMENT_LABELS = {
  correct: "Explanation: correct",
  partial: "Explanation: partly correct",
  incorrect: "Explanation: incorrect",
  uncertain: "Explanation: uncertain — evidence was insufficient",
  unavailable: "Explanation: not graded",
}
const UNAVAILABLE_LABELS = {
  model_not_configured: "Explanation not graded: Qwen model not configured",
  endpoint_unreachable: "Explanation not graded: Qwen service unreachable",
  timeout: "Explanation not graded: Qwen timed out",
  model_or_endpoint_not_found: "Explanation not graded: model or endpoint not found",
  unsupported_configuration: "Explanation not graded: QWEN_BASE_URL is invalid",
  provider_rejected_request: "Explanation not graded: service rejected the request",
  provider_http_error: "Explanation not graded: Qwen service error",
  provider_error: "Explanation not graded: Qwen service error",
  malformed_provider_response: "Explanation not graded: invalid service response",
  empty_response: "Explanation not graded: empty service response",
  invalid_grading_response: "Explanation not graded: unusable grading response",
  unsupported_teacher: "Explanation not graded: configured teacher cannot grade explanations",
  provider_unavailable: "Explanation not graded: reasoning service unavailable",
  backend_error: "Explanation not graded: internal grading error",
}
const EXHAUSTED_MESSAGE = "No unseen verified Decision Puzzles remain"

export function createDecisionPuzzles({api, board, showPosition, Chess, COLOR, el, onShow = () => {}, onPause = () => {}}) {
  const state = {task: null, result: null, answer: "", choice: "", active: false,
    busy: false, positionVerified: false, token: 0, exhausted: false}

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

  function selectedChoice() {
    if (el.choiceYes && el.choiceYes.checked) return "yes"
    if (el.choiceNo && el.choiceNo.checked) return "no"
    return state.choice
  }

  function renderTask() {
    const task = state.task
    if (!task) {
      show(el.task, false)
      show(el.start, true)
      el.start.textContent = state.exhausted ? "Pool exhausted" : "Try again"
      el.start.disabled = state.busy || state.exhausted
      return
    }
    const retryPosition = !state.positionVerified && !state.result
    show(el.start, retryPosition)
    el.start.textContent = retryPosition ? "Retry position" : "Try again"
    el.start.disabled = state.busy || state.exhausted
    show(el.task, true)
    const mover = task.mover || (task.side_to_move === "black" ? "Black" : "White")
    el.move.textContent = task.proposed_move
    el.side.textContent = `${mover} to move`
    el.question.textContent = task.question || `Is ${task.proposed_move} a good move for ${mover}?`
    if (el.choiceYesSide) el.choiceYesSide.textContent = mover
    if (el.choiceNoSide) el.choiceNoSide.textContent = mover
    if (el.choiceYes) {
      el.choiceYes.checked = selectedChoice() === "yes"
      el.choiceYes.disabled = Boolean(state.result) || !state.positionVerified || state.busy
    }
    if (el.choiceNo) {
      el.choiceNo.checked = selectedChoice() === "no"
      el.choiceNo.disabled = Boolean(state.result) || !state.positionVerified || state.busy
    }
    if (el.answer.value !== state.answer) el.answer.value = state.answer
    el.answer.disabled = Boolean(state.result) || !state.positionVerified || state.busy
    const enoughExplanation = state.answer.trim().length >= (task.explanation_min_length || 8)
    el.submit.disabled = state.busy || Boolean(state.result) || !state.positionVerified ||
      !selectedChoice() || !enoughExplanation
    el.next.disabled = state.busy || state.exhausted
    if (state.exhausted) el.next.textContent = "No unseen position"
    else el.next.textContent = "Next position →"
    show(el.submit, !state.result)
    show(el.next, Boolean(state.result))
    show(el.result, Boolean(state.result))
    if (state.result) renderResult()
    else clearEngineEvidence()
  }

  function renderResult() {
    const result = state.result
    if (!result) return
    const assessment = result.explanation_assessment || result.assessment || "unavailable"
    const label = assessment === "unavailable"
      ? (UNAVAILABLE_LABELS[result.assessment_reason] || ASSESSMENT_LABELS.unavailable)
      : (ASSESSMENT_LABELS[assessment] || ASSESSMENT_LABELS.unavailable)
    el.assessment.textContent = label
    el.assessment.className = `decision-assessment ${assessment}`
    const judgment = result.judgment || {}
    el.judgment.textContent = result.judgment_feedback ||
      `Your ${String(judgment.choice || "").toUpperCase()} was ${judgment.correct ? "correct" : "not correct"}.`
    el.judgment.className = `decision-judgment ${judgment.correct ? "correct" : "incorrect"}`
    el.feedback.textContent = result.explanation_feedback || result.feedback || "Stockfish evidence is shown below."
    el.recording.textContent = result.recording_note || ""
    el.engine.innerHTML = ""
    const evidence = result.engine || {}
    addEvidenceRow("Moving side", `${evidence.mover || "Unknown"} to move`)
    addEvidenceRow("Stockfish", `${String(evidence.category || "unknown").toUpperCase()} · ${evidence.loss_cp ?? "?"} cp lost`)
    addEvidenceRow("Proposed move", evidence.proposed_move)
    addEvidenceRow("Best move", evidence.best_move)
    addEvidenceRow("Best line", evidence.best_line)
    addEvidenceRow("After the proposal", evidence.proposed_line)
    if (evidence.eval_before_cp_for_mover != null || evidence.eval_after_cp_for_mover != null) {
      addEvidenceRow("Evaluation for the moving side",
        `${evidence.eval_before_cp_for_mover ?? "?"} cp before → ${evidence.eval_after_cp_for_mover ?? "?"} cp after`)
    }
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
    if (state.busy || state.exhausted) return
    const token = ++state.token
    state.busy = true
    el.status.textContent = "Finding an unseen verified position and checking it with Stockfish…"
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
      state.choice = ""
      if (el.choiceYes) el.choiceYes.checked = false
      if (el.choiceNo) el.choiceNo.checked = false
      state.answer = ""
      state.exhausted = false
      await restoreTaskPosition()
      if (!current(token)) return
      el.status.textContent = ""
      renderTask()
      el.choiceYes?.focus?.()
    } catch (err) {
      if (current(token)) {
        const exhausted = err && err.status === 409 && String(err.message || "").includes(EXHAUSTED_MESSAGE)
        state.exhausted = Boolean(exhausted)
        el.status.textContent = exhausted
          ? `${err.message} Continue with the regular Puzzle library or add verified examples.`
          : `Couldn't load a Decision Puzzle: ${err.message}`
        renderTask()
      }
    } finally {
      if (current(token)) {
        state.busy = false
        renderTask()
      }
    }
  }

  async function retryPosition() {
    if (!state.active || state.busy || state.exhausted) return
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
    const choice = selectedChoice()
    if (!choice) {
      el.status.textContent = "Choose Yes or No before submitting."
      el.choiceYes?.focus?.()
      return
    }
    const explanation = el.answer.value.trim()
    if (explanation.length < (state.task.explanation_min_length || 8)) {
      el.status.textContent = `Add a specific explanation (at least ${state.task.explanation_min_length || 8} characters).`
      el.answer.focus()
      return
    }
    state.choice = choice
    state.answer = explanation
    const token = ++state.token
    state.busy = true
    el.status.textContent = "Checking your verdict with Stockfish and your explanation against verified evidence…"
    renderTask()
    try {
      const result = await api(`/api/puzzles/decision/${encodeURIComponent(state.task.task_id)}/answer`, "POST",
        {choice, explanation})
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
        renderTask()
      }
    }
  }

  el.submit.addEventListener("click", submitAnswer)
  el.choiceYes.addEventListener("change", () => {
    if (el.choiceYes.checked) state.choice = "yes"
    renderTask()
  })
  el.choiceNo.addEventListener("change", () => {
    if (el.choiceNo.checked) state.choice = "no"
    renderTask()
  })
  el.answer.addEventListener("input", () => {
    state.answer = el.answer.value
    renderTask()
  })
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
      if (!state.exhausted) await loadNext()
      else renderTask()
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
      choice: selectedChoice(), active: state.active, busy: state.busy,
      positionVerified: state.positionVerified, exhausted: state.exhausted}),
  }
}
