"""
GitHub Connector.

Ingests: push events, pull requests, issues, issue comments.

Auth: GitHub App (preferred) or Personal Access Token for prototype.
Webhook secret is used to validate incoming webhook payloads.

Key design decisions:
  - PR body and commit messages are summarized by LLM in enrichment, not here.
  - We capture the structural signal (who, what repo, what file) not the content.
  - Polling uses the Events API (per-repo). Phase 2: GitHub App webhooks only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from aios_core.config import settings
from aios_core.schemas.events import (
    NormalizedEvent,
    RawEvent,
    SourceSystem,
    ProcessingStatus,
)
from connectors.base import ConnectorBase

log = logging.getLogger(__name__)

# GitHub event types we care about — all others are ignored at the connector level
WATCHED_EVENT_TYPES = {
    "PushEvent",
    "PullRequestEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "PullRequestReviewEvent",
    "CreateEvent",
    "DeleteEvent",
}

# Webhook event type → normalized event_type
WEBHOOK_TYPE_MAP: dict[str, str] = {
    "push":               "code.pushed",
    "pull_request":       "pr.{action}",
    "issues":             "issue.{action}",
    "issue_comment":      "comment.{action}",
    "pull_request_review":"pr_review.{action}",
    "create":             "ref.created",
    "delete":             "ref.deleted",
}

# Poll API event type → normalized event_type
POLL_TYPE_MAP: dict[str, str] = {
    "PushEvent":          "code.pushed",
    "PullRequestEvent":   "pr.{action}",
    "IssuesEvent":        "issue.{action}",
    "IssueCommentEvent":  "comment.created",
    "CreateEvent":        "ref.created",
}


class GitHubConnector(ConnectorBase):
    source_system = SourceSystem.GITHUB

    def __init__(self) -> None:
        super().__init__()
        self._token = settings.github_app_private_key   # Use PAT for Phase 1 simplicity
        self._webhook_secret = settings.github_webhook_secret
        self._repos = settings.github_repos
        self._http: httpx.AsyncClient | None = None

    async def connect_github(self) -> None:
        self._http = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await super().close()
        if self._http:
            await self._http.aclose()

    # ─────────────────────────────────────────────────────────────
    # Polling
    # ─────────────────────────────────────────────────────────────

    async def poll_recent(self, since: datetime) -> list[RawEvent]:
        """
        Poll GitHub Events API for each configured repo.
        GitHub Events API returns up to 300 events per page (last 90 days).
        """
        if not self._http:
            await self.connect_github()

        raw_events: list[RawEvent] = []
        since_utc = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since

        for repo in self._repos:
            try:
                page = 1
                while True:
                    resp = await self._http.get(
                        f"/repos/{repo}/events",
                        params={"per_page": 100, "page": page},
                    )
                    if resp.status_code == 404:
                        log.warning("Repo not found or no access: %s", repo)
                        break
                    resp.raise_for_status()
                    events = resp.json()
                    if not events:
                        break

                    for event in events:
                        created_str = event.get("created_at", "")
                        try:
                            event_ts = datetime.fromisoformat(
                                created_str.replace("Z", "+00:00")
                            )
                        except (ValueError, AttributeError):
                            continue

                        if event_ts < since_utc:
                            break   # Events are reverse-chronological; stop here

                        event_type = event.get("type", "")
                        if event_type not in WATCHED_EVENT_TYPES:
                            continue

                        raw_events.append(
                            RawEvent(
                                source_system=SourceSystem.GITHUB,
                                event_type=event_type,
                                raw_payload=event,
                                idempotency_key=f"github:{event_type}:{event.get('id', '')}",
                            )
                        )

                    page += 1
                    if len(events) < 100:
                        break  # Last page

            except httpx.HTTPStatusError as e:
                log.error("GitHub API error for %s: %s", repo, e.response.status_code)
            except Exception as e:
                log.error("Unexpected error polling %s: %s", repo, e)

        log.info("GitHub poll: %d events from %d repos since %s", len(raw_events), len(self._repos), since)
        return raw_events

    # ─────────────────────────────────────────────────────────────
    # Webhooks
    # ─────────────────────────────────────────────────────────────

    async def handle_webhook(
        self, payload: bytes, headers: dict[str, str]
    ) -> list[RawEvent]:
        if not await self.validate_signature(payload, headers):
            log.warning("GitHub webhook signature validation failed")
            return []

        import json
        data = json.loads(payload)
        event_type = headers.get("x-github-event", "")
        delivery_id = headers.get("x-github-delivery", "")

        action = data.get("action", "")
        normalized_type = WEBHOOK_TYPE_MAP.get(event_type, event_type)
        if "{action}" in normalized_type:
            normalized_type = normalized_type.format(action=action)

        return [
            RawEvent(
                source_system=SourceSystem.GITHUB,
                event_type=event_type,
                raw_payload=data,
                headers=dict(headers),
                idempotency_key=f"github:{event_type}:{delivery_id}",
            )
        ]

    async def validate_signature(
        self, payload: bytes, headers: dict[str, str]
    ) -> bool:
        secret = self._webhook_secret
        if not secret:
            log.warning("No GitHub webhook secret configured — skipping validation")
            return True  # Allow in development

        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False

        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig_header)

    # ─────────────────────────────────────────────────────────────
    # Schema Mapping
    # ─────────────────────────────────────────────────────────────

    def map_to_normalized(self, raw: RawEvent) -> NormalizedEvent | None:
        """Map a GitHub RawEvent to NormalizedEvent."""
        payload = raw.raw_payload
        event_type = raw.event_type

        # Extract actor email/login
        actor = payload.get("actor") or payload.get("sender") or {}
        actor_id = (
            actor.get("email") or
            actor.get("login") or
            payload.get("pusher", {}).get("email") or ""
        )

        # Extract primary entity
        repo = payload.get("repository", {})
        entity_id = repo.get("full_name", "")

        # Extract timestamp
        ts_str = payload.get("created_at") or payload.get("updated_at") or ""
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.utcnow()

        # Map to normalized event_type
        action = payload.get("action", "")
        normalized_type = WEBHOOK_TYPE_MAP.get(event_type, event_type)
        if "{action}" in normalized_type:
            normalized_type = normalized_type.format(action=action)

        # Extract meaningful payload fields (no raw content — PII scrubbed in enrichment)
        normalized_payload = self._extract_normalized_fields(event_type, payload)

        idem_key = raw.idempotency_key or NormalizedEvent.make_idempotency_key(
            SourceSystem.GITHUB, normalized_type, entity_id, timestamp
        )

        return NormalizedEvent(
            idempotency_key=idem_key,
            source_system=SourceSystem.GITHUB,
            event_type=normalized_type,
            actor_source_id=actor_id,
            entity_type=self._infer_entity_type(event_type),
            entity_source_id=entity_id,
            timestamp=timestamp,
            normalized_payload=normalized_payload,
            processing_status=ProcessingStatus.NORMALIZED,
        )

    def _extract_normalized_fields(
        self, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract structured fields. Content bodies excluded — enrichment handles summaries."""
        base: dict[str, Any] = {
            "repo": payload.get("repository", {}).get("full_name"),
            "repo_id": payload.get("repository", {}).get("id"),
        }

        if event_type in ("PullRequestEvent", "pull_request"):
            pr = payload.get("pull_request") or payload
            base.update({
                "pr_number": pr.get("number"),
                "pr_title": pr.get("title"),
                "pr_state": pr.get("state"),
                "head_sha": pr.get("head", {}).get("sha"),
                "base_branch": pr.get("base", {}).get("ref"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "changed_files": pr.get("changed_files"),
                "merged": pr.get("merged"),
                "action": payload.get("action"),
            })

        elif event_type in ("IssuesEvent", "issues"):
            issue = payload.get("issue") or payload
            base.update({
                "issue_number": issue.get("number"),
                "issue_title": issue.get("title"),
                "issue_state": issue.get("state"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "assignees": [a.get("login") for a in issue.get("assignees", [])],
                "action": payload.get("action"),
            })

        elif event_type == "PushEvent":
            commits = payload.get("commits", [])
            base.update({
                "commit_count": len(commits),
                "ref": payload.get("ref"),
                "head_commit_sha": payload.get("head_commit", {}).get("id"),
                "modified_files": list({
                    f for c in commits for f in c.get("modified", [])
                })[:50],  # Cap at 50 files
                "added_files": list({
                    f for c in commits for f in c.get("added", [])
                })[:20],
            })

        return base

    def _infer_entity_type(self, event_type: str) -> str:
        mapping = {
            "PullRequestEvent":     "Feature",
            "pull_request":         "Feature",
            "IssuesEvent":          "Feature",
            "issues":               "Feature",
            "PushEvent":            "Codeunit",
            "IssueCommentEvent":    "Message",
            "issue_comment":        "Message",
        }
        return mapping.get(event_type, "Message")
