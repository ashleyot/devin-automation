"""
Devin Automation Server

Listens for GitHub webhook events, triggers Devin sessions for issues
labelled "devin-fix", polls for completion, and serves a live dashboard.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEVIN_API_KEY = os.getenv("DEVIN_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

DEVIN_API_BASE = "https://api.devin.ai/v1"
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_FILE = DATA_DIR / "sessions.json"


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def _load_sessions() -> list[dict]:
    if SESSIONS_FILE.exists() and SESSIONS_FILE.is_file():
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


@app.route("/dashboard/clear", methods=["POST"])
def dashboard_clear():
    with _lock:
        _save_sessions([])
    return redirect("/dashboard")


# ---------------------------------------------------------------------------
# Repo Auditor
# ---------------------------------------------------------------------------

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
}


def _gh_headers() -> dict:
    headers = dict(GH_HEADERS)
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _gh_get(url: str, params: dict | None = None) -> requests.Response:
    return requests.get(url, headers=_gh_headers(), params=params, timeout=20)


def _gh_get_file(repo: str, path: str) -> str | None:
    """Fetch a file's contents from GitHub. Returns decoded text or None."""
    resp = _gh_get(f"{GH_API}/repos/{repo}/contents/{path}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content")


def _gh_search_code(repo: str, query: str) -> list[dict]:
    """Search code in a repo via GitHub search API. Returns list of items."""
    resp = _gh_get(
        f"{GH_API}/search/code",
        params={"q": f"{query} repo:{repo}", "per_page": 15},
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("items", [])


def _gh_tree(repo: str, path: str = "") -> list[dict]:
    """List directory contents from GitHub."""
    resp = _gh_get(f"{GH_API}/repos/{repo}/contents/{path}")
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _gh_create_issue(repo: str, title: str, body: str, labels: list[str]) -> dict:
    """Create a GitHub issue and return the response JSON."""
    resp = requests.post(
        f"{GH_API}/repos/{repo}/issues",
        headers=_gh_headers(),
        json={"title": title, "body": body, "labels": labels},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# -- Scanners ----------------------------------------------------------------

KNOWN_VULNERABLE = {
    "pyyaml": {"below": "6.0", "cve": "CVE-2020-14343", "desc": "Arbitrary code execution via yaml.load()"},
    "jinja2": {"below": "3.1.5", "cve": "CVE-2024-56326", "desc": "Sandbox escape vulnerability"},
    "flask": {"below": "2.3.2", "cve": "CVE-2023-30861", "desc": "Session cookie security bypass"},
    "werkzeug": {"below": "3.0.6", "cve": "CVE-2024-49767", "desc": "Potential denial of service"},
    "certifi": {"below": "2023.7.22", "cve": "CVE-2023-37920", "desc": "Removal of e-Tugra root certificate"},
    "cryptography": {"below": "41.0.6", "cve": "CVE-2023-49083", "desc": "NULL pointer dereference"},
    "pillow": {"below": "10.0.1", "cve": "CVE-2023-44271", "desc": "Denial of service via large image"},
    "requests": {"below": "2.32.0", "cve": "CVE-2024-35195", "desc": "Session credential leak on redirect"},
    "sqlalchemy": {"below": "2.0.36", "cve": "CVE-2024-11986", "desc": "SQL injection in has_table()"},
    "urllib3": {"below": "2.0.7", "cve": "CVE-2023-45803", "desc": "Request body leak on redirect"},
    "setuptools": {"below": "70.0", "cve": "CVE-2024-6345", "desc": "Remote code execution via download"},
}


def _parse_version(ver_str: str) -> tuple:
    """Parse a version string into a comparable tuple of ints."""
    parts = re.split(r"[^0-9]+", ver_str.strip())
    return tuple(int(p) for p in parts if p)


def _version_below(installed: str, threshold: str) -> bool:
    """Check if installed version is below threshold."""
    try:
        return _parse_version(installed) < _parse_version(threshold)
    except (ValueError, IndexError):
        return False


def _scan_requirements(repo: str) -> list[dict]:
    """Check requirements files for known vulnerable packages."""
    findings = []
    for req_path in ("requirements/base.txt", "requirements.txt"):
        content = _gh_get_file(repo, req_path)
        if not content:
            continue
        for line_num, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            match = re.match(r"([a-zA-Z0-9_-]+)\s*[=<>!~]+\s*([\d.]+)", line)
            if not match:
                continue
            pkg = match.group(1).lower().replace("-", "").replace("_", "")
            ver = match.group(2)
            for known_pkg, info in KNOWN_VULNERABLE.items():
                norm = known_pkg.replace("-", "").replace("_", "")
                if pkg == norm and _version_below(ver, info["below"]):
                    findings.append({
                        "type": "Security",
                        "title": f"{match.group(1)}=={ver} has known vulnerability ({info['cve']})",
                        "file": req_path,
                        "line": line_num,
                        "description": f"{info['desc']}. Upgrade to >= {info['below']}.",
                    })
    return findings


def _scan_package_json(repo: str) -> list[dict]:
    """Check package.json for deprecated or outdated dependencies."""
    findings = []
    content = _gh_get_file(repo, "package.json")
    if not content:
        content = _gh_get_file(repo, "superset-frontend/package.json")
    if not content:
        return findings
    try:
        pkg = json.loads(content)
    except json.JSONDecodeError:
        return findings

    deprecated_pkgs = {
        "request": "The 'request' package is deprecated. Use 'node-fetch' or 'axios'.",
        "querystring": "Built into Node.js; the npm package is deprecated.",
        "uuid": None,
        "tslint": "TSLint is deprecated. Migrate to ESLint with @typescript-eslint.",
        "enzyme": "Enzyme is deprecated. Use React Testing Library.",
        "moment": "moment.js is in maintenance mode. Consider day.js or date-fns.",
    }

    all_deps = {}
    for section in ("dependencies", "devDependencies"):
        all_deps.update(pkg.get(section, {}))

    for dep_name, dep_ver in all_deps.items():
        if dep_name in deprecated_pkgs and deprecated_pkgs[dep_name]:
            findings.append({
                "type": "Deps",
                "title": f"Deprecated dependency: {dep_name}",
                "file": "package.json",
                "line": None,
                "description": deprecated_pkgs[dep_name],
            })
    return findings


def _scan_console_logs(repo: str) -> list[dict]:
    """Search for console.log statements in frontend src files."""
    findings = []
    items = _gh_search_code(repo, "console.log path:src extension:ts extension:tsx extension:js")
    for item in items[:10]:
        file_path = item.get("path", "")
        if "/test" in file_path or "/__test" in file_path or ".test." in file_path or ".spec." in file_path:
            continue
        content = _gh_get_file(repo, file_path)
        if not content:
            continue
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if "console.log(" in stripped and not stripped.startswith("//") and not stripped.startswith("*"):
                findings.append({
                    "type": "Code Quality",
                    "title": f"console.log statement in production code",
                    "file": file_path,
                    "line": line_num,
                    "description": f"Remove or replace with a proper logging mechanism: {stripped[:120]}",
                })
                break
    return findings


def _scan_python_docstrings(repo: str) -> list[dict]:
    """Check for Python functions missing docstrings in utils folders."""
    findings = []
    utils_dirs = ["utils", "superset/utils"]
    for utils_dir in utils_dirs:
        entries = _gh_tree(repo, utils_dir)
        for entry in entries:
            if entry.get("type") != "file" or not entry["name"].endswith(".py"):
                continue
            if entry["name"].startswith("_"):
                continue
            file_path = entry.get("path", f"{utils_dir}/{entry['name']}")
            content = _gh_get_file(repo, file_path)
            if not content:
                continue
            lines = content.splitlines()
            for i, line in enumerate(lines):
                match = re.match(r"^def ([a-zA-Z][a-zA-Z0-9_]*)\s*\(", line)
                if not match:
                    continue
                func_name = match.group(1)
                if func_name.startswith("_"):
                    continue
                next_nonblank = ""
                for j in range(i + 1, min(i + 10, len(lines))):
                    stripped = lines[j].strip()
                    if stripped and not stripped.startswith(")") and stripped not in (")", "):"):
                        next_nonblank = stripped
                        break
                if not (next_nonblank.startswith('"""') or next_nonblank.startswith("'''")):
                    findings.append({
                        "type": "Docs",
                        "title": f"Function `{func_name}()` missing docstring",
                        "file": file_path,
                        "line": i + 1,
                        "description": f"Public function `{func_name}` lacks a docstring. Add one describing its purpose, arguments, and return value.",
                    })
            if len(findings) > 30:
                break
        if len(findings) > 30:
            break
    return findings[:20]


def _scan_ts_return_types(repo: str) -> list[dict]:
    """Check for TypeScript functions missing return types in utils folders."""
    findings = []
    ts_utils_dirs = ["src/utils", "superset-frontend/src/utils"]
    for utils_dir in ts_utils_dirs:
        entries = _gh_tree(repo, utils_dir)
        for entry in entries:
            if entry.get("type") != "file":
                continue
            name = entry["name"]
            if not (name.endswith(".ts") or name.endswith(".tsx")):
                continue
            if name.endswith(".test.ts") or name.endswith(".test.tsx"):
                continue
            file_path = entry.get("path", f"{utils_dir}/{name}")
            content = _gh_get_file(repo, file_path)
            if not content:
                continue
            lines = content.splitlines()
            for i, line in enumerate(lines):
                match = re.match(
                    r"^export\s+(?:async\s+)?function\s+([a-zA-Z][a-zA-Z0-9_]*)\s*"
                    r"(?:<[^>]*>)?\s*\([^)]*\)\s*\{",
                    line,
                )
                if match:
                    func_name = match.group(1)
                    if "): " not in line and "):" not in line.split("{")[0]:
                        findings.append({
                            "type": "Code Quality",
                            "title": f"Function `{func_name}()` missing return type",
                            "file": file_path,
                            "line": i + 1,
                            "description": f"Exported function `{func_name}` lacks an explicit return type annotation. Add a return type for better type safety.",
                        })
            if len(findings) > 20:
                break
        if len(findings) > 20:
            break
    return findings[:15]


@app.route("/audit", methods=["GET"])
def audit_page():
    return render_template("audit.html")


@app.route("/audit/run", methods=["GET"])
def audit_run():
    repo = request.args.get("repo", "").strip()
    if not repo or "/" not in repo:
        return jsonify({"error": "Invalid repo. Use format: owner/repo"}), 400

    # Verify repo exists
    resp = _gh_get(f"{GH_API}/repos/{repo}")
    if resp.status_code == 404:
        return jsonify({"error": f"Repository '{repo}' not found"}), 404
    if resp.status_code != 200:
        return jsonify({"error": f"GitHub API error: {resp.status_code}"}), 502

    findings = []
    findings.extend(_scan_requirements(repo))
    findings.extend(_scan_package_json(repo))
    findings.extend(_scan_console_logs(repo))
    findings.extend(_scan_python_docstrings(repo))
    findings.extend(_scan_ts_return_types(repo))

    return jsonify({"repo": repo, "findings": findings, "scanned_at": datetime.now(timezone.utc).isoformat()})


@app.route("/audit/create-issue", methods=["POST"])
def audit_create_issue():
    data = request.get_json(force=True)
    repo = data.get("repo", "")
    finding = data.get("finding", {})

    if not repo or not finding:
        return jsonify({"error": "Missing repo or finding"}), 400
    if not GITHUB_TOKEN:
        return jsonify({"error": "GITHUB_TOKEN not configured"}), 500

    title = f"[{finding.get('type', 'Audit')}] {finding.get('title', 'Audit finding')}"
    file_ref = finding.get("file", "")
    line_ref = finding.get("line")
    location = f"`{file_ref}`" + (f" (line {line_ref})" if line_ref else "")

    body = (
        f"## Issue found by Repo Auditor\n\n"
        f"**Type:** {finding.get('type', 'Unknown')}\n\n"
        f"**Location:** {location}\n\n"
        f"**Description:**\n{finding.get('description', '')}\n\n"
        f"---\n"
        f"*This issue was automatically created by the Repo Auditor.*"
    )

    try:
        result = _gh_create_issue(repo, title, body, ["devin-fix"])
        return jsonify({
            "issue_number": result["number"],
            "issue_url": result["html_url"],
        })
    except Exception as exc:
        logger.exception("Failed to create issue on %s", repo)
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
