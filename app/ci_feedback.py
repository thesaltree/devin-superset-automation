"""CI feedback loop: re-message the Devin session when its PR fails CI."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from app.database import get_session as db_session
from app.devin_client import DevinClient
from app.github_client import GitHubClient
from app.models import Task
from app.prompts import CI_FEEDBACK_PROMPT

log = logging.getLogger(__name__)

MAX_CI_RETRIES = 3


async def handle_check_run_event(
    payload: dict,
    devin: DevinClient,
    gh: GitHubClient,
) -> str:
    """Triggered by GitHub `check_run` webhook. Returns a status message."""
    cr = payload.get("check_run", {})
    if cr.get("status") != "completed":
        return "ignored: not completed"
    if cr.get("conclusion") not in {"failure", "timed_out", "cancelled"}:
        if cr.get("conclusion") == "success":
            return await _on_ci_success(payload)
        return f"ignored: conclusion={cr.get('conclusion')}"

    # Failure path — find owning Task by branch name
    branch = (cr.get("check_suite") or {}).get("head_branch") \
        or _branch_from_pulls(cr.get("pull_requests"))
    if not branch:
        return "ignored: no branch"

    with db_session() as s:
        task = s.execute(
            select(Task).where(Task.branch_name == branch, Task.role == "worker")
        ).scalar_one_or_none()
        if not task:
            return f"no task tracking branch {branch}"
        if task.ci_retries >= MAX_CI_RETRIES:
            task.status = "failed"
            task.error_message = f"Exceeded {MAX_CI_RETRIES} CI retries"
            task.completed_at = datetime.utcnow()
            return f"giving up on {branch} after {MAX_CI_RETRIES} retries"

        task.ci_retries += 1
        task.status = "running"
        retry_n = task.ci_retries
        issue_number = task.issue_number
        session_id = task.session_id

    logs = await gh.fetch_check_run_logs(cr["id"])
    feedback = CI_FEEDBACK_PROMPT.format(
        issue_number=issue_number,
        ci_logs=logs[:6000],
        retry_number=retry_n,
    )
    await devin.send_message(session_id, feedback)
    log.info("Sent CI feedback to session=%s retry=%d", session_id, retry_n)
    return f"ci feedback sent (retry {retry_n}/{MAX_CI_RETRIES})"


async def _on_ci_success(payload: dict) -> str:
    cr = payload["check_run"]
    branch = (cr.get("check_suite") or {}).get("head_branch")
    if not branch:
        return "success ignored: no branch"
    with db_session() as s:
        task = s.execute(
            select(Task).where(Task.branch_name == branch, Task.role == "worker")
        ).scalar_one_or_none()
        if not task:
            return "no task to mark complete"
        # Only mark complete if all required checks have passed.
        # For simplicity treat any green check as completion signal.
        if task.status != "completed":
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            return f"task #{task.id} marked completed"
        return "already completed"


def _branch_from_pulls(pulls) -> str | None:
    if not pulls:
        return None
    if isinstance(pulls, list) and pulls:
        return (pulls[0].get("head") or {}).get("ref")
    return None
