// Read-aloud for the tutor's text, with the board following along.
//
// Uses the browser's built-in speech (Web Speech API: offline Windows voices in Edge and
// Chrome, no install, no server). Chess notation is spoken as words ("Nxg5" → "knight
// takes G5"), and when the voice reaches a move or a square, that square is highlighted on
// the board and the move is highlighted in the text.
//
// The pure part (tokenize / sanToSpeech / buildSpeech) has no DOM dependency and is unit
// tested with `node --test frontend/tests`.

// A move (SAN) or a bare square, optionally with a move number: "Kf5", "4...Nxg5", "exd5",
// "e8=Q#", "O-O", "e4", and Black's moves written "...e6". Squares chained with a dash or a
// slash ("the h4-e1 diagonal", "Nbd2-f1-g3", "e4/d4") are found one by one. Not inside
// words or coordinates ("Qwen3", "h2h3", "a1-level").
const MOVE_RE = /(?:(?<![\w\-–./])|(?<=[a-h][1-8][+#]?[\-–/]))(?:(\d+)\.(?:\.\.)?\s?|\.\.\.)?(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8](?:=[QRBN])?)([+#])?(?!\w|[\-–/](?![KQRBN]?[a-h]?x?[a-h][1-8]))/g

// What a joiner between two moves/squares sounds like: "h4-e1" → "H4 to E1", "e4/d4" → "E4 or D4".
const JOINERS = {"-": " to ", "–": " to ", "/": " or "}

const PIECES = {K: "king", Q: "queen", R: "rook", B: "bishop", N: "knight"}

// Symbols that shouldn't be read out (emoji, arrows, bullets) — the words carry the meaning.
const SILENT_RE = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}•★☆✓✔✕✗]/gu

function square(sq) {
  // Upper-case letter: voices say "E four" / "A four", not "uh four".
  return sq[0].toUpperCase() + sq[1]
}

/** Spoken words for one SAN move or square: "Nxg5+" → "knight takes G5, check". */
export function sanToSpeech(san) {
  const m = /^(O-O-O|O-O|([KQRBN])?([a-h])?([1-8])?(x)?([a-h][1-8])(?:=([QRBN]))?)([+#])?$/.exec(san)
  if (!m) return san
  let words
  if (m[1] === "O-O") words = "castles kingside"
  else if (m[1] === "O-O-O") words = "castles queenside"
  else {
    const [, , piece, file, rank, capture, to, promo] = m
    const parts = []
    if (piece) parts.push(PIECES[piece])
    const from = (file ? file.toUpperCase() : "") + (rank || "")
    if (from) parts.push(piece ? `${from} to` : from)  // "knight B to D2", pawn "E takes D5"
    if (capture) parts.push("takes")
    parts.push(square(to))
    if (promo) parts.push(`promotes to a ${PIECES[promo]}`)
    words = parts.join(" ")
  }
  if (m[8] === "+") words += ", check"
  if (m[8] === "#") words += ", checkmate"
  return words
}

/** Destination square of a SAN move or square ("Nxg5" → "g5"); null for castling. */
export function targetSquare(san) {
  const m = /([a-h][1-8])(?:=[QRBN])?[+#]?$/.exec(san)
  return m ? m[1] : null
}

/** Split text into plain segments and move segments ({text, san} for moves). */
export function tokenize(text) {
  const out = []
  let last = 0
  for (const m of text.matchAll(MOVE_RE)) {
    if (m.index > last) out.push({text: text.slice(last, m.index)})
    out.push({text: m[0], san: m[2] + (m[3] || "")})
    last = m.index + m[0].length
  }
  if (last < text.length) out.push({text: text.slice(last)})
  return out
}

function cleanPlain(text) {
  return text.replace(/\bhttps?:\/\/\S+/g, " ").replace(/\(\s*\)/g, " ").replace(SILENT_RE, " ").replace(/\*\*|__|`/g, "").replace(/\.\.\.(?=\s|$)/g, ".")
}

/**
 * Spoken text for a list of segments, plus where each move sits in it.
 * `segments` come from tokenize() (a `node` field is carried through for the DOM layer).
 * Returns {spoken, marks: [{start, end, san, square, segment}]}.
 */
export function buildSpeech(segments) {
  let spoken = ""
  const marks = []
  segments.forEach((seg, i) => {
    const between = !seg.san && segments[i - 1]?.san && segments[i + 1]?.san
    // "Bxf7+ Ke7 O-O": a short pause between the moves of a line, not one long word salad.
    const joiner = between && (JOINERS[seg.text] || (/^\s+$/.test(seg.text) ? ", " : null))
    if (joiner) {
      spoken += joiner
    } else if (seg.san) {
      if (spoken && !/\s$/.test(spoken)) spoken += " "
      const words = sanToSpeech(seg.san)
      marks.push({start: spoken.length, end: spoken.length + words.length, san: seg.san,
                  square: targetSquare(seg.san), segment: i})
      spoken += words
    } else {
      spoken += cleanPlain(seg.text)
    }
  })
  // Collapse whitespace without shifting marks: rebuild with an offset map.
  let out = ""
  const map = []
  for (let i = 0; i < spoken.length; i++) {
    const ch = /\s/.test(spoken[i]) ? " " : spoken[i]
    if (ch === " " && (out.endsWith(" ") || out === "")) { map.push(out.length); continue }
    map.push(out.length)
    out += ch
  }
  map.push(out.length)
  for (const mk of marks) { mk.start = map[mk.start]; mk.end = map[mk.end] }
  return {spoken: out.trimEnd(), marks}
}

/** The mark being spoken at character `index`, if any. */
export function markAt(marks, index) {
  return marks.find(m => index >= m.start && index < m.end) || null
}

// ---------------------------------------------------------------------------
// Browser layer
// ---------------------------------------------------------------------------

const PREFS_KEY = "chess-tutor-speech"
const CHARS_PER_SECOND = 14  // fallback pacing when a voice sends no word events

// ---------- read-aloud settings (pure, tested) ----------

export const SPEEDS = {slow: 0.85, normal: 1, fast: 1.2}
// What may be read out automatically. Everything else is read only when the learner presses 🔊.
export const READ_KINDS = {
  lessons: "Lesson steps",
  explanations: "Important move explanations",
  hints: "Hints",
  puzzles: "Exercise instructions",
  analysis: "Game analysis summaries",
}
const DEFAULT_READ = {lessons: true, explanations: true, hints: true, puzzles: true, analysis: false}
export const SAMPLE_TEXT = "Knight to f3 develops a piece and controls the centre. Now it's your move — what would you play?"

/** Saved settings in today's shape. Old saves ({enabled, rate, voice}) keep their meaning. */
export function loadPrefs(raw) {
  const saved = raw && typeof raw === "object" ? raw : {}
  let speed = SPEEDS[saved.speed] ? saved.speed : "normal"
  if (!SPEEDS[saved.speed] && typeof saved.rate === "number") {
    speed = saved.rate <= 0.9 ? "slow" : saved.rate >= 1.15 ? "fast" : "normal"
  }
  const read = {...DEFAULT_READ}
  if (saved.read && typeof saved.read === "object") {
    for (const k of Object.keys(DEFAULT_READ)) if (k in saved.read) read[k] = !!saved.read[k]
  }
  const oldBrowserVoice = saved.rate !== undefined && typeof saved.voice === "string" ? saved.voice : ""
  return {
    enabled: !!saved.enabled,
    provider: typeof saved.provider === "string" && saved.provider ? saved.provider : "auto",
    voice: saved.rate !== undefined ? "" : (typeof saved.voice === "string" ? saved.voice : ""),
    browserVoice: typeof saved.browserVoice === "string" ? saved.browserVoice : oldBrowserVoice,
    speed,
    read,
  }
}

/**
 * Should this be read out without the learner asking? Only when read-aloud is on, that kind
 * of text is ticked, and (for move explanations) the moment matters — obvious moves stay quiet.
 */
export function shouldRead(prefs, kind, importance) {
  if (!prefs.enabled) return false
  if (kind && prefs.read && prefs.read[kind] === false) return false
  if (importance && !["critical", "important"].includes(importance)) return false
  return true
}

/**
 * Split spoken text into sentence-sized chunks (each ≤ max characters) that the server voice
 * can start on quickly; offsets point into `spoken` so word highlighting still lines up.
 */
export function chunkSpeech(spoken, max = 280) {
  const chunks = []
  const push = (start, end) => {
    while (start < end && /\s/.test(spoken[start])) start++
    while (end > start && /\s/.test(spoken[end - 1])) end--
    if (end > start) chunks.push({start, end, text: spoken.slice(start, end)})
  }
  const sentences = []
  const re = /[^.!?…]+(?:[.!?…]+["”’)]*|$)/g
  let m
  while ((m = re.exec(spoken)) && m[0]) sentences.push([m.index, m.index + m[0].length])
  let from = null
  let to = null
  for (const [a, b] of sentences) {
    if (b - a > max) {  // one very long sentence: break at commas, then spaces
      if (from !== null) { push(from, to); from = null }
      let s = a
      while (b - s > max) {
        const slice = spoken.slice(s, s + max)
        let cut = Math.max(slice.lastIndexOf(", "), slice.lastIndexOf("; "))
        if (cut < max / 3) cut = slice.lastIndexOf(" ")
        if (cut <= 0) cut = max
        push(s, s + cut + 1)
        s = s + cut + 1
      }
      from = s; to = b
      continue
    }
    if (from === null) { from = a; to = b; continue }
    // The first chunk stays short so the voice starts at once; later ones group sentences.
    const limit = chunks.length === 0 ? Math.min(max, 140) : max
    if (b - from <= limit) to = b
    else { push(from, to); from = a; to = b }
  }
  if (from !== null) push(from, to)
  return chunks
}

/** Voices of the usable server provider, grouped for a picker: [{persona, voices}]. */
export function voiceGroups(voices) {
  const groups = []
  for (const persona of ["female", "male"]) {
    const list = voices.filter(v => v.persona === persona)
    if (list.length) groups.push({persona, label: persona === "female" ? "Female voices" : "Male voices", voices: list})
  }
  return groups
}

/**
 * Reads tutor text aloud.
 *   natural voice — a real neural voice from the server (/api/tts), sentence by sentence, the
 *                   next sentence fetched while the current one plays; cached on both sides.
 *   browser voice — the system voices (speechSynthesis) when no natural voice is set up or it fails.
 *   neither       — the text simply stays on screen.
 * Moves are spoken as words ("knight takes e5") and highlighted on the board as they're read.
 */
export class Narrator {
  /**
   * @param {object} hooks  onMove(mark|null, element, node) — highlight (or clear) a move/square;
   *                        onState(speaking: boolean) — UI feedback;
   *                        onNotice(text) — the natural voice stopped working (now using the browser).
   * @param {object} deps   fetch / Audio / storage, replaceable in tests.
   */
  constructor(hooks = {}, deps = {}) {
    const win = typeof window !== "undefined" ? window : {}
    this.synth = "synth" in deps ? deps.synth : win.speechSynthesis
    this.browserSupported = !!(this.synth && typeof win.SpeechSynthesisUtterance === "function")
    this.fetch = deps.fetch || (typeof fetch === "function" ? fetch.bind(globalThis) : null)
    this.Audio = deps.Audio || win.Audio || null
    this.storage = "storage" in deps ? deps.storage : (typeof localStorage !== "undefined" ? localStorage : null)
    this.hooks = hooks
    let saved = {}
    try { saved = JSON.parse(this.storage && this.storage.getItem(PREFS_KEY)) || {} } catch (_) { /* ignore */ }
    this.prefs = loadPrefs(saved)
    this.server = null        // /api/tts status
    this.failures = 0         // natural-voice errors in a row; after 2 we use the browser voice
    this.audioCache = new Map()
    this.queue = []
    this.current = null
    this.generation = 0
    this.playing = null       // {audio, url}
  }

  // ----- settings -----
  get enabled() { return this.prefs.enabled }
  set enabled(on) { this.prefs.enabled = !!on }
  get rate() { return SPEEDS[this.prefs.speed] || 1 }
  get voiceName() { return this.prefs.browserVoice }
  set voiceName(name) { this.prefs.browserVoice = name }

  /** Something can speak: a natural voice on the server, or the browser's own. */
  get supported() { return this.browserSupported || !!this.naturalProvider() }

  save() {
    try { this.storage && this.storage.setItem(PREFS_KEY, JSON.stringify(this.prefs)) } catch (_) { /* private mode */ }
  }

  /** Server status from GET /api/tts. */
  configure(status) {
    this.server = status && Array.isArray(status.providers) ? status : null
    this.failures = 0
  }

  /** The server provider to use, or null for the browser voice. */
  naturalProvider(prefs = this.prefs) {
    if (!this.server || !this.fetch || !this.Audio || this.failures >= 2) return null
    if (prefs.provider === "browser") return null
    const usable = this.server.providers.filter(p => p.available && p.voices.length)
    return usable.find(p => p.id === prefs.provider) || usable.find(p => p.id === this.server.default) || usable[0] || null
  }

  engine(prefs = this.prefs) {
    if (this.naturalProvider(prefs)) return "natural"
    return this.browserSupported ? "browser" : null
  }

  voices() {
    if (!this.browserSupported) return []
    const all = this.synth.getVoices()
    const lang = ((typeof navigator !== "undefined" && navigator.language) || "en").slice(0, 2)
    const mine = all.filter(v => v.lang && v.lang.toLowerCase().startsWith(lang))
    return (mine.length ? mine : all).sort((a, b) => score(b) - score(a))
  }

  voice(name = this.prefs.browserVoice) {
    const list = this.voices()
    return list.find(v => v.name === name) || list[0] || null
  }

  /** Stop talking and forget everything queued. */
  stop() {
    this.generation++
    const queued = this.queue
    this.queue = []
    for (const item of queued) item.resolve()  // nobody waits forever on text that won't be read
    if (this.browserSupported) this.synth.cancel()
    this._stopAudio()
    this._finish()
  }

  /** Read an element's text aloud (queued behind anything already being read). */
  speak(element, options = {}) {
    if (!element || !this.engine(this._prefsFor(options))) return Promise.resolve()
    return new Promise(resolve => {
      this.queue.push({element, resolve, options})
      if (!this.current) this._next()
    })
  }

  /** Auto-read: only when read-aloud is on, this kind of text is ticked, and it matters. */
  auto(element, kind = "lessons", importance = undefined) {
    return shouldRead(this.prefs, kind, importance) ? this.speak(element) : Promise.resolve()
  }

  /** Let the learner hear a voice before choosing it. */
  preview(choice) {
    this.stop()
    const el = typeof document !== "undefined" ? document.createElement("div") : null
    if (!el) return Promise.resolve()
    el.textContent = SAMPLE_TEXT
    return this.speak(el, choice)
  }

  _prefsFor(options) {
    return options && (options.provider || options.voice || options.browserVoice || options.speed)
      ? {...this.prefs, ...options} : this.prefs
  }

  _next() {
    const item = this.queue.shift()
    if (!item) { this.current = null; this.hooks.onState && this.hooks.onState(false); return }
    const segments = segmentsOf(item.element)
    const {spoken, marks} = buildSpeech(segments)
    if (!spoken.trim()) { item.resolve(); this._next(); return }
    const generation = this.generation
    const prefs = this._prefsFor(item.options)
    const show = this._highlighter(item.element, segments)
    const done = () => {
      show(null)
      item.element.classList.remove("reading")
      if (this.current === item) this.current = null
      item.resolve()
      if (generation === this.generation) this._next()
    }
    this.current = item
    item.element.classList.add("reading")
    this.hooks.onState && this.hooks.onState(true)
    const provider = this.naturalProvider(prefs)
    if (provider) {
      this._playNatural(provider, prefs, spoken, marks, show, generation).then(rest => {
        if (generation !== this.generation) return
        if (rest === null || !this.browserSupported) done()
        else this._playBrowser(prefs, spoken, marks, show, done, rest)  // continue where it failed
      })
    } else {
      this._playBrowser(prefs, spoken, marks, show, done, 0)
    }
  }

  _highlighter(element, segments) {
    let active = null
    return mark => {
      if (mark === active) return
      if (active && segments[active.segment].node) segments[active.segment].node.classList.remove("speaking")
      active = mark
      if (mark && segments[mark.segment].node) segments[mark.segment].node.classList.add("speaking")
      this.hooks.onMove && this.hooks.onMove(mark, element, mark ? segments[mark.segment].node : null)
    }
  }

  // ----- natural (server) voice -----

  /** Audio for one chunk; the same text in the same voice is fetched only once. */
  _audioFor(provider, prefs, text) {
    const voiceIds = provider.voices.map(v => v.id)
    const voice = voiceIds.includes(prefs.voice) ? prefs.voice : voiceIds[0]
    const key = `${provider.id}|${voice}|${prefs.speed}|${text}`
    if (this.audioCache.has(key)) {
      const hit = this.audioCache.get(key)
      this.audioCache.delete(key); this.audioCache.set(key, hit)  // most recently used
      return hit
    }
    const request = this.fetch("/api/tts/speak", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, voice, speed: prefs.speed, provider: provider.id}),
    }).then(res => {
      if (!res.ok) throw new Error(`voice unavailable (${res.status})`)
      return res.blob()
    })
    request.catch(() => this.audioCache.delete(key))
    this.audioCache.set(key, request)
    while (this.audioCache.size > 80) this.audioCache.delete(this.audioCache.keys().next().value)
    return request
  }

  /**
   * Play chunk after chunk. Resolves null when everything was spoken (or reading was stopped),
   * or the index in `spoken` where the browser voice should take over after an error.
   */
  async _playNatural(provider, prefs, spoken, marks, show, generation) {
    const chunks = chunkSpeech(spoken)
    let next = chunks.length ? this._audioFor(provider, prefs, chunks[0].text) : null
    for (let i = 0; i < chunks.length; i++) {
      const chunk = chunks[i]
      let blob
      try {
        blob = await next
      } catch (_) {
        return this._naturalFailed(generation, chunk.start)
      }
      if (generation !== this.generation) return null
      // Fetch the next sentence while this one plays: no gap between sentences.
      next = i + 1 < chunks.length ? this._audioFor(provider, prefs, chunks[i + 1].text) : null
      if (next) next.catch(() => {})
      const ok = await this._playBlob(blob, chunk, marks, show, generation)
      if (generation !== this.generation) return null
      if (!ok) return this._naturalFailed(generation, chunk.start)
      this.failures = 0
    }
    return null
  }

  _naturalFailed(generation, at) {
    if (generation !== this.generation) return null
    this.failures++
    if (this.failures === 2 && this.hooks.onNotice) {
      this.hooks.onNotice(this.browserSupported
        ? "The natural voice isn't responding, so I'm using your browser's voice for now."
        : "The natural voice isn't responding — the text stays on screen.")
    }
    return at
  }

  _playBlob(blob, chunk, marks, show, generation) {
    return new Promise(resolve => {
      let url = ""
      try { url = URL.createObjectURL(blob) } catch (_) { resolve(false); return }
      const audio = new this.Audio(url)
      this.playing = {audio, url}
      let settled = false
      const finish = ok => {
        if (settled) return
        settled = true
        audio.ontimeupdate = audio.onended = audio.onerror = null
        if (this._stopResolve === stopper) this._stopResolve = null
        if (this.playing && this.playing.audio === audio) this._stopAudio()
        resolve(ok)
      }
      const stopper = () => finish(true)
      // Word highlighting: estimate the position from how far the audio has played.
      audio.ontimeupdate = () => {
        if (generation !== this.generation || !audio.duration) return
        const share = Math.min(1, audio.currentTime / audio.duration)
        show(markAt(marks, chunk.start + share * (chunk.end - chunk.start)))
      }
      audio.onended = () => finish(true)
      audio.onerror = () => finish(false)
      this._stopResolve = stopper
      const started = audio.play()
      if (started && typeof started.catch === "function") started.catch(() => finish(false))
    })
  }

  _stopAudio() {
    const playing = this.playing
    this.playing = null
    if (playing) {
      try { playing.audio.pause() } catch (_) { /* ignore */ }
      try { URL.revokeObjectURL(playing.url) } catch (_) { /* ignore */ }
    }
    const resolveStop = this._stopResolve
    this._stopResolve = null
    if (resolveStop && playing) resolveStop()
  }

  // ----- browser voice -----

  _playBrowser(prefs, spoken, marks, show, done, from) {
    if (!this.browserSupported) { done(); return }
    const text = spoken.slice(from)
    if (!text.trim()) { done(); return }
    const utter = new window.SpeechSynthesisUtterance(text)
    const voice = this.voice(prefs.browserVoice)
    if (voice) { utter.voice = voice; utter.lang = voice.lang }
    utter.rate = SPEEDS[prefs.speed] || 1
    let sawBoundary = false
    let fallbackTimer = null
    let started = 0
    const finish = () => { clearInterval(fallbackTimer); done() }
    utter.onstart = () => {
      started = performance.now()
      // Voices without word events (some network voices): estimate the position from time.
      fallbackTimer = setInterval(() => {
        if (sawBoundary) { clearInterval(fallbackTimer); return }
        const index = from + ((performance.now() - started) / 1000) * CHARS_PER_SECOND * utter.rate
        show(markAt(marks, index))
      }, 120)
    }
    utter.onboundary = ev => {
      sawBoundary = true
      show(markAt(marks, from + ev.charIndex))
    }
    utter.onend = finish
    utter.onerror = finish
    this.synth.speak(utter)
  }

  _finish() {
    if (this.current) {
      const {element, resolve} = this.current
      element.classList.remove("reading")
      element.querySelectorAll(".speaking").forEach(n => n.classList.remove("speaking"))
      this.current = null
      resolve()
    }
    this.hooks.onMove && this.hooks.onMove(null, null)
    this.hooks.onState && this.hooks.onState(false)
  }
}

function score(voice) {
  // Prefer natural-sounding and local voices (local voices send word events → exact timing).
  let s = 0
  if (/natural|neural|online/i.test(voice.name)) s += 4
  if (voice.localService) s += 1
  if (/en-US|en-GB/i.test(voice.lang)) s += 1
  return s
}

/**
 * Wrap moves/squares in an element's text in <span class="mv" data-san=...> (idempotent),
 * so they can be highlighted while spoken.
 */
export function decorateMoves(element) {
  if (!element || element.dataset.moves === "1") return element
  const doc = element.ownerDocument
  const walker = doc.createTreeWalker(element, 4 /* NodeFilter.SHOW_TEXT */)
  const nodes = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.parentElement && n.parentElement.closest("button, .no-speech")) continue
    nodes.push(n)
  }
  for (const node of nodes) {
    const parts = tokenize(node.nodeValue)
    if (!parts.some(p => p.san)) continue
    const frag = doc.createDocumentFragment()
    for (const p of parts) {
      if (p.san) {
        const span = doc.createElement("span")
        span.className = "mv"
        span.dataset.san = p.san
        span.textContent = p.text
        frag.appendChild(span)
      } else {
        frag.appendChild(doc.createTextNode(p.text))
      }
    }
    node.parentNode.replaceChild(frag, node)
  }
  element.dataset.moves = "1"
  return element
}

/** The element's text as segments, with each move segment pointing at its <span>. */
function segmentsOf(element) {
  decorateMoves(element)
  const segments = []
  const pause = () => {
    const prev = segments.length ? segments[segments.length - 1].text : ""
    segments.push({text: /[.!?:;]\s*$/.test(prev) || !prev.trim() ? " " : ". "})
  }
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        segments.push({text: child.nodeValue})
      } else if (child.nodeType === 1) {
        if (child.matches("button, .no-speech, .meta, script, style")) continue
        if (child.classList.contains("mv")) {
          segments.push({text: child.textContent, san: child.dataset.san, node: child})
          continue
        }
        const block = child.matches("br, li, div, p, ol, ul")
        if (block) pause()
        walk(child)
        if (block) pause()
      }
    }
  }
  walk(element)
  return segments
}
