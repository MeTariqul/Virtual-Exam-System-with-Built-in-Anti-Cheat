"""Background jobs for long AI work (OCR, ingestion, marking).

The proposal requires "long jobs such as OCR and grading ... run in the
background with a status shown to the teacher".  A single daemon worker
thread pulls ``Job`` rows and runs them with an application context.

``inline_mode`` (used in tests via ``TESTING=True``, or ``EXAM_AI_INLINE=1``)
runs jobs synchronously so behaviour stays deterministic.
"""

from __future__ import annotations

import os
import queue
import threading

from ..extensions import db
from ..models import Job, utcnow

_app = None
_queue: "queue.Queue[int]" = queue.Queue()
_thread: threading.Thread | None = None
_stop = threading.Event()


def init_app(app) -> None:
    """Attach the Flask app and start the worker (idempotent)."""
    global _app, _thread
    _app = app
    if inline_mode(app):
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_worker, name="ai-jobs", daemon=True)
    _thread.start()


def shutdown() -> None:
    """Stop the worker (used on app teardown / test runs)."""
    _stop.set()
    _queue.put(-1)


def inline_mode(app=None) -> bool:
    app = app or _app
    if app is not None and app.config.get("TESTING"):
        return True
    return os.environ.get("EXAM_AI_INLINE") == "1"


def enqueue(kind: str, payload: dict, app=None) -> Job:
    """Queue a job. In inline mode it runs immediately (synchronously)."""
    app = app or _app
    job = Job(kind=kind, payload=payload, status="queued")
    db.session.add(job)
    db.session.commit()

    if inline_mode(app):
        _execute(job.id)
        db.session.refresh(job)
        return job

    _queue.put(job.id)
    return job


def _handler(kind: str):
    """Map job kind -> callable(payload) (imports kept lazy & cacheable)."""
    if kind == "ocr":
        from . import ocr

        def run(payload):
            from ..models import Submission

            submission = db.session.get(Submission, int(payload["submission_id"]))
            if submission is not None:
                ocr.process_file(submission)
            return {"submission_id": payload["submission_id"]}
        return run

    if kind == "ingest":
        from . import rag

        def run(payload):
            rid = int(payload["resource_id"])
            rag.ingest_resource(rid)
            rag.mirror_to_chroma(rid)
            return {"resource_id": rid}
        return run

    if kind == "mark":
        from . import marking

        def run(payload):
            return marking.mark_attempt(int(payload["attempt_id"]))
        return run

    return None


def _execute(job_id: int):
    """Run one job row inside the current (or a fresh) application context."""
    job = db.session.get(Job, job_id)
    if job is None or job.status != "queued":
        return None
    job.status = "running"
    job.started_at = utcnow()
    db.session.commit()

    handler = _handler(job.kind)
    try:
        if handler is None:
            raise ValueError(f"Unknown job kind: {job.kind}")
        result = handler(job.payload or {}) or {}
        job.status = "done"
        job.result = result
        job.error = None
        job.finished_at = utcnow()
        db.session.commit()
        return result
    except Exception as exc:  # noqa: BLE001 - jobs must never crash the worker
        db.session.rollback()
        job = db.session.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            db.session.commit()
        return None


def _run_job(job_id: int, app) -> None:
    from flask import has_app_context

    if has_app_context():
        _execute(job_id)
        return
    if app is None:
        return
    with app.app_context():
        _execute(job_id)


def _worker() -> None:
    while not _stop.is_set():
        try:
            job_id = _queue.get(timeout=1)
        except queue.Empty:
            continue
        if job_id == -1:
            break
        _run_job(job_id, _app)


def wait_idle(timeout: float = 30.0) -> bool:
    """Testing helper: block until the queue is empty (inline = instant)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.empty():
            return True
        time.sleep(0.05)
    return _queue.empty()
