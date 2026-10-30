"""Login / logout / registration / profile."""

from flask import Blueprint, abort, flash, g, redirect, render_template, \
    request, url_for

from .. import services
from ..extensions import db
from ..models import LOCKOUT_MINUTES, User
from ..security import end_session, home_for, login_required, start_session

bp = Blueprint("auth", __name__)


@bp.route("/")
def index():
    """Root URL: send each role to its dashboard (or to login)."""
    if g.user is None:
        return redirect(url_for("auth.login"))
    return redirect(home_for(g.user))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(home_for(g.user))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()

        if user is not None and user.is_locked:
            services.audit("login_blocked_locked", f"user={username}",
                           ip_address=request.remote_addr)
            flash(f"This account is locked for {LOCKOUT_MINUTES} minutes after "
                  f"repeated failed logins. Try again later or contact an admin.", "error")
            return render_template("login.html")

        if user is None or not user.check_password(password):
            if user is not None:
                user.record_failed_login()
                db.session.commit()
            services.audit("login_failed", f"user={username}",
                           ip_address=request.remote_addr)
            flash("Wrong username or password.", "error")
        else:
            user.record_successful_login()
            start_session(user)
            db.session.commit()
            services.audit("login", user=user, ip_address=request.remote_addr)
            return redirect(home_for(user))

    return render_template("login.html")


@bp.route("/logout")
def logout():
    if g.user is not None:
        services.audit("logout", user=g.user, ip_address=request.remote_addr)
        end_session(g.user)
    flash("Signed out.", "ok")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------
# Registration (proposal: "Registration, login, logout, ..." — Core)
# --------------------------------------------------------------------------

def _valid_username(username: str) -> bool:
    return bool(username) and username.replace("_", "").replace("-", "").isalnum()


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Self-registration. Only students may register on their own; teachers
    and admins are created by the admin (accounts stay under control)."""
    if g.user is not None:
        return redirect(home_for(g.user))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        errors = []
        if not _valid_username(username):
            errors.append("Username must be letters/numbers/_/- only.")
        elif User.query.filter_by(username=username).first():
            errors.append("That username already exists.")
        if len(password) < 6:
            errors.append("Password needs at least 6 characters.")
        elif password != confirm:
            errors.append("Passwords do not match.")
        if email and "@" not in email:
            errors.append("That email does not look valid.")

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template("register.html", form=request.form.to_dict())

        user = User(username=username, full_name=full_name or username,
                    email=email or None, role="student")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        services.audit("register", f"student={username}", user=user,
                       ip_address=request.remote_addr)
        start_session(user)
        flash("Account created — welcome!", "ok")
        return redirect(home_for(user))

    return render_template("register.html", form={})


# --------------------------------------------------------------------------
# Profile (proposal Core: "profile page")
# --------------------------------------------------------------------------

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action") or "save"

        if action == "save":
            full_name = (request.form.get("full_name") or "").strip()
            email = (request.form.get("email") or "").strip()
            if email and "@" not in email:
                flash("That email does not look valid.", "error")
            else:
                g.user.full_name = full_name or g.user.username
                g.user.email = email or None
                db.session.commit()
                services.audit("profile_updated", user=g.user,
                               ip_address=request.remote_addr)
                flash("Profile updated.", "ok")

        elif action == "photo":
            photo = request.files.get("photo")
            if photo is None or not photo.filename:
                flash("Choose a photo first.", "error")
            elif photo.mimetype not in ("image/jpeg", "image/png", "image/webp"):
                flash("Photos must be JPEG, PNG or WebP.", "error")
            else:
                data = photo.read()
                if len(data) > 2 * 1024 * 1024:
                    flash("Photo too large (max 2 MB).", "error")
                else:
                    import secrets
                    from pathlib import Path
                    folder = _profile_dir()
                    name = f"user_{g.user.id}_{secrets.token_hex(4)}.jpg"
                    (folder / name).write_bytes(data)
                    previous = g.user.photo_path
                    g.user.photo_path = str(folder / name)
                    db.session.commit()
                    # Replace, don't accumulate: once the new photo is
                    # saved, drop the previous file from disk.
                    if previous and previous != g.user.photo_path:
                        try:
                            Path(previous).unlink(missing_ok=True)
                        except OSError:
                            pass
                    services.audit("photo_updated", user=g.user,
                                   ip_address=request.remote_addr)
                    flash("Profile photo updated.", "ok")

        elif action == "password":
            current = request.form.get("current_password") or ""
            new = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not g.user.check_password(current):
                flash("Current password is wrong.", "error")
            elif len(new) < 6:
                flash("New password needs at least 6 characters.", "error")
            elif new != confirm:
                flash("New passwords do not match.", "error")
            else:
                g.user.set_password(new)
                db.session.commit()
                services.audit("password_changed", user=g.user,
                               ip_address=request.remote_addr)
                flash("Password changed.", "ok")

        return redirect(url_for("auth.profile"))

    return render_template("profile.html")


def _profile_dir():
    from flask import current_app
    folder = current_app.config["PROFILE_DIR"]
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@bp.route("/avatar/<int:user_id>")
@login_required
def avatar(user_id):
    """Serve a profile photo (JPEG), a neutral placeholder otherwise."""
    from flask import send_file

    user = db.session.get(User, user_id)
    if user is None or not user.photo_path:
        abort(404)
    from pathlib import Path
    path = Path(user.photo_path)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/jpeg")
