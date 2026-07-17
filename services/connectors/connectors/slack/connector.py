"""
Slack Connector.

Ingests: messages (channels), thread replies, reactions.

Key design decisions:
  - Message body is never stored in the normalized payload (privacy).
    The enrichment service summarizes it using the LLM.
  - Thread replies are ingested as Messages with parent_message_id set.
    This preserves decision context that lives in threads.
  - Reactions (especially ✅ 👍 🚀) are captured as lightweight signals
    of decision acknowledgment — processed by MonitorAgent.
  - Channel membership changes are captured for org structure updates.

Auth: Slack Bot Token (xoxb-...) with scopes:
  channels:history, channels:read, groups:history, groups:read,
  reactions:read, users:read, users:read.email
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
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

SLACK_API_BASE = "https://slack.com/api"

WATCHED_EVENT_TYPES = {
    "message",
    "message_changed",
    "reaction_added",
    "channel_created",
    "member_joined_channel",
    "app_mention",
}

# Subtypes to skip (bot messages, channel join notifications, etc.)
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic",
    "channel_purpose", "bot_message", "channel_archive",
}


class SlackConnector(ConnectorBase):
    source_system = SourceSystem.SLACK

    def __init__(self) -> None:
        super().__init__()
        self._token = settings.slack_bot_token
        self._signing_secret = settings.slack_signing_secret
        self._target_channels = settings.slack_channels
        self._http: httpx.AsyncClient | None = None

    async def connect_slack(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=SLACK_API_BASE,
            headers={"Authorization": f"Bearer {self._token}"},
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
        if not self._http:
            await self.connect_slack()

        since_ts = str(since.replace(tzinfo=timezone.utc).timestamp())
        raw_events: list[RawEvent] = []

        channels = self._target_channels
        if not channels:
            channels = await self._list_public_channels()

        for channel_id in channels:
            try:
                messages = await self._fetch_channel_history(channel_id, since_ts)
                for msg in messages:
                    # Skip bot messages and system subtypes
                    if msg.get("subtype") in SKIP_SUBTYPES:
                        continue
                    if msg.get("bot_id"):
                        continue

                    raw_events.append(RawEvent(
                        source_system=SourceSystem.SLACK,
                        event_type="message",
                        raw_payload={
                            "channel": channel_id,
                            "message": msg,
                            "event_type": "message",
                        },
                        idempotency_key=f"slack:message:{channel_id}:{msg.get('ts', '')}",
                    ))

                    # Fetch thread replies if this is a thread parent with replies
                    thread_ts = msg.get("thread_ts")
                    reply_count = msg.get("reply_count", 0)
                    if thread_ts == msg.get("ts") and reply_count > 0:
                        replies = await self._fetch_thread_replies(channel_id, thread_ts)
                        for reply in replies:
                            if reply.get("ts") == thread_ts:
                                continue  # Skip parent
                            if reply.get("bot_id"):
                                continue
                            raw_events.append(RawEvent(
                                source_system=SourceSystem.SLACK,
                                event_type="message",
                                raw_payload={
                                    "channel": channel_id,
                                    "message": reply,
                                    "parent_ts": thread_ts,
                                    "event_type": "thread_reply",
                                },
                                idempotency_key=f"slack:reply:{channel_id}:{reply.get('ts', '')}",
                            ))

            except Exception as e:
                log.error("Error polling Slack channel %s: %s", channel_id, e)

        log.info("Slack poll: %d messages/replies from %d channels", len(raw_events), len(channels))
        return raw_events

    async def _list_public_channels(self) -> list[str]:
        resp = await self._http.get("/conversations.list", params={"limit": 200, "types": "public_channel"})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("Slack conversations.list failed: %s", data.get("error"))
            return []
        return [c["id"] for c in data.get("channels", [])]

    async def _fetch_channel_history(self, channel_id: str, oldest_ts: str) -> list[dict]:
        all_messages: list[dict] = []
        cursor = None
        while True:
            params: dict[str, Any] = {
                "channel": channel_id,
                "oldest": oldest_ts,
                "limit": 200,
                "inclusive": True,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await self._http.get("/conversations.history", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.warning("Slack history error for %s: %s", channel_id, data.get("error"))
                break
            all_messages.extend(data.get("messages", []))
            if not data.get("has_more"):
                break
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return all_messages

    async def _fetch_thread_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        resp = await self._http.get(
            "/conversations.replies",
            params={"channel": channel_id, "ts": thread_ts, "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("messages", []) if data.get("ok") else []

    # ─────────────────────────────────────────────────────────────
    # Webhooks
    # ─────────────────────────────────────────────────────────────

    async def handle_webhook(
        self, payload: bytes, headers: dict[str, str]
    ) -> list[RawEvent]:
        if not await self.validate_signature(payload, headers):
            log.warning("Slack webhook signature validation failed")
            return []

        data = json.loads(payload)

        # URL verification challenge (Slack sends this when you first register a webhook)
        if data.get("type") == "url_verification":
            return []  # Handled separately in the webhook route

        event = data.get("event", {})
        event_type = event.get("type", "")

        if event_type not in WATCHED_EVENT_TYPES:
            return []

        if event.get("subtype") in SKIP_SUBTYPES:
            return []

        channel = event.get("channel", "")
        ts = event.get("ts", event.get("event_ts", ""))

        return [
            RawEvent(
                source_system=SourceSystem.SLACK,
                event_type=event_type,
                raw_payload={
                    "event": event,
                    "channel": channel,
                    "team_id": data.get("team_id"),
                    "event_type": event_type,
                },
                headers=dict(headers),
                idempotency_key=f"slack:{event_type}:{channel}:{ts}",
            )
        ]

    async def validate_signature(
        self, payload: bytes, headers: dict[str, str]
    ) -> bool:
        secret = self._signing_secret
        if not secret:
            return True  # Dev mode

        timestamp = headers.get("x-slack-request-timestamp", "")
        sig_header = headers.get("x-slack-signature", "")

        if not timestamp or not sig_header:
            return False

        # Reject requests older than 5 minutes (replay attack prevention)
        if abs(time.time() - float(timestamp)) > 300:
            log.warning("Slack webhook timestamp too old: %s", timestamp)
            return False

        basestring = f"v0:{timestamp}:".encode() + payload
        computed = "v0=" + hmac.new(
            secret.encode(), basestring, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, sig_header)

    # ─────────────────────────────────────────────────────────────
    # Schema Mapping
    # ─────────────────────────────────────────────────────────────

    def map_to_normalized(self, raw: RawEvent) -> NormalizedEvent | None:
        payload = raw.raw_payload
        event = payload.get("event") or payload.get("message") or payload
        event_type = payload.get("event_type", raw.event_type)

        channel = payload.get("channel", event.get("channel", ""))
        user_id = event.get("user", event.get("user_id", ""))
        ts = event.get("ts", event.get("event_ts", ""))
        parent_ts = payload.get("parent_ts")

        if not ts:
            return None

        try:
            timestamp = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            timestamp = datetime.utcnow()

        normalized_event_type = {
            "message":               "message.posted",
            "thread_reply":          "message.reply",
            "message_changed":       "message.edited",
            "reaction_added":        "reaction.added",
            "channel_created":       "channel.created",
            "member_joined_channel": "channel.member_joined",
        }.get(event_type, f"slack.{event_type}")

        normalized_payload: dict[str, Any] = {
            "channel_id": channel,
            "slack_ts": ts,
            "parent_ts": parent_ts,
            "reaction": event.get("reaction") if event_type == "reaction_added" else None,
            "item_ts": event.get("item", {}).get("ts") if event_type == "reaction_added" else None,
            # NOTE: message text deliberately excluded — PII scrub + LLM summary in enrichment
        }

        return NormalizedEvent(
            idempotency_key=raw.idempotency_key or f"slack:{event_type}:{channel}:{ts}",
            source_system=SourceSystem.SLACK,
            event_type=normalized_event_type,
            actor_source_id=user_id,
            entity_type="Message",
            entity_source_id=f"{channel}:{ts}",
            timestamp=timestamp,
            normalized_payload=normalized_payload,
            processing_status=ProcessingStatus.NORMALIZED,
        )
