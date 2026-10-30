"""Admin pages: manage users, anti-cheat settings, audit log & backup."""

from flask import Blueprint, Response, abort, current_app, flash, g, \
    redirect, render_template, request, url_for

from .. import services
from ..extensions import db
from ..models import Attempt, AuditLog, Exam, Setting, User
from ..security import login_required, role_required

bp = Blueprint("admin", __name__, url_prefix="/admin")

ROLES = ("admin", "teacher", "student")


@bp.route("/")
@login_required
@role_required("admin")
def dashboard():
    exams = Exam.query.order_by(Exam.created_at.desc()).all()
    users = User.query.order_by(User.role, User.username).all()
    stats = {
        "users": len(users),
        "exams": len(exams),
        "attempts": Attempt.query.count(),
        "in_progress": Attempt.query.filter_by(status="in_progress").count(),
    }
    return render_template("admin/dashboard.html", users=users, exams=exams,
                           stats=stats, roles=ROLES)


@bp.post("/users/new")
@login_required
@role_required("admin")
def user_new():
    username = (request.form.get("username") or "").strip().lower()
    full_name = (request.form.get("full_name") or "").strip()
    role = request.form.get("role") or "student"
    password = request.form.get("password") or ""

    if not username or not username.replace("_", "").replace("-", "").isalnum():
        flash("Username must be letters/numbers/_/- only.", "error")
    elif role not in ROLES:
        flash("Unknown role.", "error")
    elif len(password) < 6:
        flash("Password needs at least 6 characters.", "error")
    elif User.query.filter_by(username=username).first():
        flash("That username already exists.", "error")
    else:
        user = User(username=username, full_name=full_name or username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        services.audit("user_created", f"{role}={username}", user=g.user,
                       ip_address=request.remote_addr)
        flash(f"User '{username}' created.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.post("/users/<int:user_id>/role")
@login_required
@role_required("admin")
def user_role(user_id):
    user = _user_or_404(user_id)
    role = request.form.get("role")
    if role not in ROLES:
        flash("Unknown role.", "error")
    elif user.id == g.user.id and role != "admin":
        flash("You cannot demote yourself.", "error")
    else:
        old_role = user.role
        user.role = role
        db.session.commit()
        services.audit("role_changed", f"{user.username}: {old_role} -> {role}",
                       user=g.user, ip_address=request.remote_addr)
        flash(f"{user.username} is now {role}.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.post("/users/<int:user_id>/password")
@login_required
@role_required("admin")
def user_password(user_id):
    user = _user_or_404(user_id)
    password = request.form.get("password") or ""
    if len(password) < 6:
        flash("Password needs at least 6 characters.", "error")
    else:
        user.set_password(password)
        # Force re-login everywhere with the new password, and clear any lockout.
        user.session_token = None
        user.failed_logins = 0
        user.locked_until = None
        db.session.commit()
        flash(f"Password for {user.username} reset.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.post("/users/<int:user_id>/delete")
@login_required
@role_required("admin")
def user_delete(user_id):
    user = _user_or_404(user_id)
    if user.id == g.user.id:
        flash("You cannot delete yourself.", "error")
    elif Attempt.query.filter_by(student_id=user.id).count():
        # Keep exam history intact: archive instead of delete.
        flash("This user has exam attempts. Change their role or "
              "delete their exams first.", "error")
    else:
        has_attempts_as_creator = Exam.query.filter_by(created_by=user.id).count()
        if has_attempts_as_creator:
            flash("This user owns exams. Delete those exams first.", "error")
        else:
            db.session.delete(user)
            db.session.commit()
            flash(f"User {user.username} deleted.", "ok")
    return redirect(url_for("admin.dashboard"))


def _user_or_404(user_id: int) -> User:
    return db.session.get(User, user_id) or abort(404)


# --------------------------------------------------------------------------
# Anti-cheat settings (proposal: "default anti-cheat rules" + editable weights)
# --------------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def settings():
    from .. import grading, services

    if request.method == "POST":
        if request.form.get("action") == "weights":
            weights = {}
            for key in grading.BASE_WEIGHTS:
                raw = request.form.get(f"w_{key}")
                if raw is None:
                    continue
                try:
                    weights[key] = max(0, min(50, int(raw)))
                except ValueError:
                    flash(f"{key}: not a number — kept the old value.", "error")
            Setting.set("risk_weights", weights)
            services.audit("weights_updated",
                           ", ".join(f"{k}={v}" for k, v in sorted(weights.items())),
                           user=g.user, ip_address=request.remote_addr)
            flash("Risk weights saved (new violations use them immediately).", "ok")
        elif request.form.get("action") == "defaults":
            defaults = {
                "violation_limit": _int_form("violation_limit", 3, 1, 50),
                "duration_minutes": _int_form("duration_minutes", 30, 1, 480),
                "snapshot_retention_days": _int_form("snapshot_retention_days", 7, 1, 365),
            }
            Setting.set("exam_defaults", defaults)
            current_app.config["SNAPSHOT_RETENTION_DAYS"] = defaults["snapshot_retention_days"]
            services.audit("defaults_updated", str(defaults), user=g.user,
                           ip_address=request.remote_addr)
            flash("Defaults saved (applies to newly created exams).", "ok")
        return redirect(url_for("admin.settings"))

    weights = grading.active_weights()
    defaults = Setting.get("exam_defaults") or {}
    return render_template("admin/settings.html",
                           weights=weights,
                           base=grading.BASE_WEIGHTS,
                           defaults=defaults,
                           retention=current_app.config.get("SNAPSHOT_RETENTION_DAYS", 7))


def _int_form(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Audit log & backup (proposal: "audit log, database backup")
# --------------------------------------------------------------------------

@bp.route("/audit")
@login_required
@role_required("admin")
def audit_log():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 50
    query = AuditLog.query.order_by(AuditLog.id.desc())
    total = query.count()
    entries = query.offset((page - 1) * per_page).limit(per_page).all()
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template("admin/audit.html", entries=entries,
                           page=page, pages=pages, total=total)


@bp.post("/audit/clear")
@login_required
@role_required("admin")
def audit_clear():
    from .. import services
    AuditLog.query.delete()
    db.session.commit()
    services.audit("audit_cleared", user=g.user, ip_address=request.remote_addr)
    flash("Audit log cleared.", "ok")
    return redirect(url_for("admin.audit_log"))


@bp.route("/backup")
@login_required
@role_required("admin")
def backup():
    """Download a consistent copy of the SQLite database."""
    from .. import services
    import sqlite3
    import tempfile
    from pathlib import Path

    source = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not source.startswith("sqlite:///"):
        flash("Backup is only supported for SQLite databases.", "error")
        return redirect(url_for("admin.dashboard"))
    source_path = Path(source.replace("sqlite:///", "", 1))
    if not source_path.exists():
        flash("Database file not found.", "error")
        return redirect(url_for("admin.dashboard"))

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    handle.close()
    dest = handle.name
    try:
        src_conn = sqlite3.connect(str(source_path))
        dest_conn = sqlite3.connect(dest)
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()
        with open(dest, "rb") as fh:
            data = fh.read()
        services.audit("database_backup", f"{len(data)} bytes", user=g.user,
                       ip_address=request.remote_addr)
        return Response(
            data, mimetype="application/vnd.sqlite3",
            headers={"Content-Disposition":
                     "attachment; filename=exam_backup.db"},
        )
    except Exception as exc:  # pragma: no cover - defensive
        flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("admin.dashboard"))
    finally:
        try:
            Path(dest).unlink(missing_ok=True)
        except OSError:
            pass
