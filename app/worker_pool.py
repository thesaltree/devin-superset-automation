"""Worker pool: spawns one Devin session per remediation task and polls each."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from app.config import get_settings
from app.database import get_session as db_session
from app.devin_client import DevinClient
from app.github_client import GitHubClient
from app.models import Task
from app.prompts import WORKER_PROMPT

log = logging.getLogger(__name__)

# Match GitHub PR URLs in Devin's output
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)")


async def dispatch_workers(plan: dict, devin: DevinClient, gh: GitHubClient) -> None:
    """Spawn worker sessions for the plan, respecting concurrency limit."""
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrent_workers)
    tasks_in_plan = plan.get("tasks", [])

    async def _run(task_meta: dict) -> None:
        async with sem:
            await _spawn_and_poll_worker(task_meta, devin, gh)

    await asyncio.gather(
        *[_run(t) for t in tasks_in_plan],
        return_exceptions=True,
    )
    log.info("All %d worker tasks dispatched", len(tasks_in_plan))


async def _spawn_and_poll_worker(
    task_meta: dict,
    devin: DevinClient,
    gh: GitHubClient,
) -> None:
    settings = get_settings()
    issue_number = task_meta["issue_number"]

    # Fetch the issue body so Devin gets the full context
    try:
        issues = await gh.list_issues(label=settings.worker_label, state="open")
        issue = next((i for i in issues if i["number"] == issue_number), None)
    except Exception:
        issue = None

    if issue is None:
        log.warning("Issue #%s not found by label; skipping", issue_number)
        return

    prompt = WORKER_PROMPT.format(
        repo=settings.github_repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        issue_url=issue["html_url"],
        issue_body=(issue.get("body") or "")[:6000],
    )

    resp = await devin.create_session(
        prompt=prompt,
        title=f"[Worker] Issue #{issue_number}: {issue['title'][:60]}",
        tags=["worker", "superset-automation", f"issue-{issue_number}"],
    )

    # Inject GH credentials so the worker can git push + gh pr create without asking.
    # (Production: register as a Devin Secret instead — see README.)
    try:
        await devin.send_message(
            resp["session_id"],
            (
                "Here is the GitHub token for this session — keep it out of logs/commits.\n"
                f"  export GH_TOKEN={settings.github_token}\n"
                f"  echo $GH_TOKEN | gh auth login --with-token\n"
                "  git config --global credential.helper '!f() { echo username=x-access-token; "
                f"echo password={settings.github_token}; }}; f'\n"
                "Use this for both `git push` and `gh pr create`. Then proceed with your task."
            ),
        )
    except Exception as e:
        log.warning("Could not seed token to worker session: %s", e)

    with db_session() as s:
        task = Task(
            role="worker",
            issue_number=issue_number,
            issue_title=issue["title"],
            issue_url=issue["html_url"],
            session_id=resp["session_id"],
            session_url=resp.get("url"),
            status="running",
            branch_name=f"devin/issue-{issue_number}",
        )
        s.add(task)
        s.flush()
        task_id = task.id

    log.info("Worker spawned for issue #%s session=%s", issue_number, resp["session_id"])
    await _poll_worker(task_id, devin)


async def _poll_worker(task_id: int, devin: DevinClient) -> None:
    poll_interval = 30
    max_wait_minutes = 60
    elapsed = 0

    while elapsed < max_wait_minutes * 60:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        with db_session() as s:
            task = s.get(Task, task_id)
            if not task:
                return
            session_id = task.session_id

        try:
            data = await devin.get_session(session_id)
        except Exception as e:
            log.warning("Worker poll error %s: %s", session_id, e)
            continue

        status = data.get("status_enum") or data.get("status")
        pr_url, pr_num = _scan_for_pr(data)

        with db_session() as s:
            task = s.get(Task, task_id)
            if pr_url and not task.pr_url:
                task.pr_url = pr_url
                task.pr_number = pr_num
                task.status = "awaiting_ci"
                log.info("Worker for issue #%s opened PR %s", task.issue_number, pr_url)
            elif DevinClient.is_blocked(status):
                # Devin is waiting for human input — surface this in dashboard
                task.status = "blocked"

            if DevinClient.is_terminal(status):
                if DevinClient.is_success(status) and task.pr_url:
                    # Stay in awaiting_ci until CI feedback closes the loop
                    pass
                elif DevinClient.is_success(status) and not task.pr_url:
                    task.status = "failed"
                    task.error_message = "Devin finished without opening a PR"
                    task.completed_at = datetime.utcnow()
                else:
                    task.status = "failed"
                    task.error_message = f"Devin terminal status: {status}"
                    task.completed_at = datetime.utcnow()
                return


def _scan_for_pr(session_data: dict) -> tuple[str | None, int | None]:
    """Look for a PR URL in Devin's session output / messages."""
    candidates: list[str] = []

    pulls = session_data.get("pull_requests") or []
    for p in pulls:
        if isinstance(p, dict) and p.get("url"):
            candidates.append(p["url"])

    for key in ("output", "result", "structured_output"):
        v = session_data.get(key)
        if isinstance(v, str):
            candidates.append(v)

    for m in session_data.get("messages") or []:
        if isinstance(m, dict):
            c = m.get("message") or m.get("content") or ""
            if isinstance(c, str):
                candidates.append(c)

    for text in candidates:
        match = PR_URL_RE.search(text)
        if match:
            return match.group(0), int(match.group(1))
    return None, None
