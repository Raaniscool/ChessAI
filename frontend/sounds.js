// Board sounds: a short, quiet click for a move, a firmer one for a capture, a two-note
// ping for check. Synthesized with the Web Audio API (no sound files, nothing to license or
// download). The sound is chosen from the move that was actually played (chess.js result),
// so illegal moves — which never produce a move object — are silent.
//
//   const sounds = createBoardSounds()
//   sounds.playMove(move, fenAfter)   // move: chess.js move object; fenAfter: de-duplication key
//
// Every call site plays a move exactly once, where the move is made (a learner move, an AI
// demonstration step, a puzzle reply). Re-renders, navigation (← →) and position resets never
// call it, and the key guard below drops a second call for the same position within a moment.

export const SOUND_KINDS = ["move", "capture", "check"]
const STORAGE_KEY = "chessai.boardSounds"
const REPEAT_MS = 400

/** "check" | "capture" | "move" for a chess.js move (check wins over capture). */
export function soundFor(move) {
  if (!move || !move.san) return null
  if (/[+#]$/.test(move.san)) return "check"
  const flags = move.flags || ""
  if (move.captured || flags.includes("c") || flags.includes("e") || move.san.includes("x")) return "capture"
  return "move"
}

// One short tone: frequency glides from f0 to f1 while the volume decays.
function tone(ctx, out, {type = "sine", f0, f1 = f0, start = 0, dur, gain}) {
  const t = ctx.currentTime + start
  const osc = ctx.createOscillator()
  const g = ctx.createGain()
  osc.type = type
  osc.frequency.setValueAtTime(f0, t)
  if (f1 !== f0) osc.frequency.exponentialRampToValueAtTime(f1, t + dur)
  g.gain.setValueAtTime(0.0001, t)
  g.gain.exponentialRampToValueAtTime(gain, t + 0.004)
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur)
  osc.connect(g).connect(out)
  osc.start(t)
  osc.stop(t + dur + 0.02)
}

// A tiny burst of filtered noise: the "wood" in a piece being set down.
function knock(ctx, out, {start = 0, dur = 0.03, gain, freq}) {
  const t = ctx.currentTime + start
  const frames = Math.max(1, Math.floor(ctx.sampleRate * dur))
  const buffer = ctx.createBuffer(1, frames, ctx.sampleRate)
  const data = buffer.getChannelData(0)
  for (let i = 0; i < frames; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / frames) ** 2
  const src = ctx.createBufferSource()
  src.buffer = buffer
  const filter = ctx.createBiquadFilter()
  filter.type = "bandpass"
  filter.frequency.value = freq
  filter.Q.value = 1.2
  const g = ctx.createGain()
  g.gain.value = gain
  src.connect(filter).connect(g).connect(out)
  src.start(t)
}

const RECIPES = {
  move(ctx, out) {
    knock(ctx, out, {gain: 0.5, freq: 1700})
    tone(ctx, out, {f0: 420, f1: 260, dur: 0.07, gain: 0.12})
  },
  capture(ctx, out) {
    knock(ctx, out, {gain: 0.7, freq: 1200, dur: 0.045})
    tone(ctx, out, {f0: 300, f1: 150, dur: 0.11, gain: 0.18})
    knock(ctx, out, {start: 0.035, gain: 0.35, freq: 2200, dur: 0.025})
  },
  check(ctx, out) {
    knock(ctx, out, {gain: 0.45, freq: 1700})
    tone(ctx, out, {type: "triangle", f0: 880, dur: 0.09, gain: 0.08, start: 0.01})
    tone(ctx, out, {type: "triangle", f0: 1175, dur: 0.12, gain: 0.08, start: 0.09})
  },
}

export function createBoardSounds({storage = globalThis.localStorage, AudioCtx = globalThis.AudioContext ||
    globalThis.webkitAudioContext, volume = 0.6, now = () => Date.now()} = {}) {
  let ctx = null
  let master = null
  let last = {key: null, at: 0}
  let enabled = true
  try { enabled = !storage || storage.getItem(STORAGE_KEY) !== "off" } catch (_) { /* default on */ }

  function context() {
    if (!AudioCtx) return null
    if (!ctx) {
      try {
        ctx = new AudioCtx()
        master = ctx.createGain()
        master.gain.value = volume
        master.connect(ctx.destination)
      } catch (_) {
        ctx = null
        AudioCtx = null  // no audio here: stay silent from now on
        return null
      }
    }
    if (ctx.state === "suspended" && ctx.resume) ctx.resume().catch(() => {})
    return ctx
  }

  const api = {
    played: [],   // the last few kinds played (for tests and debugging)
    get enabled() { return enabled },
    setEnabled(on) {
      enabled = Boolean(on)
      try { if (storage) storage.setItem(STORAGE_KEY, enabled ? "on" : "off") } catch (_) { /* not remembered */ }
    },
    /** Play `kind`; `key` (e.g. the position after the move) drops an immediate repeat. */
    play(kind, key = null) {
      if (!enabled || !RECIPES[kind]) return false
      const t = now()
      if (key !== null && key === last.key && t - last.at < REPEAT_MS) return false
      last = {key, at: t}
      api.played.push(kind)
      if (api.played.length > 20) api.played.shift()
      const c = context()
      if (!c) return true
      try { RECIPES[kind](c, master) } catch (_) { /* a sound is never worth an error */ }
      return true
    },
    playMove(move, key = null) {
      const kind = soundFor(move)
      return kind ? api.play(kind, key) : false
    },
  }
  return api
}
