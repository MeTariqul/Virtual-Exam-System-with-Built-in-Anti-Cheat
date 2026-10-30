"""Quick smoke test against a RUNNING server (python run.py).

Usage:  python smoke_test.py
Logs in as the demo student, starts the sample exam, answers everything,
commits a violation, submits, then logs in as the teacher and opens the
report. Prints PASS/FAIL for each step.
"""

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:5000"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def get(path):
    with opener.open(BASE + path, timeout=10) as r:
        return r.status, r.read().decode()


def post(path, data, headers=None):
    body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers or {})
    with opener.open(req, timeout=10) as r:
        return r.status, r.read().decode()


def csrf(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


def main():
    # 1. Login page loads
    status, html = get("/login")
    check("login page loads", status == 200 and "csrf_token" in html)

    # 2. Student login
    status, html = post("/login", {"username": "student", "password": "student123",
                                   "csrf_token": csrf(html)})
    check("student login", "My exams" in html)

    # 3. Start the sample exam
    m = re.search(r'/student/exams/(\d+)/start', html)
    if not m:
        print("      hint: no exam can be started — the demo attempts are used "
              "up.\n      Run a fresh demo first:  python -m flask --app run "
              "reseed-demo --yes")
    check("sample exam visible", bool(m))
    status, html = post(f"/student/exams/{m.group(1)}/start",
                        {"csrf_token": csrf(html)})
    check("exam page opens", "question-card" in html and "hud-timer" in html)

    m = re.search(r'"attempt_id":\s*(\d+)', html)
    attempt_id = m.group(1)

    # 4. Violation -> alert, NO submit (policy check)
    tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    import json
    req = urllib.request.Request(
        f"{BASE}/api/attempts/{attempt_id}/violations",
        data=json.dumps({"type": "tab_switch", "duration_ms": 9000}).encode(),
        headers={"Content-Type": "application/json", "X-CSRFToken": tok},
    )
    with opener.open(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    check("violation alerts student", data["ok"] and data["warning_count"] == 1)
    status, page = get(f"/student/attempts/{attempt_id}")
    check("exam still running after violation", "question-card" in page)

    # 5. Submit
    req = urllib.request.Request(
        f"{BASE}/api/attempts/{attempt_id}/submit", data=b"{}",
        headers={"Content-Type": "application/json", "X-CSRFToken": tok},
    )
    with opener.open(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    check("submit grades instantly", data["ok"] and data["score"] is not None)

    status, html = get(f"/student/attempts/{attempt_id}/result")
    check("result page renders", "%" in html)

    # 6. Teacher sees the cheat report
    status, html = get("/logout")
    status, html = get("/login")
    post("/login", {"username": "teacher", "password": "teacher123",
                    "csrf_token": csrf(html)})
    status, html = get(f"/teacher/attempts/{attempt_id}/report")
    check("teacher cheat report", "Cheat report" in html
          and "Tab switch" in html and "intact" in html)

    print("\nAll smoke checks passed. Server is working end-to-end.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        print(f"Server not reachable at {BASE}: {e}")
        sys.exit(2)
