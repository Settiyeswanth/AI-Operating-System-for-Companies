"""
Linear Connector.

Ingests: issues (tickets), comments, sprints (cycles), projects.

Linear's data model maps cleanly onto our ontology:
  - Linear Issue  → Feature node
  - Linear Comment → Message node (with parent Feature)
  - Linear Project → Project node
  - Linear Cycle  → Sprint/Project metadata

Auth: Linear API key (set LINEAR_API_KEY env var).
Webhook: Linear sends webhooks for all mutations — subscribe to:
  Issue, Comment, Cycle, Project
"""

from __future__ import annotations

import hashlib
import hmac
import json
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

LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear webhook action types we process
LINEAR_WATCHED_TYPES = {"Issue", "Comment", "Project", "Cycle"}

WEBHOOK_ACTION_MAP = {
    "create": "created",
    "update": "updated",
    "remove": "deleted",
}


class LinearConnector(ConnectorBase):
    source_system = SourceSystem.LINEAR

    def __init__(self) -> None:
        super().__init__()
        self._api_key = settings.linear_api_key
        self._webhook_secret = settings.linear_webhook_secret
        self._team_ids = settings.linear_teams
        self._http: httpx.AsyncClient | None = None

    async def connect_linear(self) -> None:
        self._http = httpx.AsyncClient(
            headers={"Authorization": self._api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await super().close()
        if self._http:
            await self._http.aclose()

    # ─────────────────────────────────────────────────────────────
    # Polling (GraphQL)
    # ─────────────────────────────────────────────────────────────

    async def poll_recent(self, since: datetime) -> list[RawEvent]:
        if not self._http:
            await self.connect_linear()

        raw_events: list[RawEvent] = []
        since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Fetch issues updated since last poll
        issues = await self._fetch_issues_since(since_str)
        for issue in issues:
            raw_events.append(RawEvent(
                source_system=SourceSystem.LINEAR,
                event_type="Issue",
                raw_payload={"type": "Issue", "action": "update", "data": issue},
                idempotency_key=f"linear:Issue:update:{issue.get('id', '')}:{since_str}",
            ))

        # Fetch comments updated since last poll
        comments = await self._fetch_comments_since(since_str)
        for comment in comments:
            raw_events.append(RawEvent(
                source_system=SourceSystem.LINEAR,
                event_type="Comment",
                raw_payload={"type": "Comment", "action": "create", "data": comment},
                idempotency_key=f"linear:Comment:create:{comment.get('id', '')}",
            ))

        log.info("Linear poll: %d issues, %d comments since %s",
                 len(issues), len(comments), since)
        return raw_events

    async def _fetch_issues_since(self, since: str) -> list[dict]:
        """Fetch all issues updated after a given timestamp, across all configured teams."""
        team_filter = ""
        if self._team_ids:
            ids_quoted = ", ".join(f'"{t}"' for t in self._team_ids)
            team_filter = f'team: {{id: {{in: [{ids_quoted}]}}}},'

        query = f"""
        query IssuesSince {{
          issues(
            filter: {{
              {team_filter}
              updatedAt: {{gte: "{since}"}}
            }}
            first: 250
          ) {{
            nodes {{
              id
              title
              description
              state {{ name type }}
              priority
              assignee {{ id name email }}
              team {{ id name }}
              createdAt
              updatedAt
              url
              labels {{ nodes {{ name }} }}
              parent {{ id title }}
            }}
          }}
        }}
        """
        return await self._graphql(query, path=["data", "issues", "nodes"])

    async def _fetch_comments_since(self, since: str) -> list[dict]:
        query = f"""
        query CommentsSince {{
          comments(
            filter: {{createdAt: {{gte: "{since}"}}}}
            first: 250
          ) {{
            nodes {{
              id
              body
              createdAt
              user {{ id name email }}
              issue {{ id title }}
              url
            }}
          }}
        }}
        """
        return await self._graphql(query, path=["data", "comments", "nodes"])

    async def _graphql(self, query: str, path: list[str]) -> list[dict]:
        if not self._http:
            return []
        try:
            resp = await self._http.post(LINEAR_API_URL, json={"query": query})
            resp.raise_for_status()
            data = resp.json()
            result = data
            for key in path:
                result = result.get(key, {})
            return result if isinstance(result, list) else []
        except Exception as e:
            log.error("Linear GraphQL error: %s", e)
            return []

    # ─────────────────────────────────────────────────────────────
    # Webhooks
    # ─────────────────────────────────────────────────────────────

    async def handle_webhook(
        self, payload: bytes, headers: dict[str, str]
    ) -> list[RawEvent]:
        if not await self.validate_signature(payload, headers):
            log.warning("Linear webhook signature validation failed")
            return []

        data = json.loads(payload)
        entity_type = data.get("type", "")
        if entity_type not in LINEAR_WATCHED_TYPES:
            return []

        action = data.get("action", "create")
        entity_id = data.get("data", {}).get("id", "")

        return [
            RawEvent(
                source_system=SourceSystem.LINEAR,
                event_type=entity_type,
                raw_payload=data,
                headers=dict(headers),
                idempotency_key=f"linear:{entity_type}:{action}:{entity_id}",
            )
        ]

    async def validate_signature(
        self, payload: bytes, headers: dict[str, str]
    ) -> bool:
        secret = self._webhook_secret
        if not secret:
            return True  # Dev mode
        sig = headers.get("linear-signature", "")
        if not sig:
            return False
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    # ─────────────────────────────────────────────────────────────
    # Schema Mapping
    # ─────────────────────────────────────────────────────────────

    def map_to_normalized(self, raw: RawEvent) -> NormalizedEvent | None:
        payload = raw.raw_payload
        entity_type = payload.get("type", raw.event_type)
        action = payload.get("action", "update")
        data = payload.get("data", payload)

        if entity_type not in LINEAR_WATCHED_TYPES:
            return None

        normalized_action = WEBHOOK_ACTION_MAP.get(action, action)
        normalized_event_type = f"{entity_type.lower()}.{normalized_action}"

        # Actor
        actor = data.get("assignee") or data.get("user") or {}
        actor_id = actor.get("email") or actor.get("id") or ""

        # Primary entity
        entity_id = data.get("id", "")

        # Timestamp
        ts_str = data.get("updatedAt") or data.get("createdAt") or ""
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.utcnow()

        normalized_payload = self._extract_linear_fields(entity_type, data)

        return NormalizedEvent(
            idempotency_key=raw.idempotency_key or f"linear:{entity_type}:{action}:{entity_id}",
            source_system=SourceSystem.LINEAR,
            event_type=normalized_event_type,
            actor_source_id=actor_id,
            entity_type=self._entity_type_map(entity_type),
            entity_source_id=entity_id,
            timestamp=timestamp,
            normalized_payload=normalized_payload,
            processing_status=ProcessingStatus.NORMALIZED,
        )

    def _extract_linear_fields(self, entity_type: str, data: dict) -> dict[str, Any]:
        if entity_type == "Issue":
            state = data.get("state", {})
            return {
                "linear_id": data.get("id"),
                "title": data.get("title"),
                "state_name": state.get("name"),
                "state_type": state.get("type"),
                "priority": data.get("priority"),
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "url": data.get("url"),
                "labels": [l.get("name") for l in data.get("labels", {}).get("nodes", [])],
                "parent_id": data.get("parent", {}).get("id") if data.get("parent") else None,
            }
        elif entity_type == "Comment":
            return {
                "linear_id": data.get("id"),
                "issue_id": data.get("issue", {}).get("id"),
                "issue_title": data.get("issue", {}).get("title"),
                "url": data.get("url"),
                # body deliberately excluded — PII scrub + summarization in enrichment
            }
        elif entity_type == "Project":
            return {
                "linear_id": data.get("id"),
                "name": data.get("name"),
                "state": data.get("state"),
                "url": data.get("url"),
            }
        return {"linear_id": data.get("id")}

    def _entity_type_map(self, linear_type: str) -> str:
        return {
            "Issue": "Feature",
            "Comment": "Message",
            "Project": "Project",
            "Cycle": "Project",
        }.get(linear_type, "Message")
