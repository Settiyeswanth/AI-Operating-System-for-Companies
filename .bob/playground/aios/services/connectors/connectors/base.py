"""
ConnectorBase — abstract interface all connectors must implement.

Architecture constraints:
  - Connectors ONLY produce RawEvent objects. No business logic.
  - Schema mapping (RawEvent → NormalizedEvent) happens in the ingestion service.
  - Connectors validate webhook signatures before emitting events.
  - All emitted events are published to Redis Pub/Sub for the ingestion service.
  - Polling is the primary ingestion method in Phase 1.
    Webhooks are supplementary real-time delivery.

Failure handling:
  - Failed events go to the Dead-Letter Queue channel in Redis.
  - Connectors never crash on a single bad event — they log and continue.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime

import redis.asyncio as aioredis

from aios_core.config import settings
from aios_core.schemas.events import NormalizedEvent, RawEvent, SourceSystem

log = logging.getLogger(__name__)

# Redis channel names
EVENTS_CHANNEL = "aios:events"
DLQ_CHANNEL = "aios:dlq"


class ConnectorBase(ABC):
    """
    Base class for all source system connectors.

    Subclasses must implement:
      - source_system property
      - poll_recent()
      - handle_webhook()
      - map_to_normalized()
      - validate_signature()
    """

    source_system: SourceSystem

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect_redis(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    # ─────────────────────────────────────────────────────────────
    # Abstract interface
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def poll_recent(self, since: datetime) -> list[RawEvent]:
        """
        Pull events from the source system since the given datetime.
        Called on the polling schedule (default: every 5 minutes).
        Returns raw events in source-native format.
        """
        ...

    @abstractmethod
    async def handle_webhook(
        self, payload: bytes, headers: dict[str, str]
    ) -> list[RawEvent]:
        """
        Process an incoming webhook payload.
        Validates the signature before processing.
        Returns empty list if signature invalid.
        """
        ...

    @abstractmethod
    def map_to_normalized(self, raw: RawEvent) -> NormalizedEvent | None:
        """
        Map a RawEvent to a NormalizedEvent.
        Returns None if the event type should be ignored (no downstream work needed).
        """
        ...

    @abstractmethod
    async def validate_signature(
        self, payload: bytes, headers: dict[str, str]
    ) -> bool:
        """
        Verify the webhook came from the expected source.
        Returns False if signature is invalid or missing.
        """
        ...

    # ─────────────────────────────────────────────────────────────
    # Publishing
    # ─────────────────────────────────────────────────────────────

    async def publish_event(self, event: NormalizedEvent) -> None:
        """
        Publish a normalized event to the Redis events channel.
        The ingestion service subscribes to this channel.
        """
        if not self._redis:
            log.error("Redis not connected — cannot publish event %s", event.event_id)
            return
        try:
            await self._redis.publish(
                EVENTS_CHANNEL,
                event.model_dump_json(),
            )
            log.debug(
                "Published %s event: %s [%s]",
                event.source_system.value,
                event.event_type,
                event.idempotency_key[:40],
            )
        except Exception as e:
            log.error("Failed to publish event %s: %s", event.event_id, e)
            await self._send_to_dlq(event, error=str(e))

    async def _send_to_dlq(self, event: NormalizedEvent, error: str) -> None:
        """Push a failed event to the dead-letter queue."""
        if not self._redis:
            return
        try:
            dlq_entry = {
                "event_id": event.event_id,
                "source_system": event.source_system.value,
                "event_type": event.event_type,
                "error": error,
                "payload": event.model_dump(),
            }
            await self._redis.lpush(DLQ_CHANNEL, json.dumps(dlq_entry))
        except Exception:
            log.exception("DLQ write failed for event %s", event.event_id)

    async def poll_and_publish(self, since: datetime) -> int:
        """
        Convenience: poll, map, and publish in one call.
        Returns the number of events successfully published.
        """
        raw_events = await self.poll_recent(since)
        published = 0
        for raw in raw_events:
            try:
                normalized = self.map_to_normalized(raw)
                if normalized:
                    await self.publish_event(normalized)
                    published += 1
            except Exception as e:
                log.warning(
                    "Failed to map/publish %s event: %s",
                    self.source_system.value,
                    e,
                )
        return published
