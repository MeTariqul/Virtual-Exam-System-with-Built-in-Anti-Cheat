"""Authentication helpers: session handling, role decorators, CSRF protection."""

from __future__ import annotations

import secrets
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from .extensions import db
from .models import User, utcnow

#: Where each role lands after login.
HOME_ENDPOINTS = {
    "admin": "admin.dashboard",
    "teacher": "teacher.dashboard",
    "student": "student.dashboard",
}


def home_for(user: User) -> str:
    return url_for(HOME_ENDPOINTS.get(user.role, "student.dashboard"))


# --------------------------------------------------------------------------
# Current user + session heartbeat
# --------------------------------------------------------------------------

def load_current_user() -> None:
    """Populate ``g.user`` from the signed session cookie.

    The session carries a token that must match the one stored on the user
    row; a login from another device replaces it and kicks the old browser.
    """
    g.user = None
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)

    uid = session.get("user_id")
    token = session.get("session_token")
    if not uid or not token:
        return

    user = db.session.get(User, uid)
    if user is None or user.session_token != token:
        # Deleted account or a newer login elsewhere — force re-authentication.
        session.clear()
        return

    g.user = user
    now = utcnow()
    if user.session_seen_at is None or (now - user.session_seen_at).total_seconds() >= 60:
        user.session_seen_at = now
        db.session.commit()


def start_session(user: User) -> None:
    """Issue a fresh session token for *user*, invalidating any other browser."""
    token = secrets.token_hex(24)
    user.session_token = token
    user.session_seen_at = utcnow()
    session.clear()
    session["user_id"] = user.id
    session["session_token"] = token
    session["csrf_token"] = secrets.token_hex(16)
    db.session.commit()


def end_session(user: User | None = None) -> None:
    if user is not None:
        user.session_token = None
        user.session_seen_at = None
    session.clear()
    db.session.commit()


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------

def _unauthenticated_response():
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Not signed in."), 401
    return redirect(url_for("auth.login", next=request.path))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _unauthenticated_response()
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    """Allow only the given roles (admin is always allowed)."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return _unauthenticated_response()
            if g.user.role != "admin" and g.user.role not in roles:
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Not allowed."), 403
                return redirect(home_for(g.user))
            return view(*args, **kwargs)

        return wrapped

    return decorator


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_protect():
    """Reject state-changing requests without a matching CSRF token."""
    if request.method in SAFE_METHODS:
        return None
    token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not expected or token != expected:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="CSRF token missing or invalid."), 400
        return "CSRF token missing or invalid.", 400
    return None
