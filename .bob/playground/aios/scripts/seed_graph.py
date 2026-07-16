#!/usr/bin/env python3
"""
Seed the Knowledge Graph with the starter ontology.

Run this ONCE before starting the connectors:
    docker exec aios-ingestion python /app/scripts/seed_graph.py
    # or locally:
    cd aios && python scripts/seed_graph.py

What this does:
  1. Creates all Neo4j constraints and indices
  2. Creates the system Person node (for pipeline-generated entities)
  3. Verifies Neo4j + Qdrant connectivity
  4. Creates the Qdrant collection if it doesn't exist

Safe to re-run — all operations are idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Add the packages and services to the path when running directly
sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "aios-core"))
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "memory"))

from aios_core.config import settings
from aios_core.logging import configure_logging
from aios_core.schemas.ontology import NodeType
from memory.graph.client import GraphClient
from memory.vector.client import VectorClient

log = logging.getLogger("seed")


async def seed_graph() -> None:
    configure_logging(settings.log_level, "seed")
    log.info("Starting Knowledge Graph seed...")

    # ── Neo4j ─────────────────────────────────────────────────────
    log.info("Connecting to Neo4j at %s", settings.neo4j_uri)
    graph = GraphClient()
    await graph.connect()

    log.info("Creating schema (constraints + indices)...")
    await graph.bootstrap_schema()

    # System node — represents the pipeline itself as an actor
    log.info("Creating system Person node...")
    await graph.upsert_node(
        node_type=NodeType.PERSON,
        node_id="system",
        properties={
            "id": "system",
            "canonical_email": "system@aios.internal",
            "display_names": ["AI OS System"],
            "roles": ["system"],
            "is_active": True,
        },
    )

    # Verify a round-trip read
    node = await graph.get_node(NodeType.PERSON, "system")
    assert node is not None, "System node not found after creation!"
    log.info("System node verified: %s", node.get("canonical_email"))

    count_result = await graph.run_raw("MATCH (n) RETURN count(n) AS cnt")
    log.info("Total nodes in graph: %d", count_result[0]["cnt"] if count_result else 0)

    await graph.close()

    # ── Qdrant ────────────────────────────────────────────────────
    log.info("Connecting to Qdrant at %s:%s", settings.qdrant_host, settings.qdrant_port)
    vector = VectorClient()
    await vector.connect()
    log.info("Qdrant collection '%s' ready", settings.qdrant_collection_name)
    await vector.close()

    log.info("")
    log.info("✓ Seed complete. The system is ready for connectors.")
    log.info("  Next: run 'python scripts/backfill.py' to ingest historical data")
    log.info("  Then: start services with 'docker compose up'")


if __name__ == "__main__":
    asyncio.run(seed_graph())
