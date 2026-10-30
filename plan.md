# Virtual Exam System with Built-in Anti-Cheat — Project Plan (v5)

> Source: `Virtual_Exam_System_Proposal_v4.pdf` + prioritized improvements (this revision)

> **STATUS — built.** The delivered system follows
> `Virtual_Exam_System_Proposal_Latest.md`: all core and upgrade objectives
> below are implemented (incl. the AI pipeline — OCR, RAG and LLM marking,
> mock-first), the no-auto-submit policy is enforced everywhere, and the
> demo dataset, tests and exports are described in `README.md`.
> Items once listed under *Future Work* (true/false & short-answer types,
> PDF result export) are now built. Superseded detail in this document is
> kept for the record.

## 1. Overview

A web application where teachers create and schedule multiple-choice exams and students
take them online from a browser. Marks are calculated automatically, so the result is ready
as soon as the student submits.

The core of the project is the **anti-cheat module**, upgraded from the original proposal
from simple event counting to an **evidence + analytics** approach:

- Record tab switching, leaving fullscreen, copy/paste attempts and (optionally) webcam
  snapshots — each with a timestamp — in a **tamper-evident, hash-chained log**.
- **Never auto-submit**: a violation is proven, stored and pushed as a live alert — the
  student sees a warning and the teacher is notified immediately. Only the teacher can
  end an attempt.
- Score violations **contextually** (a violation during a hard question weighs more).
- Detect **collusion patterns** by comparing answer sequences and timing across students.
- Give teachers a **live invigilation dashboard** (SSE) instead of only post-mortem reports.

**Positioning (unchanged, and important):** deter, detect and document — never auto-convict.
No browser-based system stops cheating completely; the final decision stays with the teacher.

**Tech stack:** HTML, CSS, JavaScript (frontend) · Python with Flask (backend) ·
SQLite with Flask-SQLAlchemy (database) · Werkzeug (password hashing) · face-api.js
(in-browser face detection) · Server-Sent Events (live dashboard) · SHA-256 (audit chain) ·
Chart.js (analytics charts).

## 2. Problem Statement

- Students can leave the exam page and search for answers elsewhere.
- Identical questions in the same order let answers be shared easily.
- Browser-only timers can be manipulated (refresh / clock changes) to gain extra time.
- Without recorded evidence, teachers cannot prove cheating happened.
- Commercial proctoring tools are expensive, require extra software, and raise privacy
  concerns.

## 3. Objectives

### Core objectives (proposal v4, adapted in v5)

1. Login system with three roles: **Admin, Teacher, Student**.
2. Teachers create, edit and schedule exams with multiple-choice questions.
3. Timed exams with automatic grading and instant results.
4. Detect and record tab switching, loss of window focus, exit from fullscreen and
   copy-paste attempts.
5. Shuffle questions **and** options differently for each student.
6. Server-side timer validation so the timer cannot be changed from the browser.
7. **No auto-submit** (changed from v4): store proof of every violation (timestamp,
   snapshot, hash-chained log entry) and send real-time alerts to **both** the student
   (in-exam warning) and the teacher (live dashboard notification). The exam continues
   until the timer expires, the student submits, or the teacher manually force-submits.
8. Webcam snapshots with missing/multiple face detection (optional module).
9. Per-attempt teacher report: violation list, risk score, snapshots.
10. Block a second login for a student who already has an active session.
11. Student exam history with CSV export.

### Upgraded objectives (new in v5)

12. **Question pools**: pick N questions randomly from a pool of M, so no two exams are
    identical even before shuffling.
13. **Tamper-evident audit log**: every event stores the hash of the previous event plus a
    server-signed session token; deleting or disabling the monitoring script breaks the
    chain and is itself flagged as a high-risk event.
14. **Session resilience**: answers auto-saved server-side every ~20 s; a crash or refresh
    restores the attempt with the server-computed remaining time (also kills the
    "refresh for extra minutes" cheat).
15. **Live invigilation dashboard**: teacher sees active attempts, question progress, live
    violation stream and countdowns in real time (SSE), and can send a warning message,
    extend time or force-submit a student.
16. **Contextual risk scoring**: violation weights adjusted by question difficulty and
    timing context, instead of flat per-type weights.
17. **Collusion detection**: flag pairs of students whose answer sequences or timing
    fingerprints are suspiciously similar (pure SQL/Python math, no ML).
18. **Privacy-first proctoring**: face detection runs entirely in the browser; only
    metadata is sent to the server; snapshots taken only on violation events, stored
    encrypted and auto-deleted after N days; visible student-facing consent screen.
19. **False-alarm reduction**: grace window for brief focus loss (e.g. first 2 s ignored)
    and low-light warning instead of an instant "no face" violation.
20. **Assessment analytics**: question difficulty (facility) index, discrimination index
    and time-per-question heatmap for the teacher.

## 4. System Architecture (3 layers)

| Layer | Contents |
|---|---|
| **Presentation** | Browser pages (admin, teacher, student) + anti-cheat script + face-api.js |
| **Application** | Flask backend: login, exams, grading, anti-cheat rules, live event stream (SSE), analytics, reports (REST API) |
| **Data** | SQLite database + encrypted snapshot folder (auto-deletion job) |

## 5. Roles & Pages

- **Admin** — manages users and roles; sees all exams and results.
- **Teacher** — creates exams (with question pools), adds questions, sets duration,
  schedule and violation limit; monitors live attempts; later checks results, cheat
  reports and analytics.
- **Student** — logs in, sees open exams, attempts them with a countdown timer (resumable
  after a crash), and sees the result immediately.

## 6. Anti-Cheat Features

| Feature | What it does |
|---|---|
| Tab switch / blur detection | Records a violation when the student leaves the exam tab or window |
| Forced fullscreen | Exam runs in fullscreen; leaving it is recorded |
| Copy, paste & right-click block | Disables copying, pasting, right-click and common shortcuts |
| Violation alerts, no auto-submit | Every violation shows the student a warning and pushes a real-time alert to the teacher; the exam keeps running |
| Escalating notifications | Past the limit (default 3) the student sees "your teacher has been notified" and the teacher's dashboard flags the attempt as over-limit — only the teacher may force-submit |
| Question pools + shuffling | N-of-M questions sampled per student; questions and options shuffled per student |
| Personalized answer key | Per-student option order makes shared answers wrong for the receiver (deterrence by design) |
| Server-side timer | Start time saved on the server and checked at submission |
| Session auto-save / resume | Answers persisted every ~20 s; refresh restores attempt with server-computed remaining time |
| One session per student | A second login for the same student is blocked |
| Tamper-evident audit log | Hash-chained events + signed session token; disabling the monitor script is itself flagged |
| Webcam snapshots and face check | In-browser face detection; photo on violation events; no face or >1 face recorded |
| Grace windows | First ~2 s of focus loss ignored; low-light warning instead of instant "no face" violation |

### Risk score model (upgraded)

Defaults from the proposal, now adjusted by context:

| Violation | Base weight | Contextual modifier |
|---|---|---|
| Tab switch | 3 | ×1.5 during a question most students take >30 s |
| Exit fullscreen | 3 | — |
| No face | 2 | Low-light produces a warning, not a violation |
| Multiple faces | 4 | — |
| Copy-paste attempt | 1 | +2 if followed by an atypically fast correct answer |
| Audit-chain break | 5 | Always high severity |

- **0–5** → Low · **6–12** → Medium · **13+** → High (bands unchanged from the proposal).
- The score is only a guide for the teacher, not a final decision.

### Collusion signals (new)

- Answer-sequence similarity between two students (identical choice patterns beyond chance).
- Timing fingerprints (same question answered instantly by both, after the same event).
- Reported in the exam-level report as "possible answer sharing — review", never as proof.

## 7. Data Model (ER)

- **User** (role: admin / teacher / student)
- **Exam** (title, duration, schedule, violation limit, pool size N, privacy settings)
- **Question** (belongs to an exam; options + correct answer + difficulty hint)
- **Attempt** (connects a user with an exam; start time, score, status, session token,
  last event hash)
- **Answer** (belongs to an attempt; auto-saved, supports resume)
- **Violation** (belongs to an attempt; type, timestamp, weight applied, prev-hash)
- **Snapshot** (belongs to an attempt; encrypted image path, timestamp, face status,
  deletion date)
- **Alert / Message** (violation notices pushed to the teacher, warnings shown to the
  student, and teacher ↔ student chat during a live attempt)

An **Attempt** connects a user with an exam; answers, violations and snapshots all belong
to an attempt.

## 8. Methodology

Iterative: build in small parts, test each part before moving on.

1. Collecting requirements: roles, exam features and anti-cheat rules (including upgraded
   objectives 12–20).
2. Design: architecture, flow charts, use cases, database design.
3. Database and login: models (incl. hash-chain fields), registration, login, role access.
4. Teacher side: exams with question pools and questions.
5. Student side: exam page, timer, shuffling, grading, session auto-save/resume.
6. Anti-cheat module: tab, fullscreen and clipboard detection → violation log with
   student/teacher alerts → tamper-evident chain → then webcam and face detection
   (privacy-first).
7. Live dashboard (SSE), reports, risk score, collusion analysis, analytics, CSV export.
8. Testing and documentation.

## 9. Timeline (7 weeks)

| Week | Work |
|---|---|
| 1 | Requirements (incl. upgrades), diagrams and project setup |
| 2 | Database (incl. audit-chain fields), login, dashboards, session resilience |
| 3 | Exam and question management with question pools |
| 4 | Student exam flow, timer, grading, auto-save/resume, grace windows |
| 5 | Anti-cheat module, violation log, contextual risk score, tamper-evident chain |
| 6 | Webcam (privacy-first), face detection, live SSE invigilation dashboard |
| 7 | Collusion + question analytics, testing, fixing bugs, documentation |

*If time is short, cut from the "Flagship → Polish" order below — never from core
reliability (timer, grading, resume).*

## 10. Upgrade Priority

| Tier | Features | Why |
|---|---|---|
| **Core upgrades** | Question pools, personalized-key deterrent, auto-save/resume, grace windows, hash-chained log | Low effort; kills the "why not Google Forms?" objection |
| **Flagship (pick 2)** | Live SSE dashboard · Collusion/pattern analytics | Makes the project unique and technically demanding |
| **Polish** | Privacy-first proctoring, question analytics, PDF cheat report | Impact + demo appeal |
| **Real impact** | Deploy and run one actual class quiz | "We graded 40 real students with it" beats any feature list |

## 11. Testing Plan

- Verify grading and timer functions (including server-side timer tamper checks and
  resume-after-refresh behavior).
- Test the full flow from login to result.
- Try to break the anti-cheat on purpose: switching tabs, leaving fullscreen, covering the
  camera, refreshing the page, **and patching/disabling the monitoring script** (chain
  must flag it).
- Verify grace windows and low-light handling do not create false violations.
- Verify exceeding the violation limit never auto-submits: alerts reach both the student
  and the teacher, all proof is stored, and only a teacher action can end the attempt.
- Validate collusion detector with a scripted pair of identical answer sequences (no false
  positives on random answers).
- Have a few classmates try the system as students and as a teacher.

## 12. Limitations

- Cannot stop a second phone, help from a person nearby, or running the exam in a VM.
- False alarms still possible (rare focus loss, very poor camera) — grace windows reduce,
  but do not eliminate them.
- The system never ends an exam automatically — even past the violation limit it only
  escalates the alerts. The teacher reviews the stored evidence and decides (warn,
  extend time, or force-submit manually).
- Webcam use raises privacy questions — addressed by in-browser processing, consent
  screen, encrypted storage and auto-deletion, but students must still be informed.

## 13. Future Work (if time permits)

- True/false and short-answer question types; PDF export of results.
- Email notification when results are published.
- Auto-generated viva questions for high-risk attempts ("prove it was you").
- Deploy with a real department for a semester-long trial.
