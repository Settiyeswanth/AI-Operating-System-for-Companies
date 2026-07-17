"""
Ingestion Pipeline — Normalization, Entity Resolution, Ledger.

This is the entry point for all organizational data after it leaves connectors.
Responsibilities:
  1. Receive NormalizedEvents from Redis Pub/Sub
  2. Deduplicate via idempotency key (ledger check)
  3. Entity resolution: map source-local IDs to canonical entity IDs
  4. Write to Temporal Event Ledger
  5. Forward resolved events to Redis for enrichment service

Pipeline rule: NEVER drop an event silently. Either process it, hold it for
review, or push it to the DLQ with a clear error message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import redis.asyncio as aioredis

from aios_core.config import settings
from aios_core.logging import configure_logging
from aios_core.schemas.events import NormalizedEvent, ProcessingStatus

from ingestion.entity_resolution.resolver import EntityResolver
from ingestion.ledger_writer import LedgerWriter

log = logging.getLogger(__name__)

# Redis channels
EVENTS_CHANNEL = "aios:events"             # Inbound from connectors
ENRICHMENT_CHANNEL = "aios:enrichment"     # Outbound to enrichment service
DLQ_CHANNEL = "aios:dlq"


class IngestionPipeline:
    """
    Subscribes to the events channel and processes each event through
    the normalization → entity resolution → ledger write → forward pipeline.
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._resolver: EntityResolver | None = None
        self._ledger: LedgerWriter | None = None

    async def start(self) -> None:
        configure_logging(settings.log_level, "ingestion")
        log.info("Ingestion pipeline starting...")

        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self._resolver = EntityResolver()
        await self._resolver.connect()
        self._ledger = LedgerWriter()

        await self._listen()

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(EVENTS_CHANNEL)
        log.info("Subscribed to Redis channel: %s", EVENTS_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                event = NormalizedEvent.model_validate(data)
                await self._process_event(event)
            except Exception as e:
                log.error("Failed to parse event from channel: %s", e)
                await self._send_to_dlq(message.get("data", ""), str(e))

    async def _process_event(self, event: NormalizedEvent) -> None:
        """Process a single event through the full pipeline."""

        # Step 1: Deduplication
        if self._ledger.exists(event.idempotency_key):
            log.debug("Duplicate event skipped: %s", event.idempotency_key)
            return

        # Step 2: Entity resolution
        resolved_event = await self._resolver.resolve(event)

        # Step 3: Write to ledger (even if held for ER review)
        self._ledger.append(resolved_event)

        # Step 4: If held for ER review, stop here
        if resolved_event.processing_status == ProcessingStatus.HELD:
            log.info(
                "Event held pending ER review: %s [%s]",
                resolved_event.event_id,
                resolved_event.actor_source_id,
            )
            return

        # Step 5: Forward to enrichment
        await self._forward_to_enrichment(resolved_event)

        log.debug(
            "Processed event %s → %s [actor: %s]",
            resolved_event.event_type,
            resolved_event.entity_type,
            resolved_event.actor_canonical_id or resolved_event.actor_source_id,
        )

    async def _forward_to_enrichment(self, event: NormalizedEvent) -> None:
        event.processing_status = ProcessingStatus.RESOLVED
        await self._redis.publish(ENRICHMENT_CHANNEL, event.model_dump_json())

    async def _send_to_dlq(self, raw_data: str, error: str) -> None:
        try:
            await self._redis.lpush(DLQ_CHANNEL, json.dumps({
                "raw": raw_data[:500],
                "error": error,
                "timestamp": datetime.utcnow().isoformat(),
            }))
        except Exception:
            log.exception("DLQ write failed")


async def main() -> None:
    pipeline = IngestionPipeline()
    await pipeline.start()


if __name__ == "__main__":
    asyncio.run(main())
