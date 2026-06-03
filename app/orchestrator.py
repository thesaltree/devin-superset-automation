"""Orchestrator: a single Devin session that plans the work for everyone else.

Lifecycle:
  1. Build prompt from template.
  2. Create Devin session.
  3. Persist as a Task(role="orchestrator", status="running").
  4. Background poll until terminal.
  5. On success, parse `REMEDIATION_PLAN_JSON:` from session messages/output.
  6. Hand off plan to the worker pool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from app.config import get_settings
from app.database import get_session as db_session
from app.devin_client import DevinClient
from app.github_client import GitHubClient
from app.models import Task
from app.prompts import ORCHESTRATOR_PROMPT

log = logging.getLogger(__name__)

PLAN_MARKER = re.compile(r"REMEDIATION_PLAN_JSON:\s*(\{.*\})", re.DOTALL)


async def start_orchestrator(devin: DevinClient, *, max_tasks: int = 5) -> int:
    """Kick off the orchestrator session. Returns the Task row id."""
    settings = get_settings()
    prompt = ORCHESTRATOR_PROMPT.format(
        repo=settings.github_repo,
        max_tasks=max_tasks,
        worker_label=settings.worker_label,
    )

    resp = await devin.create_session(
        prompt=prompt,
        title=f"[Orchestrator] Scan {settings.github_repo}",
        tags=["orchestrator", "superset-automation"],
    )

    with db_session() as s:
        task = Task(
            role="orchestrator",
            session_id=resp["session_id"],
            session_url=resp.get("url"),
            status="running",
        )
        s.add(task)
        s.flush()
        task_id = task.id

    log.info("Orchestrator started: session=%s task_id=%s", resp["session_id"], task_id)
    return task_id


async def poll_orchestrator(task_id: int, devin: DevinClient) -> None:
    """Poll the orchestrator session until it finishes, then parse the plan."""
    from app.worker_pool import dispatch_workers  # local import to avoid cycle

    poll_interval = 30  # seconds
    while True:
        await asyncio.sleep(poll_interval)
        with db_session() as s:
            task = s.get(Task, task_id)
            if not task:
                return
            session_id = task.session_id

        try:
            data = await devin.get_session(session_id)
        except Exception as e:
            log.exception("Failed to poll orchestrator %s: %s", session_id, e)
            continue

        status = data.get("status_enum") or data.get("status")
        log.info("Orchestrator %s status=%s", session_id, status)

        if not DevinClient.is_terminal(status):
            continue

        # Terminal — finalize
        with db_session() as s:
            task = s.get(Task, task_id)
            task.completed_at = datetime.utcnow()
            if DevinClient.is_success(status):
                plan = _extract_plan(data)
                if plan:
                    task.status = "completed"
                    task.structured_output = plan
                    log.info("Orchestrator produced plan with %d tasks",
                             len(plan.get("tasks", [])))
                    plan_to_dispatch = plan
                else:
                    task.status = "failed"
                    task.error_message = "Could not parse REMEDIATION_PLAN_JSON from output"
                    plan_to_dispatch = None
            else:
                task.status = "failed"
                task.error_message = f"Devin terminal status: {status}"
                plan_to_dispatch = None

        if plan_to_dispatch:
            gh = GitHubClient()
            try:
                await dispatch_workers(plan_to_dispatch, devin, gh)
            finally:
                await gh.aclose()
        return


def _extract_plan(session_data: dict) -> dict | None:
    """Search the session output / messages for the REMEDIATION_PLAN_JSON marker."""
    haystacks: list[str] = []

    # Devin returns these fields in different shapes across API versions
    for key in ("structured_output", "output", "result"):
        v = session_data.get(key)
        if isinstance(v, dict):
            return v if "tasks" in v else None
        if isinstance(v, str):
            haystacks.append(v)

    messages = session_data.get("messages") or []
    for m in messages:
        if isinstance(m, dict):
            content = m.get("message") or m.get("content") or ""
            if isinstance(content, str):
                haystacks.append(content)

    for text in haystacks:
        m = PLAN_MARKER.search(text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                log.warning("Found plan marker but JSON failed to parse")
    return None
