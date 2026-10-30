"""Teacher pages: exam CRUD, questions, live monitor, results, cheat reports."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, flash, g, \
    redirect, render_template, request, url_for
from sqlalchemy import or_

from .. import grading, services
from ..extensions import db
from ..models import Attempt, Exam, Question, utcnow
from ..security import login_required, role_required

bp = Blueprint("teacher", __name__, url_prefix="/teacher")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _owned_exam_or_404(exam_id: int) -> Exam:
    exam = db.session.get(Exam, exam_id) or abort(404)
    if g.user.role != "admin" and exam.created_by != g.user.id:
        abort(403)
    return exam


def _owned_attempt_or_404(attempt_id: int) -> Attempt:
    attempt = db.session.get(Attempt, attempt_id) or abort(404)
    exam = attempt.exam
    if g.user.role != "admin" and exam.created_by != g.user.id:
        abort(403)
    return attempt


# --------------------------------------------------------------------------
# Exam CRUD
# --------------------------------------------------------------------------

@bp.route("/")
@login_required
@role_required("teacher")
def dashboard():
    if g.user.role == "admin":
        exams = Exam.query.order_by(Exam.created_at.desc()).all()
        exam_ids = [e.id for e in exams]
    else:
        exams = Exam.query.filter_by(created_by=g.user.id) \
            .order_by(Exam.created_at.desc()).all()
        exam_ids = [e.id for e in exams]

    # Aggregate numbers for the header strip (per teacher, admins see all).
    from ..models import GradingResult, Message

    def _scoped(query):
        return query.filter(Attempt.exam_id.in_(exam_ids)) if exam_ids else \
            query.filter(False)

    attempt_query = _scoped(Attempt.query)
    stats = {
        "exams": len(exams),
        "published": sum(1 for e in exams if e.is_published),
        "attempts": attempt_query.count(),
        "in_progress": attempt_query.filter_by(status="in_progress").count(),
        "pending_grading": _scoped(
            GradingResult.query.join(Attempt,
                                     GradingResult.attempt_id == Attempt.id)
        ).filter(GradingResult.status == "suggested").count(),
        "rechecks": _scoped(
            Message.query.join(Attempt, Message.attempt_id == Attempt.id)
        ).filter(Message.kind == "recheck").count(),
    }
    return render_template("teacher/dashboard.html", exams=exams, stats=stats)


@bp.route("/exams/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def exam_new():
    from ..models import Category

    def render_form(form, blocks, selected):
        """(Re)draw the create-exam page with everything already typed."""
        return render_template(
            "teacher/exam_form.html", exam=None, form=form,
            bank_questions=_my_bank_questions(g.user),
            blocks=blocks, selected_bank=selected,
            categories=Category.query.order_by(Category.name).all(),
        )

    if request.method == "GET":
        return render_form({}, [], [])

    # Hold on to every field: a validation problem must never cost the
    # teacher the rest of the form.
    form = _form_for_rerender(request.form)
    blocks = _inline_questions_from(request.form)
    selected = _bank_ids_from(request.form)

    errors = _validate_exam_form(form)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_form(form, blocks, selected)

    exam = _exam_from_form(form, user_id=g.user.id)
    db.session.add(exam)
    db.session.flush()

    # -- questions chosen from the bank (copied, originals untouched) -----
    copies, missing = [], 0
    for question_id in selected:
        source = db.session.get(Question, question_id)
        if source is None or not _owns_question(source, g.user):
            missing += 1
            continue
        copies.append(_copy_question(source, exam, created_by=g.user.id))

    # -- questions typed right into this form (q_<n>_* fields) -----------
    #    All of them are checked BEFORE anything is saved: one broken block
    #    aborts the whole creation and the form comes back fully intact.
    typed, problems = [], []
    for sub_form in blocks:
        question, error = _build_question(sub_form, exam=exam,
                                          created_by=g.user.id)
        if error:
            label = (sub_form.get("text") or "").strip()[:40] or "?"
            problems.append(f"Question “{label}…” was not added: {error}")
        else:
            typed.append(question)

    if problems:
        db.session.rollback()
        flash("Nothing was saved — fix the questions below, then submit again.",
              "error")
        for message in problems:
            flash(message, "error")
        return render_form(form, blocks, selected)

    for question in copies + typed:
        db.session.add(question)
    db.session.commit()

    if missing:
        flash(f"{missing} selected question(s) could not be reused — they were "
              "deleted or are not yours. The rest of the exam was created.",
              "error")

    added = len(copies) + len(typed)
    if added:
        flash(f"Exam created with {added} question(s).", "ok")
    else:
        flash("Exam created. Now add questions.", "ok")
    return redirect(url_for("teacher.exam_questions", exam_id=exam.id))


@bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def exam_edit(exam_id):
    exam = _owned_exam_or_404(exam_id)
    if request.method == "POST":
        errors = _validate_exam_form(request.form.to_dict())
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("teacher/exam_form.html", exam=exam,
                                   form=_form_for_rerender(request.form))
        updated = _exam_from_form(request.form.to_dict(), user_id=exam.created_by)
        for key, value in vars(updated).items():
            if not key.startswith("_") and key not in ("id", "created_by"):
                setattr(exam, key, value)
        db.session.commit()
        flash("Exam updated.", "ok")
        return redirect(url_for("teacher.dashboard"))
    form = {
        "title": exam.title,
        "description": exam.description,
        "duration_minutes": exam.duration_minutes,
        "violation_limit": exam.violation_limit,
        "pool_size": exam.pool_size,
        "shuffle_questions": exam.shuffle_questions,
        "shuffle_options": exam.shuffle_options,
        "require_fullscreen": exam.require_fullscreen,
        "webcam_enabled": exam.webcam_enabled,
        "pass_mark": exam.pass_mark,
        "attempts_allowed": exam.attempts_allowed,
        "access_code": exam.access_code or "",
        "negative_marking": exam.negative_marking,
        "grace_seconds": exam.grace_seconds,
        "show_result": exam.show_result,
        "one_at_a_time": exam.one_at_a_time,
        "upload_window_minutes": exam.upload_window_minutes,
        "scheduled_start": exam.scheduled_start.isoformat(timespec="minutes")[:16]
        if exam.scheduled_start else "",
        "scheduled_end": exam.scheduled_end.isoformat(timespec="minutes")[:16]
        if exam.scheduled_end else "",
        "is_published": exam.is_published,
    }
    return render_template("teacher/exam_form.html", exam=exam, form=form)


@bp.post("/exams/<int:exam_id>/publish")
@login_required
@role_required("teacher")
def exam_publish(exam_id):
    exam = _owned_exam_or_404(exam_id)
    if not exam.questions:
        flash("Add questions before publishing.", "error")
        return redirect(url_for("teacher.exam_questions", exam_id=exam.id))
    exam.is_published = not exam.is_published
    db.session.commit()
    flash("Exam published." if exam.is_published else "Exam unpublished.", "ok")
    return redirect(url_for("teacher.dashboard"))


@bp.post("/exams/<int:exam_id>/delete")
@login_required
@role_required("teacher")
def exam_delete(exam_id):
    exam = _owned_exam_or_404(exam_id)
    db.session.delete(exam)
    db.session.commit()
    flash("Exam deleted.", "ok")
    return redirect(url_for("teacher.dashboard"))


@bp.post("/exams/<int:exam_id>/duplicate")
@login_required
@role_required("teacher")
def exam_duplicate(exam_id):
    """Clone an exam (settings + questions) as a new draft."""
    exam = _owned_exam_or_404(exam_id)
    copy = services.duplicate_exam(exam, g.user)
    flash(f"Exam duplicated as “{copy.title}” (draft) — edit and publish when ready.", "ok")
    return redirect(url_for("teacher.exam_edit", exam_id=copy.id))


# --------------------------------------------------------------------------
# Questions (all types) & CSV import
# --------------------------------------------------------------------------

def _category_from(name: str):
    name = (name or "").strip()
    if not name:
        return None
    from ..models import Category
    existing = Category.query.filter_by(name=name).first()
    if existing:
        return existing
    category = Category(name=name[:80])
    db.session.add(category)
    db.session.flush()
    return category


def _build_question(form: dict, exam: Exam | None = None,
                    created_by: int | None = None):
    """Validate one question form submission → Question or error string.

    ``exam=None`` builds a standalone question-bank entry (``created_by``
    owns it); otherwise the question belongs to that exam.
    """
    from ..models import Question

    text = (form.get("text") or "").strip()
    qtype = (form.get("type") or "mcq").strip()
    if qtype not in Question.TYPES:
        return None, "Unknown question type."
    if not text:
        return None, "Question text is required."

    try:
        points = int(form.get("points") or 1)
        if not 1 <= points <= 100:
            raise ValueError
    except ValueError:
        return None, "Points must be 1–100."

    difficulty = (form.get("difficulty") or "").strip()
    if difficulty not in ("", "easy", "medium", "hard"):
        difficulty = None

    category = _category_from(form.get("category") or "")
    model_answer = (form.get("model_answer") or "").strip() or None
    rubric = (form.get("rubric") or "").strip() or None

    common = dict(
        exam_id=exam.id if exam is not None else None,
        created_by=created_by,
        type=qtype, text=text, points=points,
        category=category, difficulty=difficulty or None,
        model_answer=model_answer, rubric=rubric,
    )

    if qtype in ("short_text", "long_text"):
        if not model_answer:
            return None, "Typed-answer questions need a model answer " \
                         "(used for auto-checking and AI marking)."
        return Question(**common), None

    options = [(form.get(f"option{i}") or "").strip() for i in range(1, 5)]

    if qtype == "true_false":
        value = form.get("tf_correct")
        if value not in ("true", "false"):
            return None, "Choose the correct answer (True/False)."
        return Question(**common, options=["True", "False"],
                        correct_index=0 if value == "true" else 1), None

    filled = [(i, o) for i, o in enumerate(options) if o]
    if len(filled) < 2:
        return None, "Provide at least two options."

    if qtype == "mcq":
        try:
            correct_index = int(form.get("correct"))
        except (TypeError, ValueError):
            return None, "Choose the correct option."
        if not 0 <= correct_index < len(options) or not options[correct_index]:
            return None, "Choose the correct option."
        filled_pairs = [(i, o) for i, o in filled]
        question = Question(
            **common,
            options=[o for _, o in filled_pairs],
            correct_index=[i for i, _ in filled_pairs].index(correct_index),
        )
        return question, None

    # multi
    checked = []
    for index, option in filled:
        if form.get(f"correct{index}") == "on":
            checked.append(index)
    if len(checked) < 2:
        return None, "A multiple-answer question needs at least two correct options."
    filled_pairs = [(i, o) for i, o in filled]
    remap = {original: new for new, (original, _) in enumerate(filled_pairs)}
    return Question(**common,
                    options=[o for _, o in filled_pairs],
                    correct_indices=sorted(remap[i] for i in checked),
                    correct_index=remap[checked[0]]), None


def _my_bank_questions(user):
    """Every question this teacher may reuse: questions from their own exams
    plus standalone bank entries they created themselves (admins see all)."""
    if user.role == "admin":
        query = Question.query
    else:
        query = Question.query.filter(or_(
            Question.created_by == user.id,
            Question.exam.has(Exam.created_by == user.id),
        ))
    # Standalone entries (exam_id IS NULL) sort first — they are pure bank.
    return query.order_by(Question.exam_id.is_(None).desc(),
                          Question.exam_id, Question.id).all()


def _owns_question(question: Question, user) -> bool:
    """True when *user* may reuse, copy or delete this question."""
    if user.role == "admin":
        return True
    if question.created_by == user.id:
        return True
    return question.exam is not None and question.exam.created_by == user.id


def _copy_question(source: Question, exam: Exam, created_by: int) -> Question:
    """Deep-copy a question into *exam* (the original stays where it is)."""
    return Question(
        exam_id=exam.id, created_by=created_by,
        type=source.type, text=source.text,
        options=list(source.options) if source.options else None,
        correct_index=source.correct_index,
        correct_indices=list(source.correct_indices)
        if source.correct_indices else None,
        points=source.points, category_id=source.category_id,
        difficulty=source.difficulty, model_answer=source.model_answer,
        rubric=source.rubric,
    )


def _inline_questions_from(form) -> list[dict]:
    """Collect the create-exam form's ``q_<n>_*`` question blocks.

    Returns one plain dict per block (prefix stripped, submission order),
    skipping blocks the teacher left completely empty.
    """
    blocks: dict[int, dict] = {}
    for key in form:
        match = re.match(r"^q_(\d+)_(.+)$", key)
        if match:
            blocks.setdefault(int(match.group(1)), {})[match.group(2)] = \
                form.get(key)

    # Controls the browser always posts (type, points, tf_correct…) do NOT
    # count as "the teacher wrote something" — otherwise every untouched
    # block would look filled in.  Only real content does, plus a type that
    # was deliberately changed away from the default: that is a half-finished
    # question, and the server should say so instead of dropping it.
    content_keys = ("text", "option1", "option2", "option3", "option4",
                    "model_answer", "rubric")
    collected = []
    for index in sorted(blocks):
        block = blocks[index]
        if any((block.get(k) or "").strip() for k in content_keys) \
                or (block.get("type") or "mcq").strip() != "mcq":
            collected.append(block)
    return collected


def _bank_ids_from(form) -> list[int]:
    """Selected bank-question ids, in order, de-duplicated.

    Non-numeric entries are ignored rather than raising — the field is
    whatever the client chose to send.
    """
    seen: list[int] = []
    for raw in form.getlist("bank_ids"):
        try:
            question_id = int(raw)
        except (TypeError, ValueError):
            continue
        if question_id not in seen:
            seen.append(question_id)
    return seen


# Checkboxes POST nothing when they are unticked, so a re-render has to be
# told explicitly — see _form_for_rerender().
_CHECKBOX_FIELDS = (
    "shuffle_questions", "shuffle_options", "require_fullscreen",
    "webcam_enabled", "negative_marking", "one_at_a_time",
    "show_result", "is_published",
)


def _form_for_rerender(form) -> dict:
    """Submission values used to refill a form after a validation error.

    Unticked checkboxes are missing from ``request.form`` entirely; pinning
    them to ``''`` keeps the template from ticking them again behind the
    teacher's back (they would otherwise fall back to their default).
    """
    data = form.to_dict() if hasattr(form, "to_dict") else dict(form)
    for field in _CHECKBOX_FIELDS:
        if field not in data:
            data[field] = ""
    return data


@bp.route("/exams/<int:exam_id>/questions", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def exam_questions(exam_id):
    exam = _owned_exam_or_404(exam_id)
    from ..models import Category
    categories = Category.query.order_by(Category.name).all()

    if request.method == "POST":
        values = request.form.to_dict()
        question, error = _build_question(values, exam=exam,
                                          created_by=g.user.id)
        if error:
            # Draw the page again with the question still filled in — a
            # redirect would silently throw away everything typed.
            flash(error, "error")
            return render_template("teacher/questions.html", exam=exam,
                                   categories=categories, values=values)
        db.session.add(question)
        db.session.commit()
        flash("Question added.", "ok")
        return redirect(url_for("teacher.exam_questions", exam_id=exam.id))
    return render_template("teacher/questions.html", exam=exam,
                           categories=categories, values={})


@bp.post("/exams/<int:exam_id>/questions.csv")
@login_required
@role_required("teacher")
def questions_import_csv(exam_id):
    """Bulk import from CSV (proposal: question bank "CSV import").

    Columns: text, type, options (separated by |), correct, model_answer,
    rubric, points, category, difficulty.
    For ``mcq``/``multi`` ``correct`` holds option texts or 0-based indexes
    (multi: separated by |).
    """
    import csv as _csv

    exam = _owned_exam_or_404(exam_id)
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("teacher.exam_questions", exam_id=exam.id))

    try:
        text_stream = io.StringIO(file.read().decode("utf-8-sig"))
        reader = _csv.DictReader(text_stream)
        added, errors = 0, []
        for row_no, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in row.items() if k}
            form = {
                "text": row.get("text", ""),
                "type": row.get("type", "mcq"),
                "points": row.get("points", "1"),
                "category": row.get("category", ""),
                "difficulty": row.get("difficulty", ""),
                "model_answer": row.get("model_answer", ""),
                "rubric": row.get("rubric", ""),
            }
            options = [o.strip() for o in (row.get("options") or "").split("|") if o.strip()]
            for index in range(4):
                form[f"option{index + 1}"] = options[index] if index < len(options) else ""
            correct = row.get("correct", "")
            if form["type"] == "true_false":
                form["tf_correct"] = correct.lower()[:1] if correct else "true"
                if correct.lower() not in ("true", "false", "t", "f", "1", "0", "yes", "no"):
                    form["tf_correct"] = "true"
                else:
                    form["tf_correct"] = "true" if correct.lower() in (
                        "true", "t", "1", "yes") else "false"
            elif form["type"] == "multi":
                chosen = {c.strip() for c in correct.split("|") if c.strip()}
                for index in range(4):
                    option = options[index] if index < len(options) else ""
                    is_right = (str(index) in chosen) or (option and option in chosen)
                    form[f"correct{index}"] = "on" if is_right else ""
            else:
                form["correct"] = correct if correct in ("0", "1", "2", "3") else (
                    str(options.index(correct)) if correct in options else ""
                )
            question, error = _build_question(form, exam=exam,
                                              created_by=g.user.id)
            if error:
                errors.append(f"row {row_no}: {error}")
            else:
                db.session.add(question)
                added += 1
        db.session.commit()
        if added:
            flash(f"Imported {added} question(s).", "ok")
        for message in errors[:5]:
            flash(message, "error")
        if len(errors) > 5:
            flash(f"...and {len(errors) - 5} more rows skipped.", "error")
    except UnicodeDecodeError:
        flash("That file is not a UTF-8 CSV.", "error")
    return redirect(url_for("teacher.exam_questions", exam_id=exam.id))


@bp.post("/questions/<int:question_id>/delete")
@login_required
@role_required("teacher")
def question_delete(question_id):
    question = db.session.get(Question, question_id) or abort(404)
    if question.exam_id is None:
        # Standalone bank entry: only its creator (or an admin) may delete it.
        if not _owns_question(question, g.user):
            abort(403)
        db.session.delete(question)
        db.session.commit()
        flash("Question removed from the bank.", "ok")
        return redirect(url_for("teacher.question_bank"))
    exam = _owned_exam_or_404(question.exam_id)
    db.session.delete(question)
    db.session.commit()
    flash("Question removed.", "ok")
    return redirect(url_for("teacher.exam_questions", exam_id=exam.id))


# --------------------------------------------------------------------------
# Live monitor (SSE) & results
# --------------------------------------------------------------------------

@bp.route("/exams/<int:exam_id>/monitor")
@login_required
@role_required("teacher")
def exam_monitor(exam_id):
    exam = _owned_exam_or_404(exam_id)
    attempts = (
        Attempt.query.filter_by(exam_id=exam.id)
        .order_by(Attempt.started_at.desc())
        .all()
    )
    return render_template("teacher/monitor.html", exam=exam, attempts=attempts,
                           now=utcnow())


@bp.route("/exams/<int:exam_id>/results")
@login_required
@role_required("teacher")
def exam_results(exam_id):
    exam = _owned_exam_or_404(exam_id)
    attempts = (
        Attempt.query.filter_by(exam_id=exam.id)
        .order_by(Attempt.submitted_at.desc().nulls_last())
        .all()
    )
    stats = grading.question_difficulty(exam)
    collusion = grading.find_collusion_candidates(exam)
    summary = grading.exam_summary(exam)
    discrimination = grading.discrimination_index(exam)
    return render_template("teacher/results.html", exam=exam, attempts=attempts,
                           stats=stats, collusion=collusion, summary=summary,
                           discrimination=discrimination)


@bp.route("/exams/<int:exam_id>/results.pdf")
@login_required
@role_required("teacher")
def exam_results_pdf(exam_id):
    """PDF result sheet (proposal: "Export and notices ... PDF result sheet")."""
    from ..pdf_export import result_sheet_pdf

    exam = _owned_exam_or_404(exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).all()
    summary = grading.exam_summary(exam)
    payload = result_sheet_pdf(exam, attempts, summary)
    if payload is None:
        flash("PDF export could not be generated — is fpdf2 installed? "
              "(pip install fpdf2)", "error")
        return redirect(url_for("teacher.exam_results", exam_id=exam.id))
    return Response(
        payload,
        mimetype="application/pdf",
        headers={"Content-Disposition":
                 f"attachment; filename=results_exam_{exam.id}.pdf"},
    )


@bp.post("/attempts/<int:attempt_id>/regrade")
@login_required
@role_required("teacher")
def attempt_regrade(attempt_id):
    """Re-run MCQ grading + approved marks (proposal: "teacher can re-grade")."""
    attempt = _owned_attempt_or_404(attempt_id)
    try:
        services.regrade_attempt(attempt)
        flash(f"Re-graded: {attempt.score}/{attempt.total}.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return redirect(url_for("teacher.attempt_report", attempt_id=attempt.id))


@bp.route("/exams/<int:exam_id>/results.csv")
@login_required
@role_required("teacher")
def exam_results_csv(exam_id):
    exam = _owned_exam_or_404(exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Student", "Started", "Submitted", "Score", "Total",
                     "Percent", "Warnings", "Risk score", "Risk level", "Status"])
    for a in attempts:
        percent = round(100 * a.score / a.total, 1) if a.total else ""
        writer.writerow([
            a.student.display_name,
            a.started_at.isoformat(sep=" ", timespec="seconds"),
            a.submitted_at.isoformat(sep=" ", timespec="seconds") if a.submitted_at else "",
            a.score if a.score is not None else "",
            a.total if a.total is not None else "",
            percent,
            a.warning_count,
            a.risk_score if a.risk_score is not None else "",
            a.risk_level or "",
            a.status,
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


# --------------------------------------------------------------------------
# Cheat report
# --------------------------------------------------------------------------

@bp.route("/attempts/<int:attempt_id>/report")
@login_required
@role_required("teacher")
def attempt_report(attempt_id):
    attempt = _owned_attempt_or_404(attempt_id)
    exam = attempt.exam

    stats = grading.question_difficulty(exam)
    avg_seconds = {
        qid: s["avg_seconds"] for qid, s in stats.items()
        if s["avg_seconds"] is not None
    }

    # Recompute risk with current context so the report matches the weights.
    risk, level = grading.compute_risk_score(attempt.violations, avg_seconds)
    rows = grading.build_report_rows(attempt, avg_seconds)

    chain_ok, links, broken_at = services.verify_chain(attempt)

    question_number = {qid: i + 1 for i, qid in enumerate(attempt.question_order)}
    answers = {a.question_id: a for a in attempt.answers}
    answer_rows = []
    for qid in attempt.question_order:
        q = next((x for x in exam.questions if x.id == qid), None)
        if q is None:
            continue
        ans = answers.get(qid)
        answer_rows.append({
            "number": question_number[qid],
            "question": q,
            "answer": ans,
            "correct": grading.display_correct(ans) if ans else None,
        })

    snapshots = [s for s in attempt.snapshots if s.deleted_at is None]
    notes = [m for m in attempt.messages if m.kind in ("student", "recheck")]
    gradings = {g.question_id: g for g in attempt.gradings}
    return render_template(
        "teacher/report.html",
        attempt=attempt, exam=exam, rows=rows, risk=risk, level=level,
        chain_ok=chain_ok, links=links, broken_at=broken_at,
        answer_rows=answer_rows, snapshots=snapshots,
        notes=notes, gradings=gradings,
        percent=grading.attempt_percent(attempt),
        passed=grading.has_passed(attempt),
        submissions=[s for s in attempt.submissions],
        upload_open=services.upload_window_open(attempt),
    )


@bp.route("/snapshots/<int:snapshot_id>")
@login_required
@role_required("teacher", "admin")
def snapshot_file(snapshot_id):
    from pathlib import Path
    from flask import send_file
    from ..models import Snapshot
    snap = db.session.get(Snapshot, snapshot_id) or abort(404)
    exam = snap.attempt.exam
    if g.user.role != "admin" and exam.created_by != g.user.id:
        abort(403)
    if snap.deleted_at is not None:
        abort(404)
    path = Path(snap.file_path)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@bp.route("/submission-pages/<int:page_id>")
@login_required
@role_required("teacher", "admin")
def submission_page_file(page_id):
    """One rendered page of a handwritten submission."""
    from pathlib import Path
    from flask import send_file
    from ..models import SubmissionPage

    page = db.session.get(SubmissionPage, page_id) or abort(404)
    exam = page.submission.attempt.exam
    if g.user.role != "admin" and exam.created_by != g.user.id:
        abort(403)
    if not page.image_path:
        abort(404)
    path = Path(page.image_path)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/jpeg")


# --------------------------------------------------------------------------
# AI marking: teacher review & approval (only approved marks count)
# --------------------------------------------------------------------------

@bp.route("/attempts/<int:attempt_id>/grading")
@login_required
@role_required("teacher")
def attempt_grading(attempt_id):
    """Review AI-suggested marks for one attempt (approve / edit / reject)."""
    attempt = _owned_attempt_or_404(attempt_id)
    answers = {a.question_id: a for a in attempt.answers}
    gradings = {g.question_id: g for g in attempt.gradings}
    rows = []
    for qid in attempt.question_order:
        q = next((x for x in attempt.exam.questions if x.id == qid), None)
        if q is None or not q.is_text:
            continue
        rows.append({
            "question": q,
            "answer": answers.get(qid),
            "grading": gradings.get(qid),
        })
    return render_template("teacher/grading.html", attempt=attempt, rows=rows)


@bp.post("/gradings/<int:grading_id>/review")
@login_required
@role_required("teacher")
def grading_review(grading_id):
    from ..models import GradingResult

    row = db.session.get(GradingResult, grading_id) or abort(404)
    attempt = _owned_attempt_or_404(row.attempt_id)
    action = request.form.get("action")
    raw_marks = request.form.get("marks")
    marks = None
    if raw_marks not in (None, ""):
        try:
            marks = float(raw_marks)
        except ValueError:
            flash("Marks must be a number.", "error")
            return redirect(url_for("teacher.attempt_grading",
                                    attempt_id=attempt.id))
    try:
        services.approve_grading(row, marks, action, g.user)
        flash("Mark approved — the score was re-graded." if action == "approve"
              else "Mark rejected (0 counted) — the score was re-graded.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return redirect(url_for("teacher.attempt_grading", attempt_id=attempt.id))


# --------------------------------------------------------------------------
# RAG resources (model answers' material: notes / textbook / answer key)
# --------------------------------------------------------------------------

@bp.route("/exams/<int:exam_id>/resources", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def exam_resources(exam_id):
    from ..ai import engine_report, enqueue
    from ..models import Job, Resource

    exam = _owned_exam_or_404(exam_id)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        file = request.files.get("file")
        resource = None
        if file and file.filename:
            if file.mimetype != "application/pdf":
                flash("Resources must be PDF files (or type the notes below).",
                      "error")
                return redirect(url_for("teacher.exam_resources", exam_id=exam.id))
            from pathlib import Path
            import secrets
            folder = current_app.config["UPLOAD_DIR"] / "resources"
            folder.mkdir(parents=True, exist_ok=True)
            name = f"res_{exam.id}_{secrets.token_hex(4)}.pdf"
            path = folder / name
            path.write_bytes(file.read())
            resource = Resource(exam_id=exam.id,
                                title=title or file.filename[:180],
                                file_path=str(path))
        elif notes:
            resource = Resource(exam_id=exam.id, title=title or "Typed notes")
            resource.file_path = None
            # Typed notes are stored as a UTF-8 file so ingestion is uniform.
            from pathlib import Path
            folder = current_app.config["UPLOAD_DIR"] / "resources"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"notes_{exam.id}_{len(exam.resources)}.txt"
            path.write_text(notes, encoding="utf-8")
            resource.file_path = str(path)
        if resource is None:
            flash("Upload a PDF or type some notes first.", "error")
            return redirect(url_for("teacher.exam_resources", exam_id=exam.id))

        db.session.add(resource)
        db.session.commit()
        enqueue("ingest", {"resource_id": resource.id})
        flash("Resource added — chunking/embedding runs in the background.", "ok")
        return redirect(url_for("teacher.exam_resources", exam_id=exam.id))

    jobs = (Job.query.filter(Job.kind == "ingest")
            .order_by(Job.id.desc()).limit(5).all())
    return render_template("teacher/resources.html", exam=exam,
                           engines=engine_report(), jobs=jobs)


@bp.post("/resources/<int:resource_id>/delete")
@login_required
@role_required("teacher")
def resource_delete(resource_id):
    from ..models import Resource

    resource = db.session.get(Resource, resource_id) or abort(404)
    exam = _owned_exam_or_404(resource.exam_id)
    db.session.delete(resource)
    db.session.commit()
    flash("Resource deleted.", "ok")
    return redirect(url_for("teacher.exam_resources", exam_id=exam.id))


# --------------------------------------------------------------------------
# Question bank: every question this teacher owns, copy into an exam
# --------------------------------------------------------------------------

@bp.route("/bank")
@login_required
@role_required("teacher")
def question_bank():
    from ..models import Category

    if g.user.role == "admin":
        exams = Exam.query.order_by(Exam.created_at).all()
    else:
        exams = Exam.query.filter_by(created_by=g.user.id) \
            .order_by(Exam.created_at).all()
    questions = _my_bank_questions(g.user)
    category_id = request.args.get("category", type=int)
    if category_id:
        questions = [q for q in questions if q.category_id == category_id]
    categories = Category.query.order_by(Category.name).all()
    return render_template("teacher/bank.html", exams=exams,
                           questions=questions, categories=categories,
                           selected_category=category_id)


@bp.route("/bank.csv")
@login_required
@role_required("teacher")
def question_bank_csv():
    """Export the teacher's whole question bank (round-trips with CSV import)."""
    questions = _my_bank_questions(g.user)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text", "type", "options", "correct", "model_answer",
                     "rubric", "points", "category", "difficulty"])
    for q in questions:
        if q.type == "true_false":
            correct = "true" if q.correct_index == 0 else "false"
        elif q.type == "multi":
            correct = "|".join(str(i) for i in sorted(q.correct_indices or []))
        elif q.type == "mcq":
            correct = str(q.correct_index) if q.correct_index is not None else ""
        else:
            correct = ""
        writer.writerow([
            q.text,
            q.type,
            "|".join(q.options or []),
            correct,
            q.model_answer or "",
            q.rubric or "",
            q.points,
            q.category.name if q.category else "",
            q.difficulty or "",
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=question_bank.csv"},
    )


@bp.route("/bank/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def bank_new():
    """Create a standalone question-bank entry (not attached to an exam yet).

    The teacher later picks it up — together with questions from their exams —
    from the "Select from question bank" list on the create-exam form, or
    copies it into any of their exams from the bank page.
    """
    from ..models import Category

    categories = Category.query.order_by(Category.name).all()
    if request.method == "POST":
        values = request.form.to_dict()
        question, error = _build_question(values, created_by=g.user.id)
        if error:
            # Stay on the form with the question still filled in so the
            # teacher can fix the one thing that is wrong.
            flash(error, "error")
            return render_template("teacher/bank_form.html", form=values,
                                   categories=categories)
        db.session.add(question)
        db.session.commit()
        flash("Question added to your bank — select it when you "
              "create an exam, or copy it into an exam.", "ok")
        return redirect(url_for("teacher.question_bank"))
    return render_template("teacher/bank_form.html", form={}, categories=categories)


@bp.post("/bank/<int:question_id>/copy")
@login_required
@role_required("teacher")
def question_copy(question_id):
    """Copy a bank question into another exam (shared question bank)."""
    question = db.session.get(Question, question_id) or abort(404)
    # Source may be a standalone bank entry — then ownership means creator.
    if question.exam_id is None:
        if not _owns_question(question, g.user):
            abort(403)
    else:
        _owned_exam_or_404(question.exam_id)
    target_id = request.form.get("exam_id", type=int)
    target = db.session.get(Exam, target_id) or abort(404)
    if g.user.role != "admin" and target.created_by != g.user.id:
        abort(403)
    db.session.add(_copy_question(question, target, created_by=g.user.id))
    db.session.commit()
    flash(f"Copied to “{target.title}”.", "ok")
    return redirect(url_for("teacher.question_bank"))


# --------------------------------------------------------------------------
# Live actions: message, extend, force-submit (teacher is the ONLY submitter)
# --------------------------------------------------------------------------

@bp.post("/attempts/<int:attempt_id>/message")
@login_required
@role_required("teacher")
def send_message(attempt_id):
    attempt = _owned_attempt_or_404(attempt_id)
    body = (_input().get("body") or "").strip()
    try:
        services.send_teacher_message(attempt, body)
    except services.ExamError as e:
        flash(e.message, "error")
    return _back_to_monitor(attempt)


@bp.post("/attempts/<int:attempt_id>/extend")
@login_required
@role_required("teacher")
def extend(attempt_id):
    attempt = _owned_attempt_or_404(attempt_id)
    minutes = _input().get("minutes", "5")
    try:
        services.extend_attempt(attempt, int(minutes))
        flash(f"Extended by {minutes} minute(s).", "ok")
    except (ValueError, services.ExamError) as e:
        flash(getattr(e, "message", "Invalid extension."), "error")
    return _back_to_monitor(attempt)


@bp.post("/attempts/<int:attempt_id>/force-submit")
@login_required
@role_required("teacher")
def force_submit(attempt_id):
    attempt = _owned_attempt_or_404(attempt_id)
    try:
        services.force_submit(attempt, g.user)
        flash("Attempt ended and graded.", "ok")
    except services.ExamError as e:
        flash(e.message, "error")
    return _back_to_monitor(attempt)


def _back_to_monitor(attempt: Attempt):
    return redirect(url_for("teacher.exam_monitor", exam_id=attempt.exam_id))


# --------------------------------------------------------------------------
# Form helpers (kept small and explicit so validation reads clearly)
# --------------------------------------------------------------------------

def _input() -> dict:
    """Accept both form posts and JSON bodies (the live monitor sends JSON)."""
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


def _validate_exam_form(form: dict) -> list[str]:
    errors = []
    title = (form.get("title") or "").strip()
    if not title:
        errors.append("Title is required.")
    try:
        duration = int(form.get("duration_minutes") or 0)
        if not 1 <= duration <= 480:
            errors.append("Duration must be 1–480 minutes.")
    except ValueError:
        errors.append("Duration must be a number.")
    try:
        limit = int(form.get("violation_limit") or 3)
        if not 1 <= limit <= 50:
            errors.append("Violation limit must be 1–50.")
    except ValueError:
        errors.append("Violation limit must be a number.")
    for field, label, low, high in (
        ("pass_mark", "Pass mark", 0, 100),
        ("attempts_allowed", "Attempts allowed", 1, 5),
        ("grace_seconds", "Grace seconds", 0, 120),
        ("upload_window_minutes", "Upload window", 0, 1440),
    ):
        if (form.get(field) or "").strip() == "":
            continue
        try:
            value = int(form.get(field))
            if not low <= value <= high:
                errors.append(f"{label} must be {low}–{high}.")
        except ValueError:
            errors.append(f"{label} must be a number.")
    code = (form.get("access_code") or "").strip()
    if code and (len(code) > 12 or not code.replace("_", "").isalnum()):
        errors.append("Access code: max 12 letters/numbers/_ (or leave empty).")
    start = (form.get("scheduled_start") or "").strip()
    end = (form.get("scheduled_end") or "").strip()
    if start and end and end <= start:
        errors.append("End time must be after start time.")
    return errors


def _int_or(form: dict, field: str, default: int) -> int:
    raw = (form.get(field) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _exam_from_form(form: dict, user_id: int) -> Exam:
    start = (form.get("scheduled_start") or "").strip()
    end = (form.get("scheduled_end") or "").strip()
    code = (form.get("access_code") or "").strip()
    return Exam(
        title=(form.get("title") or "").strip(),
        description=(form.get("description") or "").strip(),
        duration_minutes=_int_or(form, "duration_minutes", 30),
        violation_limit=_int_or(form, "violation_limit", 3),
        pool_size=max(0, _int_or(form, "pool_size", 0)),
        shuffle_questions=form.get("shuffle_questions") == "on",
        shuffle_options=form.get("shuffle_options") == "on",
        require_fullscreen=form.get("require_fullscreen") == "on",
        webcam_enabled=form.get("webcam_enabled") == "on",
        pass_mark=_int_or(form, "pass_mark", 40),
        attempts_allowed=_int_or(form, "attempts_allowed", 1),
        access_code=code or None,
        negative_marking=form.get("negative_marking") == "on",
        grace_seconds=_int_or(form, "grace_seconds", 5),
        show_result=form.get("show_result") == "on",
        one_at_a_time=form.get("one_at_a_time") == "on",
        upload_window_minutes=_int_or(form, "upload_window_minutes", 0),
        scheduled_start=datetime.fromisoformat(start) if start else None,
        scheduled_end=datetime.fromisoformat(end) if end else None,
        is_published=form.get("is_published") == "on",
        created_by=user_id,
    )

