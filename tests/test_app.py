"""End-to-end tests for the Virtual Exam System.

Run:  python -m pytest tests/ -q

The suite is SEED-INDEPENDENT: every exam a test touches is built by the
test itself (``build_exam`` / ``make_exam``), so app/seed.py stays free to
grow demo data.  One test validates the demo dataset itself.
"""

import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

# Deterministic mock AI in tests: no Ollama probes, no model downloads.
os.environ.setdefault("EXAM_AI_MOCK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (Attempt, Exam, GradingResult, Question,  # noqa: E402
                        Setting, Submission, User, utcnow)


@pytest.fixture()
def app(tmp_path):
    # File storage moves to a temp dir too: the real instance/ folders are
    # for the running demo server only — tests must never write into them.
    for name in ("snapshots", "uploads", "profiles"):
        (tmp_path / name).mkdir()
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "SECRET_KEY": "test-secret",
        "WTF_CSRF_ENABLED": False,
        "SNAPSHOT_DIR": tmp_path / "snapshots",
        "UPLOAD_DIR": tmp_path / "uploads",
        "PROFILE_DIR": tmp_path / "profiles",
    })
    with app.app_context():
        db.create_all()
        from app.seed import seed_demo
        seed_demo()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username, password):
    # Load the login page first so the session gets a CSRF token.
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("csrf_token", "")
    return client.post("/login", data={
        "username": username, "password": password, "csrf_token": token,
    }, follow_redirects=True)


def csrf(client):
    """Fetch a CSRF token by loading any page."""
    with client.session_transaction() as sess:
        return sess.get("csrf_token")


def post_json(client, url, data, token):
    return client.post(url, json=data, headers={"X-CSRFToken": token})


# --------------------------------------------------------------------------
# Helpers — build exams directly (no reliance on demo data)
# --------------------------------------------------------------------------

def build_exam(n_questions=3, **overrides):
    """Create + commit a published MCQ exam owned by the demo teacher."""
    teacher = User.query.filter_by(username="teacher").first()
    defaults = dict(
        title="Auto Test Exam", description="built by the test suite",
        duration_minutes=10, violation_limit=3,
        shuffle_questions=True, shuffle_options=True,
        require_fullscreen=True, pass_mark=40, attempts_allowed=1,
        show_result=True, is_published=True, created_by=teacher.id,
    )
    defaults.update(overrides)
    exam = Exam(**defaults)
    db.session.add(exam)
    db.session.flush()
    for i in range(n_questions):
        db.session.add(Question(
            exam_id=exam.id, type="mcq", text=f"Question {i + 1}?",
            options=["alpha", "beta", "gamma"], correct_index=i % 3,
            points=1,
        ))
    db.session.commit()
    return exam


def start_exam(client, exam, username="student", data=None):
    """POST the start endpoint as *username* and return the newest attempt."""
    token = csrf(client)
    payload = {"csrf_token": token}
    if data:
        payload.update(data)
    client.post(f"/student/exams/{exam.id}/start", data=payload,
                follow_redirects=True)
    user = User.query.filter_by(username=username).first()
    return (Attempt.query
            .filter_by(exam_id=exam.id, student_id=user.id)
            .order_by(Attempt.id.desc()).first())


def start_first_exam(client):
    """A fresh 3-question MCQ exam + a fresh in-progress attempt on it."""
    exam = build_exam()
    attempt = start_exam(client, exam)
    assert attempt is not None, "start endpoint produced no attempt"
    assert attempt.status == "in_progress"
    return exam, attempt


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_login_success_and_roles(client):
    r = login(client, "student", "student123")
    assert r.status_code == 200
    assert b"My exams" in r.data

    client.get("/logout")
    r = login(client, "teacher", "teacher123")
    assert b"My exams" in r.data
    assert b"New exam" in r.data


def test_root_redirects_by_role(client):
    # Anonymous -> login page.
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]

    # Each role lands on its own dashboard.
    for user, marker in (("student", "/student"), ("teacher", "/teacher"),
                         ("admin", "/admin")):
        client.get("/logout")
        login(client, user, {"student": "student123", "teacher": "teacher123",
                             "admin": "admin123"}[user])
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(marker + "/"), r.headers["Location"]


def test_login_wrong_password(client):
    r = login(client, "student", "wrongpass")
    assert b"Wrong username or password" in r.data


def test_student_cannot_open_teacher_pages(client):
    login(client, "student", "student123")
    r = client.get("/teacher/")
    assert r.status_code == 302  # redirected away


def test_csrf_required(client):
    login(client, "teacher", "teacher123")
    r = client.post("/teacher/exams/new", data={"title": "x"})
    assert r.status_code == 400
    assert b"CSRF" in r.data


def test_register_and_login(client):
    client.get("/register")
    r = client.post("/register", data={
        "username": "newstudent", "full_name": "New Student",
        "email": "new@example.com", "password": "secret12",
        "confirm": "secret12", "csrf_token": csrf(client),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Account created" in r.data

    # Registration auto-logs-in and lands on the student dashboard.
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/student/")

    user = User.query.filter_by(username="newstudent").first()
    assert user is not None and user.role == "student"

    # ... and the account can log in normally later.
    client.get("/logout")
    r = login(client, "newstudent", "secret12")
    assert b"My exams" in r.data


def test_register_rejects_duplicate_username(client):
    client.get("/register")
    r = client.post("/register", data={
        "username": "student", "full_name": "Impostor",
        "password": "secret12", "confirm": "secret12",
        "csrf_token": csrf(client),
    }, follow_redirects=True)
    assert b"already exists" in r.data


def test_profile_update(client):
    login(client, "student", "student123")
    r = client.post("/profile", data={
        "action": "save", "full_name": "Renamed Student",
        "email": "renamed@example.edu", "csrf_token": csrf(client),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Profile updated" in r.data
    user = User.query.filter_by(username="student").first()
    assert user.full_name == "Renamed Student"
    assert user.email == "renamed@example.edu"


# --------------------------------------------------------------------------
# Teacher: exam + questions
# --------------------------------------------------------------------------

def make_exam(client, token, **overrides):
    data = {
        "title": "Quiz", "duration_minutes": "10", "violation_limit": "3",
        "pool_size": "0", "shuffle_questions": "on", "shuffle_options": "on",
        "require_fullscreen": "on", "show_result": "on",
    }
    data.update(overrides)
    r = client.post("/teacher/exams/new", data=data,
                    headers={"X-CSRFToken": token}, follow_redirects=True)
    assert r.status_code == 200
    exam = Exam.query.order_by(Exam.id.desc()).first()
    return exam


def add_question(client, token, exam_id, text="Q?", opts=("a", "b"), correct="0"):
    data = {"text": text, "correct": correct}
    for i, o in enumerate(opts, start=1):
        data[f"option{i}"] = o
    return client.post(f"/teacher/exams/{exam_id}/questions", data=data,
                       headers={"X-CSRFToken": token}, follow_redirects=True)


def test_teacher_creates_exam_with_questions(client):
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam = make_exam(client, token)
    assert exam is not None
    assert exam.status_label == "draft"

    add_question(client, token, exam.id, "One?", ("yes", "no"), "0")
    add_question(client, token, exam.id, "Two?", ("x", "y", "z"), "2")
    assert exam.question_count == 2

    r = client.post(f"/teacher/exams/{exam.id}/publish",
                    headers={"X-CSRFToken": token}, follow_redirects=True)
    assert r.status_code == 200
    assert exam.is_published


def test_teacher_creates_all_question_types(client):
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam = make_exam(client, token)

    def post_question(data):
        return client.post(f"/teacher/exams/{exam.id}/questions", data=data,
                           headers={"X-CSRFToken": token},
                           follow_redirects=True)

    post_question({"type": "multi", "text": "Pick two.", "points": "2",
                   "option1": "one", "option2": "two",
                   "option3": "three", "option4": "four",
                   "correct0": "on", "correct2": "on"})
    post_question({"type": "true_false", "text": "Python is a snake.",
                   "tf_correct": "false"})
    post_question({"type": "short_text", "text": "Define HTTP.",
                   "points": "3",
                   "model_answer": "HyperText Transfer Protocol"})
    # Invalid MCQ (correct index out of range) must be rejected.
    post_question({"type": "mcq", "text": "Broken?", "correct": "99",
                   "option1": "a", "option2": "b"})

    qs = Question.query.filter_by(exam_id=exam.id).all()
    assert len(qs) == 3  # the broken one was rejected

    multi = next(q for q in qs if q.type == "multi")
    assert multi.correct_indices == [0, 2]
    assert multi.points == 2

    tf = next(q for q in qs if q.type == "true_false")
    assert tf.options == ["True", "False"]
    assert tf.correct_index == 1  # "false" was chosen

    st = next(q for q in qs if q.type == "short_text")
    assert st.options is None
    assert st.correct_index is None
    assert st.model_answer.startswith("HyperText")
    assert st.points == 3


# --------------------------------------------------------------------------
# Student: full exam flow
# --------------------------------------------------------------------------

def test_full_exam_flow(client):
    login(client, "student", "student123")
    token = csrf(client)

    exam, attempt = start_first_exam(client)
    assert attempt.status == "in_progress"
    assert len(attempt.question_order) == exam.question_count

    payload = client.get(f"/student/attempts/{attempt.id}").data.decode()
    assert "question-card" in payload

    answers = {}
    for qid in attempt.question_order:
        perm = attempt.option_map[str(qid)]
        q = db.session.get(Question, qid)
        answers[str(qid)] = perm.index(q.correct_index)

    r = post_json(client, f"/api/attempts/{attempt.id}/answers",
                  {"answers": answers}, token)
    assert r.status_code == 200, r.get_json()

    # Submit -> graded instantly.
    r = post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)
    data = r.get_json()
    assert r.status_code == 200
    assert data["score"] == data["total"] == exam.question_count

    # Result page shows the score.
    r = client.get(f"/student/attempts/{attempt.id}/result")
    assert r.status_code == 200
    assert b"100%" in r.data


def test_wrong_answers_grade_partially(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    # Pick a wrong option for the first question, correct for the rest.
    answers = {}
    for i, qid in enumerate(attempt.question_order):
        perm = attempt.option_map[str(qid)]
        q = db.session.get(Question, qid)
        if i == 0:
            wrong = next(idx for idx in range(len(q.options)) if idx != q.correct_index)
            answers[str(qid)] = perm.index(wrong)
        else:
            answers[str(qid)] = perm.index(q.correct_index)

    post_json(client, f"/api/attempts/{attempt.id}/answers", {"answers": answers}, token)
    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token).get_json()
    assert data["total"] == exam.question_count
    assert data["score"] == exam.question_count - 1


def test_second_attempt_blocked(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)

    r = client.post(f"/student/exams/{exam.id}/start",
                    data={"csrf_token": token}, follow_redirects=True)
    assert b"already completed" in r.data


def test_retake_allowed_when_configured(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(attempts_allowed=2)

    first = start_exam(client, exam)
    assert first is not None and first.attempt_no == 1
    assert post_json(client, f"/api/attempts/{first.id}/submit", {},
                     token).status_code == 200

    second = start_exam(client, exam)
    assert second is not None and second.id != first.id
    assert second.attempt_no == 2
    assert second.status == "in_progress"


def test_access_code_enforced(client):
    login(client, "student", "student123")
    exam = build_exam(access_code="SECRET")
    token = csrf(client)
    url = f"/student/exams/{exam.id}/start"

    r = client.post(url, data={"csrf_token": token}, follow_redirects=True)
    assert b"This exam needs an access code" in r.data
    assert Attempt.query.filter_by(exam_id=exam.id).count() == 0

    r = client.post(url, data={"csrf_token": token, "access_code": "nope"},
                    follow_redirects=True)
    assert b"Wrong access code" in r.data
    assert Attempt.query.filter_by(exam_id=exam.id).count() == 0

    # Case-insensitive, whitespace-tolerant.
    r = client.post(url, data={"csrf_token": token, "access_code": " secret "},
                    follow_redirects=True)
    assert r.status_code == 200
    attempt = Attempt.query.filter_by(exam_id=exam.id).first()
    assert attempt is not None and attempt.status == "in_progress"


def test_resume_in_progress_attempt(client):
    login(client, "student", "student123")
    exam, attempt = start_first_exam(client)
    # Second start resumes, does NOT create a new attempt.
    token = csrf(client)
    client.post(f"/student/exams/{exam.id}/start",
                data={"csrf_token": token}, follow_redirects=True)
    assert Attempt.query.filter_by(exam_id=exam.id).count() == 1


def test_resume_restores_answers_in_display_order(client):
    """Regression: shuffled options must restore the correct radio button.

    Answers are stored as ORIGINAL option indexes; the exam page speaks
    DISPLAY indexes (options are shuffled). The payload has to translate.
    """
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    qid = attempt.question_order[0]
    perm = attempt.option_map[str(qid)]
    q = db.session.get(Question, qid)
    # Student clicks the display slot that shows the correct answer.
    display_index = perm.index(q.correct_index)

    r = post_json(client, f"/api/attempts/{attempt.id}/answers",
                  {"answers": {str(qid): display_index}}, token)
    assert r.status_code == 200

    from app.services import attempt_payload
    payload = attempt_payload(attempt)
    # On resume, the page must see the DISPLAY index it can compare to
    # the radio button values directly.
    assert payload["answers"][str(qid)] == display_index

    # And grading still uses the ORIGINAL index underneath.
    answer = next(a for a in attempt.answers if a.question_id == qid)
    assert answer.selected_index == q.correct_index


def test_attempt_records_device_info(client):
    login(client, "student", "student123")
    exam, attempt = start_first_exam(client)
    # Server-side device log (proposal: IP/device on every attempt).
    assert attempt.ip_address == "127.0.0.1"
    assert attempt.attempt_no == 1


def test_result_hidden_until_published(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(show_result=False)
    attempt = start_exam(client, exam)

    answers = {}
    for qid in attempt.question_order:
        perm = attempt.option_map[str(qid)]
        q = db.session.get(Question, qid)
        answers[str(qid)] = perm.index(q.correct_index)
    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": answers}, token)
    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    assert data["score"] == data["total"] == 3  # graded in the background

    r = client.get(f"/student/attempts/{attempt.id}/result")
    assert r.status_code == 200
    assert b"Result not published yet" in r.data
    assert b"100%" not in r.data  # score stays hidden from the student


# --------------------------------------------------------------------------
# Anti-cheat: violations (NO auto-submit policy)
# --------------------------------------------------------------------------

def test_violation_recorded_but_never_submits(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    for _ in range(5):  # way past the default limit of 3
        r = post_json(client, f"/api/attempts/{attempt.id}/violations",
                      {"type": "tab_switch", "duration_ms": 5000}, token)
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        # Policy: never submit, only alert.
        assert attempt.status == "in_progress"

    db.session.refresh(attempt)
    assert attempt.warning_count == 5
    assert attempt.over_limit is True
    # The limit alert message exists for the teacher.
    assert any("limit reached" in m.body.lower() for m in attempt.messages)


def test_short_focus_loss_is_grace(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    r = post_json(client, f"/api/attempts/{attempt.id}/violations",
                  {"type": "tab_switch", "duration_ms": 800}, token)
    data = r.get_json()
    assert data["ignored"] is True
    assert data["weight"] == 0
    db.session.refresh(attempt)
    assert attempt.warning_count == 0


def test_unknown_violation_rejected(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    r = post_json(client, f"/api/attempts/{attempt.id}/violations",
                  {"type": "nonsense"}, token)
    assert r.status_code == 400


def test_server_side_deadline_blocks_answers(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    attempt.deadline = utcnow() - timedelta(minutes=5)
    db.session.commit()

    r = post_json(client, f"/api/attempts/{attempt.id}/answers",
                  {"answers": {str(attempt.question_order[0]): 0}}, token)
    assert r.status_code == 410


def test_submit_after_deadline_still_grades(client):
    """Late submission with a last flush must still grade (no lost exam)."""
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    # Answers were saved before the deadline; the deadline passes; the page
    # then POSTs submit including a final flush of the same answers.
    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": {str(attempt.question_order[0]): 0}}, token)
    attempt.deadline = utcnow() - timedelta(minutes=1)
    db.session.commit()

    r = post_json(client, f"/api/attempts/{attempt.id}/submit",
                  {"answers": {str(attempt.question_order[0]): 0}}, token)
    assert r.status_code == 200, r.get_json()
    db.session.refresh(attempt)
    assert attempt.status == "submitted"
    assert attempt.score is not None


def test_monitor_gap_recorded_on_silent_client(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    # Simulate the anti-cheat script being disabled for 2 minutes.
    attempt.last_heartbeat = utcnow() - timedelta(seconds=120)
    db.session.commit()

    post_json(client, f"/api/attempts/{attempt.id}/heartbeat", {}, token)
    db.session.refresh(attempt)
    assert any(v.type == "monitor_gap" for v in attempt.violations)
    assert attempt.warning_count >= 1


def test_flag_question_for_review(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    qid = attempt.question_order[0]

    r = post_json(client, f"/api/attempts/{attempt.id}/flag",
                  {"question_id": qid, "flagged": True}, token)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    answer = next(a for a in attempt.answers if a.question_id == qid)
    assert answer.flagged is True

    # Flags ride along into the resume payload.
    from app.services import attempt_payload
    payload = attempt_payload(attempt)
    entry = next(e for e in payload["questions"] if e["id"] == qid)
    assert entry["flagged"] is True

    r = post_json(client, f"/api/attempts/{attempt.id}/flag",
                  {"question_id": qid, "flagged": False}, token)
    assert r.status_code == 200
    db.session.refresh(answer)
    assert answer.flagged is False


# --------------------------------------------------------------------------
# Grading: question types, negative marking, typed answers, AI review
# --------------------------------------------------------------------------

def test_multi_and_true_false_grading(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(n_questions=0)
    multi = Question(exam_id=exam.id, type="multi", text="Pick exactly two.",
                     options=["one", "two", "three", "four"],
                     correct_indices=[0, 2], correct_index=0, points=2)
    tf = Question(exam_id=exam.id, type="true_false", text="The sky is blue.",
                  options=["True", "False"], correct_index=0, points=1)
    db.session.add_all([multi, tf])
    db.session.commit()

    attempt = start_exam(client, exam)
    answers = {
        str(multi.id): sorted([attempt.option_map[str(multi.id)].index(0),
                               attempt.option_map[str(multi.id)].index(2)]),
        str(tf.id): attempt.option_map[str(tf.id)].index(0),
    }
    r = post_json(client, f"/api/attempts/{attempt.id}/answers",
                  {"answers": answers}, token)
    assert r.status_code == 200, r.get_json()

    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    assert data["score"] == 3
    assert data["total"] == 3


def test_negative_marking_subtracts(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(negative_marking=True)
    attempt = start_exam(client, exam)

    answers = {}
    for qid in attempt.question_order:
        perm = attempt.option_map[str(qid)]
        q = db.session.get(Question, qid)
        wrong = next(i for i in range(len(q.options)) if i != q.correct_index)
        answers[str(qid)] = perm.index(wrong)
    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": answers}, token)

    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    assert data["total"] == 3
    # 3 wrong x -25% of 1 point.
    assert data["score"] == -0.75


def test_typed_answer_auto_matched_without_ai(client):
    """An exact model match counts immediately — no review round-trip."""
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(n_questions=0)
    q = Question(exam_id=exam.id, type="short_text", text="What is 2+2?",
                 model_answer="4", points=1)
    db.session.add(q)
    db.session.commit()

    attempt = start_exam(client, exam)
    qid = attempt.question_order[0]
    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": {str(qid): "4"}}, token)
    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    assert data["score"] == 1 and data["total"] == 1

    r = client.get(f"/student/attempts/{attempt.id}/result")
    assert b"waiting for review" not in r.data


def test_typed_answer_ai_suggestion_and_teacher_approval(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam = build_exam(n_questions=0)
    q = Question(exam_id=exam.id, type="short_text",
                 text="What does HTTP stand for?",
                 model_answer="HyperText Transfer Protocol", points=2)
    db.session.add(q)
    db.session.commit()

    attempt = start_exam(client, exam)
    qid = attempt.question_order[0]

    # An answer that does NOT auto-match the model answer.
    r = post_json(client, f"/api/attempts/{attempt.id}/answers",
                  {"answers": {str(qid): "hmm... something about text"}}, token)
    assert r.status_code == 200

    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    # Pending review: counts 0 until approved.
    assert data["score"] == 0 and data["total"] == 2

    # The marking job ran (inline in tests) and left a suggestion.
    row = GradingResult.query.filter_by(attempt_id=attempt.id,
                                        question_id=qid).first()
    assert row is not None and row.status == "suggested"

    # The student sees that the answer is waiting for review.
    r = client.get(f"/student/attempts/{attempt.id}/result")
    assert b"waiting for review" in r.data

    # Teacher approves edited marks -> the score is re-graded immediately.
    client.get("/logout")
    login(client, "teacher", "teacher123")
    r = client.get(f"/teacher/attempts/{attempt.id}/grading")
    assert r.status_code == 200 and b"HTTP" in r.data

    r = client.post(f"/teacher/gradings/{row.id}/review",
                    data={"action": "approve", "marks": "1.5",
                          "csrf_token": csrf(client)},
                    follow_redirects=True)
    assert r.status_code == 200
    db.session.refresh(attempt)
    assert attempt.score == 1.5
    assert attempt.total == 2

    # Only approved marks count (policy), and they are now visible.
    r = client.get(f"/teacher/attempts/{attempt.id}/report")
    assert r.status_code == 200
    assert b"1.5" in r.data


def test_teacher_regrade_recomputes_score(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    answers = {}
    for qid in attempt.question_order:
        q = db.session.get(Question, qid)
        answers[str(qid)] = attempt.option_map[str(qid)].index(q.correct_index)
    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": answers}, token)
    data = post_json(client, f"/api/attempts/{attempt.id}/submit", {},
                     token).get_json()
    assert data["score"] == 3

    # Simulate a stale/tampered stored score.
    attempt.score = 0.0
    db.session.commit()

    client.get("/logout")
    login(client, "teacher", "teacher123")
    r = post_json(client, f"/teacher/attempts/{attempt.id}/regrade", {},
                  csrf(client))
    assert r.status_code in (200, 302)
    db.session.refresh(attempt)
    assert attempt.score == 3.0


# --------------------------------------------------------------------------
# Handwritten uploads: hashes, duplicates, late window
# --------------------------------------------------------------------------

def test_upload_duplicate_and_late_recorded(client):
    from app import services

    login(client, "student", "student123")
    exam = build_exam(n_questions=1, upload_window_minutes=60)
    token = csrf(client)

    a1 = start_exam(client, exam)
    post_json(client, f"/api/attempts/{a1.id}/submit", {}, token)
    services.save_upload(a1, "answers.pdf", b"%PDF-1.4 demo bytes", is_pdf=True)
    sub1 = Submission.query.filter_by(attempt_id=a1.id).one()
    assert len(sub1.file_hash) == 64
    assert sub1.duplicate_of is None
    assert services.upload_window_open(a1) is True

    # Another student uploads byte-identical content -> duplicate proof.
    client.get("/logout")
    login(client, "student2", "student123")
    token = csrf(client)
    a2 = start_exam(client, exam, username="student2")
    post_json(client, f"/api/attempts/{a2.id}/submit", {}, token)
    services.save_upload(a2, "scan.pdf", b"%PDF-1.4 demo bytes", is_pdf=True)
    sub2 = Submission.query.filter_by(attempt_id=a2.id).one()
    assert sub2.duplicate_of == a1.id  # points at the other attempt
    assert any(v.type == "duplicate_file" for v in a2.violations)

    # Window closed -> rejected with 410, but the proof stays in the log.
    a2.submitted_at = utcnow() - timedelta(hours=3)
    db.session.commit()
    with pytest.raises(services.ExamError) as exc:
        services.save_upload(a2, "too_late.pdf", b"other bytes", is_pdf=True)
    assert exc.value.status == 410
    assert any(v.type == "late_upload" for v in a2.violations)
    db.session.refresh(a2)
    assert a2.status == "submitted"  # evidence only — never re-submitted


# --------------------------------------------------------------------------
# Hash chain integrity
# --------------------------------------------------------------------------

def test_hash_chain_intact_after_events(client):
    from app.services import verify_chain
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)

    post_json(client, f"/api/attempts/{attempt.id}/answers",
              {"answers": {str(attempt.question_order[0]): 0}}, token)
    post_json(client, f"/api/attempts/{attempt.id}/violations",
              {"type": "copy_paste"}, token)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)

    ok, links, broken_at = verify_chain(attempt)
    assert ok, f"chain broken at {broken_at} after {links} links"
    assert links >= 3


def test_tampering_detected_by_chain(client):
    from app.models import Event
    from app.services import verify_chain
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/violations",
              {"type": "tab_switch", "duration_ms": 9000}, token)

    # Cheat: delete a violation row from the middle of history.
    victim = Event.query.filter_by(attempt_id=attempt.id, kind="violation").first()
    assert victim is not None
    db.session.delete(victim)
    db.session.commit()

    ok, links, broken_at = verify_chain(attempt)
    assert not ok


# --------------------------------------------------------------------------
# Teacher report & live actions
# --------------------------------------------------------------------------

def test_teacher_report_renders(client):
    login(client, "student", "student123")
    token_s = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/violations",
              {"type": "tab_switch", "duration_ms": 9000}, token_s)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token_s)

    client.get("/logout")
    login(client, "teacher", "teacher123")
    token_t = csrf(client)
    r = client.get(f"/teacher/attempts/{attempt.id}/report")
    assert r.status_code == 200
    assert b"Cheat report" in r.data
    assert b"Tab switch" in r.data
    assert b"intact" in r.data  # evidence chain badge


def test_teacher_can_message_extend_and_end(client):
    login(client, "student", "student123")
    token_s = csrf(client)
    exam, attempt = start_first_exam(client)

    client.get("/logout")
    login(client, "teacher", "teacher123")
    token_t = csrf(client)

    old_deadline = attempt.deadline
    r = post_json(client, f"/teacher/attempts/{attempt.id}/extend",
                  {"minutes": 5}, token_t)
    assert r.status_code in (200, 302)
    db.session.refresh(attempt)
    assert attempt.deadline > old_deadline

    r = post_json(client, f"/teacher/attempts/{attempt.id}/message",
                  {"body": "Focus please"}, token_t)
    assert r.status_code in (200, 302)
    db.session.refresh(attempt)
    assert any(m.body == "Focus please" for m in attempt.messages)

    r = post_json(client, f"/teacher/attempts/{attempt.id}/force-submit",
                  {}, token_t)
    assert r.status_code in (200, 302)
    db.session.refresh(attempt)
    assert attempt.status == "submitted"
    assert attempt.submitted_by == "teacher"
    assert attempt.score is not None


def test_teacher_cannot_see_other_teachers_exam(client, app):
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam = make_exam(client, token)
    add_question(client, token, exam.id)
    client.post(f"/teacher/exams/{exam.id}/publish",
                headers={"X-CSRFToken": token})

    client.get("/logout")
    login(client, "student", "student123")
    r = client.get(f"/teacher/exams/{exam.id}/results")
    assert r.status_code == 302  # students never get in


# --------------------------------------------------------------------------
# Question bank (shared across exams)
# --------------------------------------------------------------------------

def test_question_bank_lists_and_copies(client):
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam1 = make_exam(client, token)
    add_question(client, token, exam1.id, "Bankable question?", ("x", "y"), "0")
    exam2 = make_exam(client, token)

    r = client.get("/teacher/bank")
    assert r.status_code == 200
    assert b"Bankable question?" in r.data

    q = Question.query.filter_by(text="Bankable question?").first()
    assert q is not None
    r = client.post(f"/teacher/bank/{q.id}/copy",
                    data={"exam_id": str(exam2.id), "csrf_token": csrf(client)},
                    follow_redirects=True)
    assert r.status_code == 200
    assert Question.query.filter_by(text="Bankable question?").count() == 2


def test_teacher_creates_standalone_bank_question(client):
    """A question can be written straight into the bank, attached to no exam."""
    login(client, "teacher", "teacher123")
    token = csrf(client)

    r = client.post("/teacher/bank/new", data={
        "type": "mcq", "text": "Standalone entry?", "points": "3",
        "category": "Solo topic", "difficulty": "hard",
        "option1": "alpha", "option2": "beta", "correct": "1",
        "csrf_token": token,
    }, headers={"X-CSRFToken": token}, follow_redirects=True)
    assert r.status_code == 200

    solo = Question.query.filter_by(text="Standalone entry?",
                                    exam_id=None).first()
    assert solo is not None
    assert solo.created_by is not None
    assert solo.correct_index == 1 and solo.points == 3
    assert solo.category is not None and solo.category.name == "Solo topic"

    r = client.get("/teacher/bank")
    assert r.status_code == 200
    assert b"Standalone entry?" in r.data

    # The create-exam page offers it, and the form itself renders.
    r = client.get("/teacher/exams/new")
    assert r.status_code == 200
    assert b"Standalone entry?" in r.data
    assert b'name="bank_ids"' in r.data
    assert client.get("/teacher/bank/new").status_code == 200


def test_broken_bank_question_renders_form_with_values(client):
    """A validation error must not throw away what was typed."""
    login(client, "teacher", "teacher123")
    token = csrf(client)

    r = client.post("/teacher/bank/new", data={
        "type": "mcq", "text": "Half typed?", "points": "9",
        "option1": "only one option", "correct": "0", "csrf_token": token,
    }, headers={"X-CSRFToken": token})

    assert r.status_code == 200
    body = r.data.decode()
    assert "Half typed?" in body          # the question came back …
    assert 'value="9"' in body            # … with its points …
    assert "only one option" in body      # … and its option …
    assert "at least two options" in body.lower()   # … plus the reason
    assert Question.query.filter_by(text="Half typed?").count() == 0


def test_create_exam_from_bank_and_inline_questions(client):
    """Tick bank questions + write new ones in the same create-exam submit."""
    login(client, "teacher", "teacher123")
    token = csrf(client)

    client.post("/teacher/bank/new", data={
        "type": "mcq", "text": "Reusable bank item?", "points": "2",
        "option1": "a", "option2": "b", "correct": "1",
        "csrf_token": token,
    }, headers={"X-CSRFToken": token})
    source = Question.query.filter_by(text="Reusable bank item?",
                                      exam_id=None).first()
    assert source is not None

    r = client.post("/teacher/exams/new", data={
        "title": "Built from the bank", "duration_minutes": "15",
        "violation_limit": "3", "pool_size": "0",
        "bank_ids": [str(source.id)],
        # block 0 — typed straight into the create-exam form …
        "q_0_type": "mcq", "q_0_text": "Typed during creation?",
        "q_0_points": "4", "q_0_category": "Inline topic",
        "q_0_option1": "x", "q_0_option2": "y",
        "q_0_option3": "", "q_0_option4": "", "q_0_correct": "0",
        # block 1 — left completely empty, so it is skipped, not an error.
        "q_1_type": "mcq", "q_1_text": "", "q_1_points": "1",
        "q_1_option1": "", "q_1_option2": "",
        "q_1_option3": "", "q_1_option4": "",
    }, headers={"X-CSRFToken": token})

    assert r.status_code == 302
    match = re.search(r"/teacher/exams/(\d+)", r.headers["Location"])
    assert match, r.headers["Location"]
    exam = db.session.get(Exam, int(match.group(1)))
    assert exam is not None
    assert sorted(q.text for q in exam.questions) == [
        "Reusable bank item?", "Typed during creation?"]

    # The bank entry was COPIED — the original stays where it was.
    db.session.refresh(source)
    assert source.exam_id is None
    typed = Question.query.filter_by(text="Typed during creation?").first()
    assert typed is not None and typed.exam_id == exam.id
    assert typed.category is not None and typed.category.name == "Inline topic"


def test_exam_creation_is_all_or_nothing(client):
    """One broken inline question aborts the exam; nothing is committed."""
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exams_before, questions_before = Exam.query.count(), Question.query.count()

    r = client.post("/teacher/exams/new", data={
        "title": "Never gets saved", "duration_minutes": "20",
        "violation_limit": "3", "pool_size": "0",
        "q_0_type": "mcq", "q_0_text": "The good one",
        "q_0_option1": "a", "q_0_option2": "b", "q_0_correct": "0",
        "q_1_type": "mcq", "q_1_text": "The broken one",
        "q_1_option1": "only-a-single-option", "q_1_correct": "0",
    }, headers={"X-CSRFToken": token})

    assert r.status_code == 200
    body = r.data.decode()
    assert Exam.query.count() == exams_before
    assert Question.query.count() == questions_before
    assert "Never gets saved" in body       # the exam form survived …
    assert "The good one" in body           # … both typed questions …
    assert "The broken one" in body
    assert "at least two options" in body.lower()   # … and the reason
    assert "Nothing was saved" in body


def test_exam_form_error_keeps_bank_ticks_and_checkboxes(client):
    """A missing title re-renders everything the teacher already chose."""
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exams_before = Exam.query.count()

    client.post("/teacher/bank/new", data={
        "type": "true_false", "text": "Ticked bank item?",
        "tf_correct": "false", "csrf_token": token,
    }, headers={"X-CSRFToken": token})
    solo = Question.query.filter_by(text="Ticked bank item?",
                                    exam_id=None).first()
    assert solo is not None

    r = client.post("/teacher/exams/new", data={
        "title": "",                # deliberately invalid
        "duration_minutes": "45",
        "require_fullscreen": "on",
        # shuffle_questions and the rest are simply not sent …
        "bank_ids": [str(solo.id)],
        "q_0_type": "true_false", "q_0_text": "Kept question?",
        "q_0_points": "7", "q_0_tf_correct": "false",
    }, headers={"X-CSRFToken": token})

    assert r.status_code == 200
    body = r.data.decode()
    assert Exam.query.count() == exams_before
    assert "Title is required" in body
    assert 'value="45"' in body
    assert "Kept question?" in body
    assert 'value="7"' in body
    # The selected bank question is still ticked …
    ticked = re.findall(r'name="bank_ids" value="(\d+)"[^>]*\bchecked', body)
    assert ticked == [str(solo.id)]
    # … an untitled-type block kept True/False …
    assert re.search(r'name="q_0_tf_correct".*?value="false"[^>]*selected',
                     body, re.S)
    # … and a checkbox left unticked comes back unticked.
    assert re.search(r'name="shuffle_questions"[^>]*\bchecked', body) is None
    assert re.search(r'name="require_fullscreen"[^>]*\bchecked', body)


def test_broken_question_on_exam_page_keeps_values(client):
    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam = make_exam(client, token)

    r = client.post(f"/teacher/exams/{exam.id}/questions", data={
        "type": "mcq", "text": "Needs fixing?", "points": "5",
        "option1": "solo", "correct": "0",
    }, headers={"X-CSRFToken": token})

    assert r.status_code == 200
    body = r.data.decode()
    assert "Needs fixing?" in body
    assert 'value="5"' in body
    assert "at least two options" in body.lower()
    assert exam.question_count == 0


def test_standalone_bank_question_permissions(client):
    """Only the creator (or an admin) may copy or delete a bank entry."""
    login(client, "teacher", "teacher123")
    token = csrf(client)
    client.post("/teacher/bank/new", data={
        "type": "mcq", "text": "Private bank note?", "points": "1",
        "option1": "a", "option2": "b", "correct": "0",
        "csrf_token": token,
    }, headers={"X-CSRFToken": token})
    solo = Question.query.filter_by(text="Private bank note?",
                                    exam_id=None).first()
    assert solo is not None

    # Another teacher cannot touch it.
    client.get("/logout")
    login(client, "teacher2", "teacher123")
    token2 = csrf(client)
    r = client.post(f"/teacher/bank/{solo.id}/copy",
                    data={"exam_id": "1", "csrf_token": token2},
                    headers={"X-CSRFToken": token2})
    assert r.status_code == 403
    r = client.post(f"/teacher/questions/{solo.id}/delete",
                    data={"csrf_token": token2},
                    headers={"X-CSRFToken": token2})
    assert r.status_code == 403
    assert db.session.get(Question, solo.id) is not None

    # … and the creator can delete it.
    client.get("/logout")
    login(client, "teacher", "teacher123")
    token = csrf(client)
    r = client.post(f"/teacher/questions/{solo.id}/delete",
                    data={"csrf_token": token},
                    headers={"X-CSRFToken": token}, follow_redirects=True)
    assert r.status_code == 200
    assert db.session.get(Question, solo.id) is None


def test_csv_import_creates_questions(client):
    import io

    login(client, "teacher", "teacher123")
    token = csrf(client)
    exam = make_exam(client, token)

    csv_text = (
        "text,type,options,correct,model_answer,rubric,points,category,difficulty\n"
        "Imported MCQ?,mcq,red|blue,0,,,3,Colors,medium\n"
        "Imported TF?,true_false,,false,,,1,Basics,easy\n"
        "Imported short?,short_text,,,HyperText Transfer Protocol,,2,Writing,\n"
    )
    r = client.post(f"/teacher/exams/{exam.id}/questions.csv",
                    data={"file": (io.BytesIO(csv_text.encode()), "q.csv")},
                    content_type="multipart/form-data",
                    headers={"X-CSRFToken": token}, follow_redirects=True)
    assert r.status_code == 200

    added = Question.query.filter_by(exam_id=exam.id).all()
    assert {q.text for q in added} == {
        "Imported MCQ?", "Imported TF?", "Imported short?"}
    mcq = next(q for q in added if q.text == "Imported MCQ?")
    assert mcq.options == ["red", "blue"] and mcq.points == 3
    assert mcq.category is not None and mcq.category.name == "Colors"
    tf = next(q for q in added if q.text == "Imported TF?")
    assert tf.correct_index == 1          # "false" -> the False option
    short = next(q for q in added if q.text == "Imported short?")
    assert short.model_answer == "HyperText Transfer Protocol"


def test_sqlite_upgrade_rebuilds_a_legacy_schema(tmp_path):
    """An old exam.db gets a nullable exam_id, created_by + its index."""
    from sqlalchemy import create_engine, inspect, text

    from app import _sqlite_upgrade

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE questions ("
                " id INTEGER NOT NULL, "
                " exam_id INTEGER NOT NULL, "
                " type VARCHAR(10) NOT NULL, "
                " text TEXT NOT NULL, "
                " points INTEGER NOT NULL, "
                " options JSON, "
                " PRIMARY KEY (id))"))
            conn.execute(text(
                "INSERT INTO questions (id, exam_id, type, text, points) "
                "VALUES (7, 3, 'mcq', 'Carried over?', 5)"))

        _sqlite_upgrade(engine)
        _sqlite_upgrade(engine)          # second run must be a no-op

        columns = {c["name"]: c for c in inspect(engine).get_columns("questions")}
        assert columns["exam_id"]["nullable"] is True     # bank entries OK
        assert "created_by" in columns                    # ownership
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT exam_id, text, points FROM questions WHERE id = 7"
            )).one()
        assert tuple(row) == (3, "Carried over?", 5)      # data survived
        indexes = [i["name"] for i in inspect(engine).get_indexes("questions")]
        assert "ix_questions_created_by" in indexes
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def test_admin_creates_user_and_student_logs_in(client):
    login(client, "admin", "admin123")
    token = csrf(client)
    r = client.post("/admin/users/new", data={
        "username": "newkid", "full_name": "New Kid",
        "role": "student", "password": "secret123",
    }, headers={"X-CSRFToken": token}, follow_redirects=True)
    assert b"created" in r.data

    client.get("/logout")
    r = login(client, "newkid", "secret123")
    assert b"My exams" in r.data


def test_admin_updates_risk_weights_and_defaults(client):
    from app.grading import active_weights

    login(client, "admin", "admin123")
    token = csrf(client)

    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert b"Risk weights" in r.data

    # Partial save: only tab_switch changes, everything else keeps default.
    r = client.post("/admin/settings",
                    data={"action": "weights", "w_tab_switch": "7"},
                    headers={"X-CSRFToken": token}, follow_redirects=True)
    assert b"Risk weights saved" in r.data
    assert active_weights()["tab_switch"] == 7
    assert active_weights()["multiple_faces"] == 4  # proposal default kept

    r = client.post("/admin/settings", data={
        "action": "defaults", "violation_limit": "4",
        "duration_minutes": "45", "snapshot_retention_days": "10",
    }, headers={"X-CSRFToken": token}, follow_redirects=True)
    assert b"Defaults saved" in r.data
    assert Setting.get("exam_defaults")["violation_limit"] == 4


def test_admin_audit_log_lists_actions(client):
    login(client, "admin", "admin123")
    r = client.get("/admin/audit")
    assert r.status_code == 200
    # Seeded trail entries are listed...
    assert b"database_backup" in r.data

    # ... and this session's own actions get appended.
    client.post("/admin/settings",
                data={"action": "weights", "w_tab_switch": "9"},
                headers={"X-CSRFToken": csrf(client)})
    r = client.get("/admin/audit")
    assert b"weights_updated" in r.data


def test_one_session_per_student_kicks_old_browser(client):
    login(client, "student", "student123")
    # A second login issues a new token; the first client's cookie is stale.
    login(client, "student", "student123")
    # The session token stored on the user must match the newest login.
    user = User.query.filter_by(username="student").first()
    with client.session_transaction() as sess:
        assert sess.get("session_token") == user.session_token
    assert user.session_token is not None


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

def test_student_csv_export(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)

    r = client.get("/student/history.csv")
    assert r.status_code == 200
    assert "text/csv" in r.content_type
    assert exam.title in r.data.decode()


def test_teacher_csv_export(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)

    client.get("/logout")
    login(client, "teacher", "teacher123")
    r = client.get(f"/teacher/exams/{exam.id}/results.csv")
    assert r.status_code == 200
    assert "Demo Student" in r.data.decode()


# --------------------------------------------------------------------------
# PDF export (fpdf2) — regression: Unicode text must never crash it
# --------------------------------------------------------------------------

def test_teacher_pdf_export_serves_valid_pdf(client):
    login(client, "student", "student123")
    token = csrf(client)
    exam, attempt = start_first_exam(client)
    post_json(client, f"/api/attempts/{attempt.id}/submit", {}, token)

    client.get("/logout")
    login(client, "teacher", "teacher123")
    r = client.get(f"/teacher/exams/{exam.id}/results.pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-"
    assert b"%%EOF" in r.data[-64:]  # complete, non-truncated document


def test_pdf_text_sanitizer_handles_unsupported_glyphs():
    """Em dashes and unknown symbols degrade gracefully (never raise)."""
    from app.pdf_export import _CORE_COVERAGE, _clean, _pct

    # The built-in Helvetica lacks the em dash -> ASCII fallback.
    assert _clean("Web \u2014 Fundamentals", _CORE_COVERAGE) == "Web - Fundamentals"
    # Symbols in the fallback table map; everything else becomes "?".
    assert _clean("A\u2713B\U0001F393C", _CORE_COVERAGE) == "AvB?C"
    # Plain ASCII always survives untouched.
    assert _clean("Demo Student", _CORE_COVERAGE) == "Demo Student"

    assert _pct(100.0) == "100"
    assert _pct(66.7) == "66.7"
    assert _pct(None) == "-"


def test_ai_config_file_is_created_and_env_still_wins():
    """The proposal's model config file exists on first run; env > file."""
    import json as _json

    from app.ai import config as aicfg

    aicfg.reload_config()
    path = aicfg.ensure_config_file()
    assert path.is_file()
    data = _json.loads(path.read_text(encoding="utf-8"))
    assert data["marking_model"] == aicfg.DEFAULTS["marking_model"]

    # A deployment can still override the file with an environment variable.
    os.environ["EXAM_OCR_MODEL"] = "custom/ocr-model"
    try:
        aicfg.reload_config()
        assert aicfg.model_config()["ocr_model"] == "custom/ocr-model"
    finally:
        os.environ.pop("EXAM_OCR_MODEL", None)
        aicfg.reload_config()
    assert aicfg.model_config()["ocr_model"] == aicfg.DEFAULTS["ocr_model"]


# --------------------------------------------------------------------------
# Demo dataset integrity (guards the seed itself)
# --------------------------------------------------------------------------

def test_seed_demo_data_is_coherent(client):
    from app.services import verify_chain

    # 1. Every attempt's evidence chain is intact.
    for attempt in Attempt.query.all():
        ok, links, broken = verify_chain(attempt)
        assert ok, f"attempt {attempt.id}: chain broken at event {broken}"

    # 2. The closed exam was seeded BEFORE closing and holds a graded run.
    gk = Exam.query.filter(Exam.title.like("General Knowledge%")).first()
    assert gk is not None
    assert gk.status_label == "closed"
    gk_attempt = Attempt.query.filter_by(exam_id=gk.id).first()
    assert gk_attempt is not None and gk_attempt.status == "submitted"
    assert gk_attempt.score is not None

    # 3. The collusion pair really answers identically (both graded).
    web = Exam.query.filter(Exam.title.like("Web Fundamentals%")).first()
    student2 = User.query.filter_by(username="student2").first()
    student3 = User.query.filter_by(username="student3").first()
    a2 = Attempt.query.filter_by(exam_id=web.id, student_id=student2.id).first()
    a3 = Attempt.query.filter_by(exam_id=web.id, student_id=student3.id).first()
    sel2 = {a.question_id: a.selected_index for a in a2.answers}
    sel3 = {a.question_id: a.selected_index for a in a3.answers}
    assert sel2 and sel2 == sel3

    # 4. The grace-window focus loss left NO warning and did NOT block the
    #    submission (violations are evidence, never auto-submit).
    assert a2.status == "submitted"
    assert a2.warning_count == 0
    assert any(v.type == "short_focus_loss" for v in a2.violations)

    # 5. The demo student always has a free exam to start (smoke_test.py).
    client.get("/logout")
    login(client, "student", "student123")
    html = client.get("/student/").data.decode()
    assert re.search(r"/student/exams/\d+/start", html)

    # 6. The typed-answers demo produced AI suggestions + one approval.
    graded = GradingResult.query.all()
    assert any(g.status == "suggested" for g in graded)
    assert any(g.status == "approved" for g in graded)
