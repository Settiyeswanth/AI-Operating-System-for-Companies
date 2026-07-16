"""
Neo4j Knowledge Graph client.

Wraps all graph operations. Services NEVER call Neo4j directly —
they go through this client so we have a single place to enforce:
  - Access control at read time
  - Confidence threshold filtering
  - Consistent edge property schema
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession, AsyncDriver
from neo4j.exceptions import ServiceUnavailable

from aios_core.config import settings
from aios_core.schemas.ontology import (
    NodeType,
    EdgeType,
    ANSWER_CONFIDENCE_THRESHOLD,
    LLM_INFERRED_EDGE_CONFIDENCE,
)

log = logging.getLogger(__name__)


class GraphClient:
    """
    Async Neo4j client. One instance per service — keep it alive for the
    service lifetime and close on shutdown.
    """

    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None

    async def connect(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        # Verify connectivity on startup
        await self._driver.verify_connectivity()
        log.info("Connected to Neo4j at %s", settings.neo4j_uri)

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            log.info("Neo4j connection closed")

    @asynccontextmanager
    async def session(self):
        if not self._driver:
            raise RuntimeError("GraphClient not connected. Call connect() first.")
        async with self._driver.session() as session:
            yield session

    # ─────────────────────────────────────────────────────────────
    # Bootstrap
    # ─────────────────────────────────────────────────────────────

    async def bootstrap_schema(self) -> None:
        """
        Create constraints and indices. Safe to run multiple times (IF NOT EXISTS).
        Called by seed_graph.py on first run.
        """
        constraints = [
            "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT team_id IF NOT EXISTS FOR (t:Team) REQUIRE t.id IS UNIQUE",
            "CREATE CONSTRAINT feature_id IF NOT EXISTS FOR (f:Feature) REQUIRE f.id IS UNIQUE",
            "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT incident_id IF NOT EXISTS FOR (i:Incident) REQUIRE i.id IS UNIQUE",
            "CREATE CONSTRAINT message_id IF NOT EXISTS FOR (m:Message) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT codeunit_id IF NOT EXISTS FOR (c:Codeunit) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT requirement_id IF NOT EXISTS FOR (r:Requirement) REQUIRE r.id IS UNIQUE",
        ]
        indices = [
            "CREATE INDEX feature_status IF NOT EXISTS FOR (f:Feature) ON (f.status)",
            "CREATE INDEX decision_made_at IF NOT EXISTS FOR (d:Decision) ON (d.made_at)",
            "CREATE INDEX message_timestamp IF NOT EXISTS FOR (m:Message) ON (m.timestamp)",
            "CREATE INDEX person_email IF NOT EXISTS FOR (p:Person) ON (p.canonical_email)",
            "CREATE INDEX codeunit_path IF NOT EXISTS FOR (c:Codeunit) ON (c.path)",
        ]
        async with self.session() as s:
            for stmt in constraints + indices:
                try:
                    await s.run(stmt)
                except Exception as e:
                    log.warning("Schema statement skipped (%s): %s", type(e).__name__, stmt[:60])
        log.info("Graph schema bootstrap complete")

    # ─────────────────────────────────────────────────────────────
    # Node Operations
    # ─────────────────────────────────────────────────────────────

    async def upsert_node(
        self,
        node_type: NodeType,
        node_id: str,
        properties: dict[str, Any],
    ) -> None:
        """
        Create or update a node. Merges on 'id'.
        Updated_at is always refreshed on update.
        """
        props = {k: v for k, v in properties.items() if v is not None}
        props["updated_at"] = datetime.utcnow().isoformat()
        cypher = (
            f"MERGE (n:{node_type.value} {{id: $id}}) "
            "SET n += $props"
        )
        async with self.session() as s:
            await s.run(cypher, id=node_id, props=props)

    async def get_node(
        self,
        node_type: NodeType,
        node_id: str,
    ) -> dict[str, Any] | None:
        cypher = f"MATCH (n:{node_type.value} {{id: $id}}) RETURN n"
        async with self.session() as s:
            result = await s.run(cypher, id=node_id)
            record = await result.single()
            if record:
                return dict(record["n"])
        return None

    # ─────────────────────────────────────────────────────────────
    # Edge Operations
    # ─────────────────────────────────────────────────────────────

    async def upsert_edge(
        self,
        from_type: NodeType,
        from_id: str,
        edge_type: EdgeType,
        to_type: NodeType,
        to_id: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """
        Create or update a relationship between two nodes.
        All edge properties schema is enforced here.
        """
        props = properties or {}
        props.setdefault("created_at", datetime.utcnow().isoformat())
        props.setdefault("confidence", 1.0)
        props.setdefault("created_by", "pipeline")
        props["updated_at"] = datetime.utcnow().isoformat()

        cypher = (
            f"MATCH (a:{from_type.value} {{id: $from_id}}) "
            f"MATCH (b:{to_type.value} {{id: $to_id}}) "
            f"MERGE (a)-[r:{edge_type.value}]->(b) "
            "SET r += $props"
        )
        async with self.session() as s:
            await s.run(cypher, from_id=from_id, to_id=to_id, props=props)

    # ─────────────────────────────────────────────────────────────
    # Query Library — accessed by agents
    # ─────────────────────────────────────────────────────────────

    async def find_feature_authors(self, feature_id: str) -> list[dict]:
        cypher = """
            MATCH (p:Person)-[r:AUTHORED]->(f:Feature {id: $feature_id})
            WHERE r.confidence >= $min_confidence
            RETURN p.id AS id, p.display_names AS names, p.canonical_email AS email
        """
        async with self.session() as s:
            result = await s.run(
                cypher,
                feature_id=feature_id,
                min_confidence=ANSWER_CONFIDENCE_THRESHOLD,
            )
            return [dict(r) async for r in result]

    async def find_diverging_features(
        self,
        since: datetime,
        min_confidence: float = ANSWER_CONFIDENCE_THRESHOLD,
        limit: int = 20,
    ) -> list[dict]:
        """
        Returns features with DIVERGES_FROM edges — the primary R3 signal.
        """
        cypher = """
            MATCH (f:Feature)-[d:DIVERGES_FROM]->(r)
            WHERE d.confidence >= $min_confidence
            AND datetime(d.created_at) >= datetime($since)
            RETURN f.id AS feature_id, f.title AS feature_title,
                   f.status AS status, d.confidence AS confidence,
                   d.created_at AS detected_at, labels(r)[0] AS target_type,
                   r.id AS target_id, r.summary AS target_summary
            ORDER BY d.confidence DESC
            LIMIT $limit
        """
        async with self.session() as s:
            result = await s.run(
                cypher,
                since=since.isoformat(),
                min_confidence=min_confidence,
                limit=limit,
            )
            return [dict(r) async for r in result]

    async def find_decision_context(self, feature_ids: list[str]) -> list[dict]:
        cypher = """
            MATCH (d:Decision)-[:CONSTRAINS]->(f:Feature)
            WHERE f.id IN $feature_ids
            OPTIONAL MATCH (p:Person)-[:AUTHORED]->(d)
            RETURN d.id AS decision_id, d.summary AS summary,
                   d.rationale AS rationale, d.made_at AS made_at,
                   collect(DISTINCT p.display_names[0]) AS authors,
                   collect(DISTINCT f.id) AS constrained_features
        """
        async with self.session() as s:
            result = await s.run(cypher, feature_ids=feature_ids)
            return [dict(r) async for r in result]

    async def find_incident_blast_radius(self, incident_id: str) -> dict | None:
        cypher = """
            MATCH (i:Incident {id: $incident_id})-[:AFFECTED]->(f:Feature)
            OPTIONAL MATCH (p:Person)-[:AUTHORED]->(f)
            OPTIONAL MATCH (d:Decision)-[:CONSTRAINS]->(f)
            RETURN i.id AS incident_id, i.title AS title, i.severity AS severity,
                   collect(DISTINCT {id: f.id, title: f.title}) AS features,
                   collect(DISTINCT p.display_names[0]) AS authors,
                   collect(DISTINCT {id: d.id, summary: d.summary}) AS decisions
        """
        async with self.session() as s:
            result = await s.run(cypher, incident_id=incident_id)
            record = await result.single()
            return dict(record) if record else None

    async def find_blocked_features_without_decisions(
        self, threshold_days: int = 5
    ) -> list[dict]:
        """
        MonitorAgent Rule 1: Features blocked > threshold_days with no constraining Decision.
        """
        cypher = """
            MATCH (f:Feature)
            WHERE f.status = 'blocked'
            AND f.blocked_since IS NOT NULL
            AND duration.between(datetime(f.blocked_since), datetime()).days >= $threshold_days
            AND NOT (f)<-[:CONSTRAINS]-(:Decision)
            RETURN f.id AS feature_id, f.title AS title,
                   f.blocked_since AS blocked_since,
                   duration.between(datetime(f.blocked_since), datetime()).days AS days_blocked
            ORDER BY days_blocked DESC
        """
        async with self.session() as s:
            result = await s.run(cypher, threshold_days=threshold_days)
            return [dict(r) async for r in result]

    async def find_completion_mismatches(self) -> list[dict]:
        """
        MonitorAgent Rule 2: Features SHIPPED but linked Requirements still OPEN.
        """
        cypher = """
            MATCH (f:Feature)-[:IMPLEMENTS]->(r:Requirement)
            WHERE f.status = 'shipped'
            AND r.status = 'open'
            RETURN f.id AS feature_id, f.title AS feature_title,
                   r.id AS requirement_id, r.text AS requirement_text
        """
        async with self.session() as s:
            result = await s.run(cypher)
            return [dict(r) async for r in result]

    async def mark_divergence(
        self,
        feature_id: str,
        target_type: NodeType,
        target_id: str,
        confidence: float,
        source_event_id: str,
        reasoning: str = "",
    ) -> None:
        """
        Create or update a DIVERGES_FROM edge — the primary R3 signal edge.
        Called exclusively by MonitorAgent. Never by pipeline code.
        """
        await self.upsert_edge(
            from_type=NodeType.FEATURE,
            from_id=feature_id,
            edge_type=EdgeType.DIVERGES_FROM,
            to_type=target_type,
            to_id=target_id,
            properties={
                "confidence": confidence,
                "source_event_id": source_event_id,
                "created_by": "monitor_agent",
                "reasoning": reasoning,
            },
        )

    async def run_raw(self, cypher: str, **params: Any) -> list[dict]:
        """Escape hatch for ad-hoc queries. Use sparingly."""
        async with self.session() as s:
            result = await s.run(cypher, **params)
            return [dict(r) async for r in result]


# Module-level singleton — services import this
_graph_client: GraphClient | None = None


def get_graph_client() -> GraphClient:
    global _graph_client
    if _graph_client is None:
        _graph_client = GraphClient()
    return _graph_client
