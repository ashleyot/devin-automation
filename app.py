"""
Devin Automation Server

Listens for GitHub webhook events, triggers Devin sessions for issues
labelled "devin-fix", polls for completion, and serves a live dashboard.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEVIN_API_KEY = os.getenv("DEVIN_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

DEVIN_API_BASE = "https://api.devin.ai/v1"
SESSIONS_FILE = Path(__file__).parent / "sessions.json"


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def _load_sessions() -> list[dict]:
    if SESSIONS_FILE.exists():
        with open(SESSIONS_FILE, "r") as fh:
            return json.load(fh)
    return []


def _save_sessions(sessions: list[dict]) -> None:
    with open(SESSIONS_FILE, "w") as fh:
        json.dump(sessions, fh, indent=2)


def _add_session(record: dict) -> None:
    with _lock:
        sessions = _load_sessions()
        sessions.append(record)
        _save_sessions(sessions)


def _update_session(session_id: str, updates: dict) -> None:
    with _lock:
        sessions = _load_sessions()
        for s in sessions:
            if s["session_id"] == session_id:
                s.update(updates)
                break
        _save_sessions(sessions)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    if not signature_header:
        return False
    expected = (
        "sha256="
        + hmac.new(
            WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


def _comment_on_issue(repo_full_name: str, issue_number: int, body: str) -> None:
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.post(url, json={"body": body}, headers=headers, timeout=15)
    resp.raise_for_status()
    logger.info("Commented on %s#%s", repo_full_name, issue_number)


# ---------------------------------------------------------------------------
# Devin API helpers
# ---------------------------------------------------------------------------


def _create_devin_session(prompt: str) -> dict:
    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{DEVIN_API_BASE}/sessions",
        json={"prompt": prompt},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _get_session_status(session_id: str) -> dict:
    headers = {"Authorization": f"Bearer {DEVIN_API_KEY}"}
    resp = requests.get(
        f"{DEVIN_API_BASE}/session/{session_id}",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"stopped", "failed", "finished"}


def _poll_session(
    session_id: str, session_url: str, repo_full_name: str, issue_number: int
) -> None:
    logger.info("Polling session %s every %ds", session_id, POLL_INTERVAL)
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            data = _get_session_status(session_id)
        except Exception:
            logger.exception("Error polling session %s", session_id)
            continue

        status = data.get("status_enum", "unknown")
        logger.info("Session %s status: %s", session_id, status)

        if status in TERMINAL_STATUSES:
            _update_session(
                session_id,
                {
                    "status": status,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            try:
                _comment_on_issue(
                    repo_full_name,
                    issue_number,
                    (
                        f"**Devin session {status}.**\n\n"
                        f"Session link: {session_url}\n\n"
                        f"Status: `{status}`"
                    ),
                )
            except Exception:
                logger.exception("Failed to comment on issue after completion")
            break


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "devin-automation"})


@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify signature if secret is configured
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        abort(403, "Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "issues":
        return jsonify({"skipped": True, "reason": f"event={event}"}), 200

    payload = request.get_json(force=True)
    action = payload.get("action")
    if action != "labeled":
        return jsonify({"skipped": True, "reason": f"action={action}"}), 200

    label = payload.get("label", {})
    if label.get("name") != "devin-fix":
        return jsonify({"skipped": True, "reason": "label not devin-fix"}), 200

    issue = payload.get("issue", {})
    issue_number = issue.get("number")
    issue_title = issue.get("title", "")
    issue_body = issue.get("body", "")
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    issue_url = issue.get("html_url", "")

    logger.info(
        "Received devin-fix label on %s#%s: %s",
        repo_full_name,
        issue_number,
        issue_title,
    )

    prompt = (
        f"Fix the following GitHub issue in the repository {repo_full_name}.\n\n"
        f"Issue #{issue_number}: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"Issue URL: {issue_url}\n\n"
        f"Please create a PR with the fix."
    )

    try:
        result = _create_devin_session(prompt)
    except Exception:
        logger.exception("Failed to create Devin session")
        return jsonify({"error": "Failed to create Devin session"}), 500

    session_id = result.get("session_id", "")
    session_url = result.get("url", "")

    record = {
        "session_id": session_id,
        "session_url": session_url,
        "repo": repo_full_name,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    _add_session(record)

    try:
        _comment_on_issue(
            repo_full_name,
            issue_number,
            (
                f"**Devin session started** to fix this issue.\n\n"
                f"Session link: {session_url}\n\n"
                f"I'll update this issue when the session completes."
            ),
        )
    except Exception:
        logger.exception("Failed to comment on issue")

    thread = threading.Thread(
        target=_poll_session,
        args=(session_id, session_url, repo_full_name, issue_number),
        daemon=True,
    )
    thread.start()

    return jsonify({"session_id": session_id, "session_url": session_url}), 201


@app.route("/dashboard", methods=["GET"])
def dashboard():
    sessions = _load_sessions()

    total = len(sessions)
    running = sum(1 for s in sessions if s["status"] == "running")
    completed = sum(1 for s in sessions if s["status"] == "finished")
    failed = sum(1 for s in sessions if s["status"] == "failed")
    stopped = sum(1 for s in sessions if s["status"] == "stopped")
    done = completed + failed + stopped
    success_rate = f"{(completed / done * 100):.1f}%" if done > 0 else "N/A"

    return render_template(
        "dashboard.html",
        sessions=sessions,
        total=total,
        running=running,
        completed=completed,
        failed=failed,
        success_rate=success_rate,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
