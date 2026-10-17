import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {createDecisionPuzzles} from "../decision-puzzles.js"

class FakeNode {
  constructor() {
    this._text = ""
    this._html = ""
    this.children = []
    this.events = new Map()
    this.classes = new Set(["hidden"])
    this.classList = {
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : Boolean(force)
        if (on) this.classes.add(name)
        else this.classes.delete(name)
        return on
      },
      contains: name => this.classes.has(name),
    }
    this.value = ""
    this.checked = false
    this.disabled = false
  }
  set textContent(value) { this._text = String(value ?? ""); this.children = [] }
  get textContent() { return this._text + this.children.map(child => child.textContent || "").join("") }
  set innerHTML(value) { this._html = String(value); if (!value) { this._text = ""; this.children = [] } }
  get innerHTML() { return this._html }
  addEventListener(name, fn) { this.events.set(name, fn) }
  appendChild(child) { this.children.push(child); return child }
  append(...children) { this.children.push(...children) }
  focus() { this.focused = true }
  async emit(name, event = {}) { return this.events.get(name)?.(event) }
}

function fakeDocument() {
  return {
    createElement: () => new FakeNode(),
    createTextNode: text => ({textContent: String(text)}),
  }
}

function elements() {
  return Object.fromEntries(["panel", "task", "start", "status", "move", "side", "question", "choiceYes",
    "choiceNo", "choiceYesSide", "choiceNoSide", "answer", "submit", "next", "result", "judgment",
    "assessment", "feedback", "recording", "engine"].map(id => [id, new FakeNode()]))
}

function task() {
  const position = new Chess()
  position.move("e4")
  return {task_id: "task-1", fen: position.fen(), side_to_move: "black", mover: "Black",
    proposed_move: "e5", question: "Is e5 a good move for Black?",
    explanation_min_length: 8}
}

function setup(api, ui = elements(), extra = {}) {
  return {ui, puzzles: createDecisionPuzzles({api, board: {disableMoveInput() {}}, showPosition: async () => {},
    Chess, COLOR: {white: "white", black: "black"}, el: ui, ...extra})}
}

test("Decision Puzzle restores its selected verdict, validated board, and explanation after Practice navigation", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    const currentTask = task()
    const calls = []
    const shown = []
    let disabled = 0
    let shownPanel = 0
    let paused = 0
    const api = async (path, method, body) => {
      calls.push({path, method, body})
      if (path.endsWith("/next")) return currentTask
      if (path.endsWith("/answer")) return {
        task_id: "task-1", assessment: "partial", explanation_assessment: "partial",
        judgment: {choice: "yes", correct: true, expected: "yes", side_to_move: "black"},
        judgment_feedback: "Your Yes was correct for Black.",
        explanation_feedback: "Explanation: partly correct; add the concrete reason.",
        recording_note: "Your verdict and explanation assessment were saved.", recorded: true,
        engine: {category: "good", loss_cp: 22, mover: "Black", side_to_move: "black",
          proposed_move: "e5", best_move: "Nf6", best_line: ["Nf6", "Bc4"],
          proposed_line: ["e5", "Nf3"], verified_point: "Develop and contest the centre."},
      }
      throw new Error(`Unexpected request ${path}`)
    }
    const board = {disableMoveInput() { disabled++ }}
    const ui = elements()
    const puzzles = createDecisionPuzzles({api, board, Chess, COLOR: {white: "white", black: "black"}, el: ui,
      showPosition: async (fen, options) => { shown.push({fen, options}) },
      onShow: () => { shownPanel++ }, onPause: () => { paused++ }})

    await puzzles.activate()
    assert.equal(puzzles.state().taskId, "task-1")
    assert.equal(ui.move.textContent, "e5")
    assert.equal(ui.side.textContent, "Black to move")
    assert.match(ui.question.textContent, /Black/)
    assert.equal(ui.choiceYesSide.textContent, "Black")
    assert.equal(ui.choiceNoSide.textContent, "Black")
    assert.equal(ui.task.classList.contains("hidden"), false)
    assert.equal(ui.choiceYes.checked, false)
    assert.equal(ui.choiceNo.checked, false)
    assert.equal(ui.submit.disabled, true) // both an explicit choice and an explanation are required
    assert.equal(shown[0].fen, currentTask.fen)
    assert.equal(shown[0].options.orientation, "black")
    assert.equal(disabled > 0, true)

    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "Black's move is good because it contests the centre."
    await ui.answer.emit("input")
    puzzles.leave()
    await puzzles.enter()
    assert.equal(ui.answer.value, "Black's move is good because it contests the centre.")
    assert.equal(ui.choiceYes.checked, true)
    assert.equal(puzzles.state().choice, "yes")
    assert.equal(shown.length, 2)
    assert.equal(calls.filter(call => call.path.endsWith("/next")).length, 1)
    assert.equal(shownPanel, 2)
    assert.equal(paused, 1)

    await ui.submit.emit("click")
    assert.equal(calls.at(-1).path, "/api/puzzles/decision/task-1/answer")
    assert.deepEqual(calls.at(-1).body, {
      choice: "yes", explanation: "Black's move is good because it contests the centre.",
    })
    assert.equal(puzzles.state().answered, true)
    assert.equal(ui.judgment.textContent, "Your Yes was correct for Black.")
    assert.equal(ui.assessment.textContent, "Explanation: partly correct")
    assert.match(ui.engine.textContent, /Nf6/)
    assert.equal(ui.choiceYes.disabled, true)
    assert.equal(ui.answer.disabled, true)
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("unavailable grading tells the learner why while preserving the Stockfish choice verdict", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    const ui = elements()
    const api = async path => {
      if (path.endsWith("/next")) return task()
      return {
        task_id: "task-1", assessment: "unavailable", explanation_assessment: "unavailable",
        assessment_status: "unavailable", assessment_reason: "model_not_configured",
        judgment: {choice: "yes", correct: true, expected: "yes", side_to_move: "black"},
        judgment_feedback: "Your Yes was correct for Black.",
        explanation_feedback: "Explanation not graded: QWEN_MODEL is not configured. Your reasoning was not scored.",
        recording_note: "Your objective result was saved independently.", engine: {category: "good", loss_cp: 12},
      }
    }
    const {puzzles} = setup(api, ui)
    await puzzles.activate()
    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "Black keeps a useful central grip with this move."
    await ui.answer.emit("input")
    await ui.submit.emit("click")

    assert.equal(ui.assessment.textContent, "Explanation not graded: Qwen model not configured")
    assert.match(ui.feedback.textContent, /QWEN_MODEL is not configured/)
    assert.equal(ui.judgment.textContent, "Your Yes was correct for Black.")
    assert.equal(ui.judgment.className, "decision-judgment correct")
    assert.match(ui.recording.textContent, /saved independently/)
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("response timeout is distinguishable from model-not-configured and uncertainty", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    const ui = elements()
    const api = async path => path.endsWith("/next") ? task() : {
      task_id: "task-1", assessment: "unavailable", explanation_assessment: "unavailable",
      assessment_status: "unavailable", assessment_reason: "response_timeout",
      judgment: {choice: "yes", correct: true, expected: "yes", side_to_move: "black"},
      explanation_feedback: "Qwen did not return a complete assessment within DECISION_ASSESSMENT_TIMEOUT.",
    }
    const {puzzles} = setup(api, ui)
    await puzzles.activate()
    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "Black keeps useful central control with this move."
    await ui.answer.emit("input")
    await ui.submit.emit("click")
    assert.equal(ui.assessment.textContent, "Explanation not graded: Qwen response timed out")
    assert.equal(ui.assessment.className, "decision-assessment unavailable")
    assert.match(ui.feedback.textContent, /DECISION_ASSESSMENT_TIMEOUT/)
    assert.equal(ui.judgment.className, "decision-judgment correct")
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("browser stops waiting at a finite deadline without marking an explanation incorrect", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    const ui = elements()
    const api = async (path, method, body, options) => {
      if (path.endsWith("/next")) return task()
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          const error = new Error("aborted")
          error.name = "AbortError"
          reject(error)
        }, {once: true})
      })
    }
    const {puzzles} = setup(api, ui, {assessmentRequestTimeoutMs: 10})
    await puzzles.activate()
    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "Black keeps useful central control with this move."
    await ui.answer.emit("input")
    await ui.submit.emit("click")
    assert.match(ui.status.textContent, /browser's wait limit/)
    assert.match(ui.status.textContent, /not marked incorrect/)
    assert.equal(puzzles.state().answered, false)
    assert.equal(puzzles.state().busy, false)
    assert.equal(ui.submit.disabled, false)
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("genuinely uncertain assessment is distinct from a missing model", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    const ui = elements()
    const api = async path => path.endsWith("/next") ? task() : {
      task_id: "task-1", assessment: "uncertain", explanation_assessment: "uncertain",
      assessment_status: "uncertain", assessment_reason: "insufficient_evidence",
      judgment: {choice: "yes", correct: true, expected: "yes", side_to_move: "black"},
      explanation_feedback: "The explanation was too ambiguous to assess reliably.",
    }
    const {puzzles} = setup(api, ui)
    await puzzles.activate()
    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "Maybe this is good, but I cannot tell."
    await ui.answer.emit("input")
    await ui.submit.emit("click")
    assert.equal(ui.assessment.textContent, "Explanation: uncertain — evidence was insufficient")
    assert.equal(ui.assessment.className, "decision-assessment uncertain")
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("a failed board restoration blocks assessment until the same position verifies", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    let displays = 0
    let submissions = 0
    const ui = elements()
    const {puzzles} = setup(async path => {
      if (path.endsWith("/next")) return task()
      submissions++
      throw new Error(`Unexpected request ${path}`)
    }, ui, {
      showPosition: async () => {
        displays++
        if (displays === 1) throw new Error("board verification failed")
      },
    })

    await puzzles.activate()
    assert.equal(puzzles.state().positionVerified, false)
    assert.equal(ui.submit.disabled, true)
    assert.equal(ui.answer.disabled, true)
    assert.equal(ui.start.classList.contains("hidden"), false)
    ui.choiceYes.checked = true
    await ui.choiceYes.emit("change")
    ui.answer.value = "A long enough explanation."
    await ui.answer.emit("input")
    await ui.submit.emit("click")
    assert.equal(submissions, 0)

    await ui.start.emit("click")
    assert.equal(puzzles.state().positionVerified, true)
    assert.equal(ui.submit.disabled, false)
    assert.equal(ui.start.classList.contains("hidden"), true)
    assert.equal(displays, 2)
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("a failed task request offers a visible retry instead of a dead end", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    let attempts = 0
    const api = async () => {
      attempts++
      if (attempts === 1) throw new Error("Stockfish is unavailable")
      return task()
    }
    const ui = elements()
    const {puzzles} = setup(api, ui)
    await puzzles.activate()
    assert.equal(ui.start.classList.contains("hidden"), false)
    assert.match(ui.status.textContent, /Stockfish is unavailable/)
    await ui.start.emit("click")
    assert.equal(attempts, 2)
    assert.equal(puzzles.state().taskId, "task-1")
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})

test("pool exhaustion is explicit and does not keep retrying a repeated position", async () => {
  const previousDocument = globalThis.document
  globalThis.document = fakeDocument()
  try {
    let attempts = 0
    const api = async () => {
      attempts++
      const error = new Error("No unseen verified Decision Puzzles remain, including color-swapped equivalents.")
      error.status = 409
      throw error
    }
    const ui = elements()
    const {puzzles} = setup(api, ui)
    await puzzles.activate()
    assert.equal(attempts, 1)
    assert.equal(puzzles.state().exhausted, true)
    assert.match(ui.status.textContent, /regular Puzzle library/)
    assert.equal(ui.start.disabled, true)
    await ui.start.emit("click")
    assert.equal(attempts, 1)
  } finally {
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
})
