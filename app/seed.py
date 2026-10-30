"""Rich demo data covering every feature of the system.

Seeded automatically when the database is empty, and on demand via::

    flask reseed-demo        # wipe everything and rebuild the demo set

What you get (6 accounts, 11 exams, ~60 questions):

Accounts
    admin/admin123, teacher/teacher123, teacher2/teacher123,
    student/student123, student2/student123, student3/student123

Exams
    1. Web Fundamentals — open, clean runs + a COLLUSION PAIR
       (student2 and student3 answered all 12 questions identically)
    2. Networking Basics — open, question pool (5 of 10), webcam on,
       one attempt LEFT RUNNING for the live monitor + one with
       every webcam violation
    3. Database Systems — proctored, results held back; the student's
       attempt is the HIGH-RISK cheat report: identity photo, 7 scored
       violations, snapshots, monitor gap, over the limit, teacher warning
    4. Introduction to Physics — scheduled in the future
    5. General Knowledge — closed yesterday; ended BY THE TEACHER
    6. Chemistry Midterm — draft (unpublished)
    7. Operating Systems — owned by teacher2 (permissions demo)
    8. Technical Writing — MIXED question types (MCQ, multi, true/false,
       short & long typed answers), AI marking suggestions + teacher
       approval + student re-check request + RAG resources
    9. Handwritten Solutions — upload window: one clean upload, one
       DUPLICATE file from another student, one LATE upload (rejected +
       proof recorded), student explanation note
   10. Practice Retakes — attempts_allowed=2 + access code + negative
       marking + one-question-at-a-time; student on attempt 2 (in progress)
   11. Open Practice Quiz — untouched playground (5 attempts), ready for
        the smoke test / fresh runs
"""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO

from flask import current_app

from . import services
from .extensions import db
from .models import (Attempt, Category, Exam, GradingResult, Question,
                     Resource, Setting, Snapshot, User, utcnow)

# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

ACCOUNTS = [
    # username, full name, role, password, email
    ("admin", "Admin User", "admin", "admin123", "admin@gono.edu"),
    ("teacher", "Ada Teacher", "teacher", "teacher123", "ada@gono.edu"),
    ("teacher2", "Ben Teacher", "teacher", "teacher123", "ben@gono.edu"),
    ("student", "Demo Student", "student", "student123", "demo.student@gono.edu"),
    ("student2", "Second Student", "student", "student123", "second.student@gono.edu"),
    ("student3", "Third Student", "student", "student123", "third.student@gono.edu"),
]

FAKE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
STUDENT_IPS = {
    "student": "192.168.1.24",
    "student2": "192.168.1.31",
    "student3": "10.0.0.105",
}

# --------------------------------------------------------------------------
# Question banks: (text, [options], correct_index)
# --------------------------------------------------------------------------

WEB_QUESTIONS = [
    ("What does HTML stand for?",
     ["HyperText Markup Language", "HighTech Modern Language",
      "Hyperlink Text Machine Language", "Home Tool Markup Language"], 0),
    ("Which CSS property controls text size?",
     ["font-weight", "text-size", "font-size", "size"], 2),
    ("Which HTTP status code means 'Not Found'?",
     ["200", "301", "404", "500"], 2),
    ("What does SQL stand for?",
     ["Structured Query Language", "Simple Query Logic",
      "Sequential Question Language", "System Query Link"], 0),
    ("Which of these is a JavaScript data type?",
     ["Paragraph", "Boolean", "Sentence", "Styler"], 1),
    ("What does CSS stand for?",
     ["Computer Style Sheets", "Creative Style System",
      "Cascading Style Sheets", "Colorful Style Sheets"], 2),
    ("Which HTML tag creates a hyperlink?",
     ["<link>", "<a>", "<href>", "<url>"], 1),
    ("What does JSON stand for?",
     ["JavaScript Object Notation", "Java Source Network",
      "JavaScript Online Node", "Joint Object Naming"], 0),
    ("Who created Python?",
     ["Guido van Rossum", "James Gosling", "Bjarne Stroustrup", "Dennis Ritchie"], 0),
    ("What does API stand for?",
     ["Application Programming Interface", "Applied Program Internet",
      "App Process Integration", "Advanced Protocol Interconnect"], 0),
    ("Which database is file-based and serverless?",
     ["PostgreSQL", "SQLite", "MySQL", "Oracle"], 1),
    ("What does DNS stand for?",
     ["Domain Name System", "Data Network Service",
      "Digital Node Sync", "Domain Net Search"], 0),
]

NETWORK_QUESTIONS = [
    ("Which OSI layer handles routing?",
     ["Physical", "Data Link", "Network", "Transport"], 2),
    ("What is the default port for HTTPS?",
     ["80", "443", "8080", "21"], 1),
    ("Which protocol resolves domain names?",
     ["DNS", "DHCP", "FTP", "SMTP"], 0),
    ("What does LAN stand for?",
     ["Large Area Network", "Local Area Network",
      "Long Access Node", "Linked Array Net"], 1),
    ("Which device connects different networks?",
     ["Hub", "Switch", "Router", "Repeater"], 2),
    ("What is the binary representation of 5?",
     ["100", "101", "110", "111"], 1),
    ("Which of these is a private IP address?",
     ["8.8.8.8", "192.168.1.1", "1.1.1.1", "203.0.113.5"], 1),
    ("What does DHCP automatically assign?",
     ["IP addresses", "DNS records", "Web pages", "Passwords"], 0),
    ("Which topology connects all devices to one central point?",
     ["Bus", "Ring", "Star", "Mesh"], 2),
    ("What does FTP stand for?",
     ["File Transfer Protocol", "Fast Transfer Process",
      "File Tracking Package", "Network Function"], 0),
]

DB_QUESTIONS = [
    ("What does ACID stand for in databases?",
     ["Atomicity Consistency Isolation Durability", "Access Control Internal Design",
      "Automated Check Integrity Driver", "Array Cache Internal Data"], 0),
    ("Which SQL clause filters rows?",
     ["ORDER BY", "WHERE", "GROUP BY", "HAVING"], 1),
    ("What is a primary key?",
     ["A key that opens the database", "A column with unique values",
      "An encrypted field", "A foreign reference"], 1),
    ("Which JOIN returns only matching rows?",
     ["LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN"], 2),
    ("What does normalization reduce?",
     ["Table size", "Redundancy", "Query speed", "Number of users"], 1),
    ("Which command removes a table and its structure?",
     ["REMOVE TABLE", "DELETE TABLE", "DROP TABLE", "ERASE TABLE"], 2),
    ("What is a foreign key?",
     ["A key stored abroad", "A column referencing another table's key",
      "A database index", "A backup key"], 1),
    ("Which data type stores true/false?",
     ["VARCHAR", "BOOLEAN", "INT", "DATE"], 1),
]

PHYSICS_QUESTIONS = [
    ("What is the SI unit of force?",
     ["Joule", "Newton", "Watt", "Pascal"], 1),
    ("What is the speed of light approximately?",
     ["300 km/s", "3,000 km/s", "300,000 km/s", "30,000 km/s"], 2),
    ("Which law states F = ma?",
     ["Newton's first law", "Newton's second law",
      "Newton's third law", "Law of gravity"], 1),
    ("What does a barometer measure?",
     ["Temperature", "Humidity", "Pressure", "Wind speed"], 2),
    ("What is the chemical symbol for gold?",
     ["Go", "Gd", "Ag", "Au"], 3),
]

GENERAL_QUESTIONS = [
    ("Which is the largest ocean?",
     ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("What year did the first Moon landing happen?",
     ["1965", "1969", "1972", "1975"], 1),
    ("Which planet is known as the Red Planet?",
     ["Venus", "Jupiter", "Mars", "Saturn"], 2),
    ("How many continents are there?",
     ["5", "6", "7", "8"], 2),
    ("What is the longest river in the world?",
     ["Amazon", "Nile", "Yangtze", "Danube"], 1),
]

CHEMISTRY_QUESTIONS = [
    ("What is the chemical symbol for water?",
     ["H2O", "CO2", "NaCl", "O2"], 0),
    ("What is the pH of pure water at 25°C?",
     ["5", "7", "9", "12"], 1),
    ("Which gas do plants absorb?",
     ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"], 2),
    ("What is the atomic number of carbon?",
     ["4", "6", "8", "12"], 1),
]

OS_QUESTIONS = [
    ("What does CPU stand for?",
     ["Central Processing Unit", "Computer Personal Unit",
      "Central Program Utility", "Core Processing User"], 0),
    ("Which of these is an operating system?",
     ["Python", "Linux", "HTML", "MySQL"], 1),
    ("What is RAM?",
     ["Permanent storage", "Volatile working memory",
      "A printer type", "Network cable"], 1),
]

HAND_QUESTIONS = [
    ("Which of these is a prime number?",
     ["9", "15", "17", "21"], 2),
    ("What is the derivative of x²?",
     ["x", "2x", "2", "x³"], 1),
    ("1 kilometer equals how many meters?",
     ["10", "100", "1000", "10000"], 2),
    ("What is the value of π (to two decimals)?",
     ["3.14", "3.41", "2.14", "3.16"], 0),
]

PRACTICE_QUESTIONS = [
    ("Which planet is closest to the Sun?",
     ["Venus", "Mercury", "Earth", "Mars"], 1),
    ("What color do you get by mixing red and blue?",
     ["Green", "Orange", "Purple", "Brown"], 2),
    ("How many days are in a leap year?",
     ["364", "365", "366", "367"], 2),
    ("What is the capital of France?",
     ["London", "Berlin", "Paris", "Madrid"], 2),
    ("Which instrument has 88 keys?",
     ["Guitar", "Piano", "Flute", "Drums"], 1),
]

RETAKE_QUESTIONS = [
    ("2 + 2 × 2 = ?",
     ["8", "6", "4", "10"], 1),
    ("Which is a programming language?",
     ["HTTP", "Python", "HTML", "CSS"], 1),
    ("The Sun is a…",
     ["Planet", "Star", "Moon", "Comet"], 1),
    ("How many minutes are in an hour?",
     ["50", "60", "90", "100"], 1),
    ("Water boils at (°C at sea level)?",
     ["50", "80", "100", "120"], 2),
]

# Mixed-type questions for the AI-marking demo. Dict specs carry a
# `answer` field = what the demo student will type.
TYPED_QUESTIONS = [
    dict(type="mcq",
         text="Which HTML tag creates a paragraph?",
         options=["<p>", "<div>", "<para>", "<br>"], correct=0, points=1),
    dict(type="multi",
         text="Which of these are JavaScript data types? (multiple answers)",
         options=["String", "Boolean", "Processor", "Number"],
         correct=[0, 1, 3], points=2, difficulty="medium"),
    dict(type="true_false",
         text="HTTP stands for HyperText Transfer Protocol.",
         correct=0, points=1),
    dict(type="short_text",
         text="What does HTML stand for?",
         model="HyperText Markup Language",
         rubric="Mentions HyperText\nMentions Markup\nMentions Language",
         points=2, difficulty="easy",
         answer="HTML stands for HyperText Markup Language."),
    dict(type="long_text",
         text="Explain the difference between HTTP and HTTPS, including the "
              "security aspects.",
         model=("HTTP sends data in plain text, while HTTPS is HTTP secured "
                "with TLS encryption. HTTPS uses certificates and port 443, "
                "so passwords and cookies cannot be read by anyone on the "
                "network."),
         rubric="States that HTTP is unencrypted\nMentions TLS or SSL encryption\n"
                "Mentions certificates or port 443",
         points=4, difficulty="hard",
         answer=("HTTP sends everything in plain text so anyone on the network "
                 "can read it. HTTPS wraps the same traffic in TLS encryption "
                 "using a certificate, which stops passwords and session "
                 "cookies from being stolen on public Wi-Fi.")),
]

#: question_id -> spec (used to know what the demo student will type).
_ANSWER_KEY: dict[int, dict] = {}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def seed_demo() -> None:
    """Create the full demo dataset. Runs only on an empty database."""
    if User.query.count() or Exam.query.count():
        return

    users = {}
    for username, full_name, role, password, email in ACCOUNTS:
        user = User(username=username, full_name=full_name, role=role,
                    email=email)
        user.set_password(password)
        db.session.add(user)
        users[username] = user
    db.session.commit()

    now = utcnow()
    long_open = (now - timedelta(days=3), now + timedelta(days=30))

    exam_web = _exam(users["teacher"], "Web Fundamentals — Core Concepts",
                     "A short multiple-choice quiz. Nothing is proctored here — "
                     "compare it with the proctored exams.",
                     25, 3, WEB_QUESTIONS, start=long_open[0], end=long_open[1],
                     category="Web basics", pass_mark=50)
    exam_net = _exam(users["teacher"], "Networking Basics — Pooled Quiz",
                     "Each student gets 5 of 10 questions, in a random order, "
                     "with webcam snapshots.",
                     30, 2, NETWORK_QUESTIONS, pool=5, webcam=True,
                     start=now - timedelta(days=1), end=now + timedelta(days=7),
                     category="Networking")
    exam_db = _exam(users["teacher"], "Database Systems — Proctored Midterm",
                    "Fullscreen + webcam. Warnings are sent to your teacher "
                    "live; exams are never closed automatically. Results are "
                    "published after every attempt has been reviewed.",
                    40, 3, DB_QUESTIONS, webcam=True,
                    start=now - timedelta(days=2), end=now + timedelta(days=10),
                    category="Databases", show_result=False, pass_mark=50)
    exam_physics = _exam(users["teacher"], "Introduction to Physics",
                         "Opens in two days.", 20, 3, PHYSICS_QUESTIONS,
                         start=now + timedelta(days=2), end=now + timedelta(days=5),
                         category="Physics")
    exam_gk = _exam(users["teacher"], "General Knowledge — Week 1",
                    "Closed yesterday.", 15, 3, GENERAL_QUESTIONS,
                    start=now - timedelta(days=2), end=now + timedelta(days=2),
                    category="General")
    exam_chem = _exam(users["teacher"], "Chemistry Midterm (draft)",
                      "Still being prepared.", 30, 3, CHEMISTRY_QUESTIONS,
                      publish=False, category="Chemistry")
    exam_os = _exam(users["teacher2"], "Operating Systems Basics",
                    "Quiz by Ben Teacher — Ada cannot see or manage it.",
                    15, 3, OS_QUESTIONS,
                    start=now - timedelta(days=1), end=now + timedelta(days=7),
                    category="Operating systems")
    exam_typing = _exam(users["teacher"],
                        "Technical Writing — Typed Answers",
                        "Mixed question types: single/multiple answers, "
                        "true/false and typed answers. Typed answers are "
                        "checked by AI, but only your teacher's approved "
                        "marks count.",
                        30, 3, TYPED_QUESTIONS,
                        start=now - timedelta(days=1), end=now + timedelta(days=6),
                        category="Writing skills", pass_mark=50)
    exam_hand = _exam(users["teacher"], "Handwritten Solutions",
                      "Answer the questions on paper, then upload a photo or "
                      "PDF of your work within 60 minutes of submitting.",
                      45, 3, HAND_QUESTIONS,
                      start=now - timedelta(days=3), end=now + timedelta(days=4),
                      category="Mathematics", upload_window=60, pass_mark=40)
    exam_retake = _exam(users["teacher"], "Practice Retakes (code + retry)",
                        "Access code LETMEIN, two attempts, negative marking "
                        "and one question at a time.",
                        10, 4, RETAKE_QUESTIONS,
                        start=now - timedelta(days=1), end=now + timedelta(days=10),
                        category="General", attempts=2, access_code="LETMEIN",
                        negative=True, one_at_a_time=True, pass_mark=50)
    # Created LAST so it is the first card (and the first free "Start form")
    # on the demo student's dashboard. Five attempts: a playground for trying
    # the anti-cheat (and repeated smoke-test runs) before a reseed.
    exam_practice = _exam(users["teacher"], "Open Practice Quiz",
                          "A fresh, untouched exam — start it and play with "
                          "the anti-cheat features.",
                          15, 3, PRACTICE_QUESTIONS,
                          start=now - timedelta(hours=1), end=now + timedelta(days=30),
                          category="General", attempts=5)

    # ------------------------------------------------------------------
    # Exam 1 — clean runs + the collusion pair
    # ------------------------------------------------------------------
    a = _start(exam_web, users["student"])
    _answer_all_correct(a, exam_web)
    _submit(a)
    _backdate(a, now - timedelta(days=2), now - timedelta(days=2) + timedelta(minutes=8))

    # student2 and student3 give identical answers (the collusion demo):
    # both wrong on questions 3, 7, 10 and 12, correct on the other eight.
    wrong_ids = {exam_web.questions[i].id for i in (2, 6, 9, 11)}
    for username, minutes_ago in (("student2", 190), ("student3", 175)):
        attempt = _start(exam_web, users[username])
        originals = {}
        for q in exam_web.questions:
            originals[q.id] = _wrong(q) if q.id in wrong_ids else q.correct_index
        services.save_answers_bulk(attempt, _display(attempt, originals))
        _spread_answer_times(attempt, seconds_per_question=14)
        if username == "student2":
            # A 0.7s focus loss: proof is stored, no warning counted (grace).
            # NOTE: recorded BEFORE submission — violations are in-exam only.
            services.record_violation(attempt, "tab_switch",
                                      question_id=attempt.question_order[0],
                                      duration_ms=700)
            db.session.commit()
        _submit(attempt)
        _backdate(attempt, now - timedelta(minutes=minutes_ago),
                  now - timedelta(minutes=minutes_ago) + timedelta(minutes=6))

    # ------------------------------------------------------------------
    # Exam 2 — one attempt LEFT RUNNING (live monitor demo) ...
    # ------------------------------------------------------------------
    running = _start(exam_net, users["student"])
    pool = [q for q in exam_net.questions if q.id in running.question_order]
    services.save_answers_bulk(running, _display(running, {
        pool[0].id: pool[0].correct_index,
        pool[1].id: _wrong(pool[1]),
    }))
    _spread_answer_times(running, seconds_per_question=22)
    services.record_violation(running, "tab_switch",
                              question_id=running.question_order[1],
                              duration_ms=5200)
    db.session.commit()
    # Backdate the start but keep the attempt alive with ~20 minutes left.
    _backdate(running, now - timedelta(minutes=10))
    running.last_heartbeat = utcnow()
    db.session.commit()

    # ... and one attempt that collected every webcam-related violation.
    cam = _start(exam_net, users["student2"])
    pool = [q for q in exam_net.questions if q.id in cam.question_order]
    originals = {pool[0].id: pool[0].correct_index, pool[1].id: pool[1].correct_index,
                 pool[2].id: _wrong(pool[2]), pool[3].id: pool[3].correct_index,
                 pool[4].id: _wrong(pool[4])}
    services.save_answers_bulk(cam, _display(cam, originals))
    _spread_answer_times(cam, seconds_per_question=30)

    snap_ok = _snapshot(cam, 1, "periodic")
    snap_noface = _snapshot(cam, 0, "violation")
    services.record_violation(cam, "no_face", detail={"face_count": 0},
                              snapshot_id=snap_noface.id)
    snap_two = _snapshot(cam, 2, "violation")
    services.record_violation(cam, "multiple_faces", detail={"face_count": 2},
                              snapshot_id=snap_two.id)
    services.record_violation(cam, "low_light", detail={"brightness": 12})
    services.record_violation(cam, "face_check_unavailable",
                              detail={"reason": "model_load_failed"})
    _snapshot(cam, 1, "periodic")
    _submit(cam)
    _backdate(cam, now - timedelta(days=1), now - timedelta(days=1) + timedelta(minutes=11))

    # ------------------------------------------------------------------
    # Exam 3 — the flagship HIGH-RISK cheat report (results held back)
    # ------------------------------------------------------------------
    cheat = _start(exam_db, users["student"])
    _answer_some(cheat, exam_db, correct=6)   # 6/8 right, so grading looks real
    services.send_teacher_message(cheat, "This is your final warning — "
                                         "please stay on the exam tab.")

    # Identity photo taken at the start (compared with the profile photo).
    _snapshot(cheat, 1, "identity")
    _snapshot(cheat, 1, "periodic")
    services.record_violation(cheat, "tab_switch", question_id=cheat.question_order[0],
                              duration_ms=4500)
    snap = _snapshot(cheat, 0, "violation")
    services.record_violation(cheat, "no_face", detail={"face_count": 0},
                              snapshot_id=snap.id)
    services.record_violation(cheat, "fullscreen_exit")
    services.record_violation(cheat, "copy_paste", detail={"key": "ctrl+c"})
    snap = _snapshot(cheat, 2, "violation")
    services.record_violation(cheat, "multiple_faces", detail={"face_count": 2},
                              snapshot_id=snap.id)
    services.record_violation(cheat, "tab_switch", question_id=cheat.question_order[2],
                              duration_ms=6200)
    # The anti-cheat script went silent for 2+ minutes: the server noticed.
    cheat.last_heartbeat = utcnow() - timedelta(seconds=134)
    services.heartbeat(cheat)
    services.record_violation(cheat, "low_light", detail={"brightness": 9})
    _snapshot(cheat, 1, "periodic")
    _submit(cheat)
    _backdate(cheat, now - timedelta(days=1), now - timedelta(days=1) + timedelta(minutes=13))

    # A calmer run on the same exam (medium risk) for comparison.
    calm = _start(exam_db, users["student2"])
    _answer_some(calm, exam_db, correct=5)
    services.record_violation(calm, "tab_switch", question_id=calm.question_order[0],
                              duration_ms=9000)
    services.record_violation(calm, "tab_switch", question_id=calm.question_order[1],
                              duration_ms=7700)
    services.record_violation(calm, "copy_paste", detail={"key": "printscreen"})
    _snapshot(calm, 1, "periodic")
    _submit(calm)
    _backdate(calm, now - timedelta(days=1) - timedelta(hours=1),
              now - timedelta(days=1) - timedelta(hours=1) + timedelta(minutes=9))

    # ------------------------------------------------------------------
    # Exam 5 — closed yesterday, ended BY THE TEACHER
    # ------------------------------------------------------------------
    gk = _start(exam_gk, users["student"])
    _answer_some(gk, exam_gk, correct=4)
    services.force_submit(gk, users["teacher"])
    _backdate(gk, now - timedelta(days=2) + timedelta(hours=3),
              now - timedelta(days=2) + timedelta(hours=4))

    # ------------------------------------------------------------------
    # Exam 7 — the other teacher's exam (permission demo)
    # ------------------------------------------------------------------
    os_attempt = _start(exam_os, users["student"])
    _answer_some(os_attempt, exam_os, correct=2)
    _submit(os_attempt)
    _backdate(os_attempt, now - timedelta(days=3),
              now - timedelta(days=3) + timedelta(minutes=4))

    # ------------------------------------------------------------------
    # Exam 8 — mixed types + AI marking + approval + re-check request
    # ------------------------------------------------------------------
    typed = _start(exam_typing, users["student"])
    _mixed_answers(typed, exam_typing, correct=3)
    _submit(typed)          # grades the objective parts, queues the AI job
    _drain_jobs()           # run the marking job now (deterministic seed)

    # Teacher approves the short answer (approved marks count), leaves the
    # long one pending so the review UI has something to act on.
    short_q = next(q for q in exam_typing.questions if q.type == "short_text")
    row = GradingResult.query.filter_by(attempt_id=typed.id,
                                        question_id=short_q.id).first()
    if row is not None:
        services.approve_grading(row, row.suggested_marks, "approve",
                                 users["teacher"])
    services.add_student_note(
        typed, "Could you re-check my long answer? I mentioned TLS and "
               "certificates.", kind="recheck")
    _backdate(typed, now - timedelta(days=1) - timedelta(hours=2),
              now - timedelta(days=1) - timedelta(hours=2) + timedelta(minutes=11))

    # RAG material for this exam (chunked + embedded via the job pipeline).
    notes_path = _write_resource_file(exam_typing,
                                      "HTTP: plain-text protocol on port 80.\n"
                                      "HTTPS: HTTP wrapped in TLS encryption, "
                                      "port 443, uses certificates.\n"
                                      "HTML: HyperText Markup Language, the "
                                      "structure of web pages.\n"
                                      "CSS: Cascading Style Sheets, controls "
                                      "how pages look.\n"
                                      "JavaScript: adds behaviour; data types "
                                      "include String, Boolean and Number.")
    resource = Resource(exam_id=exam_typing.id,
                        title="Course notes — web basics", file_path=str(notes_path))
    db.session.add(resource)
    db.session.commit()
    from .ai import enqueue as _enqueue
    _enqueue("ingest", {"resource_id": resource.id})
    _drain_jobs()

    # ------------------------------------------------------------------
    # Exam 9 — handwritten uploads: clean, duplicate, late + student note
    # ------------------------------------------------------------------
    paper = _handwritten_pdf()

    up = _start(exam_hand, users["student3"])
    _answer_some(up, exam_hand, correct=3)
    _submit(up)
    _backdate(up, now - timedelta(minutes=40), now - timedelta(minutes=35))
    services.save_upload(up, "my_answers.pdf", paper, is_pdf=True)

    # student uploads the SAME bytes while the window is still open →
    # SHA-256 duplicate proof + violation (never a submit).
    dup = _start(exam_hand, users["student"])
    _answer_some(dup, exam_hand, correct=2)
    _submit(dup)
    _backdate(dup, now - timedelta(minutes=30), now - timedelta(minutes=25))
    services.save_upload(dup, "my_answers.pdf", paper, is_pdf=True)
    services.add_student_note(
        dup, "Sorry — I scanned my answer sheet twice by accident, the file "
             "is identical on purpose.", kind="student")

    # student2 was too late: rejected, but the proof is recorded.
    late = _start(exam_hand, users["student2"])
    _answer_some(late, exam_hand, correct=2)
    _submit(late)
    _backdate(late, now - timedelta(days=2), now - timedelta(days=2) + timedelta(minutes=9))
    try:
        services.save_upload(late, "late_work.pdf", paper, is_pdf=True)
    except services.ExamError:
        pass  # expected: window closed (the violation row was recorded)

    _drain_jobs()           # OCR for the two stored uploads

    # ------------------------------------------------------------------
    # Exam 10 — retake: attempt 1 submitted, attempt 2 in progress
    # ------------------------------------------------------------------
    first = _start(exam_retake, users["student"],
                   access_code="LETMEIN")
    _answer_some(first, exam_retake, correct=3)   # 2 wrong → −0.25 each
    _submit(first)
    _backdate(first, now - timedelta(days=1) - timedelta(hours=3),
              now - timedelta(days=1) - timedelta(hours=3) + timedelta(minutes=6))

    second = _start(exam_retake, users["student"], access_code="LETMEIN")
    services.save_answers_bulk(second, _display(
        second, {second.question_order[0]: RETAKE_QUESTIONS[0][2]}))
    db.session.commit()

    # ------------------------------------------------------------------
    # Exam 11 (practice) stays untouched — the smoke test starts it.
    # ------------------------------------------------------------------

    # exam_gk really ended yesterday; it was held open only while seeding.
    exam_gk.scheduled_end = now - timedelta(hours=1)

    # ------------------------------------------------------------------
    # Admin-side demo content: settings, audit trail, profile photos
    # ------------------------------------------------------------------
    Setting.set("exam_defaults", {
        "violation_limit": 3, "duration_minutes": 30,
        "snapshot_retention_days": 7,
    })
    db.session.commit()

    _profile_photo(users["teacher"], (58, 84, 160))
    _profile_photo(users["student"], (60, 140, 90))

    _audit_trail(users)

    db.session.commit()
    _drain_jobs()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _category(name):
    if not name:
        return None
    existing = Category.query.filter_by(name=name).first()
    if existing:
        return existing
    category = Category(name=name)
    db.session.add(category)
    db.session.flush()
    return category


def _add_question(exam_id, spec, category) -> Question:
    """Build a Question from a tuple bank item or a dict spec."""
    if isinstance(spec, dict):
        qtype = spec.get("type", "mcq")
        options = spec.get("options")
        correct_index = spec.get("correct")
        correct_indices = None
        if qtype == "true_false":
            options = ["True", "False"]
            correct_index = int(spec["correct"])
        elif qtype == "multi":
            correct_indices = list(spec["correct"])
            correct_index = min(correct_indices)
        elif qtype in ("short_text", "long_text"):
            options = None
            correct_index = None
        question = Question(
            exam_id=exam_id, type=qtype, text=spec["text"],
            options=options, correct_index=correct_index,
            correct_indices=correct_indices,
            points=spec.get("points", 1),
            difficulty=spec.get("difficulty"),
            model_answer=spec.get("model"), rubric=spec.get("rubric"),
            category=category,
        )
        db.session.add(question)
        db.session.flush()
        if spec.get("answer"):
            _ANSWER_KEY[question.id] = spec
        return question

    text, options, correct = spec
    question = Question(exam_id=exam_id, type="mcq", text=text,
                        options=options, correct_index=correct,
                        category=category)
    db.session.add(question)
    db.session.flush()
    return question


def _exam(owner, title, description, duration, limit, bank, *, pool=0,
          webcam=False, start=None, end=None, publish=True,
          category=None, pass_mark=40, attempts=1, access_code=None,
          negative=False, show_result=True, one_at_a_time=False,
          upload_window=0) -> Exam:
    exam = Exam(
        title=title, description=description,
        duration_minutes=duration, violation_limit=limit, pool_size=pool,
        shuffle_questions=True, shuffle_options=True,
        require_fullscreen=True, webcam_enabled=webcam,
        pass_mark=pass_mark, attempts_allowed=attempts,
        access_code=access_code, negative_marking=negative,
        show_result=show_result, one_at_a_time=one_at_a_time,
        upload_window_minutes=upload_window,
        scheduled_start=start, scheduled_end=end, is_published=publish,
        created_by=owner.id,
    )
    db.session.add(exam)
    db.session.flush()
    cat = _category(category)
    for spec in bank:
        _add_question(exam.id, spec, cat)
    db.session.commit()
    return exam


def _start(exam, student, access_code=None) -> Attempt:
    attempt, _ = services.start_attempt(
        student.id, exam,
        ip_address=STUDENT_IPS.get(student.username, "192.168.1.50"),
        user_agent=FAKE_UA,
        access_code=access_code,
    )
    return attempt


def _wrong(q: Question) -> int:
    return (q.correct_index + 1) % len(q.options)


def _display(attempt, originals: dict[int, int]) -> dict[str, int]:
    """Convert {question_id: original option index} to display coordinates."""
    return {
        str(qid): attempt.option_map[str(qid)].index(orig)
        for qid, orig in originals.items()
    }


def _answer_all_correct(attempt, exam) -> None:
    originals = {
        q.id: q.correct_index
        for q in exam.questions if q.id in attempt.question_order
    }
    services.save_answers_bulk(attempt, _display(attempt, originals))
    _spread_answer_times(attempt, seconds_per_question=17)


def _answer_some(attempt, exam, correct: int) -> None:
    """Answer `correct` questions right, the rest wrong (shuffled choice)."""
    originals = {}
    for i, qid in enumerate(attempt.question_order):
        q = next(x for x in exam.questions if x.id == qid)
        if q.options is None:      # typed answer — handled by _mixed_answers
            continue
        originals[qid] = q.correct_index if i < correct else _wrong(q)
    services.save_answers_bulk(attempt, _display(attempt, originals))
    _spread_answer_times(attempt, seconds_per_question=26)


def _mixed_answers(attempt, exam, correct: int = 3) -> None:
    """Answer a mixed-type paper: objective parts partly right, typed parts
    filled with the demo answers (what a real student would type)."""
    payload: dict[str, object] = {}
    for i, qid in enumerate(attempt.question_order):
        q = next(x for x in exam.questions if x.id == qid)
        perm = attempt.option_map[str(qid)]
        if q.type in ("mcq", "true_false"):
            original = q.correct_index if i < correct else _wrong(q)
            payload[str(qid)] = perm.index(original)
        elif q.type == "multi":
            good = list(q.correct_indices or [])
            chosen = good if i < correct else (good[:-1] or good[:1])
            payload[str(qid)] = sorted(perm.index(c) for c in chosen)
        else:
            spec = _ANSWER_KEY.get(qid) or {}
            payload[str(qid)] = spec.get("answer", "")
    services.save_answers_bulk(attempt, payload)
    _spread_answer_times(attempt, seconds_per_question=24)


def _spread_answer_times(attempt, seconds_per_question: int) -> None:
    """Give each answered question a believable first-answer timestamp."""
    t = attempt.started_at
    for answer in sorted(attempt.answers, key=lambda x: x.question_id):
        answered = (answer.selected_index is not None
                    or bool(answer.selected_indices)
                    or bool((answer.response or "").strip()))
        if answered:
            t += timedelta(seconds=seconds_per_question)
            answer.first_answered_at = t
    db.session.commit()


def _snapshot(attempt, face_count: int, reason: str) -> Snapshot:
    """Write a simulated webcam frame to disk and chain it as evidence."""
    folder = current_app.config["SNAPSHOT_DIR"] / str(attempt.id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = (f"seed_{attempt.id}_{reason}_{face_count}_"
                f"{len(list(folder.glob('*.jpg'))) + 1}.jpg")
    path = folder / filename
    path.write_bytes(_webcam_image(face_count))

    snapshot = Snapshot(attempt_id=attempt.id, file_path=str(path),
                        reason=reason, face_count=face_count)
    db.session.add(snapshot)
    db.session.flush()
    services.append_event(attempt, "snapshot", {
        "snapshot_id": snapshot.id, "reason": reason,
        "face_count": face_count, "file": filename,
    }, ref_type="snapshot", ref_id=snapshot.id)
    db.session.commit()
    return snapshot


def _webcam_image(face_count: int) -> bytes:
    """A small grey 'webcam frame' so the report has something to show."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow only feeds the demo
        return b"\xff\xd8\xff\xd9"  # minimal valid JPEG

    img = Image.new("RGB", (320, 240), (44, 48, 60))
    draw = ImageDraw.Draw(img)
    # Camera vignette.
    for i in range(12):
        draw.rectangle([i, i, 319 - i, 239 - i], outline=(50 + i, 54 + i, 66 + i))

    faces = [(160, 120, 46)] if face_count == 1 else \
            [(112, 116, 38), (216, 122, 34)] if face_count >= 2 else []
    for cx, cy, r in faces:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(196, 156, 122))
        draw.ellipse([cx - 16, cy - 12, cx - 6, cy - 2], fill=(40, 34, 30))
        draw.ellipse([cx + 6, cy - 12, cx + 16, cy - 2], fill=(40, 34, 30))
        draw.arc([cx - 16, cy + 4, cx + 16, cy + 26], 20, 160, fill=(120, 84, 66), width=3)

    label = "SIMULATED WEBCAM — demo data"
    sub = f"faces detected: {face_count}"
    draw.text((14, 14), label, fill=(220, 224, 235))
    draw.text((14, 214), sub, fill=(180, 186, 200))
    if face_count == 0:
        draw.text((14, 30), "!! NO FACE !!", fill=(255, 120, 120))
    elif face_count > 1:
        draw.text((14, 30), "!! MULTIPLE FACES !!", fill=(255, 170, 90))

    buf = BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _handwritten_pdf() -> bytes:
    """A one-page 'scanned answer sheet' PDF (PIL writes PDFs natively)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
                b"trailer<</Root 1 0 R>>\n%%EOF")

    img = Image.new("RGB", (800, 1130), (250, 248, 240))
    draw = ImageDraw.Draw(img)
    draw.text((60, 50), "Handwritten Solutions — Demo Student sheet",
              fill=(30, 30, 40))
    y = 110
    for i, line in enumerate(
        ["1) 17 is prime (only divisible by 1 and itself)",
         "2) d/dx (x^2) = 2x",
         "3) 1 km = 1000 m",
         "4) pi = 3.14",
         "",
         "Working:",
         "   17 / 1 = 17,  17 / 17 = 1,  no other divisors",
         "   x^2 -> 2*x (power rule)",
         "   1000 meters in a kilo-meter",
         "   circle / diameter = 3.14159..."],
        start=1,
    ):
        # Fake handwriting: slightly wavy underline under each line.
        draw.text((70, y), line, fill=(40, 44, 90))
        draw.line([(70, y + 16), (70 + 8 * len(line), y + 14)],
                  fill=(120, 130, 180), width=1)
        y += 42

    buf = BytesIO()
    img.save(buf, "PDF", resolution=100.0)
    return buf.getvalue()


def _write_resource_file(exam, text: str):
    """Store typed RAG notes as a UTF-8 file (uniform ingestion path)."""
    from pathlib import Path
    folder = current_app.config["UPLOAD_DIR"] / "resources"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"seed_notes_{exam.id}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _profile_photo(user, rgb: tuple[int, int, int]) -> None:
    """Generate and store a simple profile photo (identity check reference)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        return
    from pathlib import Path

    img = Image.new("RGB", (160, 160), rgb)
    draw = ImageDraw.Draw(img)
    draw.ellipse([50, 30, 110, 90], fill=(226, 197, 166))     # head
    draw.pieslice([28, 92, 132, 200], 180, 360, fill=(240, 240, 245))  # shoulders
    draw.text((10, 145), f"{user.username} — demo photo", fill=(235, 238, 245))

    folder = current_app.config["PROFILE_DIR"]
    folder.mkdir(parents=True, exist_ok=True)
    # Deterministic name: re-seeding overwrites the same file instead of
    # leaving one photo behind per run.
    path = folder / f"user_{user.id}_demo.jpg"
    img.save(path, "JPEG", quality=80)
    user.photo_path = str(path)
    db.session.commit()


def _audit_trail(users: dict) -> None:
    """Backdated admin audit-log entries (login/role/publish/backup...)."""
    from .models import AuditLog
    now = utcnow()
    entries = [
        ("admin", "login", "", 5 * 24 * 60),
        ("admin", "user_created", "student3=Third Student", 4 * 24 * 60),
        ("teacher", "login", "", 3 * 24 * 60),
        ("teacher", "publish", "Web Fundamentals — Core Concepts", 3 * 24 * 60 - 40),
        ("admin", "role_changed", "teacher2: teacher -> teacher", 2 * 24 * 60),
        ("teacher", "login", "", 26 * 60),
        ("student", "login", "", 25 * 60),
        ("admin", "weights_updated", "tab_switch=3, fullscreen_exit=3, ...", 20 * 60),
        ("admin", "database_backup", "exam_backup.db", 12 * 60),
        ("teacher", "login", "", 90),
    ]
    for username, action, detail, minutes_ago in entries:
        user = users.get(username)
        entry = AuditLog(
            user_id=user.id if user else None,
            username=username if user else None,
            action=action, detail=detail,
            ip_address="192.168.1.10",
            created_at=now - timedelta(minutes=minutes_ago),
        )
        db.session.add(entry)
    db.session.commit()


def _drain_jobs() -> None:
    """Run any queued background jobs now (deterministic demo data)."""
    from .ai import jobs as ai_jobs
    from .models import Job

    for job in Job.query.filter_by(status="queued").order_by(Job.id).all():
        ai_jobs._execute(job.id)
    db.session.commit()


def _submit(attempt) -> None:
    services.submit_attempt(attempt, by="student")


def _backdate(attempt, started, submitted=None) -> None:
    """Shift an attempt (and everything inside it) into the past.

    Used so the demo history looks realistic. Chain integrity is unaffected:
    event hashes cover payloads, not timestamps.
    """
    from .models import Answer, Violation
    delta = started - attempt.started_at
    attempt.started_at = started
    attempt.deadline += delta
    if attempt.last_heartbeat:
        attempt.last_heartbeat += delta
    for answer in Answer.query.filter_by(attempt_id=attempt.id).all():
        if answer.first_answered_at:
            answer.first_answered_at += delta
    for violation in Violation.query.filter_by(attempt_id=attempt.id).all():
        violation.created_at += delta
    for snap in attempt.snapshots:
        snap.created_at += delta
    for message in attempt.messages:
        message.created_at += delta
    if submitted and attempt.status == "submitted":
        attempt.submitted_at = submitted
    db.session.commit()
