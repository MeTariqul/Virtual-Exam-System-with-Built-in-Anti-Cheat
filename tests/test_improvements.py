"""Tests for the improvement pass: discrimination index, timer auto-expiry,
exam duplication, question-bank CSV export, security headers and the
audit-log action code exposure."""

import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

os.environ.setdefault("EXAM_AI_MOCK", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app, grading, services  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Attempt, Exam, Question, User, utcnow  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    for name in ("snapshots", "uploads", "profiles"):
        (tmp_path / name).mkdir()
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "SECRET_KEY": "test-secret",
        "SNAPSHOT_DIR": tmp_path / "snapshots",
        "UPLOAD_DIR": tmp_path / "uploads",
        "PROFILE_DIR": tmp_path / "profiles",
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _user(username, role="student"):
    user = User(username=username, full_name=username, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _teacher():
    return _user("t_discrimination", role="teacher")


def _make_exam(teacher, n=2, **overrides):
    exam = Exam(
        title="Feature Test Exam",
        duration_minutes=10,
        violation_limit=3,
        shuffle_questions=False,
        shuffle_options=False,
        pass_mark=40,
        attempts_allowed=1,
        show_result=True,
        is_published=True,
        created_by=teacher.id,
    )
    for key, value in overrides.items():
        setattr(exam, key, value)
    db.session.add(exam)
    db.session.flush()
    for i in range(n):
        db.session.add(Question(
            exam_id=exam.id, type="mcq", text=f"Question {i + 1}?",
            options=["alpha", "beta", "gamma"], correct_index=0, points=1,
        ))
    db.session.commit()
    return exam


def _attempt(student, exam, correct=True):
    attempt, _ = services.start_attempt(student.id, exam)
    for q in exam.questions:
        # shuffle is off, so display index == original index.
        display = q.correct_index if correct else (q.correct_index + 1) % len(q.options)
        services.save_answer(attempt, q.id, display)
    services.submit_attempt(attempt, by="student")
    return attempt


# --------------------------------------------------------------------------
# Discrimination index
# --------------------------------------------------------------------------

def test_discrimination_index_separates_strong_from_weak(app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=2)
    for i in range(3):
        services.start_attempt(_user(f"strong{i}").id, exam)
    strong = Attempt.query.filter_by(exam_id=exam.id).all()
    for a in strong:
        for q in exam.questions:
            services.save_answer(a, q.id, q.correct_index)
        services.submit_attempt(a, by="student")

    for i in range(3):
        services.start_attempt(_user(f"weak{i}").id, exam)
    weak = [a for a in Attempt.query.filter_by(exam_id=exam.id).all()
            if a.id not in {s.id for s in strong}]
    for a in weak:
        for q in exam.questions:
            services.save_answer(a, q.id, (q.correct_index + 1) % len(q.options))
        services.submit_attempt(a, by="student")

    d = grading.discrimination_index(exam)
    assert set(d) == {q.id for q in exam.questions}
    # Top group (all correct) minus bottom group (all wrong) == 1.0.
    for q in exam.questions:
        assert d[q.id] == 1.0
        assert grading.discrimination_label(d[q.id]) == "good"


def test_discrimination_index_needs_enough_data(app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=1)
    d = grading.discrimination_index(exam)
    assert d == {exam.questions[0].id: None}
    assert grading.discrimination_label(None) == "—"


# --------------------------------------------------------------------------
# Timer auto-expiry
# --------------------------------------------------------------------------

def test_expire_overdue_attempts_submits_past_deadline(app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=1)
    student = _user("late_student")
    attempt, _ = services.start_attempt(student.id, exam)
    attempt.deadline = utcnow() - timedelta(minutes=5)
    db.session.commit()

    expired = services.expire_overdue_attempts()
    assert expired == 1
    db.session.refresh(attempt)
    assert attempt.status == "submitted"
    assert attempt.submitted_by == "timer"


def test_expire_overdue_attempts_leaves_future_attempts_alone(app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=1)
    student = _user("on_time_student")
    attempt, _ = services.start_attempt(student.id, exam)  # deadline in future

    expired = services.expire_overdue_attempts()
    assert expired == 0
    db.session.refresh(attempt)
    assert attempt.status == "in_progress"


# --------------------------------------------------------------------------
# Exam duplication
# --------------------------------------------------------------------------

def test_duplicate_exam_creates_draft_copy(app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=3)
    db.session.add(Question(
        exam_id=exam.id, type="short_text", text="Explain in one line.",
        model_answer="A short answer.", points=2,
    ))
    db.session.commit()

    copy = services.duplicate_exam(exam, teacher)
    assert copy.id != exam.id
    assert copy.title == f"{exam.title} (copy)"
    assert copy.is_published is False
    assert copy.created_by == teacher.id
    assert len(copy.questions) == 4
    assert {q.type for q in copy.questions} == {"mcq", "short_text"}
    # Deep copy: mutating the copy never touches the original.
    first_copy = copy.questions[0]
    first_copy.text = "edited copy text"
    db.session.commit()
    assert exam.questions[0].text != "edited copy text"


def test_duplicate_exam_route(client, app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=2)

    # Sign in as the teacher.
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    client.post("/login", data={
        "username": teacher.username, "password": "password123",
        "csrf_token": token,
    }, follow_redirects=True)

    # Login issues a fresh CSRF token — re-read it before POSTing.
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    r = client.post(f"/teacher/exams/{exam.id}/duplicate",
                    data={"csrf_token": token}, follow_redirects=True)
    assert r.status_code == 200
    assert b"(copy)" in r.data
    copies = Exam.query.filter(Exam.title == f"{exam.title} (copy)").all()
    assert len(copies) == 1
    assert copies[0].is_published is False


# --------------------------------------------------------------------------
# Question bank CSV export
# --------------------------------------------------------------------------

def test_question_bank_csv_round_trips(client, app):
    teacher = _teacher()
    exam = _make_exam(teacher, n=1)

    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    client.post("/login", data={
        "username": teacher.username, "password": "password123",
        "csrf_token": token,
    }, follow_redirects=True)

    r = client.get("/teacher/bank.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    body = r.data.decode("utf-8-sig")
    assert body.startswith("text,type,options,correct,model_answer,rubric,points,category,difficulty")
    assert "Question 1?" in body


# --------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------

def test_security_headers_present(client, app):
    client.get("/login")
    r = client.get("/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "same-origin"
    assert "camera=(self)" in r.headers.get("Permissions-Policy", "")
    assert r.headers.get("Cache-Control") == "no-store"


# --------------------------------------------------------------------------
# Audit log: raw action code is exposed alongside the human label
# --------------------------------------------------------------------------

def test_audit_log_exposes_raw_action_code(client, app):
    # Seed a couple of audit rows directly.
    from app.models import AuditLog
    admin = _user("a_admin", role="admin")
    db.session.add(AuditLog(user_id=admin.id, username="a_admin",
                            action="database_backup", detail="x",
                            ip_address="127.0.0.1"))
    db.session.commit()

    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    client.post("/login", data={
        "username": "a_admin", "password": "password123",
        "csrf_token": token,
    }, follow_redirects=True)

    r = client.get("/admin/audit")
    assert r.status_code == 200
    # The raw code survives in a data-action attribute (greppable / scriptable),
    # while the visible label is humanized.
    assert b'data-action="database_backup"' in r.data
    assert b"Database backup" in r.data
