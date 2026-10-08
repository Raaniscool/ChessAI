// Read-aloud settings, sentence chunking and the natural/browser voice engines.
import test from "node:test"
import assert from "node:assert/strict"
import {Narrator, READ_KINDS, SPEEDS, buildSpeech, chunkSpeech, loadPrefs, shouldRead, voiceGroups} from "../speech.js"

test("settings: defaults, old saves keep their meaning", () => {
  const d = loadPrefs(null)
  assert.equal(d.enabled, false)
  assert.equal(d.speed, "normal")
  assert.equal(d.provider, "auto")
  assert.deepEqual(Object.keys(d.read), Object.keys(READ_KINDS))
  assert.equal(d.read.analysis, false)  // long summaries: only when asked for

  const old = loadPrefs({enabled: true, rate: 1.2, voice: "Microsoft Aria Online"})
  assert.equal(old.enabled, true)
  assert.equal(old.speed, "fast")
  assert.equal(old.browserVoice, "Microsoft Aria Online")
  assert.equal(old.voice, "")
  assert.equal(loadPrefs({rate: 0.8}).speed, "slow")

  const now = loadPrefs({enabled: true, speed: "slow", provider: "kokoro", voice: "bm_george",
    read: {hints: false, bogus: true}})
  assert.equal(now.voice, "bm_george")
  assert.equal(now.read.hints, false)
  assert.equal("bogus" in now.read, false)
  assert.equal(loadPrefs({speed: "warp"}).speed, "normal")
  assert.deepEqual(Object.keys(SPEEDS), ["slow", "normal", "fast"])
})

test("auto-read follows the settings and what matters", () => {
  const prefs = loadPrefs({enabled: true, read: {hints: false}})
  assert.equal(shouldRead(prefs, "lessons"), true)
  assert.equal(shouldRead(prefs, "hints"), false)
  assert.equal(shouldRead(prefs, "explanations", "critical"), true)
  assert.equal(shouldRead(prefs, "explanations", "important"), true)
  assert.equal(shouldRead(prefs, "explanations", "supporting"), false)
  assert.equal(shouldRead(prefs, "explanations", "obvious"), false)
  assert.equal(shouldRead(loadPrefs({enabled: false}), "lessons"), false)
})

test("chunks: sentence-sized, short first chunk, offsets line up", () => {
  const spoken = "Correct! " + "The knight attacks the king and the rook at the same time, which is called a fork. ".repeat(8)
  const chunks = chunkSpeech(spoken)
  assert.ok(chunks.length > 2)
  assert.ok(chunks[0].text.length <= 140, chunks[0].text)
  for (const c of chunks) {
    assert.ok(c.text.length <= 280)
    assert.equal(spoken.slice(c.start, c.end), c.text)
  }
  const joined = chunks.map(c => c.text).join(" ").replace(/\s+/g, " ")
  assert.equal(joined, spoken.trim().replace(/\s+/g, " "))
  assert.deepEqual(chunkSpeech("Knight to f3."), [{start: 0, end: 13, text: "Knight to f3."}])
  assert.deepEqual(chunkSpeech("   "), [])
  const run = "word ".repeat(150)  // no punctuation at all
  const pieces = chunkSpeech(run)
  assert.ok(pieces.length >= 3 && pieces.every(c => c.text.length <= 280 && !/^\s|\s$/.test(c.text)))
})

test("voice picker groups by persona", () => {
  const groups = voiceGroups([
    {id: "af_heart", persona: "female"}, {id: "am_michael", persona: "male"}, {id: "bf_emma", persona: "female"}])
  assert.deepEqual(groups.map(g => [g.persona, g.voices.length]), [["female", 2], ["male", 1]])
})

// ---------- engines (DOM needed) ----------

let JSDOM = null
try { ({JSDOM} = await import("jsdom")) } catch (_) { /* optional */ }

function setup({natural = true, failing = false, browser = true, provider = "auto"} = {}) {
  const dom = new JSDOM("<!doctype html><body></body>")
  globalThis.window = dom.window
  const spoken = []
  const synth = browser ? {speak: u => { spoken.push(u); setTimeout(() => u.onend && u.onend(), 1) }, cancel() {}, getVoices: () => []} : null
  dom.window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text } }
  const requests = []
  const fakeFetch = async (url, opts) => {
    requests.push({url, body: JSON.parse(opts.body)})
    if (failing) return {ok: false, status: 503, blob: async () => new Blob([])}
    return {ok: true, status: 200, blob: async () => new Blob(["audio"], {type: "audio/wav"})}
  }
  const played = []
  class FakeAudio {
    constructor(url) { this.url = url; this.duration = 1; this.currentTime = 0; played.push(this) }
    play() { setTimeout(() => { this.currentTime = 1; this.ontimeupdate && this.ontimeupdate(); this.onended && this.onended() }, 2); return Promise.resolve() }
    pause() { this.paused = true }
  }
  const store = new Map()
  const storage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v)}
  storage.setItem("chess-tutor-speech", JSON.stringify({enabled: true, provider, voice: "bm_george", speed: "slow"}))
  const notices = []
  const narrator = new Narrator({onNotice: t => notices.push(t)}, {fetch: fakeFetch, Audio: FakeAudio, synth, storage})
  if (natural) {
    narrator.configure({default: "kokoro", providers: [
      {id: "kokoro", available: true, voices: [{id: "af_heart", persona: "female"}, {id: "bm_george", persona: "male"}]},
      {id: "openai", available: false, voices: []}]})
  }
  const el = text => { const d = dom.window.document.createElement("div"); d.textContent = text; return d }
  return {narrator, requests, played, spoken, notices, el, storage}
}

const LONG = "Good move. " + "Your knight now attacks both the king and the queen, so Black must move the king. ".repeat(4)

test("natural voice: sentence by sentence, chosen voice and speed, cached", {skip: !JSDOM}, async () => {
  const {narrator, requests, played, spoken, el} = setup()
  assert.equal(narrator.engine(), "natural")
  await narrator.speak(el(LONG))
  assert.ok(requests.length >= 2, "long text is split into several requests")
  assert.ok(requests.every(r => r.url === "/api/tts/speak" && r.body.voice === "bm_george" && r.body.speed === "slow"))
  assert.equal(played.length, requests.length)
  assert.equal(spoken.length, 0)
  const before = requests.length
  await narrator.speak(el(LONG))
  assert.equal(requests.length, before, "the same sentences are not fetched twice")
})

test("natural voice: unknown saved voice uses the provider's first voice", {skip: !JSDOM}, async () => {
  const {narrator, requests, el} = setup()
  narrator.prefs.voice = "gone_voice"
  await narrator.speak(el("Rook to e1."))
  assert.equal(requests[0].body.voice, "af_heart")
})

test("natural voice fails: browser voice takes over, then stays", {skip: !JSDOM}, async () => {
  const {narrator, requests, spoken, notices, el} = setup({failing: true})
  await narrator.speak(el("Knight takes e5. That wins a pawn."))
  assert.equal(requests.length >= 1, true)
  assert.equal(spoken.length, 1)
  assert.match(spoken[0].text, /knight takes e5/i)  // moves spoken as words
  await narrator.speak(el("Bishop to c4."))
  assert.equal(notices.length, 1)
  assert.equal(narrator.engine(), "browser")  // after two failures: no more waiting on the server
})

test("no voice at all: the text just stays on screen", {skip: !JSDOM}, async () => {
  const {narrator, el} = setup({failing: true, browser: false})
  await narrator.speak(el("Queen to d8."))
  await narrator.speak(el("Queen to d8."))
  assert.equal(narrator.engine(), null)
  assert.equal(narrator.supported, false)
  await narrator.speak(el("Anything"))  // resolves immediately
})

test("browser setting or no server voice: no audio requests", {skip: !JSDOM}, async () => {
  const a = setup({provider: "browser"})
  await a.narrator.speak(a.el("Castle kingside."))
  assert.equal(a.requests.length, 0)
  assert.equal(a.spoken.length, 1)
  assert.equal(a.spoken[0].rate, SPEEDS.slow)
  const b = setup({natural: false})
  await b.narrator.speak(b.el("Castle kingside."))
  assert.equal(b.requests.length, 0)
})

test("preview uses the voice being tried, not the saved one; settings persist", {skip: !JSDOM}, async () => {
  const {narrator, requests, storage} = setup()
  globalThis.document = window.document
  await narrator.preview({provider: "kokoro", voice: "af_heart"})
  assert.equal(requests[0].body.voice, "af_heart")
  assert.equal(narrator.prefs.voice, "bm_george")
  narrator.prefs.speed = "fast"
  narrator.save()
  assert.equal(JSON.parse(storage.getItem("chess-tutor-speech")).speed, "fast")
  delete globalThis.document
})

test("stop cancels playback and queued text", {skip: !JSDOM}, async () => {
  const {narrator, played, el} = setup()
  const first = narrator.speak(el(LONG))
  const second = narrator.speak(el("Queued text."))
  await new Promise(r => setTimeout(r, 1))
  narrator.stop()
  await first
  await second
  assert.equal(narrator.current, null)
  assert.ok(played.length <= 2)
})

test("auto-read respects the kind and importance", {skip: !JSDOM}, async () => {
  const {narrator, requests, el} = setup()
  narrator.prefs.read.hints = false
  await narrator.auto(el("A hint."), "hints")
  await narrator.auto(el("Obvious recapture."), "explanations", "obvious")
  assert.equal(requests.length, 0)
  await narrator.auto(el("This wins the queen."), "explanations", "critical")
  assert.equal(requests.length, 1)
  assert.deepEqual(buildSpeech([{text: "Nf3", san: "Nf3"}]).spoken.length > 0, true)
})
