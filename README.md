# Devin Automation Server

A Flask server that listens for GitHub webhook events and automatically triggers [Devin](https://devin.ai) sessions to fix issues labelled **devin-fix**.

## Features

- **Webhook listener** — receives GitHub `issues` events on `POST /webhook`
- **Label trigger** — only acts when the `devin-fix` label is applied
- **Devin API integration** — creates a Devin session with the issue context
- **Polling** — monitors the session until it reaches a terminal state
- **GitHub comments** — posts the session link and final status back on the issue
- **Session persistence** — saves all session records to `sessions.json`
- **Live dashboard** — shows stats and a table of all sessions at `/dashboard`
- **Containerised** — ships with `Dockerfile` and `docker-compose.yml`

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and set DEVIN_API_KEY and GITHUB_TOKEN
```

| Variable | Required | Description |
|---|---|---|
| `DEVIN_API_KEY` | Yes | Devin API key ([get one here](https://app.devin.ai/settings/api-keys)) |
| `GITHUB_TOKEN` | Yes | GitHub personal access token with `repo` scope |
| `WEBHOOK_SECRET` | No | Shared secret for webhook signature verification |
| `POLL_INTERVAL` | No | Seconds between status polls (default: `30`) |

### 2. Run with Docker

```bash
docker compose up --build -d
```

The server starts on port **5050**.

### 3. Run without Docker

```bash
pip install -r requirements.txt
python app.py
```

### 4. Set up the GitHub webhook

1. Go to your repo → **Settings** → **Webhooks** → **Add webhook**
2. **Payload URL:** `https://your-server:5050/webhook`
3. **Content type:** `application/json`
4. **Secret:** same value as `WEBHOOK_SECRET` in `.env` (optional)
5. **Events:** select **Issues**

## How It Works

```
GitHub Issue            Automation Server            Devin API
     │                        │                         │
     │── label: devin-fix ──▶ │                         │
     │                        │── POST /v1/sessions ──▶ │
     │                        │◀── session_id, url ─── │
     │◀── comment (started) ──│                         │
     │                        │                         │
     │                        │── GET /v1/session/… ──▶ │  (poll)
     │                        │◀── status ──────────── │
     │                        │         ...             │
     │◀── comment (done) ─────│                         │
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/webhook` | GitHub webhook receiver |
| `GET` | `/dashboard` | Live dashboard UI |

## Dashboard

Visit `http://localhost:5050/dashboard` to see:

- Total tasks, running, completed, and failed counts
- Success rate percentage
- A table of all sessions with links to the Devin session and GitHub issue

The page auto-refreshes every 15 seconds.

## Project Structure

```
devin-automation/
├── app.py                 # Flask application
├── templates/
│   └── dashboard.html     # Dashboard template
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container image
├── docker-compose.yml     # Compose configuration
├── .dockerignore
├── .env.example           # Environment template
├── sessions.json          # Session records (auto-created)
└── README.md
```
