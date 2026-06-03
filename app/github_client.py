"""Thin async GitHub client for issue + PR + CI operations."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self) -> None:
        s = get_settings()
        self._repo = s.github_repo
        self._headers = {
            "Authorization": f"Bearer {s.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=self._headers,
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- issues
    async def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        r = await self._client.post(
            f"/repos/{self._repo}/issues",
            json={"title": title, "body": body, "labels": labels or []},
        )
        r.raise_for_status()
        return r.json()

    async def list_issues(self, label: str, state: str = "open") -> list[dict[str, Any]]:
        r = await self._client.get(
            f"/repos/{self._repo}/issues",
            params={"labels": label, "state": state, "per_page": 100},
        )
        r.raise_for_status()
        # GitHub /issues returns PRs too; filter them out
        return [i for i in r.json() if "pull_request" not in i]

    # ------------------------------------------------------------------ PRs
    async def get_pr(self, pr_number: int) -> dict[str, Any]:
        r = await self._client.get(f"/repos/{self._repo}/pulls/{pr_number}")
        r.raise_for_status()
        return r.json()

    async def find_pr_by_branch(self, branch: str) -> dict[str, Any] | None:
        r = await self._client.get(
            f"/repos/{self._repo}/pulls",
            params={"head": f"{self._repo.split('/')[0]}:{branch}", "state": "open"},
        )
        r.raise_for_status()
        prs = r.json()
        return prs[0] if prs else None

    # ---------------------------------------------------------------- CI logs
    async def fetch_check_run_logs(self, check_run_id: int) -> str:
        """Best-effort fetch of CI failure context. Falls back to summary text."""
        r = await self._client.get(f"/repos/{self._repo}/check-runs/{check_run_id}")
        if r.status_code != 200:
            return f"(could not fetch check run {check_run_id}: {r.status_code})"
        cr = r.json()
        parts = [
            f"Check name: {cr.get('name')}",
            f"Conclusion: {cr.get('conclusion')}",
            f"Title: {cr.get('output', {}).get('title')}",
            f"Summary:\n{cr.get('output', {}).get('summary') or '(none)'}",
            f"Details URL: {cr.get('details_url')}",
        ]
        text = (cr.get("output") or {}).get("text")
        if text:
            parts.append(f"Text:\n{text[:4000]}")
        return "\n".join(parts)

    # ------------------------------------------------------- webhook signature
    @staticmethod
    def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = (
            "sha256="
            + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(expected, signature_header)
