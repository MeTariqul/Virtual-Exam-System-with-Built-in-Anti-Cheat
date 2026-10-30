"""Core exam-taking business logic.

Everything the API layer needs to start attempts, save answers, record
violations and submit — kept out of the routes so it can be tested directly.

Key rules implemented here (from plan.md):

* The timer is server-side: ``deadline`` is stored on the attempt and checked
  on every answer save and at submission.
* Violations NEVER auto-submit. They are stored as proof, scored, and pushed
  as alerts to the student and the teacher. Only the teacher force-submits,
  or time/student action ends the attempt.
* Every meaningful event joins a per-attempt SHA-256 hash chain so tampering
  with the record is detectable.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from flask import current_app

from . import grading
from .events import channel_for_attempt, channel_for_exam, publish
from .extensions import db
from .models import (
    GENESIS_HASH,
    Answer,
    Attempt,
    Event,
    Message,
    Question,
    Snapshot,
    Violation,
    utcnow,
)

#: If no heartbeat arrives for this long, record a monitor_gap violation.
MONITOR_GAP_SECONDS = 45
#: Brief focus losses shorter than this are recorded but never scored.
GRACE_WINDOW_MS = 2000
#: Answer saves are rejected this long after the deadline (clock slop).
DEADLINE_GRACE_SECONDS = 5


class ExamError(Exception):
    """Recoverable error with a message meant for the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------
# Hash chain
# --------------------------------------------------------------------------

def append_event(attempt: Attempt, kind: str, payload: dict, ref_type: str | None = None,
                 ref_id: int | None = None) -> Event:
    """Append one tamper-evident event to the attempt's hash chain."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    prev = attempt.last_event_hash or GENESIS_HASH
    event_hash = hashlib.sha256(f"{prev}|{kind}|{body}".encode("utf-8")).hexdigest()
    event = Event(
        attempt_id=attempt.id,
        kind=kind,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload,
        prev_hash=prev,
        event_hash=event_hash,
    )
    db.session.add(event)
    attempt.last_event_hash = event_hash
    return event


def verify_chain(attempt: Attempt) -> tuple[bool, int, int | None]:
    """Recompute the chain. Returns ``(ok, links_checked, broken_at_event_id)``."""
    events = Event.query.filter_by(attempt_id=attempt.id).order_by(Event.id).all()
    prev = GENESIS_HASH
    checked = 0
    for event in events:
        if event.prev_hash != prev:
            return False, checked, event.id
        body = json.dumps(event.payload, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(f"{prev}|{event.kind}|{body}".encode("utf-8")).hexdigest()
        if event.event_hash != expected:
            return False, checked, event.id
        prev = event.event_hash
        checked += 1
    # The stored head must match the last link.
    if attempt.last_event_hash != prev and events:
        return False, checked, events[-1].id
    return True, checked, None


# --------------------------------------------------------------------------
# Starting an attempt
# --------------------------------------------------------------------------

def start_attempt(student_id: int, exam, ip_address: str | None = None,
                  user_agent: str | None = None,
                  access_code: str | None = None) -> tuple[Attempt, bool]:
    """Create (or resume) the student's attempt for *exam*.

    Returns ``(attempt, created)`` — created is False when an in-progress
    attempt was resumed.  Enforces ``Exam.attempts_allowed`` (retakes create
    a fresh attempt numbered ``attempt_no + 1``) and the optional access code.
    The client's IP / device string is stored for the server-side device log.
    """
    existing = Attempt.query.filter_by(exam_id=exam.id, student_id=student_id) \
        .order_by(Attempt.id.desc()).first()
    if existing and existing.status == "in_progress":
        _record_monitor_gap(existing)
        db.session.commit()
        return existing, False

    if not exam.is_published:
        raise ExamError("This exam is not published yet.", status=403)
    if not exam.questions:
        raise ExamError("This exam has no questions yet.", status=400)
    now = utcnow()
    if exam.scheduled_start and now < exam.scheduled_start:
        raise ExamError("This exam has not opened yet.", status=403)
    if exam.scheduled_end and now > exam.scheduled_end:
        raise ExamError("This exam is closed.", status=403)

    if exam.access_code:
        if access_code is None:
            raise ExamError("This exam needs an access code.", status=403)
        if access_code.strip().lower() != exam.access_code.strip().lower():
            raise ExamError("Wrong access code.", status=403)

    finished = Attempt.query.filter_by(
        exam_id=exam.id, student_id=student_id, status="submitted"
    ).count()
    if finished >= exam.attempts_allowed:
        raise ExamError("You have already completed this exam.", status=409)

    # Pick the paper: N-of-M sampling, then shuffle.
    pool = list(exam.questions)
    if exam.pool_size and 0 < exam.pool_size < len(pool):
        pool = secrets.SystemRandom().sample(pool, exam.pool_size)
    if exam.shuffle_questions:
        secrets.SystemRandom().shuffle(pool)

    option_map: dict[str, list[int]] = {}
    for q in pool:
        options = q.options or []
        order = list(range(len(options)))
        if exam.shuffle_options:
            secrets.SystemRandom().shuffle(order)
        option_map[str(q.id)] = order

    attempt = Attempt(
        exam_id=exam.id,
        student_id=student_id,
        attempt_no=finished + 1,
        ip_address=(ip_address or "")[:64] or None,
        user_agent=(user_agent or "")[:400] or None,
        started_at=now,
        deadline=now + timedelta(minutes=exam.duration_minutes),
        question_order=[q.id for q in pool],
        option_map=option_map,
        last_heartbeat=now,
    )
    db.session.add(attempt)
    db.session.flush()  # need attempt.id for answers + events

    for q in pool:
        db.session.add(Answer(attempt_id=attempt.id, question_id=q.id))

    append_event(
        attempt,
        "system",
        {"action": "attempt_started", "exam_id": exam.id,
         "attempt_no": attempt.attempt_no,
         "ip": attempt.ip_address, "device": attempt.user_agent,
         "deadline": attempt.deadline.isoformat()},
    )
    publish(channel_for_exam(exam.id), {"kind": "attempt_started", "attempt_id": attempt.id})
    db.session.commit()
    return attempt, True


def attempt_payload(attempt: Attempt) -> dict:
    """Everything the exam page needs to render (no correct answers included)."""
    exam = attempt.exam
    questions_by_id = {q.id: q for q in exam.questions}
    answers_by_q = {a.question_id: a for a in attempt.answers}

    questions = []
    for position, qid in enumerate(attempt.question_order):
        q = questions_by_id.get(qid)
        if q is None:
            continue
        perm = attempt.option_map.get(str(qid)) or list(range(len(q.options or [])))
        answer = answers_by_q.get(qid)
        entry = {
            "position": position + 1,
            "id": q.id,
            "type": q.type,
            "text": q.text,
            "points": q.points,
            "options": [ (q.options or [])[i] for i in perm ],
            # original index of each displayed option, for grading:
            "display_to_original": perm,
            "flagged": bool(answer.flagged) if answer else False,
            "response": (answer.response or "") if answer else "",
            # multi answers are stored as ORIGINAL indexes — convert to display:
            "selected": None,
            "word_limit": 300 if q.type == "long_text" else 60,
        }
        if q.type == "multi":
            original = answer.selected_indices if answer and answer.selected_indices else []
            entry["selected"] = [perm.index(i) for i in original if i in perm]
        elif q.type in ("mcq", "true_false"):
            if answer and answer.selected_index is not None \
                    and answer.selected_index in perm:
                entry["selected"] = perm.index(answer.selected_index)
        questions.append(entry)

    # Legacy shape kept for the single-choice radio restore logic.
    answers: dict[str, int] = {}
    for entry in questions:
        if entry["type"] in ("mcq", "true_false") and entry["selected"] is not None:
            answers[str(entry["id"])] = entry["selected"]

    return {
        "attempt_id": attempt.id,
        "exam_id": attempt.exam_id,
        "title": exam.title,
        "description": exam.description,
        "questions": questions,
        "answers": answers,
        "deadline": attempt.deadline.isoformat(),
        "started_at": attempt.started_at.isoformat(),
        "server_time": utcnow().isoformat(),
        "violation_limit": exam.violation_limit,
        "warning_count": attempt.warning_count,
        "over_limit": attempt.over_limit,
        "require_fullscreen": exam.require_fullscreen,
        "webcam_enabled": exam.webcam_enabled,
        "one_at_a_time": exam.one_at_a_time,
        "pass_mark": exam.pass_mark,
        "consent": attempt.webcam_consent,
        "messages": [
            {"kind": m.kind, "body": m.body, "time": m.created_at.isoformat()}
            for m in attempt.messages[-20:]
        ],
    }


# --------------------------------------------------------------------------
# Heartbeat & monitor gap (anti-tamper)
# --------------------------------------------------------------------------

def heartbeat(attempt: Attempt) -> None:
    """Called by the exam page every ~15s while the tab is alive."""
    if attempt.status != "in_progress":
        return
    _record_monitor_gap(attempt)
    attempt.last_heartbeat = utcnow()
    db.session.commit()


def _record_monitor_gap(attempt: Attempt) -> None:
    """If the client was silent past the threshold, log a monitor_gap event.

    This fires on the *next* contact after a gap (heartbeat, answer save or
    violation), so disabling the anti-cheat script cannot go unnoticed.
    """
    if attempt.status != "in_progress" or attempt.last_heartbeat is None:
        return
    silence = (utcnow() - attempt.last_heartbeat).total_seconds()
    if silence <= MONITOR_GAP_SECONDS:
        return

    v = Violation(
        attempt_id=attempt.id,
        type="monitor_gap",
        detail={"seconds": int(silence)},
        weight=grading.active_weights().get("monitor_gap", 5),
        ignored=False,
    )
    db.session.add(v)
    db.session.flush()
    append_event(
        attempt,
        "violation",
        {"type": "monitor_gap", "seconds": int(silence), "violation_id": v.id},
        ref_type="violation",
        ref_id=v.id,
    )
    _apply_warning(attempt, v)
    attempt.last_heartbeat = utcnow()
    publish(
        channel_for_attempt(attempt.id),
        {"kind": "alert", "type": "monitor_gap", "warning_count": attempt.warning_count,
         "over_limit": attempt.over_limit, "message": "Monitoring was interrupted."},
    )
    publish(
        channel_for_exam(attempt.exam_id),
        {"kind": "violation", "attempt_id": attempt.id, "type": "monitor_gap",
         "warning_count": attempt.warning_count, "over_limit": attempt.over_limit},
    )


# --------------------------------------------------------------------------
# Answer saving (auto-save): single choice, multi choice and typed text
# --------------------------------------------------------------------------

def _ensure_answer_writable(attempt: Attempt, question_id: int) -> Answer:
    """Shared guards for every answer save. Returns the attempt's Answer row."""
    if attempt.status != "in_progress":
        raise ExamError("This attempt is already submitted.", status=409)

    _record_monitor_gap(attempt)

    grace = attempt.exam.grace_seconds or DEADLINE_GRACE_SECONDS
    if utcnow() > attempt.deadline + timedelta(seconds=grace):
        raise ExamError("Time is up — the server deadline has passed.", status=410)

    if question_id not in attempt.question_order:
        raise ExamError("That question is not part of this attempt.", status=400)

    answer = Answer.query.filter_by(attempt_id=attempt.id, question_id=question_id).first()
    if answer is None:
        answer = Answer(attempt_id=attempt.id, question_id=question_id)
        db.session.add(answer)
    return answer


def _touch_answer(answer: Answer, now) -> None:
    if answer.first_answered_at is None:
        answer.first_answered_at = now


def save_answer(attempt: Attempt, question_id: int, display_index: int | None) -> None:
    """Persist one single-choice selection (auto-save)."""
    answer = _ensure_answer_writable(attempt, question_id)

    # Map display position -> original option index.
    original_index: int | None = None
    if display_index is not None:
        perm = attempt.option_map.get(str(question_id)) or []
        if not 0 <= display_index < len(perm):
            raise ExamError("Invalid option.", status=400)
        original_index = perm[display_index]

    changed = answer.selected_index != original_index
    answer.selected_index = original_index
    if original_index is not None:
        _touch_answer(answer, utcnow())

    if changed:
        append_event(
            attempt,
            "answer",
            {"question_id": question_id, "selected": original_index},
            ref_type="answer",
            ref_id=question_id,
        )
    db.session.commit()


def save_multi_answer(attempt: Attempt, question_id: int,
                      display_indices: list[int] | None) -> None:
    """Persist a multi-answer selection (stored as ORIGINAL indexes)."""
    answer = _ensure_answer_writable(attempt, question_id)
    perm = attempt.option_map.get(str(question_id)) or []
    originals: list[int] = []
    for index in display_indices or []:
        if not isinstance(index, int) or not 0 <= index < len(perm):
            raise ExamError("Invalid option.", status=400)
        original = perm[index]
        if original not in originals:
            originals.append(original)
    originals.sort()

    changed = sorted(answer.selected_indices or []) != originals
    answer.selected_indices = originals or None
    answer.selected_index = originals[0] if len(originals) == 1 else None
    if originals:
        _touch_answer(answer, utcnow())

    if changed:
        append_event(
            attempt,
            "answer",
            {"question_id": question_id, "selected_indices": originals},
            ref_type="answer",
            ref_id=question_id,
        )
    db.session.commit()


#: Sentinel so "no text key" differs from "clear the text".
_UNSET = object()


def save_text_answer(attempt: Attempt, question_id: int, response=_UNSET,
                     max_chars: int = 4000) -> None:
    """Persist a typed answer (short/long text question)."""
    answer = _ensure_answer_writable(attempt, question_id)
    if response is _UNSET:
        return
    if not isinstance(response, str):
        raise ExamError("Text answer expected.", status=400)
    response = response[:max_chars]
    changed = (answer.response or "") != response
    answer.response = response
    if response.strip():
        _touch_answer(answer, utcnow())

    if changed:
        append_event(
            attempt,
            "answer",
            # The text itself is part of the tamper-evident record.
            {"question_id": question_id, "response": response},
            ref_type="answer",
            ref_id=question_id,
        )
    db.session.commit()


def set_flag(attempt: Attempt, question_id: int, flagged: bool) -> None:
    """Flag / unflag a question for review (proposal: flag for review)."""
    if attempt.status != "in_progress":
        raise ExamError("This attempt is already submitted.", status=409)
    if question_id not in attempt.question_order:
        raise ExamError("That question is not part of this attempt.", status=400)
    answer = Answer.query.filter_by(attempt_id=attempt.id, question_id=question_id).first()
    if answer is None:
        answer = Answer(attempt_id=attempt.id, question_id=question_id)
        db.session.add(answer)
    answer.flagged = bool(flagged)
    db.session.commit()


def save_answers_bulk(attempt: Attempt, answers: dict) -> None:
    """Save a ``{question_id: value}`` map (used by auto-save and submit).

    Value shapes: ``int``/``None`` = single choice (display index),
    ``list`` = multi choice, ``str`` = typed answer.
    """
    for qid, value in answers.items():
        try:
            qid_int = int(qid)
        except (TypeError, ValueError):
            continue
        try:
            if isinstance(value, list):
                indices = [int(v) for v in value]
                save_multi_answer(attempt, qid_int, indices)
            elif isinstance(value, str):
                save_text_answer(attempt, qid_int, value)
            else:
                display = None if value in (None, "", -1) else int(value)
                save_answer(attempt, qid_int, display)
        except ExamError:
            raise
        except (TypeError, ValueError):
            continue


# --------------------------------------------------------------------------
# Violations — store proof, alert both sides, NEVER auto-submit
# --------------------------------------------------------------------------

def record_violation(
    attempt: Attempt,
    vtype: str,
    question_id: int | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
    snapshot_id: int | None = None,
) -> dict:
    """Record one violation and return the alert payload for the student.

    The exam is NEVER submitted from here — proof is stored and both the
    student and the teacher are alerted (plan.md: no auto-submit).
    """
    if vtype not in grading.active_weights():
        raise ExamError("Unknown violation type.", status=400)
    if attempt.status != "in_progress":
        raise ExamError("This attempt is already submitted.", status=409)

    _record_monitor_gap(attempt)

    # Grace window: a focus loss shorter than 2s is proof, but not scored.
    ignored = False
    if vtype in ("tab_switch", "window_blur") and duration_ms is not None \
            and duration_ms < GRACE_WINDOW_MS:
        vtype = "short_focus_loss"
        ignored = True

    question_id = question_id if question_id in attempt.question_order else None
    weight = grading.violation_weight(vtype, None, ignored)
    was_over_limit = attempt.over_limit

    violation = Violation(
        attempt_id=attempt.id,
        type=vtype,
        question_id=question_id,
        detail=detail or {},
        duration_ms=duration_ms,
        weight=weight,
        ignored=ignored,
        snapshot_id=snapshot_id,
    )
    db.session.add(violation)
    db.session.flush()

    append_event(
        attempt,
        "violation",
        {"type": vtype, "violation_id": violation.id, "weight": weight,
         "question_id": question_id},
        ref_type="violation",
        ref_id=violation.id,
    )

    _apply_warning(attempt, violation)

    limit = attempt.exam.violation_limit
    just_crossed = attempt.over_limit and not was_over_limit
    student_message = _student_alert_message(vtype, attempt.warning_count, limit,
                                             attempt.over_limit)

    # Push alerts to BOTH sides (student stream + teacher's live dashboard).
    publish(channel_for_attempt(attempt.id), {
        "kind": "alert",
        "type": vtype,
        "label": grading.violation_label(vtype),
        "warning_count": attempt.warning_count,
        "limit": limit,
        "over_limit": attempt.over_limit,
        "ignored": ignored,
        "message": student_message,
        "time": violation.created_at.isoformat(),
    })
    publish(channel_for_exam(attempt.exam_id), {
        "kind": "violation",
        "attempt_id": attempt.id,
        "student": attempt.student.display_name,
        "type": vtype,
        "label": grading.violation_label(vtype),
        "warning_count": attempt.warning_count,
        "over_limit": attempt.over_limit,
        "ignored": ignored,
        "time": violation.created_at.isoformat(),
    })

    # The moment the limit is crossed, leave one clear note in the attempt
    # log for the teacher (still no auto-submit, ever).
    if just_crossed:
        note = Message(
            attempt_id=attempt.id,
            kind="system",
            body=(f"Violation limit reached ({attempt.warning_count}/{limit}). "
                  "The exam continues; the teacher has been alerted for review."),
        )
        db.session.add(note)

    db.session.commit()
    return {
        "violation_id": violation.id,
        "type": vtype,
        "ignored": ignored,
        "weight": weight,
        "warning_count": attempt.warning_count,
        "limit": limit,
        "over_limit": attempt.over_limit,
        "message": student_message,
    }


def _apply_warning(attempt: Attempt, violation: Violation) -> None:
    if violation.ignored or violation.weight <= 0:
        return
    attempt.warning_count += 1
    if attempt.warning_count >= attempt.exam.violation_limit:
        attempt.over_limit = True


def _student_alert_message(vtype: str, count: int, limit: int, over_limit: bool) -> str:
    label = grading.violation_label(vtype)
    if over_limit:
        return (f"{label} recorded. You have reached the warning limit "
                f"({count}/{limit}) — your teacher has been notified and is "
                "reviewing this attempt.")
    if vtype in ("short_focus_loss", "low_light", "face_check_unavailable"):
        return f"{label} — recorded for the log (no warning counted)."
    return f"{label} — warning {count} of {limit}. Your teacher has been notified."


# --------------------------------------------------------------------------
# Teacher actions
# --------------------------------------------------------------------------

def send_teacher_message(attempt: Attempt, body: str) -> Message:
    body = (body or "").strip()
    if not body:
        raise ExamError("Message cannot be empty.", status=400)
    message = Message(attempt_id=attempt.id, kind="teacher", body=body)
    db.session.add(message)
    db.session.flush()
    append_event(attempt, "system", {"action": "teacher_message", "message_id": message.id})
    publish(channel_for_attempt(attempt.id), {
        "kind": "message", "body": body, "time": message.created_at.isoformat(),
    })
    publish(channel_for_exam(attempt.exam_id), {
        "kind": "message", "attempt_id": attempt.id, "body": body,
    })
    db.session.commit()
    return message


def extend_attempt(attempt: Attempt, minutes: int) -> None:
    if attempt.status != "in_progress":
        raise ExamError("Attempt already submitted.", status=409)
    if not 1 <= minutes <= 120:
        raise ExamError("Extension must be 1–120 minutes.", status=400)
    attempt.deadline += timedelta(minutes=minutes)
    message = Message(
        attempt_id=attempt.id,
        kind="system",
        body=f"Your teacher extended the exam by {minutes} minute(s).",
    )
    db.session.add(message)
    db.session.flush()
    append_event(attempt, "system", {
        "action": "extended", "minutes": minutes,
        "deadline": attempt.deadline.isoformat(),
    })
    publish(channel_for_attempt(attempt.id), {
        "kind": "extended", "deadline": attempt.deadline.isoformat(),
        "body": message.body,
    })
    db.session.commit()


def force_submit(attempt: Attempt, by_user) -> None:
    """Teacher ends the attempt explicitly (the ONLY non-time submission path)."""
    if attempt.status != "in_progress":
        raise ExamError("Attempt already submitted.", status=409)
    submit_attempt(attempt, by="teacher", note=f"Ended by {by_user.display_name}")


def duplicate_exam(exam, by_user) -> "Exam":
    """Clone an exam — settings and questions only — as a new draft.

    Attempts, live monitoring state and resources are intentionally NOT
    copied: a duplicated exam is a fresh, unpublished paper the teacher can
    rename and re-schedule.  Each question is deep-copied so editing the
    clone never mutates the original (and vice versa).
    """
    from .models import Exam, Question

    copy = Exam(
        title=f"{exam.title} (copy)",
        description=exam.description,
        duration_minutes=exam.duration_minutes,
        violation_limit=exam.violation_limit,
        pool_size=exam.pool_size,
        shuffle_questions=exam.shuffle_questions,
        shuffle_options=exam.shuffle_options,
        require_fullscreen=exam.require_fullscreen,
        webcam_enabled=exam.webcam_enabled,
        pass_mark=exam.pass_mark,
        attempts_allowed=exam.attempts_allowed,
        access_code=exam.access_code,
        negative_marking=exam.negative_marking,
        grace_seconds=exam.grace_seconds,
        show_result=exam.show_result,
        one_at_a_time=exam.one_at_a_time,
        upload_window_minutes=exam.upload_window_minutes,
        # A fresh copy starts as an unscheduled draft.
        scheduled_start=None,
        scheduled_end=None,
        is_published=False,
        created_by=by_user.id,
    )
    db.session.add(copy)
    db.session.flush()  # need copy.id for the questions

    for q in exam.questions:
        db.session.add(Question(
            exam_id=copy.id,
            created_by=by_user.id,
            type=q.type,
            text=q.text,
            options=list(q.options) if q.options else None,
            correct_index=q.correct_index,
            correct_indices=list(q.correct_indices) if q.correct_indices else None,
            points=q.points,
            category_id=q.category_id,
            difficulty=q.difficulty,
            model_answer=q.model_answer,
            rubric=q.rubric,
        ))
    db.session.commit()
    return copy


# --------------------------------------------------------------------------
# Submission & grading
# --------------------------------------------------------------------------

def submit_attempt(attempt: Attempt, by: str = "student", note: str | None = None) -> None:
    """Grade the attempt. Idempotent — safe to call twice (refresh, double POST)."""
    if attempt.status == "submitted":
        return

    _record_monitor_gap(attempt)

    score, total = grading.grade_attempt(attempt)
    attempt.score = score
    attempt.total = total
    attempt.status = "submitted"
    attempt.submitted_by = by
    attempt.submitted_at = utcnow()

    # Risk score with contextual question timing, if analytics are available.
    avg_seconds = _question_avg_seconds(attempt.exam)
    risk, level = grading.compute_risk_score(attempt.violations, avg_seconds)
    attempt.risk_score = risk
    attempt.risk_level = level

    append_event(attempt, "submit", {
        "by": by, "score": score, "total": total, "risk": risk,
        "note": note,
    })
    publish(channel_for_attempt(attempt.id), {
        "kind": "submitted", "score": score, "total": total,
    })
    publish(channel_for_exam(attempt.exam_id), {
        "kind": "attempt_submitted", "attempt_id": attempt.id,
        "student": attempt.student.display_name,
    })
    db.session.commit()

    # Typed answers go to the AI marking pipeline (background, mock-friendly).
    has_text = any(q.is_text for q in attempt.exam.questions)
    if has_text:
        from .ai import enqueue
        enqueue("mark", {"attempt_id": attempt.id})


def expire_overdue_attempts(now=None) -> int:
    """Submit every in-progress attempt whose deadline (+grace) has passed.

    This is the server-side half of "the timer ends an attempt" (README.md).
    The exam page normally submits itself at 00:00, but if a student closes
    the browser or drops the connection the attempt must not linger as
    "in progress" forever — this grades it with ``submitted_by="timer"``.
    It is idempotent and never touches a violation limit or webcam event;
    only *time* triggers it, never a violation.
    """
    now = now or utcnow()
    candidates = Attempt.query.filter_by(status="in_progress").all()
    expired = 0
    for attempt in candidates:
        grace = attempt.exam.grace_seconds or DEADLINE_GRACE_SECONDS
        if now <= attempt.deadline + timedelta(seconds=grace):
            continue
        try:
            submit_attempt(attempt, by="timer", note="Time expired.")
            expired += 1
        except ExamError:
            db.session.rollback()
    return expired


def _question_avg_seconds(exam) -> dict[int, float]:
    """Average seconds students spend per question (for contextual scoring)."""
    stats = grading.question_difficulty(exam)
    return {
        qid: s["avg_seconds"]
        for qid, s in stats.items()
        if s.get("avg_seconds") is not None
    }


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

def save_snapshot(attempt: Attempt, image_bytes: bytes, reason: str,
                  face_count: int = -1) -> Snapshot:
    """Store a webcam snapshot on disk and chain it as evidence."""
    if attempt.status != "in_progress":
        raise ExamError("This attempt is already submitted.", status=409)
    if len(image_bytes) > 2 * 1024 * 1024:
        raise ExamError("Snapshot too large (max 2 MB).", status=413)

    folder = current_app.config["SNAPSHOT_DIR"] / str(attempt.id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{utcnow().strftime('%Y%m%d%H%M%S%f')}_{reason}_{secrets.token_hex(4)}.jpg"
    path = folder / filename
    path.write_bytes(image_bytes)

    snapshot = Snapshot(
        attempt_id=attempt.id,
        file_path=str(path),
        reason=reason,
        face_count=face_count,
    )
    db.session.add(snapshot)
    db.session.flush()
    append_event(attempt, "snapshot", {
        "snapshot_id": snapshot.id, "reason": reason, "face_count": face_count,
        "file": filename,
    }, ref_type="snapshot", ref_id=snapshot.id)
    db.session.commit()
    return snapshot


def delete_expired_snapshots() -> int:
    """Retention job: delete snapshot files older than SNAPSHOT_RETENTION_DAYS."""
    days = current_app.config.get("SNAPSHOT_RETENTION_DAYS", 7)
    cutoff = utcnow() - timedelta(days=days)
    expired = Snapshot.query.filter(Snapshot.created_at < cutoff,
                                    Snapshot.deleted_at.is_(None)).all()
    for snap in expired:
        try:
            from pathlib import Path
            Path(snap.file_path).unlink(missing_ok=True)
        except OSError:
            pass
        snap.deleted_at = utcnow()
    if expired:
        db.session.commit()
    return len(expired)


# --------------------------------------------------------------------------
# Handwritten uploads (proposal: upload window, SHA-256, duplicate check)
# --------------------------------------------------------------------------

def upload_window_open(attempt: Attempt) -> bool:
    """True while the exam accepts handwritten pages for this attempt."""
    window = attempt.exam.upload_window_minutes
    if window <= 0 or attempt.submitted_at is None:
        return False
    return utcnow() <= attempt.submitted_at + timedelta(minutes=window)


def save_upload(attempt: Attempt, filename: str, data: bytes,
                is_pdf: bool) -> "Submission":
    """Store one uploaded paper, hash it and flag duplicates / late uploads.

    The exam must already be submitted (uploads happen after the exam, inside
    ``Exam.upload_window_minutes``).  Violations here are stored as proof and
    alerted exactly like in-exam ones — nothing is ever re-submitted or
    reopened automatically.
    """
    from .models import Submission

    if attempt.status != "submitted":
        raise ExamError("Submit the exam before uploading answers.", status=409)
    if attempt.exam.upload_window_minutes <= 0:
        raise ExamError("This exam does not accept file uploads.", status=403)
    if not upload_window_open(attempt):
        # Rejected AND recorded — the proof stays in the log for the teacher.
        _record_evidence_violation(attempt, "late_upload", {
            "file": filename[:100],
            "window_minutes": attempt.exam.upload_window_minutes,
        })
        raise ExamError("The upload window for this exam has closed.", status=410)
    if not data:
        raise ExamError("Empty file.", status=400)
    if len(data) > 10 * 1024 * 1024:
        raise ExamError("File too large (max 10 MB).", status=413)

    folder = current_app.config["UPLOAD_DIR"] / str(attempt.id)
    folder.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.sha256(data).hexdigest()

    # Duplicate check: has another student uploaded byte-identical content?
    duplicate = (
        Submission.query.filter(Submission.file_hash == file_hash,
                                Submission.attempt_id != attempt.id)
        .first()
    )

    safe_name = "".join(ch for ch in (filename or "upload")
                        if ch.isalnum() or ch in "._- ")[:80] or "upload"
    stamp = utcnow().strftime("%Y%m%d%H%M%S%f")
    path = folder / f"{stamp}_{safe_name}"
    path.write_bytes(data)

    submission = Submission(
        attempt_id=attempt.id,
        file_path=str(path),
        original_name=safe_name,
        file_hash=file_hash,
        is_pdf=is_pdf,
        duplicate_of=duplicate.attempt_id if duplicate else None,
    )
    db.session.add(submission)
    db.session.flush()
    append_event(attempt, "submission", {
        "submission_id": submission.id, "file": safe_name,
        "sha256": file_hash, "is_pdf": is_pdf,
        "duplicate_of": submission.duplicate_of,
    }, ref_type="submission", ref_id=submission.id)

    if duplicate is not None:
        _record_evidence_violation(attempt, "duplicate_file", {
            "file": safe_name, "sha256": file_hash[:16],
            "same_as_attempt": duplicate.attempt_id,
        }, weight=grading.active_weights().get("duplicate_file", 2))

    db.session.commit()

    from .ai import enqueue
    enqueue("ocr", {"submission_id": submission.id})
    return submission


def merge_image_uploads(attempt: Attempt, files: list[tuple[str, bytes]]) -> "Submission":
    """Merge several photo uploads into one PDF, then store it."""
    from pathlib import Path

    from .ai.ocr import merge_images_to_pdf

    if not files:
        raise ExamError("No images provided.", status=400)

    staging = current_app.config["UPLOAD_DIR"] / "_staging" / str(attempt.id)
    staging.mkdir(parents=True, exist_ok=True)
    stored: list[Path] = []
    for index, (name, data) in enumerate(files, start=1):
        if len(data) > 10 * 1024 * 1024:
            raise ExamError("Image too large (max 10 MB each).", status=413)
        suffix = Path(name).suffix.lower() or ".jpg"
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            suffix = ".jpg"
        target = staging / f"img_{index:02d}{suffix}"
        target.write_bytes(data)
        stored.append(target)

    merged = staging / "merged.pdf"
    if merge_images_to_pdf(stored, merged):
        submission = save_upload(attempt, "handwritten_answers.pdf",
                                 merged.read_bytes(), is_pdf=True)
    else:  # Pillow missing: keep the first photo as-is
        first = stored[0]
        submission = save_upload(attempt, first.name, first.read_bytes(),
                                 is_pdf=False)
    return submission


def _record_evidence_violation(attempt: Attempt, vtype: str, detail: dict,
                               weight: int | None = None) -> None:
    """Record a proof-only violation AFTER submission.

    Same rules as in-exam violations: store proof, alert the teacher, never
    touch the submission state (the attempt stays exactly as it ended).
    """
    if weight is None:
        weight = grading.active_weights().get(vtype, 1)
    violation = Violation(
        attempt_id=attempt.id,
        type=vtype,
        detail=detail,
        weight=weight,
        ignored=False,
        created_at=utcnow(),
    )
    db.session.add(violation)
    db.session.flush()
    append_event(attempt, "violation", {
        "type": vtype, "violation_id": violation.id, "weight": weight,
    }, ref_type="violation", ref_id=violation.id)
    # Score is only meaningful while the attempt is running; refresh it here.
    attempt.warning_count += 1
    attempt.over_limit = attempt.warning_count >= attempt.exam.violation_limit
    risk, level = grading.compute_risk_score(attempt.violations)
    attempt.risk_score = risk
    attempt.risk_level = level
    publish(channel_for_exam(attempt.exam_id), {
        "kind": "violation", "attempt_id": attempt.id,
        "student": attempt.student.display_name, "type": vtype,
        "label": grading.violation_label(vtype),
        "warning_count": attempt.warning_count,
        "over_limit": attempt.over_limit, "ignored": False,
        "time": violation.created_at.isoformat(),
    })
    db.session.commit()


# --------------------------------------------------------------------------
# Student explanations & re-check requests (proposal: student notes)
# --------------------------------------------------------------------------

def add_student_note(attempt: Attempt, body: str, kind: str = "student") -> Message:
    """Student explains a violation or asks for a re-check of their marks."""
    body = (body or "").strip()
    if not body:
        raise ExamError("Note cannot be empty.", status=400)
    if kind not in ("student", "recheck"):
        raise ExamError("Unknown note kind.", status=400)
    message = Message(attempt_id=attempt.id, sender_id=attempt.student_id,
                      kind=kind, body=body[:2000])
    db.session.add(message)
    db.session.flush()
    append_event(attempt, "system", {
        "action": "student_note" if kind == "student" else "recheck_requested",
        "message_id": message.id,
    })
    publish(channel_for_exam(attempt.exam_id), {
        "kind": "note", "attempt_id": attempt.id,
        "student": attempt.student.display_name, "body": body[:300],
    })
    db.session.commit()
    return message


# --------------------------------------------------------------------------
# AI marking approval & re-grading (teacher stays in control)
# --------------------------------------------------------------------------

def approve_grading(grading_row, marks: float | None, action: str,
                    by_user) -> None:
    """Approve, edit or reject an AI-suggested mark, then recompute the score."""
    if action not in ("approve", "reject"):
        raise ExamError("Unknown action.", status=400)
    attempt = grading_row.attempt
    if marks is not None:
        marks = max(0.0, min(float(marks), float(grading_row.max_marks)))
    if action == "approve":
        grading_row.status = "approved"
        grading_row.final_marks = marks if marks is not None \
            else grading_row.suggested_marks
    else:
        grading_row.status = "rejected"
        grading_row.final_marks = 0.0 if marks is None else marks
    grading_row.approved_by = by_user.display_name
    db.session.flush()
    append_event(attempt, "grading", {
        "grading_id": grading_row.id, "question_id": grading_row.question_id,
        "action": action, "marks": grading_row.final_marks,
        "by": by_user.display_name,
    }, ref_type="grading", ref_id=grading_row.id)
    regrade_attempt(attempt)


def regrade_attempt(attempt: Attempt) -> None:
    """Recompute score/total from current answers + approved marks."""
    if attempt.status != "submitted":
        raise ExamError("Only submitted attempts can be re-graded.", status=409)
    score, total = grading.grade_attempt(attempt)
    changed = (attempt.score, attempt.total) != (score, total)
    attempt.score = score
    attempt.total = total
    if changed:
        append_event(attempt, "system", {
            "action": "regraded", "score": score, "total": total,
        })
    db.session.commit()


# --------------------------------------------------------------------------
# Audit log (admin tool: "audit log" — proposal Advanced)
# --------------------------------------------------------------------------

def audit(action: str, detail: str = "", user=None, ip_address: str | None = None) -> None:
    """Append one row to the user-action audit log (never raises)."""
    from .models import AuditLog
    try:
        entry = AuditLog(
            user_id=user.id if user else None,
            username=(user.username if user else None),
            action=action[:48],
            detail=(detail or "")[:400],
            ip_address=(ip_address or "")[:64] or None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:  # pragma: no cover - auditing must not break requests
        db.session.rollback()
