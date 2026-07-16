"""
Enrichment Pipeline — the missing link between ingestion and the Knowledge Graph.

This service is the critical bridge:

  Ingestion (entity-resolved events)
      ↓  Redis aios:enrichment channel
  Enrichment Pipeline  ← THIS FILE
      ↓                      ↓
  Neo4j (graph nodes)   Qdrant (vector chunks)

For each NormalizedEvent received:
  1. Build a text representation of the entity (title, status, type, team)
  2. PII-scrub the text
  3. Embed the text via Ollama (nomic-embed-text → 768-dim vector)
  4. Write the entity node to Neo4j (upsert — safe to re-run)
  5. Write the AUTHORED edge if actor_canonical_id is resolved
  6. Write the text chunk to Qdrant for semantic search
  7. Update the event processing_status to ENRICHED in the ledger

Privacy invariant:
  Raw message bodies from Slack/Linear are NEVER stored.
  Only LLM-generated summaries pass through this pipeline.
  Text content is PII-scrubbed before embedding or graph storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime

import redis.asyncio as aioredis

from aios_core.config import settings
from aios_core.llm_gateway import get_llm_gateway, LLMMessage
from aios_core.logging import configure_logging
from aios_core.schemas.events import NormalizedEvent, ProcessingStatus
from aios_core.schemas.ontology import NodeType, EdgeType

from enrichment.pii_scrubber import get_scrubber

log = logging.getLogger(__name__)

ENRICHMENT_CHANNEL = "aios:enrichment"
DLQ_CHANNEL = "aios:dlq"

# Entity type string → NodeType enum
ENTITY_TYPE_MAP: dict[str, NodeType] = {
    "Feature": NodeType.FEATURE,
    "Decision": NodeType.DECISION,
    "Incident": NodeType.INCIDENT,
    "Message": NodeType.MESSAGE,
    "Codeunit": NodeType.CODEUNIT,
    "Project": NodeType.PROJECT,
    "Team": NodeType.TEAM,
    "Person": NodeType.PERSON,
}


class EnrichmentPipeline:
    """
    Subscribes to aios:enrichment and processes each resolved NormalizedEvent.
    Writes enriched entities to Neo4j and vector chunks to Qdrant.
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._graph = None
        self._vector = None
        self._ledger = None
        self._llm = None
        self._scrubber = get_scrubber()

    async def start(self) -> None:
        configure_logging(settings.log_level, "enrichment")
        log.info("Enrichment pipeline starting...")

        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self._llm = get_llm_gateway()

        # Connect memory clients
        from memory.graph.client import get_graph_client
        from memory.vector.client import get_vector_client, ChunkDoc
        from memory.ledger.store import get_ledger

        self._graph = get_graph_client()
        await self._graph.connect()
        log.info("Connected to Neo4j")

        self._vector = get_vector_client()
        await self._vector.connect()
        log.info("Connected to Qdrant")

        self._ledger = get_ledger()
        log.info("Event ledger ready")

        await self._listen()

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(ENRICHMENT_CHANNEL)
        log.info("Subscribed to Redis channel: %s", ENRICHMENT_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                event = NormalizedEvent.model_validate(data)
                await self._process_event(event)
            except Exception as e:
                log.error("Enrichment: failed to parse message: %s", e)
                await self._send_to_dlq(message.get("data", ""), str(e))

    async def _process_event(self, event: NormalizedEvent) -> None:
        """
        Enrich a single resolved NormalizedEvent.
        Writes to Neo4j + Qdrant. Never raises — logs errors and continues.
        """
        entity_type_str = event.entity_type or "Message"
        node_type = ENTITY_TYPE_MAP.get(entity_type_str)

        if not node_type:
            log.debug("Skipping enrichment for unknown entity type: %s", entity_type_str)
            return

        entity_id = event.entity_canonical_id or event.entity_source_id
        if not entity_id:
            log.debug("Skipping event with no entity ID: %s", event.event_id)
            return

        try:
            # ── Step 1: Build text representation ─────────────────
            text = self._build_entity_text(event, node_type)

            # ── Step 2: PII scrub ──────────────────────────────────
            clean_text = self._scrubber.scrub(text)

            # ── Step 3: Embed ─────────────────────────────────────
            try:
                vectors = await self._llm.embed([clean_text])
                embedding = vectors[0]
            except Exception as e:
                log.warning("Embedding failed for %s: %s — skipping vector write", entity_id, e)
                embedding = None

            # ── Step 4: Write node to Neo4j ───────────────────────
            node_props = self._build_node_properties(event, node_type, clean_text)
            await self._graph.upsert_node(node_type, entity_id, node_props)

            # ── Step 5: Write AUTHORED edge ───────────────────────
            actor_id = event.actor_canonical_id
            if actor_id and actor_id != "system":
                try:
                    await self._graph.upsert_edge(
                        from_type=NodeType.PERSON,
                        from_id=actor_id,
                        edge_type=EdgeType.AUTHORED,
                        to_type=node_type,
                        to_id=entity_id,
                        properties={
                            "source_event_id": event.event_id,
                            "created_by": "pipeline",
                            "confidence": 1.0,
                        },
                    )
                except Exception as e:
                    # Edge write failure is non-fatal — node is already written
                    log.warning("AUTHORED edge write failed (actor %s → %s): %s", actor_id, entity_id, e)

            # ── Step 6: Write chunk to Qdrant ────────────────────
            if embedding:
                from memory.vector.client import ChunkDoc
                chunk = ChunkDoc(
                    chunk_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{event.source_system.value}:{entity_id}")),
                    source_artifact_id=entity_id,
                    content=clean_text,
                    source_system=event.source_system.value,
                    source_url=event.normalized_payload.get("url") or event.normalized_payload.get("source_url"),
                    timestamp=event.timestamp,
                    entity_refs=[entity_id] + ([actor_id] if actor_id else []),
                    access_tags={},  # Default open access; enrichment sets tags
                    embedding=embedding,
                )
                await self._vector.upsert_chunk(chunk)

            # ── Step 7: Update ledger status ─────────────────────
            if self._ledger:
                self._ledger.update_status(event.event_id, ProcessingStatus.ENRICHED)

            log.debug(
                "Enriched %s:%s [%s] by %s",
                node_type.value,
                entity_id[:16],
                event.event_type,
                actor_id or "unknown",
            )

        except Exception as e:
            log.error(
                "Enrichment failed for event %s (%s:%s): %s",
                event.event_id[:8], entity_type_str, entity_id[:16], e,
                exc_info=True,
            )
            await self._send_to_dlq(event.model_dump_json(), str(e))

    def _build_entity_text(self, event: NormalizedEvent, node_type: NodeType) -> str:
        """
        Build a human-readable text description of the entity.
        This is what gets embedded and stored in Qdrant.
        Content bodies are deliberately excluded — enrichment summarises them.
        """
        p = event.normalized_payload
        event_type = event.event_type

        if node_type == NodeType.FEATURE:
            parts = [
                p.get("title") or p.get("pr_title") or p.get("issue_title") or event_type,
                f"status: {p.get('state_name') or p.get('issue_state') or 'unknown'}",
                f"team: {p.get('team_name') or 'unknown'}",
                f"repo: {p.get('repo') or ''}",
                f"labels: {', '.join(p.get('labels', []))}",
            ]
        elif node_type == NodeType.MESSAGE:
            parts = [
                f"Message in {p.get('channel_id') or 'channel'}",
                f"event: {event_type}",
                f"source: {event.source_system.value}",
            ]
        elif node_type == NodeType.CODEUNIT:
            parts = [
                f"Code change in {p.get('repo') or 'repository'}",
                f"files modified: {', '.join((p.get('modified_files') or [])[:5])}",
                f"branch: {p.get('ref') or ''}",
                f"commits: {p.get('commit_count') or 1}",
            ]
        elif node_type == NodeType.DECISION:
            parts = [
                p.get("summary") or f"Decision from {event.source_system.value}",
                f"rationale: {p.get('rationale') or ''}",
            ]
        elif node_type == NodeType.INCIDENT:
            parts = [
                p.get("title") or f"Incident from {event.source_system.value}",
                f"severity: {p.get('severity') or 'unknown'}",
            ]
        elif node_type == NodeType.PROJECT:
            parts = [
                p.get("name") or f"Project from {event.source_system.value}",
                f"state: {p.get('state') or 'unknown'}",
            ]
        else:
            parts = [f"{node_type.value} from {event.source_system.value}: {event_type}"]

        return ". ".join(part for part in parts if part and part.strip(". "))

    def _build_node_properties(
        self, event: NormalizedEvent, node_type: NodeType, clean_text: str
    ) -> dict:
        """Build Neo4j node properties from a NormalizedEvent."""
        p = event.normalized_payload
        base = {
            "id": event.entity_canonical_id or event.entity_source_id,
            "source_ids": {event.source_system.value: event.entity_source_id},
            "source_system": event.source_system.value,
            "event_type": event.event_type,
            "created_at": event.timestamp.isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "is_stale": False,
            "content_summary": clean_text[:500],
        }

        if node_type == NodeType.FEATURE:
            state = p.get("state_name") or p.get("issue_state") or p.get("pr_state") or "planned"
            # Map source state names to canonical FeatureStatus values
            state_map = {
                "todo": "planned", "triage": "planned", "backlog": "planned",
                "in progress": "in_progress", "inprogress": "in_progress",
                "in review": "in_review", "inreview": "in_review",
                "done": "shipped", "closed": "shipped", "merged": "shipped",
                "cancelled": "abandoned", "canceled": "abandoned", "won't fix": "abandoned",
                "blocked": "blocked",
            }
            canonical_state = state_map.get(state.lower(), "planned")
            base.update({
                "title": p.get("title") or p.get("pr_title") or p.get("issue_title") or event.event_type,
                "status": canonical_state,
                "priority": str(p.get("priority") or "medium").lower(),
                "source_url": p.get("url"),
            })
        elif node_type == NodeType.MESSAGE:
            base.update({
                "channel_type": event.source_system.value,
                "author_id": event.actor_canonical_id or event.actor_source_id,
                "timestamp": event.timestamp.isoformat(),
                "source_url": p.get("url"),
                "parent_message_id": p.get("parent_ts"),
            })
        elif node_type == NodeType.CODEUNIT:
            base.update({
                "path": (p.get("modified_files") or ["unknown"])[0],
                "repository": p.get("repo") or "",
                "language": None,
                "last_modified_by_id": event.actor_canonical_id or event.actor_source_id,
            })

        return base

    async def _send_to_dlq(self, raw_data: str, error: str) -> None:
        try:
            if self._redis:
                await self._redis.lpush(DLQ_CHANNEL, json.dumps({
                    "raw": raw_data[:500],
                    "error": error,
                    "service": "enrichment",
                    "timestamp": datetime.utcnow().isoformat(),
                }))
        except Exception:
            log.exception("Enrichment DLQ write failed")


async def main() -> None:
    pipeline = EnrichmentPipeline()
    await pipeline.start()


if __name__ == "__main__":
    asyncio.run(main())
