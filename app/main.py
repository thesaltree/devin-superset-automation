"""FastAPI application — entry point.

Endpoints:
  POST /trigger/scan      Manually start the orchestrator
  POST /webhook/github    Receives GitHub events (check_run failures, label adds)
  GET  /dashboard         Live HTMX dashboard
  GET  /api/tasks         JSON list of all tasks (for dashboard polling)
  GET  /api/metrics       Aggregate metrics
  GET  /healthz           Liveness probe
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.ci_feedback import handle_check_run_event
from app.config import get_settings
from app.database import get_session as db_session, init_db
from app.devin_client import DevinClient
from app.github_client import GitHubClient
from app.models import Task
from app.orchestrator import poll_orchestrator, start_orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("automation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.devin = DevinClient()
    app.state.github = GitHubClient()
    log.info("Automation server started")
    yield
    await app.state.devin.aclose()
    await app.state.github.aclose()


app = FastAPI(title="Devin Superset Automation", lifespan=lifespan)

# Dashboard static + templates
try:
    app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")
except RuntimeError:
    pass  # static dir may not exist yet


# ---------------------------------------------------------------------- health
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------- manual trigger
@app.post("/trigger/scan")
async def trigger_scan(max_tasks: int = 5):
    """Kick off an Orchestrator session."""
    devin: DevinClient = app.state.devin
    task_id = await start_orchestrator(devin, max_tasks=max_tasks)
    asyncio.create_task(poll_orchestrator(task_id, devin))
    return {"orchestrator_task_id": task_id, "status": "started"}


@app.post("/trigger/dispatch_pending")
async def dispatch_pending():
    """Spawn workers for any open `devin-task` issue that has no worker yet.

    Useful after orchestrator manual-recovery or when issues are filed by humans.
    """
    from app.worker_pool import _spawn_and_poll_worker

    settings = get_settings()
    gh: GitHubClient = app.state.github
    issues = await gh.list_issues(label=settings.worker_label, state="open")

    with db_session() as s:
        worked = {
            t.issue_number for t in s.execute(select(Task).where(Task.role == "worker")).scalars()
        }

    dispatched = []
    for issue in issues:
        n = issue["number"]
        if n in worked:
            continue
        # Use asyncio.create_task so all workers truly run in parallel
        # (BackgroundTasks would run them sequentially)
        asyncio.create_task(
            _spawn_and_poll_worker({"issue_number": n}, app.state.devin, gh)
        )
        dispatched.append(n)
    return {"dispatched_for_issues": dispatched, "total_open_devin_task_issues": len(issues)}


@app.post("/trigger/unblock/{task_id}")
async def unblock_task(task_id: int, message: str):
    """Send a free-form follow-up message to a blocked Devin session, then resume polling."""
    from app.worker_pool import _poll_worker

    devin: DevinClient = app.state.devin
    with db_session() as s:
        task = s.get(Task, task_id)
        if not task:
            raise HTTPException(404, "task not found")
        session_id = task.session_id
        task.status = "running"
        task.error_message = None
        task.completed_at = None
        role = task.role

    await devin.send_message(session_id, message)

    if role == "worker":
        asyncio.create_task(_poll_worker(task_id, devin))
    else:
        from app.orchestrator import poll_orchestrator
        asyncio.create_task(poll_orchestrator(task_id, devin))

    return {"unblocked": task_id, "session_id": session_id}


@app.post("/trigger/reconcile")
async def reconcile():
    """Reconcile DB state with reality:

    - Workers with a PR but stale status -> "completed" (PR opened = success).
    - Orchestrators marked failed but whose plan produced PRs -> "completed".
    Useful for cleaning up after the demo run before recording.
    """
    from datetime import datetime as _dt

    fixed = []
    with db_session() as s:
        rows = s.execute(select(Task)).scalars().all()
        for t in rows:
            if t.role == "worker" and t.pr_url and t.status != "completed":
                t.status = "completed"
                t.error_message = None
                if not t.completed_at:
                    t.completed_at = _dt.utcnow()
                fixed.append({"id": t.id, "issue": t.issue_number, "pr": t.pr_number})
        # Orchestrator: if any worker has a PR, mark it completed
        any_pr = any(r.pr_url for r in rows if r.role == "worker")
        for t in rows:
            if t.role == "orchestrator" and any_pr and t.status != "completed":
                t.status = "completed"
                t.error_message = None
                if not t.completed_at:
                    t.completed_at = _dt.utcnow()
                fixed.append({"id": t.id, "role": "orchestrator"})
    return {"reconciled": fixed}


# ------------------------------------------------------------------- webhooks
@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
):
    settings = get_settings()
    body = await request.body()

    if not GitHubClient.verify_signature(
        settings.github_webhook_secret, body, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="bad signature")

    payload = await request.json()
    event = x_github_event
    log.info("Webhook received: %s", event)

    if event == "check_run":
        asyncio.create_task(
            handle_check_run_event(payload, app.state.devin, app.state.github)
        )
        return {"queued": "check_run"}

    if event == "issues" and payload.get("action") == "labeled":
        if (payload.get("label") or {}).get("name") == settings.worker_label:
            # Manual single-issue worker dispatch
            from app.worker_pool import _spawn_and_poll_worker

            issue = payload["issue"]
            task_meta = {"issue_number": issue["number"]}
            asyncio.create_task(
                _spawn_and_poll_worker(task_meta, app.state.devin, app.state.github)
            )
            return {"queued": f"worker for issue #{issue['number']}"}

    return {"ignored": event}


# ----------------------------------------------------------------------- API
@app.get("/api/tasks")
async def list_tasks():
    with db_session() as s:
        rows = s.execute(select(Task).order_by(Task.created_at.desc())).scalars().all()
        return [
            {
                "id": t.id,
                "role": t.role,
                "issue_number": t.issue_number,
                "issue_title": t.issue_title,
                "issue_url": t.issue_url,
                "session_id": t.session_id,
                "session_url": t.session_url,
                "status": t.status,
                "pr_url": t.pr_url,
                "pr_number": t.pr_number,
                "ci_retries": t.ci_retries,
                "error": t.error_message,
                "duration_seconds": t.duration_seconds(),
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in rows
        ]


@app.get("/api/metrics")
async def metrics():
    with db_session() as s:
        rows = s.execute(select(Task)).scalars().all()
        workers = [t for t in rows if t.role == "worker"]
        completed = [t for t in workers if t.status == "completed"]
        failed = [t for t in workers if t.status == "failed"]
        running = [t for t in workers if t.status in {"running", "awaiting_ci", "queued"}]
        durations = [t.duration_seconds() for t in completed if t.duration_seconds()]
        avg_dur = int(sum(durations) / len(durations)) if durations else 0
        success_rate = (
            round(100 * len(completed) / (len(completed) + len(failed)))
            if (completed or failed)
            else 0
        )
        return {
            "orchestrators": len([t for t in rows if t.role == "orchestrator"]),
            "workers_total": len(workers),
            "workers_completed": len(completed),
            "workers_failed": len(failed),
            "workers_running": len(running),
            "ci_retries_total": sum(t.ci_retries for t in workers),
            "success_rate_pct": success_rate,
            "avg_duration_seconds": avg_dur,
            "prs_opened": len([t for t in workers if t.pr_url]),
        }


# -------------------------------------------------------------- dashboard UI
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    from pathlib import Path

    path = Path("dashboard/index.html")
    if not path.exists():
        return HTMLResponse("<h1>Dashboard not built yet</h1>", status_code=200)
    return HTMLResponse(path.read_text())


@app.get("/", response_class=JSONResponse)
async def root():
    return {
        "name": "Devin Superset Automation",
        "endpoints": [
            "POST /trigger/scan",
            "POST /webhook/github",
            "GET  /dashboard",
            "GET  /api/tasks",
            "GET  /api/metrics",
        ],
    }
