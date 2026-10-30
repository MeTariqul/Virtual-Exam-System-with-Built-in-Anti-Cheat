"""Grading, risk scoring and report helpers.

Pure functions only — no Flask imports — so they are easy to unit test.
"""

from __future__ import annotations

from .models import Attempt, User, Violation

# --------------------------------------------------------------------------
# Risk scoring (weights come from the project proposal, section "Risk score")
# --------------------------------------------------------------------------

#: Proposal defaults for the risk weights. Admins may override them per
#: violation type (Settings page); these stay as the fallback.
BASE_WEIGHTS: dict[str, int] = {
    "tab_switch": 3,
    "fullscreen_exit": 3,
    "window_blur": 3,        # loss of browser focus
    "copy_paste": 1,
    "no_face": 2,
    "multiple_faces": 4,
    "monitor_gap": 5,        # anti-cheat script stopped reporting (chain break)
    # Recorded for evidence but never scored:
    "short_focus_loss": 0,   # returned within the 2s grace window
    "low_light": 0,          # camera too dark — a warning, not a violation
    "face_check_unavailable": 0,
    "late_upload": 1,        # handwritten pages after the upload window
    "duplicate_file": 2,     # byte-identical upload from another student
}

#: Fraction of a question's points lost per wrong answer when the exam uses
#: negative marking (proposal: "negative marking" — 25%).
NEGATIVE_FRACTION = 0.25


def active_weights() -> dict[str, int]:
    """BASE_WEIGHTS with any admin overrides from the settings table applied."""
    overrides = None
    try:
        from .models import Setting
        overrides = Setting.get("risk_weights")
    except Exception:  # pragma: no cover - no app context / fresh DB
        overrides = None
    if not isinstance(overrides, dict):
        return dict(BASE_WEIGHTS)
    merged = dict(BASE_WEIGHTS)
    for key, value in overrides.items():
        if key in merged:
            try:
                merged[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    return merged

#: A tab switch that happens while a question is on screen counts extra.
HARD_QUESTION_SECONDS = 30
CONTEXT_MULTIPLIER = 1.5

RISK_BANDS = ((0, 5, "low"), (6, 12, "medium"), (13, 10**9, "high"))


def risk_level(score: int) -> str:
    """Map a numeric risk score to low / medium / high."""
    for low, high, label in RISK_BANDS:
        if low <= score <= high:
            return label
    return "high" if score > RISK_BANDS[-1][1] else "low"


def violation_weight(
    violation_type: str,
    avg_answer_seconds: float | None = None,
    ignored: bool = False,
) -> int:
    """Weight for one violation, including the contextual multiplier.

    ``avg_answer_seconds`` is how long students typically take on the question
    that was on screen when the violation happened; if it is over 30 seconds
    a tab switch counts 1.5x (switching away mid-question is more suspicious).
    """
    if ignored:
        return 0
    weight = active_weights().get(violation_type, 1)
    if violation_type in ("tab_switch", "window_blur") and avg_answer_seconds is not None:
        if avg_answer_seconds > HARD_QUESTION_SECONDS:
            weight = int(round(weight * CONTEXT_MULTIPLIER))
    return weight


def compute_risk_score(violations, question_avg_seconds: dict[int, float] | None = None) -> tuple[int, str]:
    """Return ``(score, level)`` for a list of violations."""
    question_avg_seconds = question_avg_seconds or {}
    total = 0
    for v in violations:
        avg = question_avg_seconds.get(v.question_id) if v.question_id else None
        total += violation_weight(v.type, avg, v.ignored)
    return total, risk_level(total)


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

#: How similar (0..1) a typed answer must be to the model answer to score
#: automatically when no approved AI/teacher result exists yet.
TEXT_MATCH_THRESHOLD = 0.8


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation — for fair matching."""
    out = []
    for ch in (text or "").lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def text_similarity(response: str, model_answer: str) -> float:
    """0..1 similarity between the student's text and the model answer."""
    import difflib

    a = normalize_text(response)
    b = normalize_text(model_answer)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def matches_model_answer(response: str, model_answer: str | None) -> bool | None:
    """True/False for an attempted answer, None when there is nothing to
    check against (no model answer = awaiting AI/teacher review)."""
    if response is None or not response.strip():
        return None
    if not model_answer or not model_answer.strip():
        return None
    return text_similarity(response, model_answer) >= TEXT_MATCH_THRESHOLD


def grade_attempt(attempt: Attempt) -> tuple[float, float]:
    """Return ``(score, total)`` for an attempt.

    * MCQ / true-false: points for a correct option (minus 25% per wrong
      answer when the exam uses negative marking).
    * Multi-answer: exact set match, no partial credit.
    * Text: approved AI/teacher marks count first; otherwise an automatic
      similarity check against the model answer (0 while pending review).
    """
    exam = attempt.exam
    negative = exam.negative_marking
    approved = {
        g.question_id: g.final_marks
        for g in attempt.gradings
        if g.status == "approved" and g.final_marks is not None
    }
    answers = {a.question_id: a for a in attempt.answers}
    by_id = {q.id: q for q in exam.questions}

    score = 0.0
    total = 0.0
    # Grade the questions THIS attempt contains (pooled exams hand out a
    # subset — the total must never include questions the student never saw).
    for qid in attempt.question_order:
        q = by_id.get(qid)
        if q is None:
            continue
        total += q.points
        answer = answers.get(q.id)
        if answer is None:
            continue

        if q.type in ("mcq", "true_false"):
            if answer.selected_index is None:
                continue
            if answer.selected_index == q.correct_index:
                score += q.points
            elif negative:
                score -= q.points * NEGATIVE_FRACTION

        elif q.type == "multi":
            chosen = set(answer.selected_indices or
                         ([] if answer.selected_index is None else [answer.selected_index]))
            correct = set(q.correct_indices or
                          ([] if q.correct_index is None else [q.correct_index]))
            if chosen and chosen == correct:
                score += q.points
            elif negative and chosen:
                score -= q.points * NEGATIVE_FRACTION

        else:  # short_text / long_text
            if q.id in approved:
                score += max(0.0, min(float(approved[q.id]), q.points))
            elif matches_model_answer(answer.response, q.model_answer):
                score += q.points
            # else: pending AI/teacher review — counts 0 until approved.

    return round(score, 2), round(total, 2)


def is_correct(answer) -> bool | None:
    """True/False if answered and checkable, None if blank or pending."""
    if answer is None:
        return None
    q = answer.question
    if q.type in ("mcq", "true_false"):
        if answer.selected_index is None:
            return None
        return answer.selected_index == q.correct_index
    if q.type == "multi":
        chosen = set(answer.selected_indices or
                     ([] if answer.selected_index is None else [answer.selected_index]))
        if not chosen:
            return None
        correct = set(q.correct_indices or
                      ([] if q.correct_index is None else [q.correct_index]))
        return chosen == correct
    return matches_model_answer(answer.response, q.model_answer)


def _grading_for(answer):
    """The (single) GradingResult row for this answer, if any."""
    if answer is None or answer.attempt is None:
        return None
    return next(
        (g for g in answer.attempt.gradings
         if g.question_id == answer.question_id),
        None,
    )


def is_text_pending(answer) -> bool:
    """True when a typed answer is still waiting for AI/teacher review.

    An auto-match against the model answer, or an approved mark, settles the
    question; anything else may still gain (partial) marks, so it is pending.
    """
    if answer is None or answer.question.type not in ("short_text", "long_text"):
        return False
    if not (answer.response or "").strip():
        return False
    row = _grading_for(answer)
    if row is not None and row.status == "approved":
        return False
    # Full credit from the automatic check = final, nothing left to review.
    return matches_model_answer(
        answer.response, answer.question.model_answer
    ) is not True


def display_correct(answer, grading_row=None) -> bool | None:
    """What the ✔/✘ badge should show, taking approved marks into account.

    * Approved marks win: 0 → ✘, full → ✔, partial → None (…, the score line
      shows the exact fraction).
    * Otherwise the normal auto-check decides (None while blank/pending).
    """
    if answer is None:
        return None
    if answer.question.type in ("short_text", "long_text"):
        row = grading_row if grading_row is not None else _grading_for(answer)
        if row is not None and row.status == "approved" \
                and row.final_marks is not None:
            if row.final_marks <= 0:
                return False
            if row.final_marks >= answer.question.points:
                return True
            return None  # partial credit — shown as … next to the marks line
        return is_correct(answer)
    return is_correct(answer)


def attempt_percent(attempt) -> float | None:
    if attempt.score is None or not attempt.total:
        return None
    return round(100.0 * attempt.score / attempt.total, 1)


def has_passed(attempt) -> bool | None:
    percent = attempt_percent(attempt)
    if percent is None:
        return None
    return percent >= attempt.exam.pass_mark


def exam_summary(exam) -> dict:
    """Average / highest / lowest / pass rate over submitted attempts."""
    graded = [a for a in exam.attempts if a.status == "submitted" and a.total]
    percents = [attempt_percent(a) for a in graded]
    percents = [p for p in percents if p is not None]
    if not percents:
        return {"count": 0, "average": None, "highest": None, "lowest": None,
                "pass_rate": None, "passed": 0, "percentages": []}
    passed = sum(1 for p in percents if p >= exam.pass_mark)
    return {
        "count": len(percents),
        "average": round(sum(percents) / len(percents), 1),
        "highest": max(percents),
        "lowest": min(percents),
        "pass_rate": round(100.0 * passed / len(percents), 1),
        "passed": passed,
        "percentages": sorted(percents),
    }


# --------------------------------------------------------------------------
# Risk report rows (used by the teacher's cheat report page)
# --------------------------------------------------------------------------

def build_report_rows(attempt: Attempt, question_avg_seconds: dict[int, float] | None = None) -> list[dict]:
    """Human-readable rows for the cheat report timeline."""
    question_avg_seconds = question_avg_seconds or {}
    question_number = {qid: i + 1 for i, qid in enumerate(attempt.question_order)}
    rows = []
    for v in attempt.violations:
        avg = question_avg_seconds.get(v.question_id) if v.question_id else None
        weight = violation_weight(v.type, avg, v.ignored)
        rows.append(
            {
                "id": v.id,
                "time": v.created_at,
                "type": v.type,
                "label": violation_label(v.type),
                "question_number": question_number.get(v.question_id),
                "detail": v.detail or {},
                "weight": weight,
                "ignored": v.ignored,
                "snapshot_id": v.snapshot_id,
            }
        )
    return rows


def violation_label(violation_type: str) -> str:
    return {
        "tab_switch": "Tab switch",
        "window_blur": "Window lost focus",
        "fullscreen_exit": "Left fullscreen",
        "copy_paste": "Copy / paste attempt",
        "no_face": "No face detected",
        "multiple_faces": "Multiple faces detected",
        "monitor_gap": "Monitoring interrupted",
        "short_focus_loss": "Brief focus loss (grace)",
        "low_light": "Camera too dark",
        "face_check_unavailable": "Face check unavailable",
        "late_upload": "Late handwritten upload",
        "duplicate_file": "Duplicate file (same as another student)",
    }.get(violation_type, violation_type)


# --------------------------------------------------------------------------
# Question quality analytics (difficulty, discrimination)
# --------------------------------------------------------------------------

def question_difficulty(exam) -> dict[int, dict]:
    """Per-question stats: ``{question_id: {answered, correct, rate, avg_seconds}}``.

    Only *submitted* attempts are considered so partial runs do not skew data.
    """
    stats: dict[int, dict] = {}
    submitted = [a for a in exam.attempts if a.status == "submitted"]
    for q in exam.questions:
        stats[q.id] = {"answered": 0, "correct": 0, "rate": None, "avg_seconds": None}

    time_samples: dict[int, list[float]] = {}
    for attempt in submitted:
        # Time on each question = gap between this answer and the previous one.
        answered = sorted(
            (a for a in attempt.answers if a.first_answered_at),
            key=lambda a: a.first_answered_at,
        )
        prev_time = attempt.started_at
        for ans in answered:
            delta = (ans.first_answered_at - prev_time).total_seconds()
            # Guard against clock weirdness / extended attempts.
            if 0 <= delta <= attempt.exam.duration_minutes * 60 + 3600:
                time_samples.setdefault(ans.question_id, []).append(delta)
            prev_time = ans.first_answered_at

        for ans in attempt.answers:
            s = stats.get(ans.question_id)
            if s is None:
                continue
            answered = ans.selected_index is not None or bool(
                (ans.response or "").strip()
            ) or bool(ans.selected_indices)
            if not answered:
                continue
            s["answered"] += 1
            if is_correct(ans):
                s["correct"] += 1

    for qid, s in stats.items():
        if s["answered"]:
            s["rate"] = s["correct"] / s["answered"]
        samples = time_samples.get(qid) or []
        if samples:
            s["avg_seconds"] = sum(samples) / len(samples)
    return stats


def difficulty_label(rate: float | None) -> str:
    if rate is None:
        return "—"
    if rate >= 0.8:
        return "easy"
    if rate >= 0.5:
        return "medium"
    return "hard"


def discrimination_index(exam) -> dict[int, float | None]:
    """Classic discrimination index (D) per question.

    Submitted attempts are ranked by total score and split into an upper and
    a lower group (~27% each, Ebel's method). For each question D is the
    fraction correct in the upper group minus the fraction correct in the
    lower group, so it ranges from -1.0 (good students get it wrong) through
    0 (no signal) to +1.0 (perfectly discriminating).

    Questions nobody in either group saw (pooled out of their paper) return
    ``None``. With fewer than 4 submitted attempts there is not enough data.
    """
    submitted = [a for a in exam.attempts if a.status == "submitted" and a.total]
    result: dict[int, float | None] = {q.id: None for q in exam.questions}
    if len(submitted) < 4:
        return result

    ranked = sorted(submitted, key=lambda a: (a.score or 0.0) / a.total)
    k = max(1, round(len(ranked) * 0.27))
    top, bottom = ranked[-k:], ranked[:k]

    def _rate(group) -> float | None:
        """Fraction of *group* that answered *q* correctly."""
        seen = correct = 0
        for attempt in group:
            if q.id not in attempt.question_order:
                continue
            seen += 1
            answer = next(
                (a for a in attempt.answers if a.question_id == q.id), None
            )
            if is_correct(answer) is True:
                correct += 1
        return correct / seen if seen else None

    for q in exam.questions:
        top_rate = _rate(top)
        bottom_rate = _rate(bottom)
        if top_rate is not None and bottom_rate is not None:
            result[q.id] = round(top_rate - bottom_rate, 3)
    return result


def discrimination_label(d: float | None) -> str:
    """Readable verdict for a discrimination index value."""
    if d is None:
        return "—"
    if d >= 0.3:
        return "good"
    if d >= 0.1:
        return "fair"
    if d >= 0:
        return "poor"
    return "negative"


# --------------------------------------------------------------------------
# Collusion detection (answer-pattern similarity between students)
# --------------------------------------------------------------------------

def find_collusion_candidates(exam, min_shared: int = 10, threshold: float = 0.9) -> list[dict]:
    """Pairs of students whose answers are suspiciously identical.

    Compares only questions both students actually answered. Returns a list of
    dictionaries; empty when there is not enough shared data (e.g. small pools).
    """
    submitted = [a for a in exam.attempts if a.status == "submitted" and a.answers]
    pairs: list[dict] = []
    for i in range(len(submitted)):
        for j in range(i + 1, len(submitted)):
            a, b = submitted[i], submitted[j]
            shared = _shared_answers(a, b)
            if len(shared) < min_shared:
                continue
            identical = sum(1 for sel_a, sel_b in shared if sel_a == sel_b)
            ratio = identical / len(shared)
            if ratio >= threshold:
                pairs.append(
                    {
                        "attempt_a": a,
                        "attempt_b": b,
                        "shared": len(shared),
                        "identical": identical,
                        "ratio": ratio,
                    }
                )
    return pairs


def _shared_answers(a: Attempt, b: Attempt) -> list[tuple]:
    """[(answer_a, answer_b), ...] for questions both students answered.

    Typed answers are skipped (free text never matches byte-for-byte);
    multi-answer questions compare as sets.
    """
    by_q_b = {ans.question_id: ans for ans in b.answers}
    out = []
    for ans in a.answers:
        other = by_q_b.get(ans.question_id)
        if other is None:
            continue
        qtype = ans.question.type
        if qtype in ("short_text", "long_text"):
            continue
        if qtype == "multi":
            mine = ans.selected_indices or ([] if ans.selected_index is None
                                            else [ans.selected_index])
            theirs = other.selected_indices or ([] if other.selected_index is None
                                                else [other.selected_index])
            if not mine or not theirs:
                continue
            out.append((frozenset(mine), frozenset(theirs)))
            continue
        if ans.selected_index is None or other.selected_index is None:
            continue
        out.append((ans.selected_index, other.selected_index))
    return out
