"""Thin async client for the Devin API.

Reference: https://docs.devin.ai/api-reference/v1/overview

Endpoints used:
- POST   /v1/sessions                       create a session
- GET    /v1/session/{id}                   poll status / output
- POST   /v1/session/{id}/message           send a follow-up message (used by CI feedback loop)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class DevinClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.devin_api_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {s.devin_api_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=30.0, headers=self._headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ sessions
    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        idempotent: bool = True,
        unlisted: bool = False,
    ) -> dict[str, Any]:
        """Create a new Devin session and return the response payload.

        The payload typically contains `session_id` and `url`.
        """
        body: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if unlisted:
            body["unlisted"] = True

        r = await self._client.post(f"{self._base}/sessions", json=body)
        r.raise_for_status()
        data = r.json()
        log.info("Created Devin session %s", data.get("session_id"))
        return data

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Fetch session details. Status is in `status_enum` per Devin docs."""
        r = await self._client.get(f"{self._base}/session/{session_id}")
        r.raise_for_status()
        return r.json()

    async def send_message(self, session_id: str, message: str) -> None:
        """Append a message to an existing Devin session (resumes if suspended)."""
        r = await self._client.post(
            f"{self._base}/session/{session_id}/message",
            json={"message": message},
        )
        r.raise_for_status()
        log.info("Sent message to Devin session %s", session_id)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def is_terminal(status: str | None) -> bool:
        """Truly-terminal Devin statuses: finished, stopped, expired.

        NOTE: `blocked` is NOT terminal — Devin pauses waiting for user input
        and will resume the moment we POST a message to the session.
        """
        if not status:
            return False
        return status.lower() in {"finished", "stopped", "expired"}

    @staticmethod
    def is_blocked(status: str | None) -> bool:
        return (status or "").lower() == "blocked"

    @staticmethod
    def is_success(status: str | None) -> bool:
        if not status:
            return False
        return status.lower() in {"finished"}
