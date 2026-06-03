# Devin Superset Automation

> An **autonomous remediation pipeline** for Apache Superset, powered by the Devin API.
> One Devin session **plans**, multiple Devin sessions **execute in parallel**, and a
> CI feedback loop **self-corrects** failed PRs — with a live dashboard for observability.

Built as a hiring assignment for **Cognition.ai** (the team behind Devin).

---

## 🎯 The problem

Engineering teams accumulate two kinds of debt that nobody enjoys clearing:

1. **CVE-driven dependency upgrades** — high signal, low excitement
2. **Lint / static-analysis findings** — death by a thousand papercuts

Existing tools (Dependabot, Renovate, Snyk autofix) handle *trivial* version bumps
but break down when fixes require:
- reading a CHANGELOG to understand breaking changes,
- updating call sites and tests,
- interpreting CI failure logs,
- iterating until the build is green.

Those steps require an **autonomous coding agent**. That's where Devin comes in.

---

## 🏗️ Architecture

```
                        Trigger
                  (cron / webhook / curl)
                            │
                            ▼
            ┌──────────────────────────────┐
            │  ORCHESTRATOR Devin session  │   ← scans repo (pip-audit, ruff, bandit)
            │   plans, files GitHub issues │     emits REMEDIATION_PLAN_JSON
            └──────────────┬───────────────┘
                           │  controller parses plan
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
       Worker Devin   Worker Devin   Worker Devin    ← spawned in parallel
       session 1      session 2      session 3         (concurrency-limited)
            │              │              │
            ▼              ▼              ▼
          PR #1          PR #2          PR #3
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                  GitHub Actions CI
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
            ✅ green               ❌ failure
            ready for             POST /webhook/github
            review                │
                                  ▼
                       Controller re-messages
                       the SAME Devin session
                       with the failure logs
                                  │
                                  ▼
                       Devin pushes a fix to
                       the same branch
```

Live observability dashboard reads `/api/tasks` + `/api/metrics` and refreshes every 3s.

---

## ✨ What makes this design interesting

| Concept | Most demos | This system |
|---|---|---|
| **Issue discovery** | Human creates issues | Orchestrator Devin scans + creates them |
| **Trigger** | One webhook | Manual API + webhook + (extensible: cron) |
| **Session lifecycle** | Fire-and-forget | Polled state machine: queued → running → blocked → awaiting_ci → completed |
| **Failure handling** | Logged & ignored | CI failure → re-message same Devin session → self-correct |
| **Parallelism** | Sequential | `asyncio.create_task` + semaphore (concurrency-limited) |
| **Devin API surface** | `POST /sessions` only | Sessions + Messages (CI feedback, blocker recovery) |
| **Dogfooding** | None | Devin built part of this dashboard via the very API the system uses |

---

## 🚀 Quick start

### Prerequisites
- Docker Desktop
- ngrok (free tier is fine)
- A Devin API key — https://app.devin.ai → Settings → API Keys
- A GitHub fine-grained PAT scoped to your fork with: **Contents: write, Issues: write, Pull requests: write, Workflows: write, Administration: write** (the last one only if Issues are disabled on a fresh fork)
- A fork of Apache Superset under your account

### Run it

```bash
git clone https://github.com/thesaltree/devin-superset-automation
cd devin-superset-automation
cp .env.example .env
# edit .env and fill in DEVIN_API_KEY, GITHUB_TOKEN, GITHUB_REPO, GITHUB_WEBHOOK_SECRET

docker compose up --build -d
curl http://localhost:8080/healthz   # → {"status":"ok"}
```

### Expose the webhook (separate terminal)
```bash
ngrok http 8080
# copy the https://...ngrok-free.dev URL
```

In your forked Superset repo → **Settings → Webhooks → Add webhook**:
- Payload URL: `https://<ngrok-url>/webhook/github`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET` in `.env`
- Events: **Issues**, **Check runs**, **Pull requests**

### Trigger your first run
```bash
curl -X POST "http://localhost:8080/trigger/scan?max_tasks=3"
open http://localhost:8080/dashboard
```

You'll watch the Orchestrator Devin session scan the repo, file issues, and the controller spawn parallel worker sessions that open PRs.

---

## 📡 API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/trigger/scan?max_tasks=N` | Spawn an Orchestrator session |
| `POST` | `/trigger/dispatch_pending` | Spawn workers for any open `devin-task` issue without one |
| `POST` | `/trigger/unblock/{task_id}?message=...` | Send a follow-up to a blocked Devin session and resume polling |
| `POST` | `/webhook/github` | GitHub events (HMAC-verified) — handles `check_run` failure → re-message, `issues.labeled` → spawn worker |
| `GET`  | `/api/tasks` | Full task list (orchestrator + workers, with status, PRs, retries, durations) |
| `GET`  | `/api/metrics` | Aggregated metrics for the dashboard |
| `GET`  | `/dashboard` | Live dashboard |
| `GET`  | `/healthz` | Liveness probe |

---

## 📁 Project layout

```
devin-superset-automation/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── README.md
├── app/
│   ├── config.py            # Pydantic settings loaded from .env
│   ├── database.py          # SQLAlchemy + SQLite (swappable to Postgres)
│   ├── models.py            # Task model with full lifecycle states
│   ├── devin_client.py      # POST /sessions, GET /session/{id}, POST /session/{id}/message
│   ├── github_client.py     # Issue/PR/CI helpers + HMAC webhook signature verification
│   ├── prompts.py           # Orchestrator / Worker / CI-feedback prompt templates
│   ├── orchestrator.py      # Single planning session — parses REMEDIATION_PLAN_JSON
│   ├── worker_pool.py       # Parallel worker sessions, semaphore-limited
│   ├── ci_feedback.py       # Self-correction loop on CI failure
│   └── main.py              # FastAPI entrypoint
├── dashboard/
│   └── index.html           # Live dashboard, polls /api/tasks + /api/metrics
└── tests/
```

---

## 🔍 Observability — "How do I know it's working?"

If you were an engineering leader, here's what you'd see:

1. **Live dashboard** at `/dashboard` — every task, status pill, PR link, and Devin session URL
2. **Metrics** at `/api/metrics`:
   - `workers_total / completed / failed / running`
   - `prs_opened`
   - `success_rate_pct`
   - `avg_duration_seconds`
   - `ci_retries_total` ← the killer metric proving the self-correction loop works
3. **Devin's own session traces** — every action Devin takes is recorded in `https://app.devin.ai/sessions/{id}` (deep-linked from the dashboard)
4. **GitHub artifacts** — issues filed by the Orchestrator + PRs from each Worker, all labeled and traceable

---

## 🔮 Production hardening — what changes for a real customer

| Concern | This demo | Production |
|---|---|---|
| Credentials | Token sent via `send_message` on demand | Register as a **Devin Secret** so workers boot pre-authenticated |
| State store | SQLite | Postgres + Alembic migrations |
| Triggers | Manual + GitHub webhook | + APScheduler cron (weekly scans) + Linear/Jira webhook + Slack `/scan` slash command |
| Concurrency | In-process semaphore | Redis queue + worker pool |
| Retries | 3 CI feedback loops | Exponential backoff + dead-letter queue |
| RBAC | One service token | Devin **Service Users** + per-team RBAC |
| Plan persistence | Re-derived from session | Versioned plans with `parallel_safe` dependency graph |
| Self-improvement | None | After a PR merges, generate a SKILL.md so future Devins reuse the recipe |

---

## 🧠 Why Devin specifically?

A regular script can bump a version pin. Only an autonomous agent can:

1. Read `package.json` / `requirements/*.txt` to find the package
2. Read the upstream CHANGELOG to identify breaking changes
3. Find every call site across a 4M-line repo and update them
4. Run the test suite — read the failures — fix them
5. Push, open a PR, and **respond to CI failures by pushing more fixes**

That last bullet is the one that breaks every static automation tool. It requires
genuine agentic loops with environmental feedback — and that's the bar Devin clears.

---

## 📜 License

MIT — built as a take-home assignment for Cognition.
