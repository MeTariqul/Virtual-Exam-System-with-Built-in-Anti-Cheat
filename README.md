# Virtual Exam System with Built-in Anti-Cheat

A web application where teachers create and schedule exams and students take them
online. Marks are graded instantly, and a browser-based anti-cheat module records
evidence of suspicious behaviour — it **never** auto-submits; it stores proof and
alerts both the student and the teacher. Only the teacher (or the timer, or the
student's own submit) can end an attempt.

Built with **HTML, CSS, JavaScript, Python (Flask) and SQLite** — no build step,
no external services. Typed answers, handwritten uploads and course materials also
get an optional **AI pipeline** (OCR · retrieval · LLM marking) that runs in
mock mode out of the box and upgrades in place when you install the extras.

## Quick start

```bash
pip install -r requirements.txt
python run.py
```

Open **http://127.0.0.1:5000** and sign in:

| Role    | Username    | Password     | What it demonstrates |
|---------|-------------|--------------|----------------------|
| Admin   | `admin`     | `admin123`   | Users, risk weights, audit log |
| Teacher | `teacher`   | `teacher123` | 10 exams, question bank, reports, AI marking, live monitor |
| Teacher | `teacher2`  | `teacher123` | A second teacher's exams (permissions) |
| Student | `student`   | `student123` | Clean runs, a high-risk cheat report, retakes |
| Student | `student2`  | `student123` | Collusion pair, grace-window run, uploads |
| Student | `student3`  | `student123` | Collusion pair, duplicate upload |

The first run creates the database and seeds a full demo dataset: **11 exams**
(~65 questions) covering every state the system can be in — open, scheduled,
closed, draft, pooled, proctored, results-held-back, upload-window, access-code,
one-question-at-a-time — plus 15 finished/running attempts with violations,
snapshots, AI gradings, uploads and intact evidence chains.

To rebuild the demo dataset from scratch at any time:

```bash
python -m flask --app run reseed-demo --yes
```

### Try the anti-cheat in 60 seconds

1. Sign in as `student`, start **Open Practice Quiz**.
2. Switch browser tabs → a warning banner appears and the teacher's live
   monitor logs the violation (the exam keeps running — no auto-submit).
3. Leave fullscreen → another violation is recorded.
4. Copy/paste and right-click are blocked and logged.
5. Sign in as `teacher` in another browser → **Live** shows the violation feed
   in real time; **Results → report** shows the timeline, risk score, webcam
   snapshots and the intact hash-chain badge.

## Features

### Exams & questions

- Three roles (Admin / Teacher / Student) with role-based pages and JSON API
- **Five question types**: single-choice, multiple-answer, true/false,
  short text and long text (the last two carry a model answer + rubric)
- **Question pools**: each student gets N of M questions, shuffled, with
  shuffled options (shared answers become useless)
- Scheduling (start/end), duration, violation limit, pass mark,
  **attempts allowed** + attempt counter, **access codes**, negative marking
- **One question at a time** mode with a navigator and 🚩 flags
- **Server-side timer**: start/deadline live on the server; refreshing does not
  add time (a 5-second grace covers submit latency). If a student closes the
  browser and never returns, the server **auto-expires** the attempt at the
  deadline (graded as `submitted_by=timer`), so it never lingers "in progress"
- Auto-save every ~20 s; a crash or refresh resumes the attempt with the
  remaining server time
- Question **CSV bulk import** and a shared **question bank**:
  write standalone bank questions of their own, tick bank questions on the
  create-exam form (they are **copied in — originals stay untouched**),
  write new questions in the same form, filter the bank list by keyword, and
  **export the whole bank back to CSV** (round-trips with the importer)
- **Duplicate an exam** in one click: settings + questions are deep-copied
  into a new *draft* (attempts and resources are not carried over)
- **Creating an exam is all-or-nothing**: every typed question is validated
  before anything is saved, so one mistake brings the whole form back with
  everything you had already entered — bank ticks, checkbox states and all
- Grace windows: the first ~2 s of focus loss are ignored, and a dark room is a
  warning — not a violation

### Grading & results

- Instant, type-aware grading; negative marking for wrong answers;
  percent formatted cleanly; pass/fail against the exam's pass mark
- **AI marking for typed answers**: the engine *suggests* marks (similarity to
  the model answer + rubric coverage + confidence). Nothing counts until the
  teacher **approves** it — rejected/approved rows, re-grade button, and the
  student sees only the final approved marks
- Students can leave an explanation or request a **re-check**; teachers re-grade
- Exam history with per-attempt percent; **CSV export** (teacher results,
  student history) and a **PDF result sheet** (fpdf2, Unicode-safe)
- **Question analytics**: difficulty (facility), **discrimination index**
  (upper-vs-lower 27% split) and average time per question in the results
  table; **collusion detection** compares answer sequences between students
  and is reported as "possible answer sharing — review", never as proof

### Anti-cheat (evidence, not punishment)

- Tab switch / window-focus loss detection with a grace window
- Fullscreen enforcement; copy, paste, right-click and dev-tools shortcuts
  blocked and logged
- Webcam snapshots every 45 s plus a first **identity shot** at the start,
  with in-browser face checks (face-api.js): missing or multiple faces are
  recorded; the cheat report shows your profile photo next to the identity
  shot for comparison
- **Violations never auto-submit.** Each one stores proof (timestamp, question,
  snapshot, chain entry), alerts the student and pushes a live alert to the
  teacher. Only the teacher's force-submit, the timer, or the student's own
  submit ends an attempt
- Tamper-evident audit log: every answer save, violation and snapshot joins a
  SHA-256 hash chain; disabling the monitoring script is itself recorded as a
  `monitor_gap` violation. `flask verify-chains` recomputes every chain
- One session per student: a second login kicks the first device
- Per-attempt **risk score** (bands: 0–5 low, 6–12 medium, 13+ high) with
  contextual weighting — a tab switch on a question most students take >30 s
  on counts 1.5×:

  | Violation | Weight | | Violation | Weight |
  |---|---|---|---|---|
  | `tab_switch` | 3 | | `no_face` | 2 |
  | `fullscreen_exit` | 3 | | `multiple_faces` | 4 |
  | `window_blur` | 3 | | `monitor_gap` | 5 |
  | `copy_paste` | 1 | | `late_upload` | 1 |
  | `duplicate_file` | 2 | | `short_focus_loss`, `low_light`, `face_check_unavailable` | 0 (evidence only) |

  Every weight is editable by the admin (**Settings**), no restart needed.
  The score guides review — it is not a verdict.

### Live teacher tools (Server-Sent Events)

- **Live monitor**: active attempts, countdowns, warning counts and an instant
  violation feed; send a message, extend time (+5 min), or force-submit
- **Cheat report per attempt**: timeline with weights, risk score, webcam
  gallery, identity reference, answers, chain-integrity badge, AI marking
  status, student notes and handwritten submissions

### Handwritten solutions

- Students upload photos/PDF of their work inside the exam's **upload window**
- SHA-256 file hashing flags **duplicate** files across attempts and **late**
  uploads (rejected with proof recorded) — both feed the risk score
- Pages are stored, rendered for review and run through **OCR** (text
  extraction with confidence), viewable by the teacher

### AI pipeline (mock-first, works offline)

The proposal's AI parts are built as a pluggable pipeline in `app/ai/`:

- **OCR** — PDFs are read as text directly; image-only pages fall back to a
  TrOCR-style model when you install the extras
- **RAG** — teachers upload course material (PDF/notes) on the exam's
  *Resources* page; it is chunked and embedded, and retrieved chunks give the
  marker extra context
- **Marking** — heuristic similarity + rubric coverage by default; swap in a
  local **Ollama** LLM (`ollama pull llama3.2`) for real model marking
- **Jobs** — OCR / ingest / marking run in a background worker with a status
  list on the Resources page (or inline in tests)

Out of the box everything runs in **mock mode** — deterministic, offline, no
downloads. Upgrade when you want the real engines:

```bash
pip install -r requirements-ai.txt     # sentence-transformers, torch,
                                       # transformers, chromadb, ollama
```

Useful variables: `EXAM_AI_MOCK=1` (force mock), `EXAM_AI_INLINE=1` (run jobs
synchronously). Model names live in `instance/ai_config.json` — the file is
created with the defaults on first run; edit it and restart to swap models
(environment variables still win over the file).

### Admin tools

- Create users, change roles, reset passwords, delete accounts
- Edit every **risk weight** live; view the **audit log** (who did what, when,
  from which IP) — the raw action code is kept in a `data-action` attribute
  so entries stay greppable/scriptable next to their human-readable label
- Overview of every exam and attempt, with an aggregate **stats strip**
  (exams, attempts, AI marks awaiting review, re-check requests) on the
  teacher dashboard
- CLI commands: `reseed-demo`, `verify-chains`, `cleanup-snapshots`,
  `expire-attempts`, `seed-demo`, `init-db`

### Security hardening

- Hardened HTTP headers on every response: `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`, a `Permissions-Policy` that
  only grants the camera (no mic/geolocation/payment), and `Cache-Control:
  no-store` so answers and reports are never served from a shared cache
- Brute-force lockout after repeated failed logins (per account)

### UX

- **Modern "Iris" design system** (plain CSS, no build step): gradient brand
  accents, glass top bar with pill navigation and active-page highlighting,
  generous radii, soft tinted surfaces and calm typography across every page
- **Dark mode** (toggle in the top bar, remembered per browser) restyled to
  match the light theme token-for-token
- Accessible by default: skip-to-content link, landmark roles, labelled exam
  controls, focus traps on the exam gates, an accessible confirm dialog and
  toast notifications instead of native alerts
- Profile photos, display names, password change
- Flash guidance everywhere the student can get stuck (fullscreen, webcam
  consent, access codes, expired attempts)

## Tests

```bash
pip install pytest
python -m pytest tests/ -q      # 66 tests: flows, anti-cheat, grading, chain,
                                # CSV, PDF, AI marking, collusion, seed data,
                                # discrimination, timer expiry, duplication
python smoke_test.py            # 9 end-to-end checks against a running server
python -m flask --app run verify-chains   # recompute every evidence chain
```

The pytest suite is **seed-independent** (each test builds its own exams), plus
one test that guards the demo dataset itself.

## Project layout

```
run.py                  entry point (python run.py)
app/
  __init__.py           app factory, filters, CLI commands
  models.py             18 tables: User, Exam, Question, Attempt, Answer,
                        Violation, Snapshot, Message, Event (hash chain),
                        Category, Submission, Resource/Chunk, GradingResult,
                        Job, Setting, AuditLog, ...
  services.py           attempt lifecycle, violations, heartbeats, uploads,
                        grading hooks, audit, chain
  grading.py            type-aware grading, risk score + context, analytics,
                        collusion signals
  security.py           login session, roles, CSRF
  events.py             in-process pub/sub for Server-Sent Events
  seed.py               demo accounts + 11-exam dataset
  pdf_export.py         Unicode-safe PDF result sheet (fpdf2)
  ai/                   mock-first AI pipeline: config, ocr, rag, marking, jobs
  routes/               auth, student, teacher, admin, JSON API
  templates/            Jinja2 pages (teacher/_question_fields.html is the
                        shared question editor used by the exam, exam-questions
                        and bank forms)
  static/               css/style.css + extra.css, js/exam.js, js/monitor.js,
                        js/question-form.js (question blocks + bank filter)
tests/test_app.py          pytest suite (57 tests)
tests/test_improvements.py pytest suite (9 feature tests)
smoke_test.py           end-to-end check against a running server
requirements.txt        core dependencies
requirements-ai.txt     OPTIONAL AI upgrades (see above)
instance/               exam.db, snapshots/, uploads/, profiles/,
                        ai_config.json (created at runtime)
plan.md                 project plan
Virtual_Exam_System_Proposal_Latest.md   the proposal this was built from
```

## Configuration

Environment variables (optional):

- `EXAM_SECRET_KEY` — session signing key (random per start if unset;
  set it in production so restarts do not log everyone out)
- `EXAM_AI_MOCK=1` / `EXAM_AI_INLINE=1` — AI pipeline modes (above)
- `instance/ai_config.json` — AI model names; created with the defaults on
  first run, edit it and restart to swap models (env vars still win)
- `EXAM_PDF_FONT` — path to a Unicode TTF for the PDF export
  (Arial/DejaVu are auto-detected; the export degrades gracefully without)
- Snapshot retention: `SNAPSHOT_RETENTION_DAYS` (default 7) — expired photos
  are deleted at startup; run `flask --app run cleanup-snapshots` any time

## Honest limitations

Browser-based proctoring can discourage and document cheating, not prevent it:
a second phone, a person nearby, or a virtual machine are outside the
browser's reach, and false alarms are possible (notifications, bad lighting).
The identity check is a visual comparison for the teacher, not biometric
matching, and AI marks are suggestions until approved by a teacher. That is
why this system records evidence and leaves every decision — including ending
an attempt — to the teacher.
