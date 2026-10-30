"""Database models for the Virtual Exam System.

Conventions
-----------
* All timestamps are naive UTC (``utcnow()``) so SQLite stores them cleanly.
* ``Event`` rows form a tamper-evident hash chain per attempt: every meaningful
  action (answer save, violation, snapshot, submission) stores the hash of the
  previous event, so deleting or rewriting history breaks the chain.
"""

from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

#: Hash used as the starting point of every event chain.
GENESIS_HASH = "0" * 64

#: Brute-force protection defaults (editable later via admin settings if desired).
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (SQLite friendly)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False, default="")
    email = db.Column(db.String(120), nullable=True)
    #: Optional profile photo (used for the identity check comparison).
    photo_path = db.Column(db.String(400), nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="student")  # admin|teacher|student

    # One-session-per-student: a login issues a fresh token; the token stored
    # here is compared against the cookie, so an old browser gets kicked out.
    session_token = db.Column(db.String(64), nullable=True)
    session_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # Brute-force protection: failed login count and lockout expiry.
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

    attempts = db.relationship("Attempt", back_populates="student")

    # -- password helpers -------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def session_is_fresh(self) -> bool:
        """True while another browser has been seen within the last 10 minutes."""
        if not self.session_token or not self.session_seen_at:
            return False
        return (utcnow() - self.session_seen_at).total_seconds() < 600

    # -- brute-force protection -------------------------------------------

    @property
    def is_locked(self) -> bool:
        """True while the account is locked due to repeated failed logins."""
        if not self.locked_until:
            return False
        return utcnow() < self.locked_until

    def record_failed_login(self) -> None:
        """Increment the failure counter and lock the account when the limit is reached."""
        self.failed_logins += 1
        if self.failed_logins >= MAX_FAILED_LOGINS:
            self.locked_until = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)

    def record_successful_login(self) -> None:
        """Reset the failure counter and clear any lockout."""
        self.failed_logins = 0
        self.locked_until = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username} ({self.role})>"


# --------------------------------------------------------------------------
# Exams & questions
# --------------------------------------------------------------------------

class Exam(db.Model):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")

    duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    violation_limit = db.Column(db.Integer, nullable=False, default=3)
    #: Sample N questions from the pool per student (0 = use every question).
    pool_size = db.Column(db.Integer, nullable=False, default=0)
    shuffle_questions = db.Column(db.Boolean, nullable=False, default=True)
    shuffle_options = db.Column(db.Boolean, nullable=False, default=True)
    require_fullscreen = db.Column(db.Boolean, nullable=False, default=True)
    webcam_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # -- proposal "Exam setup" / "Extra exam settings" ---------------------
    #: Percent a student needs to pass (shown on the result page).
    pass_mark = db.Column(db.Integer, nullable=False, default=40)
    #: How many submitted attempts a student may take (1 = single shot).
    attempts_allowed = db.Column(db.Integer, nullable=False, default=1)
    #: Optional access code the student must type before starting.
    access_code = db.Column(db.String(12), nullable=True)
    #: Wrong MCQ answers lose 25% of the question's points when enabled.
    negative_marking = db.Column(db.Boolean, nullable=False, default=False)
    #: Seconds past the deadline still accepted (slow connections).
    grace_seconds = db.Column(db.Integer, nullable=False, default=5)
    #: Publish the score to the student right after submission.
    show_result = db.Column(db.Boolean, nullable=False, default=True)
    #: One-question-at-a-time mode (no going back).
    one_at_a_time = db.Column(db.Boolean, nullable=False, default=False)
    #: Minutes after submission during which handwritten pages may be
    #: uploaded (0 = uploads disabled for this exam).
    upload_window_minutes = db.Column(db.Integer, nullable=False, default=0)

    scheduled_start = db.Column(db.DateTime, nullable=True)
    scheduled_end = db.Column(db.DateTime, nullable=True)
    is_published = db.Column(db.Boolean, nullable=False, default=False)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    creator = db.relationship("User", foreign_keys=[created_by])
    questions = db.relationship(
        "Question",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="Question.id",
    )
    attempts = db.relationship(
        "Attempt", back_populates="exam", cascade="all, delete-orphan"
    )
    resources = db.relationship(
        "Resource", back_populates="exam", cascade="all, delete-orphan"
    )

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def effective_pool_size(self) -> int:
        """How many questions each student receives."""
        if self.pool_size and 0 < self.pool_size < len(self.questions):
            return self.pool_size
        return len(self.questions)

    def is_open(self, now: datetime | None = None) -> bool:
        """True when the exam is published and inside its schedule window."""
        now = now or utcnow()
        if not self.is_published:
            return False
        if self.scheduled_start and now < self.scheduled_start:
            return False
        if self.scheduled_end and now > self.scheduled_end:
            return False
        return True

    @property
    def status_label(self) -> str:
        if not self.is_published:
            return "draft"
        now = utcnow()
        if self.scheduled_start and now < self.scheduled_start:
            return "scheduled"
        if self.scheduled_end and now > self.scheduled_end:
            return "closed"
        return "open"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Exam {self.id} {self.title!r}>"


class Question(db.Model):
    __tablename__ = "questions"

    #: Question types. mcq/multi/true_false are auto-graded; short_text and
    #: long_text are checked against the model answer (exact match) or by the
    #: AI marking pipeline when it is enabled.
    TYPES = ("mcq", "multi", "true_false", "short_text", "long_text")
    TEXT_TYPES = ("short_text", "long_text")

    id = db.Column(db.Integer, primary_key=True)
    #: NULL = a standalone question-bank entry (not attached to any exam;
    #: teachers copy it into exams when creating or editing them).
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=True, index=True)
    #: Owner of a standalone bank question (questions inside an exam belong
    #: to that exam's owner).
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    type = db.Column(db.String(16), nullable=False, default="mcq")
    text = db.Column(db.Text, nullable=False)
    #: JSON list of option strings (NULL for typed-answer questions).
    options = db.Column(db.JSON, nullable=True)
    #: Original index of the correct option (NULL for typed/multi questions).
    correct_index = db.Column(db.Integer, nullable=True)
    #: JSON list of correct indexes for multi-answer questions (NULL otherwise).
    correct_indices = db.Column(db.JSON, nullable=True)
    points = db.Column(db.Integer, nullable=False, default=1)

    # -- question bank metadata -------------------------------------------
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True)
    difficulty = db.Column(db.String(8), nullable=True)  # easy|medium|hard

    # -- typed answers / AI marking ----------------------------------------
    model_answer = db.Column(db.Text, nullable=True)
    rubric = db.Column(db.Text, nullable=True)

    exam = db.relationship("Exam", back_populates="questions")
    category = db.relationship("Category", back_populates="questions")

    @property
    def is_text(self) -> bool:
        return self.type in self.TEXT_TYPES

    @property
    def label(self) -> str:
        return {
            "mcq": "Single answer", "multi": "Multiple answers",
            "true_false": "True / False", "short_text": "Short text",
            "long_text": "Long text",
        }.get(self.type, self.type)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Question {self.id} exam={self.exam_id} {self.type}>"


class Category(db.Model):
    """Question-bank category (topic) shared across a teacher's exams."""

    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    questions = db.relationship("Question", back_populates="category")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.name}>"


# --------------------------------------------------------------------------
# Attempts (one exam run by one student)
# --------------------------------------------------------------------------

class Attempt(db.Model):
    __tablename__ = "attempts"
    # NOTE: one *active* attempt per student is enforced in services.py;
    # a unique constraint here would block the "attempts allowed" retakes.

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    #: Which try this is (1 = first), bounded by Exam.attempts_allowed.
    attempt_no = db.Column(db.Integer, nullable=False, default=1)

    #: Server-side device log (proposal: "IP and device logging").
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(400), nullable=True)

    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    #: Server-authoritative deadline (start + duration + teacher extensions).
    deadline = db.Column(db.DateTime, nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="in_progress")  # in_progress|submitted
    submitted_by = db.Column(db.String(16), nullable=True)  # student|teacher

    #: Float because negative marking and AI marks can be fractional.
    score = db.Column(db.Float, nullable=True)
    total = db.Column(db.Float, nullable=True)

    # Per-attempt paper: which questions, in which order, with which option
    # permutation (values are original option indexes in display order).
    question_order = db.Column(db.JSON, nullable=False)
    option_map = db.Column(db.JSON, nullable=False)

    warning_count = db.Column(db.Integer, nullable=False, default=0)
    over_limit = db.Column(db.Boolean, nullable=False, default=False)
    risk_score = db.Column(db.Integer, nullable=True)
    risk_level = db.Column(db.String(8), nullable=True)  # low|medium|high

    # Tamper-evident chain: hash of the most recent Event row for this attempt.
    last_event_hash = db.Column(db.String(64), nullable=False, default=GENESIS_HASH)
    last_heartbeat = db.Column(db.DateTime, nullable=True, default=utcnow)

    webcam_consent = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    exam = db.relationship("Exam", back_populates="attempts")
    student = db.relationship("User", back_populates="attempts")
    answers = db.relationship(
        "Answer", back_populates="attempt", cascade="all, delete-orphan"
    )
    violations = db.relationship(
        "Violation",
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="Violation.id",
    )
    snapshots = db.relationship(
        "Snapshot", back_populates="attempt", cascade="all, delete-orphan"
    )
    messages = db.relationship(
        "Message",
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )
    events = db.relationship(
        "Event",
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="Event.id",
    )
    submissions = db.relationship(
        "Submission", back_populates="attempt", cascade="all, delete-orphan"
    )
    gradings = db.relationship(
        "GradingResult", back_populates="attempt", cascade="all, delete-orphan"
    )

    @property
    def seconds_left(self) -> int:
        return max(0, int((self.deadline - utcnow()).total_seconds()))

    @property
    def is_active(self) -> bool:
        return self.status == "in_progress"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attempt {self.id} exam={self.exam_id} student={self.student_id} {self.status}>"


class Answer(db.Model):
    __tablename__ = "answers"
    __table_args__ = (
        db.UniqueConstraint("attempt_id", "question_id", name="uq_answer_attempt_question"),
    )

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False, index=True)
    #: Original option index chosen by the student (NULL = not answered yet).
    selected_index = db.Column(db.Integer, nullable=True)
    #: All chosen indexes for multi-answer questions (NULL for single MCQs).
    selected_indices = db.Column(db.JSON, nullable=True)
    #: Typed response for short/long text questions (NULL for MCQs).
    response = db.Column(db.Text, nullable=True)
    #: Student marked this question to come back to it.
    flagged = db.Column(db.Boolean, nullable=False, default=False)
    first_answered_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempt = db.relationship("Attempt", back_populates="answers")
    question = db.relationship("Question")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Answer a{self.attempt_id} q{self.question_id}={self.selected_index}>"


class Violation(db.Model):
    __tablename__ = "violations"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    #: tab_switch | window_blur | fullscreen_exit | copy_paste | no_face |
    #: multiple_faces | monitor_gap | short_focus_loss | low_light | face_check_unavailable
    type = db.Column(db.String(32), nullable=False)
    question_id = db.Column(db.Integer, nullable=True)
    detail = db.Column(db.JSON, nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    weight = db.Column(db.Integer, nullable=False, default=1)
    ignored = db.Column(db.Boolean, nullable=False, default=False)  # inside a grace window
    snapshot_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    attempt = db.relationship("Attempt", back_populates="violations")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Violation {self.id} {self.type} w={self.weight}>"


class Snapshot(db.Model):
    __tablename__ = "snapshots"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    file_path = db.Column(db.String(400), nullable=False)
    reason = db.Column(db.String(32), nullable=False, default="periodic")  # periodic|violation
    #: 0 = no face, 1 = one face, 2+ = several faces, -1 = check unavailable
    face_count = db.Column(db.Integer, nullable=False, default=-1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)  # set by the retention job

    attempt = db.relationship("Attempt", back_populates="snapshots")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Snapshot {self.id} attempt={self.attempt_id} {self.reason}>"


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    #: system | teacher | student (explanation note) | recheck (re-check request)
    kind = db.Column(db.String(16), nullable=False, default="system")
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    attempt = db.relationship("Attempt", back_populates="messages")
    sender = db.relationship("User", foreign_keys=[sender_id])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.id} {self.kind}>"


class Event(db.Model):
    """One link in the tamper-evident hash chain of an attempt."""

    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    #: answer | violation | snapshot | submit | system | submission | grading
    kind = db.Column(db.String(16), nullable=False)
    ref_type = db.Column(db.String(16), nullable=True)  # answer|violation|snapshot|NULL
    ref_id = db.Column(db.Integer, nullable=True)
    payload = db.Column(db.JSON, nullable=False)
    prev_hash = db.Column(db.String(64), nullable=False)
    event_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    attempt = db.relationship("Attempt", back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Event {self.id} {self.kind} {self.event_hash[:8]}>"


# --------------------------------------------------------------------------
# Handwritten submissions (upload window, SHA-256 duplicate check, OCR)
# --------------------------------------------------------------------------

class Submission(db.Model):
    """One uploaded handwritten paper (PDF or merged photos) per attempt."""

    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    file_path = db.Column(db.String(400), nullable=False)
    original_name = db.Column(db.String(255), nullable=False, default="")
    #: SHA-256 of the file bytes — the same file from two students is flagged.
    file_hash = db.Column(db.String(64), nullable=False, index=True)
    is_pdf = db.Column(db.Boolean, nullable=False, default=False)
    #: queued | processing | done | failed
    ocr_status = db.Column(db.String(16), nullable=False, default="queued")
    #: True when the upload arrived after the exam's upload window closed.
    late = db.Column(db.Boolean, nullable=False, default=False)
    #: Another student uploaded a byte-identical file.
    duplicate_of = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    attempt = db.relationship("Attempt", back_populates="submissions")
    pages = db.relationship(
        "SubmissionPage", back_populates="submission",
        cascade="all, delete-orphan", order_by="SubmissionPage.page_no",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Submission {self.id} attempt={self.attempt_id} {self.ocr_status}>"


class SubmissionPage(db.Model):
    """A rendered page of a submission with its OCR text."""

    __tablename__ = "submission_pages"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submissions.id"),
                              nullable=False, index=True)
    page_no = db.Column(db.Integer, nullable=False)
    #: NULL when the page image could not be rendered (mock mode without a
    #: PDF library) — the teacher then reviews the extracted text only.
    image_path = db.Column(db.String(400), nullable=True)
    extracted_text = db.Column(db.Text, nullable=True, default="")
    #: Confidence 0..1 from the OCR engine (None = not scored).
    ocr_confidence = db.Column(db.Float, nullable=True)

    submission = db.relationship("Submission", back_populates="pages")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SubmissionPage {self.submission_id}#{self.page_no}>"


# --------------------------------------------------------------------------
# RAG resources (teacher uploads -> chunks -> retrieval for AI marking)
# --------------------------------------------------------------------------

class Resource(db.Model):
    __tablename__ = "resources"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    file_path = db.Column(db.String(400), nullable=True)  # NULL = typed notes
    #: queued | processing | ready | failed
    status = db.Column(db.String(16), nullable=False, default="queued")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    exam = db.relationship("Exam", back_populates="resources")
    chunks = db.relationship(
        "ResourceChunk", back_populates="resource",
        cascade="all, delete-orphan", order_by="ResourceChunk.chunk_index",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Resource {self.id} {self.title!r} {self.status}>"


class ResourceChunk(db.Model):
    """One retrievable slice of a resource, with its embedding as JSON."""

    __tablename__ = "resource_chunks"

    id = db.Column(db.Integer, primary_key=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"),
                            nullable=False, index=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    page = db.Column(db.Integer, nullable=True)
    text = db.Column(db.Text, nullable=False)
    #: JSON list of floats (embedding vector). Stored locally so the app runs
    #: without ChromaDB; when ChromaDB is installed it mirrors these rows.
    embedding = db.Column(db.JSON, nullable=True)

    resource = db.relationship("Resource", back_populates="chunks")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Chunk {self.resource_id}#{self.chunk_index}>"


# --------------------------------------------------------------------------
# AI marking results (suggestion -> teacher approval)
# --------------------------------------------------------------------------

class GradingResult(db.Model):
    __tablename__ = "grading_results"
    __table_args__ = (
        db.UniqueConstraint("attempt_id", "question_id", name="uq_grading_attempt_question"),
    )

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False, index=True)

    suggested_marks = db.Column(db.Float, nullable=True)
    #: Marks the teacher finally approved (only these count in the score).
    final_marks = db.Column(db.Float, nullable=True)
    max_marks = db.Column(db.Float, nullable=False, default=1.0)
    reason = db.Column(db.Text, nullable=True)
    matched_points = db.Column(db.JSON, nullable=True)
    missing_points = db.Column(db.JSON, nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    model_name = db.Column(db.String(80), nullable=True)
    prompt_version = db.Column(db.String(16), nullable=True)
    #: suggested | approved | rejected (rejected = teacher graded manually)
    status = db.Column(db.String(16), nullable=False, default="suggested")
    approved_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempt = db.relationship("Attempt", back_populates="gradings")
    question = db.relationship("Question")

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GradingResult a{self.attempt_id} q{self.question_id} {self.status}>"


# --------------------------------------------------------------------------
# Background jobs (OCR / chunking / marking run off the request cycle)
# --------------------------------------------------------------------------

class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(24), nullable=False)  # ocr | ingest | mark
    #: queued | running | done | failed
    status = db.Column(db.String(16), nullable=False, default="queued")
    payload = db.Column(db.JSON, nullable=False, default=dict)
    result = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.id} {self.kind} {self.status}>"


# --------------------------------------------------------------------------
# Admin: global settings & audit log
# --------------------------------------------------------------------------

class Setting(db.Model):
    """Key/value store for global anti-cheat weights and defaults."""

    __tablename__ = "settings"

    key = db.Column(db.String(48), primary_key=True)
    value = db.Column(db.JSON, nullable=False)

    @staticmethod
    def get(key: str, default=None):
        row = db.session.get(Setting, key)
        return row.value if row is not None else default

    @staticmethod
    def set(key: str, value) -> None:
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value


class AuditLog(db.Model):
    """User-action audit trail (logins, account changes, publishes)."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    username = db.Column(db.String(64), nullable=True)  # kept after deletion
    action = db.Column(db.String(48), nullable=False, index=True)
    detail = db.Column(db.String(400), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.id} {self.action}>"
