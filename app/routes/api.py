"""JSON API used by the exam page and the teacher's live dashboard.

Endpoints
---------
POST /api/attempts/<id>/answer       save one answer (auto-save)
POST /api/attempts/<id>/answers      save a batch of answers
POST /api/attempts/<id>/heartbeat    keep-alive from the exam page
POST /api/attempts/<id>/violations   record a violation (never submits!)
POST /api/attempts/<id>/snapshot     store a webcam snapshot
POST /api/attempts/<id>/consent      student allows/refuses webcam
POST /api/attempts/<id>/submit       student submits (idempotent)
GET  /api/attempts/<id>/stream       SSE alerts for the student
GET  /api/exams/<id>/stream          SSE live feed for the teacher
"""

from __future__ import annotations

import base64

from flask import Blueprint, Response, abort, g, jsonify, request, stream_with_context

from .. import services
from ..events import channel_for_attempt, channel_for_exam, subscribe, unsubscribe
from ..extensions import db
from ..models import Attempt, Exam
from ..security import login_required, role_required
from ..services import ExamError

bp = Blueprint("api", __name__, url_prefix="/api")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _own_attempt(attempt_id: int) -> Attempt:
    attempt = db.session.get(Attempt, attempt_id) or abort(404)
    if attempt.student_id != g.user.id:
        abort(403)
    return attempt


def _teacher_exam(exam_id: int) -> Exam:
    exam = db.session.get(Exam, exam_id) or abort(404)
    # A teacher only monitors their own exams; admin sees everything.
    if g.user.role != "admin" and exam.created_by != g.user.id:
        abort(403)
    return exam


def _json() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ExamError("JSON body expected.", status=400)
    return data


# --------------------------------------------------------------------------
# Student: exam interaction
# --------------------------------------------------------------------------

@bp.post("/attempts/<int:attempt_id>/answer")
@login_required
@role_required("student")
def save_answer(attempt_id):
    attempt = _own_attempt(attempt_id)
    data = _json()
    try:
        qid = int(data.get("question_id"))
        if "text" in data:
            services.save_text_answer(attempt, qid, data.get("text") or "")
        elif isinstance(data.get("display_indices"), list):
            services.save_multi_answer(
                attempt, qid, [int(v) for v in data["display_indices"]]
            )
        else:
            display = data.get("display_index")
            display = None if display in (None, "", -1) else int(display)
            services.save_answer(attempt, qid, display)
    except ExamError as e:
        return jsonify(ok=False, error=e.message, code=e.status), e.status
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Bad answer payload."), 400
    return jsonify(ok=True, saved=True)


@bp.post("/attempts/<int:attempt_id>/flag")
@login_required
@role_required("student")
def flag_question(attempt_id):
    """Flag/unflag a question for review (saved with the attempt)."""
    attempt = _own_attempt(attempt_id)
    data = _json()
    try:
        services.set_flag(attempt, int(data.get("question_id")),
                          bool(data.get("flagged")))
    except ExamError as e:
        return jsonify(ok=False, error=e.message), e.status
    except (TypeError, ValueError):
        return jsonify(ok=False, error="question_id expected."), 400
    return jsonify(ok=True)


@bp.post("/attempts/<int:attempt_id>/answers")
@login_required
@role_required("student")
def save_answers(attempt_id):
    attempt = _own_attempt(attempt_id)
    data = _json()
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return jsonify(ok=False, error="answers object expected."), 400
    try:
        services.save_answers_bulk(attempt, answers)
    except ExamError as e:
        return jsonify(ok=False, error=e.message, code=e.status), e.status
    return jsonify(ok=True, saved=len(answers))


@bp.post("/attempts/<int:attempt_id>/heartbeat")
@login_required
@role_required("student")
def heartbeat(attempt_id):
    attempt = _own_attempt(attempt_id)
    services.heartbeat(attempt)
    return jsonify(ok=True, server_time=attempt.deadline.isoformat(),
                   status=attempt.status)


@bp.post("/attempts/<int:attempt_id>/violations")
@login_required
@role_required("student")
def violations(attempt_id):
    attempt = _own_attempt(attempt_id)
    data = _json()
    vtype = data.get("type")
    if not isinstance(vtype, str):
        return jsonify(ok=False, error="type expected."), 400

    question_id = data.get("question_id")
    if question_id is not None:
        try:
            question_id = int(question_id)
        except (TypeError, ValueError):
            question_id = None

    duration_ms = data.get("duration_ms")
    if duration_ms is not None:
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            duration_ms = None

    detail = data.get("detail")
    snapshot_id = data.get("snapshot_id")
    try:
        result = services.record_violation(
            attempt, vtype,
            question_id=question_id,
            duration_ms=duration_ms,
            detail=detail if isinstance(detail, dict) else None,
            snapshot_id=int(snapshot_id) if snapshot_id else None,
        )
    except ExamError as e:
        return jsonify(ok=False, error=e.message), e.status
    # NOTE: we deliberately never submit the exam here (no auto-submit policy).
    return jsonify(ok=True, **result)


@bp.post("/attempts/<int:attempt_id>/snapshot")
@login_required
@role_required("student")
def snapshot(attempt_id):
    attempt = _own_attempt(attempt_id)
    data = _json()
    image = data.get("image")  # data URL: data:image/jpeg;base64,...
    if not isinstance(image, str) or "," not in image:
        return jsonify(ok=False, error="image data URL expected."), 400
    try:
        raw = base64.b64decode(image.split(",", 1)[1])
        face_count = int(data.get("face_count", -1))
        reason = str(data.get("reason", "periodic"))[:32]
        snap = services.save_snapshot(attempt, raw, reason, face_count)
    except (ValueError, services.ExamError) as e:
        message = e.message if isinstance(e, services.ExamError) else "Bad image."
        status = e.status if isinstance(e, services.ExamError) else 400
        return jsonify(ok=False, error=message), status

    # Face-check findings become violations immediately (proof stored, no submit).
    if data.get("face_violation"):
        try:
            result = services.record_violation(
                attempt, str(data["face_violation"]),
                detail={"face_count": face_count},
                snapshot_id=snap.id,
            )
            return jsonify(ok=True, snapshot_id=snap.id, **result)
        except ExamError as e:
            return jsonify(ok=False, error=e.message), e.status
    return jsonify(ok=True, snapshot_id=snap.id)


@bp.post("/attempts/<int:attempt_id>/consent")
@login_required
@role_required("student")
def consent(attempt_id):
    attempt = _own_attempt(attempt_id)
    data = _json()
    attempt.webcam_consent = bool(data.get("granted"))
    db.session.commit()
    return jsonify(ok=True, granted=attempt.webcam_consent)


@bp.post("/attempts/<int:attempt_id>/submit")
@login_required
@role_required("student")
def submit(attempt_id):
    attempt = _own_attempt(attempt_id)
    try:
        # Allow the page to flush its last answers first if included. If the
        # deadline just passed, the answers were already auto-saved — grading
        # must still happen, so a late flush never blocks submission.
        data = request.get_json(silent=True) or {}
        answers = data.get("answers")
        if isinstance(answers, dict):
            try:
                services.save_answers_bulk(attempt, answers)
            except ExamError as e:
                if e.status != 410:
                    raise
        services.submit_attempt(attempt, by="student")
    except ExamError as e:
        return jsonify(ok=False, error=e.message), e.status
    return jsonify(ok=True, score=attempt.score, total=attempt.total,
                   attempt_id=attempt.id)


# --------------------------------------------------------------------------
# Server-Sent Events
# --------------------------------------------------------------------------

def _sse(q, channel):
    """Stream events from *q* as text/event-stream until the client leaves."""

    def generate():
        try:
            # Re-send anything published between page load and subscribe.
            yield ": connected\n\n"
            while True:
                try:
                    event = q.get(timeout=15)
                except Exception:
                    # Heartbeat comment keeps proxies/browsers from timing out.
                    yield ": ping\n\n"
                    continue
                import json as _json
                yield f"data: {_json.dumps(event)}\n\n"
        finally:
            unsubscribe(channel, q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@bp.get("/attempts/<int:attempt_id>/stream")
@login_required
@role_required("student")
def attempt_stream(attempt_id):
    attempt = _own_attempt(attempt_id)
    channel = channel_for_attempt(attempt.id)
    q = subscribe(channel)
    return _sse(q, channel)


@bp.get("/exams/<int:exam_id>/stream")
@login_required
@role_required("teacher", "admin")
def exam_stream(exam_id):
    exam = _teacher_exam(exam_id)
    channel = channel_for_exam(exam.id)
    q = subscribe(channel)
    return _sse(q, channel)
