"""Application factory for the Virtual Exam System.

Creates the Flask app, initialises the database, registers blueprints,
filters and CLI commands.  Run ``python run.py`` to start it.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .extensions import db

#: Human-friendly labels for internal enum codes shown in the UI. The raw
#: value is still used (e.g. as a CSS class), only the visible text changes.
HUMAN_LABELS: dict[str, str] = {
    # attempt status
    "in_progress": "In progress",
    "submitted": "Submitted",
    # exam status
    "draft": "Draft",
    "scheduled": "Scheduled",
    "open": "Open",
    "closed": "Closed",
    # OCR / resource / job status
    "queued": "Queued",
    "processing": "Processing",
    "ready": "Ready",
    "running": "Running",
    "done": "Done",
    "failed": "Failed",
    # AI marking status
    "suggested": "Suggested",
    "approved": "Approved",
    "rejected": "Rejected",
    # webcam snapshot reason
    "periodic": "Periodic check",
    "violation": "Violation",
    "identity": "Identity check",
    # background job kinds
    "ocr": "OCR",
    "ingest": "Ingest",
    "mark": "Marking",
    # who ended the attempt
    "student": "Student",
    "teacher": "Teacher",
}


def _sqlite_upgrade(engine) -> None:
    """Bring an older SQLite database up to the current schema.

    ``db.create_all()`` only creates *missing* tables; when a column grows a
    new capability (here: question-bank questions that are not attached to
    an exam), SQLite needs a small rebuild. Runs once, then the checks are
    no-ops forever.
    """
    from sqlalchemy import inspect, text

    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if "questions" not in inspector.get_table_names():
        return
    columns = {c["name"]: c for c in inspector.get_columns("questions")}

    # 1) questions.exam_id: NOT NULL -> nullable (SQLite table rebuild).
    exam_id = columns.get("exam_id")
    if exam_id is not None and not exam_id["nullable"]:
        with engine.begin() as conn:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='questions'")).scalar()
            index_ddls = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='questions' AND sql IS NOT NULL")).scalars().all()
            new_ddl = (ddl or "").replace(
                "CREATE TABLE questions", "CREATE TABLE questions_new", 1
            ).replace("exam_id INTEGER NOT NULL", "exam_id INTEGER", 1)
            if new_ddl and new_ddl != ddl:
                names = [c["name"] for c in inspector.get_columns("questions")]
                column_list = ", ".join(names)
                conn.execute(text(new_ddl))
                conn.execute(text(
                    f"INSERT INTO questions_new ({column_list}) "
                    f"SELECT {column_list} FROM questions"))
                conn.execute(text("DROP TABLE questions"))
                conn.execute(text("ALTER TABLE questions_new RENAME TO questions"))
                for index_ddl in index_ddls:
                    conn.execute(text(index_ddl))

    # 2) questions.created_by (plain ADD COLUMN).
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("questions")}
    if "created_by" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE questions ADD COLUMN created_by INTEGER"))

    # 3) the lookup index on that column.  db.create_all() only creates
    #    indexes for tables it creates itself, and the rebuild in step 1
    #    re-creates the old ones only — so add ours explicitly.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_questions_created_by "
            "ON questions (created_by)"))

    # 4) users.failed_logins / locked_until: brute-force protection columns
    #    (added after the database already exists).
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {c["name"]: c for c in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "failed_logins" not in columns:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0"))
        if "locked_until" not in columns:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN locked_until DATETIME"))


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)

    base_dir = Path(__file__).resolve().parent.parent
    instance_dir = base_dir / "instance"
    snapshot_dir = instance_dir / "snapshots"
    upload_dir = instance_dir / "uploads"
    profile_dir = instance_dir / "profiles"
    instance_dir.mkdir(exist_ok=True)
    snapshot_dir.mkdir(exist_ok=True)
    upload_dir.mkdir(exist_ok=True)
    profile_dir.mkdir(exist_ok=True)

    app.config.from_mapping(
        # Replace via environment in production:  set EXAM_SECRET_KEY=...
        SECRET_KEY=__import__("os").environ.get("EXAM_SECRET_KEY") or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{instance_dir / 'exam.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SNAPSHOT_DIR=snapshot_dir,
        UPLOAD_DIR=upload_dir,
        PROFILE_DIR=profile_dir,
        SNAPSHOT_RETENTION_DAYS=7,
        # Handwritten uploads (10 MB max) plus headroom for form posts.
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)

    db.init_app(app)

    # -- database (create tables on first run) ---------------------------
    with app.app_context():
        from . import models  # noqa: F401  (register models with the metadata)

        db.create_all()
        _sqlite_upgrade(db.engine)

        # First run: create demo accounts + a sample exam so the app is
        # usable immediately (see app/seed.py for credentials).
        from .seed import seed_demo

        seed_demo()

    # -- background AI jobs (OCR / ingest / marking) ----------------------
    from .ai import jobs as ai_jobs

    ai_jobs.init_app(app)

    # Model names live in instance/ai_config.json — create it with the
    # defaults on first run so it can be edited without touching the code.
    from .ai.config import ensure_config_file

    ensure_config_file()

    # -- blueprints -------------------------------------------------------
    from .routes.admin import bp as admin_bp
    from .routes.api import bp as api_bp
    from .routes.auth import bp as auth_bp
    from .routes.student import bp as student_bp
    from .routes.teacher import bp as teacher_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)

    # -- request hooks ----------------------------------------------------
    from .security import csrf_protect, load_current_user

    app.before_request(load_current_user)
    app.before_request(csrf_protect)

    # -- overdue-attempt sweep (throttled) --------------------------------
    # The exam page submits itself at 00:00, but a closed tab must not leave
    # an attempt "in progress" forever.  Run the sweep lazily at most every
    # ~15 s of wall-clock time so it stays cheap on busy instances.
    import threading
    import time as _time

    _sweep_state = {"at": 0.0, "lock": threading.Lock()}
    _SWEEP_INTERVAL = 15.0

    @app.before_request
    def sweep_overdue_attempts():
        if request.path.startswith("/static/"):
            return
        if _time.monotonic() - _sweep_state["at"] < _SWEEP_INTERVAL:
            return
        with _sweep_state["lock"]:
            if _time.monotonic() - _sweep_state["at"] < _SWEEP_INTERVAL:
                return
            try:
                from .services import expire_overdue_attempts
                expire_overdue_attempts()
            except Exception:  # pragma: no cover - never block a request on this
                db.session.rollback()
            finally:
                _sweep_state["at"] = _time.monotonic()

    # -- security headers --------------------------------------------------
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Camera is only needed for the in-exam webcam module; everything
        # else is locked down by default.
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(), geolocation=(), payment=()",
        )
        if not request.path.startswith("/static/"):
            # Exam answers and reports must never sit in a shared cache.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- template helpers -------------------------------------------------
    @app.template_filter("dt")
    def format_dt(value):
        """'13 Oct 2026, 14:05' — used everywhere timestamps are shown."""
        if value is None:
            return "—"
        return value.strftime("%d %b %Y, %H:%M")

    @app.template_filter("clock")
    def format_clock(value):
        if value is None:
            return "—"
        return value.strftime("%H:%M:%S")

    @app.template_filter("marks")
    def format_marks(value):
        """7 -> '7', 7.5 -> '7.5' (scores can be fractional now)."""
        if value is None:
            return "—"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{value:g}" if isinstance(value, float) else str(value)

    @app.template_filter("pct")
    def format_pct(value):
        """100.0 -> '100', 66.7 -> '66.7' (template appends the % sign)."""
        if value is None:
            return "—"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{value:g}" if isinstance(value, float) else str(value)

    @app.template_filter("label")
    def human_label(value):
        """'in_progress' -> 'In progress', 'ocr' -> 'OCR', etc.

        Unknown values fall back to a readable title-cased form, so no code
        ever shows an ugly internal value verbatim.
        """
        if value is None:
            return "—"
        key = str(value)
        return HUMAN_LABELS.get(key, key.replace("_", " ").capitalize())

    @app.context_processor
    def inject_globals():
        from flask import g, session

        return {
            "csrf_token": session.get("csrf_token", ""),
            "current_user": g.user,
        }

    # -- error pages ------------------------------------------------------
    def wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def not_found(e):
        if wants_json():
            return jsonify(ok=False, error="Not found."), 404
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(403)
    def forbidden(e):
        if wants_json():
            return jsonify(ok=False, error="Not allowed."), 403
        return render_template("error.html", code=403, message="You are not allowed to do that."), 403

    @app.errorhandler(HTTPException)
    def http_error(e):
        if wants_json():
            return jsonify(ok=False, error=e.description), e.code
        if e.code == 404:
            return render_template("error.html", code=404, message="Page not found."), 404
        if e.code == 403:
            return render_template("error.html", code=403, message="You are not allowed to do that."), 403
        return render_template("error.html", code=e.code, message=e.description), e.code

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if wants_json():
            return jsonify(ok=False, error="Internal server error."), 500
        return render_template("error.html", code=500, message="Something went wrong on the server."), 500

    # -- CLI commands -----------------------------------------------------
    _register_commands(app)

    # Snapshot retention runs once at startup (cheap, idempotent).
    with app.app_context():
        try:
            from .services import delete_expired_snapshots

            delete_expired_snapshots()
        except Exception:  # pragma: no cover - never block startup on cleanup
            pass

    return app


def _register_commands(app: Flask) -> None:
    import click

    @app.cli.command("init-db")
    def init_db_command():
        """Create all tables (idempotent)."""
        db.create_all()
        click.echo("Database ready.")

    @app.cli.command("seed-demo")
    def seed_command():
        """Create demo accounts and a sample exam."""
        from .seed import seed_demo

        seed_demo()
        click.echo("Demo data created.")

    @app.cli.command("reseed-demo")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def reseed_command(yes):
        """WIPE the database and snapshot/upload folders, then re-seed demo data.

        Use this whenever you want a fresh copy of the full demo dataset
        (every exam state, cheat report, collusion pair, AI demo, ...).
        """
        import shutil

        if not yes and not click.confirm("This deletes ALL data in instance/exam.db. Continue?"):
            click.echo("Aborted.")
            return
        db.drop_all()
        db.create_all()
        for folder in ("snapshots", "uploads", "profiles"):
            target = app.config["SNAPSHOT_DIR"].parent / folder
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                target.mkdir(exist_ok=True)
        from .seed import seed_demo

        seed_demo()
        click.echo("Database wiped and demo data re-seeded.")
        click.echo("Tip: run `flask verify-chains` to confirm the evidence chains.")

    @app.cli.command("cleanup-snapshots")
    def cleanup_command():
        """Delete webcam snapshots past the retention window."""
        from .services import delete_expired_snapshots

        n = delete_expired_snapshots()
        click.echo(f"Removed {n} expired snapshot(s).")

    @app.cli.command("expire-attempts")
    def expire_command():
        """Submit in-progress attempts that have passed their deadline."""
        from .services import expire_overdue_attempts

        n = expire_overdue_attempts()
        click.echo(f"Expired {n} overdue attempt(s).")

    @app.cli.command("verify-chains")
    def verify_command():
        """Recompute every attempt's hash chain and report broken ones."""
        from .models import Attempt
        from .services import verify_chain

        broken = 0
        for attempt in Attempt.query.all():
            ok, checked, at = verify_chain(attempt)
            if not ok:
                broken += 1
                click.echo(f"Attempt {attempt.id}: BROKEN after {checked} links "
                           f"(event {at})")
        click.echo(f"Checked {Attempt.query.count()} attempt(s); {broken} broken.")
