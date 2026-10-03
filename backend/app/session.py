"""Lesson sessions — the server owns the authoritative board state.

The browser renders what it is told; every learner move is validated here
against the session's `chess.Board` before any engine or teacher is involved.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace

import chess

from . import chess_system
from .chess_system import ChessError
from .engine.classification import Classification
from .lessons import Lesson, LessonLibrary, get_library
from .lessons.schema import DemonstrateStep, ExerciseStep, TeachStep
from .teacher.library_feedback import library_feedback, strip_said

_CATEGORY_RANK = [
    Classification.EXCELLENT,
    Classification.GOOD,
    Classification.INACCURATE,
    Classification.MISTAKE,
    Classification.BLUNDER,
]


# Notes that contradict a lesson-verified move: the library already checked that it
# keeps the win (a long mate beyond the live search depth is not a "missed mate").
_LESSON_OVERRIDDEN_NOTES = ("missed_mate", "decided_position")


def agree_with_lesson(feedback):
    """Grade a move the lesson accepts so the card never says "Blunder" next to "Correct!".

    Accepted moves were verified by Stockfish when the lesson was built, under the
    teaching policy (within 120 cp of the best move, or still clearly winning). The
    live grader uses the stricter loss thresholds, so such a move could be labelled
    Mistake while the lesson rightly accepts it. It is shown as Good instead, with an
    `engine_prefers` note so the explanation names the engine's stronger move. The
    engine's numbers (loss, evaluations, lines) are left untouched.
    """
    if _CATEGORY_RANK.index(feedback.category) <= _CATEGORY_RANK.index(Classification.GOOD):
        return feedback
    notes = [n for n in feedback.notes if n not in _LESSON_OVERRIDDEN_NOTES] + ["engine_prefers"]
    return replace(feedback, category=Classification.GOOD, notes=notes)


class SessionError(Exception):
    """Base for session problems; carries an HTTP-ish status."""
    status = 400


class SessionNotFound(SessionError):
    status = 404


class ExerciseConflict(SessionError):
    """Action not allowed in the session's current state."""
    status = 409


@dataclass
class Session:
    id: str
    lesson_id: str
    step_index: int = 0
    board: chess.Board = field(default_factory=chess.Board)
    history: list[str] = field(default_factory=list)
    hint_index: int = 0
    exercise_accepted: bool = False
    status: str = "active"  # active | completed
    transcript: list[dict] = field(default_factory=list)
    last_feedback: object | None = None  # MoveFeedback of the latest graded move
    last_context: object | None = None   # LessonContext it was graded in
    # Adaptive lessons: the session's own copy of the steps (examples can be swapped or
    # inserted mid-lesson by learner.adapt), and what happened so far.
    steps: list = field(default_factory=list)
    wrong_tries: int = 0                 # wrong moves on the current exercise
    example_state: dict = field(default_factory=dict)  # example id -> tries/hints/reveal
    outcomes: list = field(default_factory=list)       # learner.adapt.Outcome per finished example
    changes: int = 0                     # adaptations made in this lesson
    notes: dict = field(default_factory=dict)          # id(step) -> coach note shown with it
    said: dict = field(default_factory=dict)           # example id -> sentences the instant feedback showed
    learner_id: str = "local"


class SessionManager:
    def __init__(self, library: LessonLibrary | None = None):
        self.library = library or get_library()
        self._sessions: dict[str, Session] = {}
        self.completed_lessons: set[str] = set()

    # --- lifecycle ---

    def start(self, lesson_id: str) -> tuple[Session, dict]:
        lesson = self.library.lesson(lesson_id)
        session = Session(
            id=uuid.uuid4().hex[:12],
            lesson_id=lesson_id,
            steps=list(lesson.steps),
        )
        self._enter_step(session, lesson, 0)
        self._sessions[session.id] = session
        self._learner(lambda p: p.record_lesson(self._lesson_concepts(session), completed=False))
        return session, self.step_payload(session)

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise SessionNotFound(f"Unknown session: {session_id}")
        return self._sessions[session_id]

    def lesson(self, session: Session) -> Lesson:
        return self.library.lesson(session.lesson_id)

    # --- board/step mechanics ---

    def _enter_step(self, session: Session, lesson: Lesson, index: int) -> None:
        session.step_index = index
        session.hint_index = 0
        session.wrong_tries = 0
        session.exercise_accepted = False
        step = self.steps(session)[index]
        if isinstance(step, TeachStep):
            if step.board and "fen" in step.board:
                session.board = chess_system.parse_fen(step.board["fen"])
        elif isinstance(step, DemonstrateStep):
            session.board = chess_system.parse_fen(step.fen)
        elif isinstance(step, ExerciseStep):
            session.board = chess_system.parse_fen(step.fen)
            # puzzle timing: the clock starts at the example's first exercise step
            self._example_state(session, step).setdefault("started", time.monotonic())

    def steps(self, session: Session) -> list:
        return session.steps or self.lesson(session).steps

    def current_step(self, session: Session) -> object:
        return self.steps(session)[session.step_index]

    # --- Knowledge Library examples (verified entries only) ---

    def _example_for(self, session: Session, at_or_before: bool = True):
        """The verified library example the current step presents (or the latest one
        before it, e.g. for chat on a following step). None for ordinary lessons."""
        from .knowledge.library import get_knowledge

        steps = self.steps(session)
        indexes = range(session.step_index, -1, -1) if at_or_before else [session.step_index]
        for i in indexes:
            ref = getattr(steps[i], "example", None)
            if ref:
                return get_knowledge().get(ref)  # trusted_only: never an unverified entry
        return None

    def _example_facts(self, session: Session, example, board: chess.Board | None,
                       reveal: bool = True) -> list[str]:
        from .knowledge.library import get_knowledge
        from .knowledge.retrieval import teaching_facts

        if example is None:
            return []
        return teaching_facts(example, get_knowledge(), board=board, level=self.lesson(session).difficulty,
                              reveal=reveal)

    def _solution_pending(self, session: Session, example_id: str) -> bool:
        """True while the learner still has to find a move of this example (no spoilers)."""
        steps = self.steps(session)
        for i in range(session.step_index, len(steps)):
            step = steps[i]
            if isinstance(step, ExerciseStep) and step.example == example_id:
                if i > session.step_index or not session.exercise_accepted:
                    return True
        return False

    @staticmethod
    def _usage():
        from .knowledge.usage import get_usage
        return get_usage()

    def step_payload(self, session: Session) -> dict:
        lesson = self.lesson(session)
        steps = self.steps(session)
        step = steps[session.step_index]
        base = {
            "index": session.step_index,
            "type": step.type,
            "total_steps": len(steps),
            "lesson_title": lesson.title,
            "accepted": session.exercise_accepted,
            # What Continue leads to, so the button can say "Your turn: practise it".
            "next_type": (steps[session.step_index + 1].type
                          if session.step_index + 1 < len(steps) else None),
        }
        note = session.notes.get(id(step))
        if note:
            base["coach_note"] = note
        example = self._example_for(session, at_or_before=False) if getattr(step, "example", None) else None
        if example is not None:
            base["example"] = {"id": example.id, "title": example.title, "concept": example.concept,
                               "source_url": (example.source or {}).get("source_url"),
                               "explainable": not self._solution_pending(session, example.id)}
        if isinstance(step, TeachStep):
            board = chess_system.validate_board_spec(
                {**(step.board or {}), "fen": session.board.fen()}
            )
            text = step.text
            said = session.said.get(getattr(step, "example", None) or "")
            if said:  # the instant feedback already said part of this example's explanation
                text = strip_said(text, said) or text
            return {**base, "text": text, "board": board}
        if isinstance(step, DemonstrateStep):
            after = chess_system.validate_board_spec(
                step.board or {}, board=chess_system.parse_fen(step.fen)
            )
            final = chess_system.parse_fen(step.fen)
            for uci in step.moves:
                final.push(chess_system.parse_move(final, uci))
            return {
                **base,
                "text": step.text,
                "start_fen": step.fen,
                "moves": step.moves,
                "comments": step.comments,
                "final_fen": final.fen(),
                "board_after": {**after, "fen": final.fen()},
            }
        assert isinstance(step, ExerciseStep)
        return {
            **base,
            "prompt": step.prompt,
            "side": step.side,
            "concepts": step.concepts or lesson.concepts,
            "hints_total": len(step.hints),
            "hints_used": session.hint_index,
            "board": {"fen": step.fen, "lock": False, "highlights": []},
        }

    def advance(self, session: Session) -> dict | None:
        """Move to the next step. Returns payload, or None when completed."""
        if session.status == "completed":
            raise ExerciseConflict("Lesson already completed")
        lesson = self.lesson(session)
        step = self.steps(session)[session.step_index]
        if isinstance(step, ExerciseStep) and not session.exercise_accepted:
            raise ExerciseConflict("Resolve the exercise before moving on")
        next_index = session.step_index + 1
        if next_index >= len(self.steps(session)):
            session.status = "completed"
            self.completed_lessons.add(lesson.id)
            self._record_completed(session)
            self._learner(lambda p: p.record_lesson(self._lesson_concepts(session), completed=True,
                                                    lesson_id=lesson.id))
            return None
        self._enter_step(session, lesson, next_index)
        return self.step_payload(session)

    def _record_completed(self, session: Session) -> None:
        ids = list(dict.fromkeys(s.example for s in self.steps(session) if getattr(s, "example", None)))
        for eid in ids:
            try:
                self._usage().record_completed(eid)
            except OSError:  # statistics must never break a lesson
                pass

    # --- exercise interaction ---

    def apply_move(self, session: Session, uci: str) -> dict:
        from .engine import get_engine
        from .config import get_settings
        from .teacher import FallbackTeacher, LessonContext

        if session.status == "completed":
            raise ExerciseConflict("Lesson already completed")
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("This step does not accept moves")

        before = session.board.copy()
        move = chess_system.parse_move(before, uci)  # raises ChessError if illegal
        san = before.san(move)

        engine = get_engine()  # may raise EngineUnavailable -> 503
        feedback = engine.evaluate_move(before, move)

        accepted = self._is_accepted(step, san, feedback)
        if accepted and step.advance_on == "accepted_move":
            feedback = agree_with_lesson(feedback)
        push = accepted or step.advance_on == "any_legal"
        if push:
            session.board.push(move)
            session.history.append(move.uci())
        state = self._example_state(session, step)
        first_move_of_example = self._first_exercise_of_example(session)
        if accepted:
            session.exercise_accepted = True
            if first_move_of_example and state.get("key_found") is None:
                state["key_found"] = session.wrong_tries == 0 and session.hint_index == 0
        else:
            session.wrong_tries += 1
            state["wrong"] = state.get("wrong", 0) + 1

        lesson = self.lesson(session)
        example = self._example_for(session, at_or_before=False) if step.example else None
        from .teacher.importance import classify, read_aloud, wants_ai
        importance, _why = classify(feedback, accepted=accepted, key_move=first_move_of_example or not step.example,
                                    board=before)
        level, style, note = self._teaching_prefs(lesson, importance)
        context = LessonContext(
            course_title=self.library.course(lesson.course_id).title,
            lesson_title=lesson.title,
            concepts=step.concepts or lesson.concepts,
            exercise_prompt=step.prompt,
            accepted_moves=step.accepted_san,
            facts=self._example_facts(session, example, before),
            example_id=example.id if example else None,
            move_accepted=accepted,
            level=level,
            style=style,
            importance=importance,
            learner_note=note,
        )
        if example is not None:
            try:
                self._usage().record_attempt(example.id, accepted, hints=session.hint_index)
            except OSError:
                pass
        # Answer instantly with the engine verdict + deterministic explanation.
        # The (slower) Qwen explanation is streamed separately via explain_stream(),
        # so the learner never waits on the language model to see the result.
        session.last_feedback = feedback
        session.last_context = context
        result = {
            "accepted": accepted,
            "feedback": feedback.as_dict(),
            "explanation": FallbackTeacher().explain_move(feedback, context),
            "teacher": "fallback",
            # the AI teacher only where a coach would say more (teacher.importance)
            "ai_explanation": get_settings().qwen_configured() and wants_ai(importance, style),
            "importance": importance,
            "read_aloud": read_aloud(importance),
            "san": san,
        }
        if accepted and example is not None:
            # A verified library example: its stored, verified teaching text is the feedback, at
            # once. The AI teacher is not asked to re-explain what the library already knows; it
            # stays available on request ("Explain deeper") and for wrong moves.
            final = not self._solution_pending(session, example.id)
            concept = self.knowledge_concept_name(example)
            verified = library_feedback(example, before, san, list(step.accepted_san or []), final=final,
                                        said=step.continue_text or "", idea=concept)
            if verified:
                result.update(explanation=verified["text"], teacher="library", ai_explanation=False,
                              deeper=final)
                session.said.setdefault(example.id, set()).update(verified["sentences"])
            else:
                result["ai_explanation"] = False  # still nothing for the language model to add here
        if accepted:
            result["continue_text"] = step.continue_text
            adapted = self._exercise_resolved(session, step)
            if adapted:
                result["adapted"] = adapted
        else:
            # Not good enough: position stays put so the learner can try again.
            result["reset_fen"] = step.fen
            from .learner.adapt import help_offer
            offer = help_offer(session.wrong_tries, session.hint_index, len(step.hints))
            if offer:
                result["help"] = offer
            result["tries"] = session.wrong_tries
        return result

    @staticmethod
    def _is_accepted(step: ExerciseStep, san: str, feedback) -> bool:
        if step.advance_on == "any_legal":
            return True
        if step.advance_on == "accepted_move":
            return san in (step.accepted_san or [])
        # min_classification
        try:
            played = Classification(feedback.category.value)
            required = Classification(step.min_category)
        except ValueError:
            return False
        return _CATEGORY_RANK.index(played) <= _CATEGORY_RANK.index(required)

    def hint(self, session: Session) -> dict:
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("Hints are only available during exercises")
        if session.exercise_accepted:
            raise ExerciseConflict("Exercise already solved")
        if session.hint_index >= len(step.hints):
            return {"exhausted": True, "index": session.hint_index, "total": len(step.hints)}
        hint = step.hints[session.hint_index]
        session.hint_index += 1
        state = self._example_state(session, step)
        state["hints"] = state.get("hints", 0) + 1
        return {
            "hint": hint,
            "index": session.hint_index,
            "total": len(step.hints),
            "remaining": len(step.hints) - session.hint_index,
            "exhausted": session.hint_index >= len(step.hints),
        }

    def reveal(self, session: Session) -> dict:
        """Learner explicitly asks for the solution (never automatic)."""
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("Nothing to reveal in this step")
        already = session.exercise_accepted
        session.exercise_accepted = True
        out = {"accepted_moves": step.accepted_san or [], "accepted": True}
        if not already:
            self._example_state(session, step)["revealed"] = True
            adapted = self._exercise_resolved(session, step)
            if adapted:
                out["adapted"] = adapted
        # the move itself, so the board can play it (UCI of the first accepted move)
        if step.accepted_san:
            try:
                board = chess_system.parse_fen(step.fen)
                out["uci"] = board.parse_san(step.accepted_san[0]).uci()
            except ValueError:
                pass
        return out

    @staticmethod
    def knowledge_concept_name(example) -> str | None:
        try:
            from .knowledge.library import get_knowledge
            concept = get_knowledge().concepts.get(example.concept)
        except Exception:  # the name is a nicety
            return None
        return concept.name if concept else None

    def _teaching_prefs(self, lesson, importance: str) -> tuple[str, str, str]:
        """(level, explanation style, learner note) for explanations in this lesson."""
        try:
            from .learner import get_profile, prompt_context
            from .learner.personalize import personalized
            profile = get_profile()
            if personalized(profile):
                note = prompt_context(profile, self._lesson_concepts_cached(lesson)) if importance == "critical" else ""
                return profile.level, profile.explanation_style, note
            return lesson.difficulty if lesson.difficulty in ("beginner", "intermediate", "advanced") else \
                "beginner", profile.explanation_style, ""
        except Exception:  # pragma: no cover - defensive
            return "beginner", "balanced", ""

    def _lesson_concepts_cached(self, lesson) -> list[str]:
        from .knowledge.library import get_knowledge
        library = get_knowledge()
        out = []
        for name in lesson.concepts[:2]:
            found = library.match_concepts(name)
            if found:
                out.append(found[0])
        return out

    def completion_summary(self, session: Session) -> dict:
        """How the lesson went, in plain words, and what to do next."""
        from .knowledge.library import get_knowledge
        from .learner import get_profile
        from .learner.recommend import suggestions

        done = session.outcomes
        clean = sum(1 for o in done if o.score >= 1.0)
        looked = sum(1 for o in done if o.revealed)
        if not done:
            text = ""
        elif clean == len(done):
            text = f"You solved all {len(done)} positions on the first try." if len(done) > 1 else \
                "You solved it on the first try."
        elif looked == len(done):
            text = "These were hard. That's fine: we'll come back to this idea with clearer positions."
        else:
            text = f"First-try solves: {clean} of {len(done)}."
            if looked:
                text += " The ones you looked up will come back in a later review."
        try:
            nxt = suggestions(get_profile(), get_knowledge(), just_studied=self._lesson_concepts(session))
        except Exception:  # suggestions must never break finishing a lesson
            nxt = []
        return {"summary": {"positions": len(done), "first_try": clean, "revealed": looked, "text": text,
                            "adapted": session.changes},
                "next_steps": nxt[:3]}

    # --- the learner model and in-lesson adaptation ---

    @staticmethod
    def _learner(fn):
        """Apply an update to the learner profile; the profile must never break a lesson."""
        try:
            from .learner import get_store
            return get_store().update("local", fn)
        except Exception:  # pragma: no cover - defensive
            import logging
            logging.getLogger(__name__).warning("learner profile update failed", exc_info=True)
            return None

    def _lesson_concepts(self, session: Session) -> list[str]:
        """Library concept ids this lesson teaches (its examples' concepts, else its names)."""
        from .knowledge.library import get_knowledge
        library = get_knowledge()
        lesson = self.lesson(session)
        out = []
        for name in lesson.concepts or [lesson.title]:  # the subject first ("checkmate") ...
            found = library.match_concepts(name)
            if found and found[0] not in out:
                out.append(found[0])
        for step in self.steps(session):  # ... then the examples' own ideas (back-rank mate ...)
            ref = getattr(step, "example", None)
            ex = library.get(ref) if ref else None
            if ex is not None and ex.concept not in out:
                out.append(ex.concept)
        return out

    @staticmethod
    def _state_key(session: Session, step) -> str:
        return step.example or f"step{session.step_index}"

    def _example_state(self, session: Session, step) -> dict:
        return session.example_state.setdefault(self._state_key(session, step), {})

    def _exercise_indexes(self, session: Session, key: str) -> list[int]:
        return [i for i, s in enumerate(self.steps(session))
                if isinstance(s, ExerciseStep) and (s.example or f"step{i}") == key]

    def _first_exercise_of_example(self, session: Session) -> bool:
        step = self.current_step(session)
        idx = self._exercise_indexes(session, self._state_key(session, step))
        return bool(idx) and idx[0] == session.step_index

    def _exercise_resolved(self, session: Session, step) -> dict | None:
        """An exercise was solved or revealed. When it was the example's last one, record the
        outcome in the learner model and let learner.adapt reshape the rest of the lesson."""
        from .knowledge.difficulty import LABEL_RATING, puzzle_rating
        from .knowledge.library import get_knowledge
        from .learner.adapt import Outcome

        key = self._state_key(session, step)
        idx = self._exercise_indexes(session, key)
        if not idx or session.step_index != idx[-1]:
            return None  # more moves of this example to come
        state = session.example_state.get(key, {})
        library = get_knowledge()
        example = library.get(step.example) if step.example else None
        if example is not None:
            concept, rating = example.concept, puzzle_rating(example)
        else:
            concepts = self._lesson_concepts(session)
            if not concepts:
                return None
            level = self.lesson(session).difficulty
            concept, rating = concepts[0], LABEL_RATING[{"beginner": 1, "intermediate": 3}.get(level, 4)]
        revealed = bool(state.get("revealed"))
        wrong, hints = state.get("wrong", 0), state.get("hints", 0)
        solved = not revealed
        first_try = wrong == 0
        recorded = self._learner(lambda p: p.record_attempt(concept, rating, solved=solved, first_try=first_try,
                                                            hints=hints, revealed=revealed,
                                                            learner_moves=len(idx)))
        score = recorded["score"] if recorded else 0.0
        if example is not None:
            started = state.get("started")
            seconds = round(time.monotonic() - started, 1) if started is not None else None
            try:
                self._usage().record_resolved(example.id, solved, first_try, hints=hints, seconds=seconds,
                                              revealed=revealed, concept=concept)
            except OSError:
                pass
        outcome = Outcome(key, concept, rating, score, learner_moves=len(idx), wrong=wrong, hints=hints,
                          revealed=revealed, key_found=bool(state.get("key_found")))
        session.outcomes.append(outcome)
        if example is None:
            return None
        return self._adapt(session, library)

    def _example_blocks(self, session: Session) -> list[tuple[str, int, int]]:
        """(example id, first step, end step) of each example block in the step list."""
        blocks: list[list] = []
        for i, s in enumerate(self.steps(session)):
            ref = getattr(s, "example", None)
            if not ref:
                continue
            if blocks and blocks[-1][0] == ref and blocks[-1][2] == i:
                blocks[-1][2] = i + 1
            else:
                blocks.append([ref, i, i + 1])
        return [tuple(b) for b in blocks]

    def _adapt(self, session: Session, library) -> dict | None:
        from .knowledge.difficulty import puzzle_rating
        from .learner import get_profile
        from .learner.adapt import decide

        blocks = self._example_blocks(session)
        here = next((b for b in blocks if b[1] <= session.step_index < b[2]), None)
        if here is None:
            return None
        upcoming = []
        for eid, start, end in blocks:
            if start >= here[2]:
                ex = library.get(eid)
                if ex is None:
                    continue
                exercises = any(isinstance(s, ExerciseStep) for s in self.steps(session)[start:end])
                upcoming.append((eid, puzzle_rating(ex), "practice" if exercises else "demonstration"))
        profile = get_profile()
        used = {b[0] for b in blocks}
        known = {c for c, st in profile.concepts.items() if st.attempts or st.lessons_completed}
        usage = self._usage()
        try:
            seen, last_used = usage.seen_counts(), usage.last_used()
        except OSError:
            seen, last_used = {}, {}
        decision = decide(session.outcomes, upcoming, used, session.changes, library, level=profile.level,
                          known=known, seen=seen, last_used=last_used)
        if decision is None:
            return None
        new_steps = self._steps_for(library, decision)
        if not new_steps:
            return None
        steps = list(self.steps(session))
        if decision.replace:
            target = next(b for b in blocks if b[0] == decision.replace and b[1] >= here[2])
            steps[target[1]:target[2]] = new_steps
        else:
            steps[here[2]:here[2]] = new_steps
        session.steps = steps
        session.changes += 1
        session.notes[id(new_steps[0])] = decision.note
        return {"kind": decision.kind, "note": decision.note, "example": decision.example_id,
                "total_steps": len(steps)}

    def _steps_for(self, library, decision) -> list:
        """Lesson steps for an adaptation's example, validated like any lesson."""
        import re
        from .lessons.schema import LessonError, parse_lesson
        from .planner.knowledge_lessons import example_steps

        example = library.get(decision.example_id)
        if example is None:
            return []
        names = [library.concepts[example.concept].name] if example.concept in library.concepts else []
        raw = example_steps(example, decision.role, 1, 1, names)
        if raw and raw[0].get("text"):
            raw[0]["text"] = re.sub(r"^Example 1 of 1: ", "Extra example: ", raw[0]["text"])
        try:
            return parse_lesson({"id": "adapt", "title": "adapt", "description": "", "steps": raw},
                                course_id="_generated").steps
        except (LessonError, ValueError):
            return []

    # --- chat ---

    def _chat_context(self, session: Session):
        from .teacher import LessonContext

        lesson = self.lesson(session)
        example = self._example_for(session)
        solving = example is not None and self._solution_pending(session, example.id)
        return LessonContext(
            course_title=self.library.course(lesson.course_id).title,
            lesson_title=lesson.title,
            concepts=lesson.concepts,
            facts=self._example_facts(session, example, session.board, reveal=not solving),
            example_id=example.id if example else None,
        )

    def chat(self, session: Session, message: str) -> dict:
        from .teacher import chat_or_fallback, get_teacher

        if not message or not message.strip():
            raise ChessError("Empty message")
        message = message.strip()[:MAX_CHAT_CHARS]  # a pasted essay must not flood the model
        context = self._chat_context(session)
        history = list(session.transcript)  # prompt gets the new message exactly once
        reply, teacher_used = chat_or_fallback(get_teacher(), message, context, history)
        session.transcript += [{"role": "user", "content": message},
                               {"role": "assistant", "content": reply}]
        return {"reply": reply, "teacher": teacher_used}

    # --- streaming (Qwen text appears as it's generated) ---

    def explain_stream(self, session: Session):
        """Event generator explaining the latest graded move. Validates before streaming."""
        from .teacher import FallbackTeacher, stream_events
        from .teacher.consistency import verdict_conflicts
        from .teacher.prompts import build_move_feedback_messages

        feedback, context = session.last_feedback, session.last_context
        if feedback is None:
            raise ExerciseConflict("No move to explain yet")
        # A reply contradicting the verdict or the lesson result ("Blunder" + "the
        # right move!") is replaced by the deterministic explanation of the same facts.
        return stream_events(
            lambda: build_move_feedback_messages(feedback, context),
            lambda: FallbackTeacher().explain_move(feedback, context),
            validate=lambda text: verdict_conflicts(text, feedback, context.move_accepted),
        )

    def explain_example_stream(self, session: Session):
        """Stream an explanation of the current library example from its verified facts.

        Offline (or if Qwen's reply contradicts the verified example) the learner gets
        the library's own verified explanation instead.
        """
        from .knowledge.facts import check_explanation
        from .teacher import stream_events
        from .teacher.prompts import build_example_messages

        example = self._example_for(session)
        if example is None:
            raise ExerciseConflict("This step has no library example to explain")
        if self._solution_pending(session, example.id):
            raise ExerciseConflict("Solve the exercise first — the explanation would give it away")
        context = self._chat_context(session)
        level = self.lesson(session).difficulty
        return stream_events(
            lambda: build_example_messages(context, level=level),
            lambda: example.explanation or example.description,
            validate=lambda text: check_explanation(text, example),
        )

    def chat_stream(self, session: Session, message: str):
        from .teacher import FallbackTeacher, stream_events
        from .teacher.prompts import build_chat_messages

        if not message or not message.strip():
            raise ChessError("Empty message")
        message = message.strip()[:MAX_CHAT_CHARS]  # a pasted essay must not flood the model
        context = self._chat_context(session)
        history = list(session.transcript)
        events = stream_events(
            lambda: build_chat_messages(message, context, history),
            lambda: FallbackTeacher().chat(message, context, history),
        )

        def recording():
            for event in events:
                if event["type"] == "done":
                    session.transcript += [{"role": "user", "content": message},
                                           {"role": "assistant", "content": event["text"]}]
                yield event
        return recording()


MAX_CHAT_CHARS = 2000
_manager: SessionManager | None = None


def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


def set_manager(manager: SessionManager | None) -> None:
    global _manager
    _manager = manager
