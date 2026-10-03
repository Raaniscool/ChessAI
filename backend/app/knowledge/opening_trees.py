"""Verified opening trees: many branches per opening, taught one branch at a time.

Built offline by scripts/build_opening_trees.py into knowledge/data/opening_trees/<id>.json:
  - moves: the Lichess opening database (CC0); an opening = every database line whose name belongs
    to it. Thin openings get their named lines continued by Stockfish (marked "engine").
  - every position has a Stockfish evaluation and every move a loss and a verdict (sound <= 120 cp,
    dubious <= 250 cp, mistake) — the same thresholds as the library's opening profile.
  - curated text (plans, what to watch for), verified traps and checked historical games.

At run time nothing here calls an engine or Qwen. This module answers:
  find(goal)              which opening (and which named variation) a request is about
  branch(tree, node, lvl) the branch to teach: a path from the start to a teaching endpoint, whose
                          length comes from the tree (named milestones), not from a fixed move count
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import chess

TREE_DIR = Path(__file__).resolve().parent / "data" / "opening_trees"
DATA = TREE_DIR

# A branch is taught only if every learner move is sound and no opponent move is a real mistake
# (opponent gambits — "dubious" — are fine: they are part of the theory being learned).
LEARNER_VERDICTS = {"sound"}
OPPONENT_VERDICTS = {"sound", "dubious"}
# Level -> the teaching endpoint: the first named milestone at least this deep (plies). The line's
# length therefore depends on where the opening's own named positions are (variable length).
LEVEL_MIN_PLIES = {"beginner": 8, "intermediate": 14}
ADVANCED_MIN_PLIES = 18  # an advanced branch that stops earlier is continued into deeper named theory
MAX_ALTERNATIVES = 4

_STOP = {
    "a", "an", "the", "i", "me", "my", "to", "want", "teach", "learn", "learning", "show", "how", "play",
    "playing", "please", "can", "you", "about", "with", "for", "of", "in", "on", "and", "or", "as", "against",
    "white", "black", "opening", "openings", "defense", "defence", "game", "variation", "variations", "line",
    "lines", "lesson", "lessons", "course", "theory", "study", "explain", "basics", "basic", "intro",
    "introduction", "beginner", "beginners", "intermediate", "advanced", "help", "more", "deeper", "some",
    "what", "is", "it", "do", "does", "main", "mainline", "system", "attack", "gambit", "trap", "traps",
}
# words that may name a variation even though they are stop words elsewhere
_KEEP_IN_NAMES = {"attack", "gambit", "system", "trap", "main"}


def normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("'s", "s").replace("'", "")
    return re.findall(r"[a-z0-9]+", text)


_STRAY_DIGITS = re.compile(r"\b([A-Z][a-z]{3,})\d+$")


def _clean_name(name: str | None) -> str | None:
    """Upstream typos such as "Spassky System3" (never touches move names like "with e3")."""
    return _STRAY_DIGITS.sub(r"\1", name) if name else name


@dataclass
class Node:
    i: int
    parent: int
    san: str | None
    eval: int
    best: str | None
    loss: int = 0
    verdict: str = "sound"
    name: str | None = None
    eco: str | None = None
    aka: list[str] = field(default_factory=list)
    src: str = "db"
    main: bool = False
    depth: int = 0
    children: list[int] = field(default_factory=list)


@dataclass
class Branch:
    tree: "OpeningTree"
    node: int                     # the teaching endpoint
    line: list[str]               # SAN from the start position
    name: str                     # the deepest named position on the line
    milestones: list[tuple[int, str]]
    engine_plies: int             # trailing moves that are Stockfish's continuation, not named theory
    side: str
    alternatives: list[dict]
    final_eval: int               # learner's point of view, cp

    @property
    def title(self) -> str:
        return self.name

    def as_dict(self) -> dict:
        return {"tree": self.tree.id, "node": self.node, "name": self.name, "line": self.line,
                "plies": len(self.line), "milestones": [{"ply": p, "name": n} for p, n in self.milestones],
                "engine_plies": self.engine_plies, "side": self.side, "alternatives": self.alternatives,
                "final_eval": self.final_eval}


class OpeningTree:
    def __init__(self, doc: dict):
        self.doc = doc
        self.id: str = doc["id"]
        self.title: str = doc["title"]
        self.side: str = doc["side"]
        self.aliases: list[str] = doc.get("aliases", [])
        self.names: list[str] = doc.get("names", [])
        self.catalog_topic: str | None = doc.get("catalog_topic")
        self.library_concept: str | None = doc.get("library_concept")
        self.summary: str = doc.get("summary", "")
        self.plans: list[str] = doc.get("plans", [])
        self.watch_for: list[str] = doc.get("watch_for", [])
        self.counts: dict = doc.get("counts", {})
        self.traps: list[dict] = doc.get("traps", [])
        self.games: list[dict] = doc.get("games", [])
        self.verification: dict = doc.get("verification", {})
        self.nodes: list[Node] = []
        for rec in doc["nodes"]:
            node = Node(i=rec["i"], parent=rec["p"], san=rec["m"], eval=rec["e"], best=rec.get("b"),
                        loss=rec.get("l", 0), verdict=rec.get("v", "sound"), name=_clean_name(rec.get("n")),
                        eco=rec.get("eco"), aka=rec.get("aka", []), src=rec.get("src", "db"),
                        main=bool(rec.get("main")))
            if node.parent >= 0:
                parent = self.nodes[node.parent]
                node.depth = parent.depth + 1
                parent.children.append(node.i)
            self.nodes.append(node)
        self._named_below = self._count_named()
        self._by_epd: dict[str, int] | None = None

    # ---------------------------------------------------------------- structure

    def _count_named(self) -> list[int]:
        counts = [0] * len(self.nodes)
        for node in reversed(self.nodes):  # children come after their parent
            counts[node.i] += 1 if node.name else 0
            if node.parent >= 0:
                counts[node.parent] += counts[node.i]
        return counts

    def path(self, i: int) -> list[Node]:
        out = []
        while i > 0:
            out.append(self.nodes[i])
            i = self.nodes[i].parent
        return out[::-1]

    def sans(self, i: int) -> list[str]:
        return [n.san for n in self.path(i)]

    def board(self, i: int) -> chess.Board:
        board = chess.Board()
        for san in self.sans(i):
            board.push_san(san)
        return board

    def learner_moves(self, node: Node, side: str) -> bool:
        """Is `node`'s move played by the learner (`side`)? Ply 1 is White's."""
        return (node.depth % 2 == 1) == (side == "white")

    def move_ok(self, node: Node, side: str) -> bool:
        allowed = LEARNER_VERDICTS if self.learner_moves(node, side) else OPPONENT_VERDICTS
        return node.verdict in allowed

    def teachable(self, i: int, side: str | None = None) -> bool:
        side = side or self.side
        return all(self.move_ok(n, side) for n in self.path(i))

    def opening_root(self) -> int:
        """The opening's defining position: the shallowest named node on the main line."""
        for node in self.nodes:
            if node.main and node.name:
                return node.i
        return 0

    def main_end(self) -> int:
        end = 0
        for node in self.nodes:
            if node.main:
                end = node.i if node.depth > self.nodes[end].depth else end
        return end

    def named_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.name]

    def find_position(self, board: chess.Board) -> int | None:
        """The tree node for a position (transpositions included), if any."""
        if self._by_epd is None:
            boards: dict[int, chess.Board] = {0: chess.Board()}
            by_epd = {boards[0].epd(): 0}
            for node in self.nodes[1:]:  # parents come before children
                board_i = boards[node.parent].copy(stack=False)
                board_i.push_san(node.san)
                boards[node.i] = board_i
                by_epd.setdefault(board_i.epd(), node.i)
            self._by_epd = by_epd
        return self._by_epd.get(board.epd())

    # ---------------------------------------------------------------- branches

    def continuation(self, start: int, side: str) -> list[int]:
        """From `start`, follow the line the tree treats as most important: the main line where
        it continues, else the child with the most named positions below it (the database's
        theory), stopping where no teachable child continues."""
        out, i = [], start
        while True:
            kids = [self.nodes[k] for k in self.nodes[i].children if self.move_ok(self.nodes[k], side)]
            kids = [k for k in kids if self._named_below[k.i] > 0 or k.src != "db"]
            if not kids:
                return out
            kids.sort(key=lambda k: (not k.main, -self._named_below[k.i], k.src != "db", k.i))
            i = kids[0].i
            out.append(i)

    def alternatives(self, line_nodes: list[int], side: str) -> list[dict]:
        """Named deviations along the branch: the opponent's other replies (what to expect) and
        the learner's other sound moves, most important first."""
        out = []
        on_line = set(line_nodes)
        for i in line_nodes:
            node = self.nodes[i]
            parent = self.nodes[node.parent]
            for k in parent.children:
                sib = self.nodes[k]
                if k in on_line or sib.src != "db":
                    continue
                named = self._first_named(k)
                if named is None or not self.move_ok(sib, side):
                    continue
                out.append({"ply": sib.depth, "san": sib.san, "by": "opponent" if not self.learner_moves(sib, side) else "you",
                            "name": self.nodes[named].name, "weight": self._named_below[k]})
        out.sort(key=lambda a: (-a["weight"], a["ply"]))
        seen, picked = set(), []
        for a in out:
            if a["name"] in seen:
                continue
            seen.add(a["name"])
            picked.append({k: v for k, v in a.items() if k != "weight"})
            if len(picked) >= MAX_ALTERNATIVES:
                break
        return sorted(picked, key=lambda a: a["ply"])

    def _first_named(self, i: int) -> int | None:
        if self.nodes[i].name:
            return i
        for k in self.nodes[i].children:
            if self.nodes[k].name:
                return k
        best = None
        for k in self.nodes[i].children:
            found = self._first_named(k)
            if found is not None and (best is None or self.nodes[found].depth < self.nodes[best].depth):
                best = found
        return best

    def branch(self, node: int | None = None, level: str | None = None, side: str | None = None) -> Branch | None:
        side = side or self.side
        start = node if node is not None else self.opening_root()
        if not self.teachable(start, side):
            return None
        path_nodes = [n.i for n in self.path(start)]
        cont = self.continuation(start, side)
        candidates = path_nodes + cont
        named = [i for i in candidates if self.nodes[i].name]
        reachable = [i for i in named if self.nodes[i].depth >= self.nodes[start].depth]
        level = level if level in ("beginner", "intermediate", "advanced") else "beginner"
        if level == "advanced":
            end = candidates[-1] if candidates else start
            if self.nodes[end].depth < ADVANCED_MIN_PLIES:
                floor = 0 if node is None else self.nodes[start].depth
                end = (self._deeper(end, floor, side, ADVANCED_MIN_PLIES, deepest=True)
                       or self._deeper(end, floor, side, self.nodes[end].depth + 1, deepest=True) or end)
        else:
            want = LEVEL_MIN_PLIES[level]
            deep_enough = [i for i in reachable if self.nodes[i].depth >= want]
            end = deep_enough[0] if deep_enough else (reachable[-1] if reachable else start)
            if self.nodes[end].depth < want:
                floor = 0 if node is None else self.nodes[start].depth
                end = self._deeper(end, floor, side, want, deepest=False) or end
        if node is not None and self.nodes[end].depth < self.nodes[node].depth:
            end = node
        line_nodes = [n.i for n in self.path(end)]
        named_on_line = []
        for i in line_nodes:  # the database often names several depths alike: keep the first of a run
            if self.nodes[i].name and (not named_on_line or self.nodes[named_on_line[-1]].name != self.nodes[i].name):
                named_on_line.append(i)
        name = self.nodes[named_on_line[-1]].name if named_on_line else self.title
        engine_plies = 0
        for i in reversed(line_nodes):
            if self.nodes[i].src == "db":
                break
            engine_plies += 1
        cp = self.nodes[end].eval
        return Branch(tree=self, node=end, line=self.sans(end), name=name,
                      milestones=[(self.nodes[i].depth, self.nodes[i].name) for i in named_on_line],
                      engine_plies=engine_plies, side=side,
                      alternatives=self.alternatives(line_nodes, side),
                      final_eval=cp if side == "white" else -cp)

    def _deeper(self, end: int, floor: int, side: str, min_depth: int, deepest: bool) -> int | None:
        """The line to `end` stops too early for the level: the named theory that leaves it as late
        as possible (never above depth `floor`, i.e. still inside a requested variation) and reaches
        `min_depth` (the deepest such position for advanced, the shallowest otherwise). Only
        teachable moves are followed; every tree position belongs to this opening."""
        line = [n.i for n in self.path(end)]
        for k in reversed(line):
            if self.nodes[k].depth < floor:
                break
            found: list[int] = []
            stack = [k]
            while stack:
                x = stack.pop()
                for c in self.nodes[x].children:
                    child = self.nodes[c]
                    if c in line or not self.move_ok(child, side):
                        continue
                    if child.name and child.src == "db" and child.depth >= min_depth:
                        found.append(c)
                    stack.append(c)
            if found:
                pick = max if deepest else min
                return pick(found, key=lambda c: (self.nodes[c].depth if deepest else -self.nodes[c].depth, -c))
        return None

    def traps_for(self, line: list[str] | None = None, min_shared: int = 4) -> list[dict]:
        """Verified traps; for a specific branch only the ones that share its first moves."""
        if line is None:
            return list(self.traps)
        out = []
        for trap in self.traps:
            shared = 0
            for a, b in zip(trap["moves"], line):
                if a != b:
                    break
                shared += 1
            if shared >= min(min_shared, len(line)):
                out.append(trap)
        return out

    def games_for(self, line: list[str] | None = None, min_shared: int = 6) -> list[dict]:
        if line is None:
            return list(self.games)
        out = []
        for game in self.games:
            shared = 0
            for a, b in zip(game["moves"], line):
                if a != b:
                    break
                shared += 1
            if shared >= min(min_shared, len(line)):
                out.append(game)
        return out

    def branch_names(self, limit: int = 6, exclude: str | None = None) -> list[str]:
        """The tree's most important named variations (for "other branches" suggestions): ranked by
        how many of the tree's positions carry the variation's name, reachable within 14 plies by
        teachable moves; one entry per variation ("Staunton Gambit" covers "... Accepted")."""
        root = self.opening_root()
        count: dict[str, int] = {}
        first: dict[str, int] = {}
        families = tuple(self.names) + (self.title,)
        for node in self.nodes:
            if not node.name or node.i == root or ":" not in node.name:
                continue
            base = node.name.split(",")[0]
            if ": " not in base or all(t in _STOP for t in normalize(base.split(": ", 1)[1])):
                continue  # "Scandinavian Defense: Main Line" is what a plain request teaches
            if not (base.split(": ")[0].startswith(families) or self.title in base):
                continue  # a different family that happens to share moves
            count[base] = count.get(base, 0) + 1
            if base not in first or node.depth < self.nodes[first[base]].depth:
                first[base] = node.i
        ranked = sorted((b for b in count if self.nodes[first[b]].depth <= 14 and self.teachable(first[b])),
                        key=lambda b: (-count[b], self.nodes[first[b]].depth, b))
        skip = {_variation_key(exclude, self.title)} if exclude else set()
        names = []
        for base in ranked:
            parent = re.sub(r" (Accepted|Declined)$", "", base)
            if parent != base and parent in count:
                base = parent  # "Smith-Morra Gambit", not "Smith-Morra Gambit Accepted"
            key = _variation_key(base, self.title)
            if key in skip or any(key.startswith(k + " ") for k in skip):
                continue
            skip.add(key)
            names.append(base)
            if len(names) >= limit:
                break
        return names


def _variation_key(name: str, title: str) -> str:
    """"Philidor Defense: Nimzowitsch Variation" and "...: Nimzowitsch" are one variation."""
    base = name.split(",")[0]
    label = base.split(": ", 1)[-1]
    if label == title:  # "Queen's Pawn Game: London System": the family is what distinguishes it
        label = base
    return re.sub(r" (Accepted|Declined|Variation)$", "", label)


# ---------------------------------------------------------------- the collection

@dataclass
class Match:
    tree: OpeningTree
    node: int | None              # a named variation inside the tree, or None (the opening itself)
    matched: str                  # what was recognised
    residual: list[str]           # request words that named nothing (kept for honesty/debug)


class OpeningTrees:
    def __init__(self, directory: Path | None = None):
        self.trees: dict[str, OpeningTree] = {}
        for path in sorted((directory or DATA).glob("*.json")):
            if path.name.startswith("_"):
                continue
            tree = OpeningTree(json.loads(path.read_text(encoding="utf-8")))
            self.trees[tree.id] = tree

    def __len__(self) -> int:
        return len(self.trees)

    def get(self, tree_id: str) -> OpeningTree | None:
        return self.trees.get(tree_id)

    def for_topic(self, topic_id: str) -> OpeningTree | None:
        return next((t for t in self.trees.values() if t.catalog_topic == topic_id), None)

    def find(self, goal: str) -> Match | None:
        words = normalize(goal)
        if not words:
            return None
        best: tuple[int, OpeningTree, list[str]] | None = None
        for tree in self.trees.values():
            for alias in tree.aliases + [tree.title]:
                toks = normalize(alias)
                if toks and _contains(words, toks) and (best is None or len(toks) > best[0]):
                    best = (len(toks), tree, toks)
        if best is not None:
            tree = best[1]
            # Several of the tree's names may be in the request ("Semi-Slav Defense: Meran" has
            # "slav defense" and "semi slav"): keep the reading that explains the rest best.
            readings = []
            for alias in tree.aliases + [tree.title]:
                toks = normalize(alias)
                if not toks or not _contains(words, toks):
                    continue
                rest = _without(words, toks)
                residual = [w for w in rest if w not in _STOP]
                if not residual:
                    readings.append(((0, 0), None))
                    continue
                soft = _soft_words(rest)
                node = _best_named(tree, residual, soft)
                if node is not None:
                    readings.append(((_name_score(tree.nodes[node].name, residual, tree, soft), len(residual)), node))
            if not readings:  # "Philidor position" is an endgame, not the Philidor Defense
                return None
            _key, node = min(readings, key=lambda r: (r[0], r[1] is not None, r[1] or 0))
            if node is None:
                return Match(tree, None, tree.title, [])
            return Match(tree, node, tree.nodes[node].name, [])
        # no opening named: a variation name alone ("Najdorf", "Fried Liver"), if unambiguous
        residual = [w for w in words if w not in _STOP]
        if not residual:
            return None
        hits = []
        for tree in self.trees.values():
            node = _best_named(tree, residual, _soft_words(words))
            if node is not None:
                hits.append((_name_score(tree.nodes[node].name, residual, tree, _soft_words(words)), tree, node))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[0], h[1].nodes[h[2]].depth))
        if len(hits) > 1 and hits[0][0] == hits[1][0] and hits[0][1] is not hits[1][1]:
            return None  # "exchange variation": several openings have one — not ours to guess
        _s, tree, node = hits[0]
        return Match(tree, node, tree.nodes[node].name, [])


def _contains(words: list[str], toks: list[str]) -> bool:
    n = len(toks)
    return any(words[i:i + n] == toks for i in range(len(words) - n + 1))


def _without(words: list[str], toks: list[str]) -> list[str]:
    n = len(toks)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == toks:
            return words[:i] + words[i + n:]
    return words


def _variation_tokens(name: str, tree: OpeningTree) -> list[str]:
    toks = normalize(name)
    for fam in tree.names:
        ft = normalize(fam)
        if toks[:len(ft)] == ft:
            return toks[len(ft):]
    return toks


def _name_score(name: str, residual: list[str], tree: OpeningTree, soft: frozenset = frozenset()) -> int:
    """How far the variation's name is from what was asked (lower = closer): its extra words, the
    asked-for "attack"/"gambit"/... it lacks, and a heavy penalty for an unasked "Anti-" line
    (the Anti-Fried Liver Defense is the opposite of the Fried Liver Attack)."""
    toks = _variation_tokens(name, tree)
    extra = [t for t in toks if t not in residual and t not in soft and (t not in _STOP or t in _KEEP_IN_NAMES)]
    score = len(extra) + sum(1 for w in soft if w not in toks)
    if "anti" in extra:
        score += 3
    return score


def _soft_words(goal_words: list[str]) -> frozenset:
    return frozenset(w for w in goal_words if w in _KEEP_IN_NAMES)


def _best_named(tree: OpeningTree, residual: list[str], soft: frozenset = frozenset()) -> int | None:
    want = set(residual)
    best = None
    for node in tree.nodes:
        if not node.name:
            continue
        toks = set(_variation_tokens(node.name, tree))
        if not want <= toks:
            continue
        key = (_name_score(node.name, residual, tree, soft), node.depth, node.i)
        if best is None or key < best[0]:
            best = (key, node.i)
    return best[1] if best else None


_trees: OpeningTrees | None = None
_lock = threading.Lock()


def get_opening_trees() -> OpeningTrees:
    global _trees
    with _lock:
        if _trees is None:
            _trees = OpeningTrees()
        return _trees


def reset_opening_trees() -> None:
    global _trees
    with _lock:
        _trees = None
