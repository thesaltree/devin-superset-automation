# Devin Superset Automation

An **autonomous remediation pipeline** for Apache Superset, powered by the Devin API.

One Devin session **plans** (scans the repo, files issues, builds a remediation plan).
Multiple Devin sessions **execute** in parallel (one PR per issue).
A CI feedback loop **self-corrects** failed PRs by re-messaging the same Devin session.
A live dashboard reports everything in real time.

## Architecture

```
Trigger (cron / webhook / manual)
        │
        ▼
┌─────────────────────────────┐
│  ORCHESTRATOR Devin session │  scans repo → creates issues → emits plan
└──────────────┬──────────────┘
               │
   ┌───────────┼───────────┐
   ▼           ▼           ▼
 Worker 1   Worker 2   Worker 3   (parallel Devin sessions, one per issue)
   │           │           │
   ▼           ▼           ▼
  PR #1      PR #2       PR #3
   │           │           │
   └───────────┼───────────┘
               ▼
       GitHub Actions CI
               │
       on failure → re-message same Devin session → self-correct
```

## Quick start

1. `cp .env.example .env` and fill in your Devin API key, GitHub token, and webhook secret.
2. `docker compose up --build`
3. In another terminal: `ngrok http 8080` and copy the public HTTPS URL.
4. In your forked Superset repo, add a webhook:
   - URL: `https://<ngrok-url>/webhook/github`
   - Secret: same value as `GITHUB_WEBHOOK_SECRET` in `.env`
   - Events: `Issues`, `Check runs`, `Pull requests`
5. Trigger your first scan: `curl -X POST http://localhost:8080/trigger/scan`
6. Open the dashboard: http://localhost:8080/dashboard

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/trigger/scan` | Manually start the Orchestrator |
| `POST` | `/webhook/github` | Receives GitHub events (CI failures, label adds) |
| `GET`  | `/dashboard` | Live HTMX dashboard |
| `GET`  | `/api/tasks` | JSON list of all tasks |
| `GET`  | `/healthz` | Liveness probe |
