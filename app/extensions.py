"""Shared Flask extensions.

Keeping extensions in their own module avoids circular imports:
models import `db` from here, and the app factory initialises it later.
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
