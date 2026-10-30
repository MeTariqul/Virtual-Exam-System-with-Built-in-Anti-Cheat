"""Entry point for the Virtual Exam System.

Run with:  python run.py
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=True gives auto-reload and a readable error page during development.
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
