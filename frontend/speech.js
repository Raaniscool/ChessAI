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
// "e8=Q#", "O-O", "e4". Not inside words or coordinates ("Qwen3", "h2h3").
const MOVE_RE = /(?<![\w\-./])(?:(\d+)\.(?:\.\.)?\s?)?(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8](?:=[QRBN])?)([+#])?(?![\w-])/g

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
    if (seg.san) {
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

export class Narrator {
  /**
   * @param {object} hooks  onMove(mark|null, element) — highlight (or clear) a move/square;
   *                        onState(speaking: boolean) — UI feedback.
   */
  constructor(hooks = {}) {
    this.synth = typeof window !== "undefined" ? window.speechSynthesis : undefined
    this.supported = !!(this.synth && typeof window.SpeechSynthesisUtterance === "function")
    this.hooks = hooks
    const saved = (() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {} } catch (_) { return {} } })()
    this.enabled = !!saved.enabled
    this.rate = saved.rate || 1
    this.voiceName = saved.voice || ""
    this.queue = []
    this.current = null
    this.generation = 0
  }

  save() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({enabled: this.enabled, rate: this.rate, voice: this.voiceName}))
    } catch (_) { /* private mode */ }
  }

  voices() {
    if (!this.supported) return []
    const all = this.synth.getVoices()
    const lang = (navigator.language || "en").slice(0, 2)
    const mine = all.filter(v => v.lang && v.lang.toLowerCase().startsWith(lang))
    return (mine.length ? mine : all).sort((a, b) => score(b) - score(a))
  }

  voice() {
    const list = this.voices()
    return list.find(v => v.name === this.voiceName) || list[0] || null
  }

  /** Stop talking and forget everything queued. */
  stop() {
    this.generation++
    this.queue = []
    if (this.supported) this.synth.cancel()
    this._finish()
  }

  /** Read an element's text aloud (queued behind anything already being read). */
  speak(element) {
    if (!this.supported || !element) return Promise.resolve()
    return new Promise(resolve => {
      this.queue.push({element, resolve})
      if (!this.current) this._next()
    })
  }

  /** Auto-read: only when the learner turned read-aloud on. */
  auto(element) {
    return this.enabled ? this.speak(element) : Promise.resolve()
  }

  _next() {
    const item = this.queue.shift()
    if (!item) { this.current = null; this.hooks.onState && this.hooks.onState(false); return }
    const {element, resolve} = item
    const segments = segmentsOf(element)
    const {spoken, marks} = buildSpeech(segments)
    if (!spoken.trim()) { resolve(); this._next(); return }

    const generation = this.generation
    const utter = new window.SpeechSynthesisUtterance(spoken)
    const voice = this.voice()
    if (voice) { utter.voice = voice; utter.lang = voice.lang }
    utter.rate = this.rate
    let active = null
    let sawBoundary = false
    let fallbackTimer = null
    let started = 0
    const show = mark => {
      if (mark === active) return
      if (active && segments[active.segment].node) segments[active.segment].node.classList.remove("speaking")
      active = mark
      if (mark && segments[mark.segment].node) segments[mark.segment].node.classList.add("speaking")
      this.hooks.onMove && this.hooks.onMove(mark, element)
    }
    const done = () => {
      clearInterval(fallbackTimer)
      show(null)
      element.classList.remove("reading")
      if (this.current === item) this.current = null
      resolve()
      if (generation === this.generation) this._next()
    }
    utter.onstart = () => {
      started = performance.now()
      // Voices without word events (some network voices): estimate the position from time.
      fallbackTimer = setInterval(() => {
        if (sawBoundary) { clearInterval(fallbackTimer); return }
        const index = ((performance.now() - started) / 1000) * CHARS_PER_SECOND * this.rate
        show(markAt(marks, index))
      }, 120)
    }
    utter.onboundary = ev => {
      sawBoundary = true
      show(markAt(marks, ev.charIndex))
    }
    utter.onend = done
    utter.onerror = done
    this.current = item
    element.classList.add("reading")
    this.hooks.onState && this.hooks.onState(true)
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
