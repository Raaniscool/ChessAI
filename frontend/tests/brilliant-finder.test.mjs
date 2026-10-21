import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {MoveHistory} from "../board-nav.js"
import {createBrilliantFinder} from "../brilliant-finder.js"

// A verified non-start puzzle position: Black to move after 1.e4.
const POSITION = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
const LINE = [
  {move: "e7e5", reply: "g1f3"},
  {move: "b8c6", reply: "f1b5"},
]

class FakeClassList {
  constructor() { this.values = new Set() }
  toggle(name, force) {
    if (force === undefined) force = !this.values.has(name)
    if (force) this.values.add(name)
    else this.values.delete(name)
    return force
  }
  contains(name) { return this.values.has(name) }
}

function element(value = "") {
  const listeners = new Map()
  return {
    classList: new FakeClassList(),
    textContent: "",
    value,
    disabled: false,
    listeners,
    addEventListener(type, fn) { listeners.set(type, fn) },
    async dispatch(type, event = {}) { return listeners.get(type)?.(event) },
  }
}

function elements() {
  return {
    difficulty: element("adaptive"), difficultyNote: element(), profile: element(), status: element(),
    task: element(), complexity: element(), selected: element(), start: element(), reveal: element(),
    skip: element(), retry: element(), next: element(), newRound: element(), result: element(), verdict: element(),
    explanation: element(), setup: element(), sacrifice: element(), defense: element(), score: element(),
    alternative: element(), selectedLine: element(), bestLine: element(), recording: element(),
    showSelected: element(), showBest: element(),
  }
}

function key(fen) {
  return new Chess(fen).fen().split(/\s+/).slice(0, 4).join(" ")
}
function same(a, b) { return key(a) === key(b) }

function play(fen, uci) {
  const chess = new Chess(fen)
  const move = chess.move({from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || "q"})
  if (!move) throw new Error(`illegal fixture move ${uci} from ${fen}`)
  return {uci: move.from + move.to + (move.promotion || ""), san: move.san, afterFen: chess.fen(), move}
}

let boardFen = ""
function boardStub() {
  return {
    handler: null,
    side: null,
    disableMoveInput() { this.handler = null },
    enableMoveInput(handler, side) { this.handler = handler; this.side = side },
  }
}

function legalInputHandlerFor(board) {
  return (getChess, onMove) => async event => {
    if (!event || event.type !== "test-move") return false
    const chess = getChess()
    const beforeFen = chess.fen()
    const applied = play(beforeFen, event.uci)
    boardFen = applied.afterFen
    const transition = {beforeFen, uci: applied.uci, move: applied.move, afterFen: applied.afterFen}
    return onMove(applied.move.from, applied.move.to, {transition})
  }
}

function makeResult(task, played) {
  const line = [...played]
  const lineHistory = MoveHistory.fromMoves(task.fen, line.map(m => m.san), Chess)
  const sacrificeFen = lineHistory.nodes[Math.min(2, lineHistory.last)].fen
  return {
    outcome: "guided_solution", completed: true, revealed: false, success: true, solved: true,
    mistakes: 0, label: "Combination completed", recorded: true, analysis_status: "complete",
    verdict: "sound_sacrifice", selected_move: "Nc6", selected_uci: "b8c6", depth: 10,
    score: {perspective: "black", loss_cp: 0, before: {label: "0.00 pawns for you"}, after: {label: "0.00 pawns for you"}},
    best_move: {san: "Nc6", uci: "b8c6"}, stronger_alternative: null,
    analysis: {analysis_status: "complete", verdict: "sound_sacrifice", label: "Sound sacrifice",
      position_fen: sacrificeFen, best_line: {moves: ["Nc6", "Bb5"], ucis: ["b8c6", "f1b5"]}},
    explanation: {summary: "The stored line has one preparatory player move before its key sacrifice, Nc6.",
      main_idea: "A tactical combination", source_explanation: "The setup leaves the opponent with a forcing defense.",
      preparatory_line: "e5 → Nf3", sacrifice_move: "Nc6", played_key_move: "Nc6",
      strongest_defense: "Bb5", defense_source: "stockfish", engine_label: "Sound sacrifice"},
    verified_solution: {moves: line.map(m => m.san)},
    played_line: {moves: line.map(m => m.san)},
  }
}

function fixtureApi({beforeMove = null} = {}) {
  const calls = []
  const tasks = new Map()
  let preference = "adaptive"
  let nextCount = 0
  const routeState = new Map()
  const api = async (path, method = "GET", body = {}) => {
    calls.push({path, method, body})
    if (path === "/api/puzzles/brilliant/profile") {
      return {difficulty: preference, available: 171, labels_note: "relative difficulty",
        bands: ["beginner", "easy", "intermediate", "advanced", "expert"].map(level => ({
          difficulty: level, label: level[0].toUpperCase() + level.slice(1), count: 34,
          description: `${level} relative complexity`,
        })),
        adaptive: {summary: "Aimed at your tactics level from a starting estimate."},
        results: {total: 0, scored: 0, recent: []}}
    }
    if (path === "/api/puzzles/brilliant/preferences") {
      preference = body.difficulty
      return {difficulty: preference}
    }
    if (path === "/api/puzzles/brilliant/next") {
      nextCount += 1
      const task = {task_id: `task-${nextCount}`, id: `puzzle-${nextCount}`, fen: POSITION,
        side_to_move: "black", prompt: "Play the full combination yourself.",
        difficulty: {preference, label: preference, complexity_band: preference, complexity: {decision_points: 2}}}
      tasks.set(task.task_id, {task, step: 0, currentFen: POSITION, played: [], cache: new Map(), result: null})
      return task
    }
    const match = path.match(/^\/api\/puzzles\/brilliant\/(task-\d+)\/(move|state|reveal|retry)$/)
    if (!match) throw new Error(`unexpected route: ${path}`)
    const [, taskId, action] = match
    const server = tasks.get(taskId)
    if (!server) throw new Error("unknown task")
    if (action === "state") {
      return {id: server.task.id, task_id: taskId, status: server.result ? "complete" : "playing",
        current_fen: server.currentFen, step_index: server.step, played: server.played, result: server.result}
    }
    if (action === "reveal") {
      const result = {outcome: "revealed", completed: false, revealed: true, success: false, solved: false,
        label: "Verified solution revealed", recorded: true, analysis_status: "not_run",
        explanation: {source_explanation: "Verified source explanation."},
        verified_solution: {moves: ["e5", "Nf3", "Nc6", "Bb5"]},
        played_line: {moves: server.played.map(item => item.san)}}
      server.result = result
      return {done: true, current_fen: server.currentFen, result}
    }
    if (action === "retry") {
      nextCount += 1
      const nextTask = {...server.task, task_id: `task-${nextCount}`}
      tasks.set(nextTask.task_id, {task: nextTask, step: 0, currentFen: POSITION, played: [], cache: new Map(), result: null})
      return nextTask
    }
    if (action === "move") {
      if (beforeMove) await beforeMove({taskId, body, server})
      const cacheKey = `${body.step_index}:${body.move}`
      if (server.cache.has(cacheKey)) return server.cache.get(cacheKey)
      if (body.step_index !== server.step || !same(body.before_fen, server.currentFen)) {
        const err = new Error("stale turn")
        err.status = 409
        throw err
      }
      const step = LINE[server.step]
      const player = play(server.currentFen, body.move)
      if (body.move !== step.move) {
        const wrong = {outcome: "incorrect", message: "That misses the verified line. Try again.",
          step_index: server.step, next_step_index: server.step, current_fen: server.currentFen,
          player_move: null, opponent_move: null, done: false, result: null}
        server.cache.set(cacheKey, wrong)
        return wrong
      }
      const opponent = play(player.afterFen, step.reply)
      server.played.push({role: "player", uci: player.uci, san: player.san, after_fen: player.afterFen})
      server.played.push({role: "opponent", uci: opponent.uci, san: opponent.san, after_fen: opponent.afterFen})
      server.currentFen = opponent.afterFen
      const oldIndex = server.step
      server.step += 1
      const done = server.step >= LINE.length
      if (done) server.result = makeResult(server.task, server.played)
      const response = {outcome: done ? "complete" : "correct", message: done ? "Verified line complete." : "Correct.",
        step_index: oldIndex, next_step_index: server.step, current_fen: server.currentFen,
        player_move: {uci: player.uci, san: player.san, after_fen: player.afterFen},
        opponent_move: {uci: opponent.uci, san: opponent.san, after_fen: opponent.afterFen},
        done, result: done ? server.result : null}
      server.cache.set(cacheKey, response)
      return response
    }
  }
  return {api, calls, tasks, preference: () => preference}
}

function setupFinder(api, calls = []) {
  const els = elements()
  boardFen = ""
  const showPosition = async fen => { boardFen = fen; return {fen} }
  const isPositionVerified = fen => boardFen && same(boardFen, fen)
  const verifiedMove = async (fen, uci) => {
    assert.ok(same(boardFen, fen), "verified opponent reply starts from the displayed player position")
    const result = play(fen, uci)
    boardFen = result.afterFen
    return {beforeFen: fen, afterFen: result.afterFen, uci: result.uci, move: result.move}
  }
  const board = boardStub()
  const finder = createBrilliantFinder({api, board, showPosition, verifiedMove, isPositionVerified, Chess,
    COLOR: {white: "white", black: "black"}, legalInputHandler: legalInputHandlerFor(board), MoveHistory,
    showNode: async node => { boardFen = node.fen }, nav: null, setStatus() {}, el: els})
  return {finder, els, board, getBoardFen: () => boardFen, calls}
}

test("Finder uses guided board turns immediately and difficulty changes only future positions", async () => {
  const fixture = fixtureApi()
  const {finder, els, board, getBoardFen} = setupFinder(fixture.api, fixture.calls)
  await finder.enter()
  await finder.next()
  const activeId = finder.state().id
  assert.equal(activeId, "puzzle-1")
  assert.equal(finder.state().preference, "adaptive")
  assert.equal(board.side, "black")
  assert.ok(board.handler)
  assert.equal("submit" in els, false)
  assert.equal(finder.submit, undefined)

  const firstHandler = board.handler
  await firstHandler({type: "test-move", uci: "e7e5"})
  assert.equal(finder.state().stepIndex, 1)
  assert.equal(new Chess(getBoardFen()).turn(), "b")
  assert.equal(fixture.calls.filter(call => call.path.endsWith("/move")).length, 1)
  assert.equal(fixture.calls.filter(call => call.path.includes("/move"))[0].body.step_index, 0)
  assert.equal(board.side, "black")

  const positionAfterReply = getBoardFen()
  await finder.setDifficulty("expert")
  assert.equal(fixture.preference(), "expert")
  assert.equal(finder.state().preference, "expert")
  assert.equal(finder.state().id, activeId)
  assert.equal(finder.state().stepIndex, 1)
  assert.ok(same(getBoardFen(), positionAfterReply))
  assert.match(els.status.textContent, /current puzzle and move progress are unchanged/i)

  await board.handler({type: "test-move", uci: "b8c6"})
  assert.equal(finder.state().result, true)
  assert.equal(finder.state().solved, true)
  assert.equal(els.verdict.textContent, "Combination completed")
  assert.match(els.setup.textContent, /e5 → Nf3/)
  assert.match(els.sacrifice.textContent, /Nc6/)
  assert.match(els.defense.textContent, /Bb5/)
  assert.match(els.selectedLine.textContent, /e5 → Nf3 → Nc6 → Bb5/)
  assert.match(els.recording.textContent, /Recorded as solved/)

  await finder.setDifficulty("beginner")
  assert.equal(finder.state().id, activeId)
  assert.equal(finder.state().result, true)
  await finder.next()
  assert.equal(finder.state().id, "puzzle-2")
  assert.equal(finder.state().preference, "beginner")
  const nextCall = fixture.calls.filter(call => call.path === "/api/puzzles/brilliant/next")[1]
  assert.deepEqual(nextCall.body.exclude, ["puzzle-1"])
})

test("incorrect board move restores the same position and never triggers an opponent reply", async () => {
  const fixture = fixtureApi()
  const {finder, board, getBoardFen} = setupFinder(fixture.api)
  await finder.enter()
  await finder.next()
  const before = getBoardFen()
  await board.handler({type: "test-move", uci: "d7d6"})
  assert.ok(same(getBoardFen(), before))
  assert.equal(finder.state().stepIndex, 0)
  assert.equal(board.side, "black")
  assert.match(fixture.calls.find(call => call.path.endsWith("/move")).path, /\/move$/)
  assert.equal(fixture.calls.filter(call => call.path.endsWith("/move")).length, 1)
  assert.match(finder.state().currentFen, /rnbqkbnr/)
  assert.match(finder.state().result ? "done" : "playing", /playing/)

  await board.handler({type: "test-move", uci: "e7e5"})
  assert.equal(finder.state().stepIndex, 1)
  assert.equal(fixture.calls.filter(call => call.path.endsWith("/move")).length, 2)
})

test("duplicate input is blocked while the server is processing the move", async () => {
  let release
  let started
  const waitForMove = new Promise(resolve => { release = resolve })
  const called = new Promise(resolve => { started = resolve })
  const fixture = fixtureApi({beforeMove: async () => { started(); await waitForMove }})
  const {finder, board} = setupFinder(fixture.api)
  await finder.enter()
  await finder.next()
  const handler = board.handler
  const pending = handler({type: "test-move", uci: "e7e5"})
  await called
  assert.equal(board.handler, null)
  assert.equal(finder.state().busy, true)
  assert.equal(fixture.calls.filter(call => call.path.endsWith("/move")).length, 1)
  release()
  await pending
  assert.equal(finder.state().stepIndex, 1)
  assert.equal(board.side, "black")
})

test("explicit reveal shows the complete verified line without auto-playing, and retry restarts same puzzle", async () => {
  const fixture = fixtureApi()
  const {finder, els, board, getBoardFen} = setupFinder(fixture.api)
  await finder.enter()
  await finder.next()
  const start = getBoardFen()
  await finder.reveal()
  assert.equal(finder.state().result, true)
  assert.equal(finder.state().solved, false)
  assert.ok(same(getBoardFen(), start))
  assert.match(els.verdict.textContent, /solution revealed/i)
  assert.match(els.recording.textContent, /not solved/i)
  assert.match(els.selectedLine.textContent, /e5 → Nf3 → Nc6 → Bb5/)
  assert.ok(board.handler === null)

  // Displaying the line on the board requires the explicit review click.
  await els.showSelected.dispatch("click")
  assert.ok(!same(getBoardFen(), start))
  await finder.retry()
  assert.equal(finder.state().id, "puzzle-1")
  assert.equal(finder.state().stepIndex, 0)
  assert.equal(finder.state().result, false)
  assert.ok(same(getBoardFen(), start))
  assert.ok(board.handler)
})
