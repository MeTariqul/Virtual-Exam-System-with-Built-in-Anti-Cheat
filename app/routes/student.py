"""Student pages: dashboard, exam page, results, history, uploads, notes."""

import csv
import io

from flask import Blueprint, Response, abort, flash, g, redirect, \
    render_template, request, url_for

from .. import services
from ..extensions import db
from ..grading import attempt_percent, display_correct, has_passed, \
    is_text_pending, violation_label
from ..models import Attempt, Exam, utcnow
from ..security import login_required, role_required

bp = Blueprint("student", __name__, url_prefix="/student")


@bp.route("/")
@login_required
@role_required("student")
def dashboard():
    exams = Exam.query.filter_by(is_published=True).order_by(Exam.created_at.desc()).all()
    # Latest attempt wins per exam (retakes create several rows).
    attempts: dict[int, Attempt] = {}
    for a in Attempt.query.filter_by(student_id=g.user.id).order_by(Attempt.id.desc()):
        attempts.setdefault(a.exam_id, a)
    all_attempts = Attempt.query.filter_by(student_id=g.user.id).all()
    now = utcnow()

    submitted = [a for a in all_attempts if a.status == "submitted" and a.total]
    percents = [p for p in (attempt_percent(a) for a in submitted) if p is not None]
    stats = {
        "open": sum(1 for e in exams if e.is_open(now)),
        "finished": len(submitted),
        "average": round(sum(percents) / len(percents), 1) if percents else None,
        "passed": sum(1 for a in submitted if has_passed(a)),
    }

    rows = []
    for exam in exams:
        attempt = attempts.get(exam.id)
        in_progress = attempt is not None and attempt.status == "in_progress"
        finished = sum(1 for a in all_attempts
                       if a.exam_id == exam.id and a.status == "submitted")
        # Show open exams, exams with a running attempt, and past results.
        if not exam.is_open(now) and attempt is None and not finished:
            continue
        rows.append({
            "exam": exam,
            "attempt": attempt,
            "in_progress": in_progress,
            "finished": finished,
            "left": max(0, exam.attempts_allowed - finished),
            "can_start": attempt is None or (attempt.status == "submitted"
                                             and finished < exam.attempts_allowed),
            "percent": attempt_percent(attempt) if attempt else None,
            "passed": has_passed(attempt) if attempt else None,
            "upload_open": (attempt is not None and attempt.status == "submitted"
                            and services.upload_window_open(attempt)),
        })
    return render_template("student/dashboard.html", rows=rows, stats=stats)


@bp.route("/exams/<int:exam_id>/start", methods=["POST"])
@login_required
@role_required("student")
def start_exam(exam_id):
    exam = db.session.get(Exam, exam_id) or abort(404)
    try:
        attempt, _created = services.start_attempt(
            g.user.id, exam,
            ip_address=request.remote_addr,
            user_agent=(request.headers.get("User-Agent") or "")[:400],
            access_code=request.form.get("access_code"),
        )
    except services.ExamError as e:
        flash(e.message, "error")
        return redirect(url_for("student.dashboard"))
    return redirect(url_for("student.take_exam", attempt_id=attempt.id))


@bp.route("/attempts/<int:attempt_id>")
@login_required
@role_required("student")
def take_exam(attempt_id):
    attempt = _own_attempt_or_404(attempt_id)
    if attempt.status == "submitted":
        return redirect(url_for("student.result", attempt_id=attempt.id))
    payload = services.attempt_payload(attempt)
    return render_template("student/exam.html", payload=payload, attempt=attempt)


@bp.route("/attempts/<int:attempt_id>/result")
@login_required
@role_required("student")
def result(attempt_id):
    attempt = _own_attempt_or_404(attempt_id)
    if attempt.status != "submitted":
        flash("The exam is still running.", "ok")
        return redirect(url_for("student.take_exam", attempt_id=attempt.id))

    exam = attempt.exam
    answers = {a.question_id: a for a in attempt.answers}
    gradings = {g.question_id: g for g in attempt.gradings}
    rows = []
    for i, qid in enumerate(attempt.question_order):
        q = next((x for x in exam.questions if x.id == qid), None)
        if q is None:
            continue
        ans = answers.get(qid)
        rows.append({
            "number": i + 1,
            "question": q,
            "answer": ans,
            "correct": display_correct(ans, gradings.get(qid)) if ans else None,
            "pending": is_text_pending(ans),
            "grading": gradings.get(qid),
        })

    percent = attempt_percent(attempt)
    violations = [violation_label(v.type) for v in attempt.violations if not v.ignored]
    notes = [m for m in attempt.messages if m.kind in ("student", "recheck")]
    return render_template(
        "student/result.html",
        attempt=attempt, rows=rows, percent=percent, violations=violations,
        notes=notes,
        show_score=exam.show_result,
        passed=has_passed(attempt),
        upload_open=services.upload_window_open(attempt),
        can_recheck=exam.show_result,
    )


@bp.route("/attempts/<int:attempt_id>/upload", methods=["POST"])
@login_required
@role_required("student")
def upload_answers(attempt_id):
    """Handwritten pages: one or many photos (merged) or a PDF."""
    attempt = _own_attempt_or_404(attempt_id)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return _back_to_result(attempt)

    try:
        images = [f for f in files if (f.mimetype or "").startswith("image/")]
        pdfs = [f for f in files if f.mimetype == "application/pdf"]
        others = [f for f in files if f not in images and f not in pdfs]
        if others:
            raise services.ExamError(
                "Only PDF or image files (JPG/PNG) are accepted.", status=400)

        saved = 0
        for pdf in pdfs:
            services.save_upload(attempt, pdf.filename, pdf.read(), is_pdf=True)
            saved += 1
        if images:
            services.merge_image_uploads(
                attempt, [(f.filename, f.read()) for f in images])
            saved += 1
        flash(f"Uploaded {saved} file(s) — OCR is running in the background.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return _back_to_result(attempt)


@bp.route("/attempts/<int:attempt_id>/note", methods=["POST"])
@login_required
@role_required("student")
def add_note(attempt_id):
    """Explain a violation (proposal: student explanation)."""
    attempt = _own_attempt_or_404(attempt_id)
    try:
        services.add_student_note(attempt, request.form.get("body") or "",
                                  kind="student")
        flash("Note sent to your teacher.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return _back_to_result(attempt)


@bp.route("/attempts/<int:attempt_id>/recheck", methods=["POST"])
@login_required
@role_required("student")
def request_recheck(attempt_id):
    """Ask the teacher to re-check the marks (proposal: re-check request)."""
    attempt = _own_attempt_or_404(attempt_id)
    try:
        services.add_student_note(attempt, request.form.get("body") or "",
                                  kind="recheck")
        flash("Re-check request sent to your teacher.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return _back_to_result(attempt)


def _back_to_result(attempt: Attempt):
    return redirect(url_for("student.result", attempt_id=attempt.id))


@bp.route("/history")
@login_required
@role_required("student")
def history():
    attempts = (
        Attempt.query.filter_by(student_id=g.user.id)
        .order_by(Attempt.started_at.desc())
        .all()
    )
    return render_template(
        "student/history.html", attempts=attempts,
        percents={a.id: attempt_percent(a) for a in attempts},
    )


@bp.route("/history.csv")
@login_required
@role_required("student")
def history_csv():
    attempts = (
        Attempt.query.filter_by(student_id=g.user.id)
        .order_by(Attempt.started_at.asc())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Exam", "Attempt", "Started", "Submitted", "Score", "Total",
                     "Percent", "Violations", "Risk", "Status"])
    for a in attempts:
        writer.writerow([
            a.exam.title,
            a.attempt_no,
            a.started_at.isoformat(sep=" ", timespec="seconds"),
            a.submitted_at.isoformat(sep=" ", timespec="seconds") if a.submitted_at else "",
            a.score if a.score is not None else "",
            a.total if a.total is not None else "",
            attempt_percent(a) if a.score is not None else "",
            a.warning_count,
            f"{a.risk_score} ({a.risk_level})" if a.risk_level else "",
            a.status,
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=my_results.csv"},
    )


def _own_attempt_or_404(attempt_id: int) -> Attempt:
    attempt = db.session.get(Attempt, attempt_id) or abort(404)
    if attempt.student_id != g.user.id:
        abort(403)
    return attempt
