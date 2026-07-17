"""
Graph ontology registry.

Defines the canonical set of node types and edge types used in the
Knowledge Graph. All pipeline code references these constants —
no magic strings in Cypher queries.
"""

from __future__ import annotations

from enum import Enum


class NodeType(str, Enum):
    PERSON = "Person"
    TEAM = "Team"
    FEATURE = "Feature"
    DECISION = "Decision"
    INCIDENT = "Incident"
    MESSAGE = "Message"
    CODEUNIT = "Codeunit"
    REQUIREMENT = "Requirement"
    PROJECT = "Project"


class EdgeType(str, Enum):
    # Person → work artifacts
    AUTHORED = "AUTHORED"

    # Feature relationships
    IMPLEMENTS = "IMPLEMENTS"       # Feature → Requirement
    DEPENDS_ON = "DEPENDS_ON"       # Feature → Feature
    DIVERGES_FROM = "DIVERGES_FROM" # Feature → Requirement|Decision  ← R3 SIGNAL

    # Decision effects
    CONSTRAINS = "CONSTRAINS"       # Decision → Feature|Project

    # Incident effects
    AFFECTED = "AFFECTED"           # Incident → Feature|Codeunit

    # Message references
    REFERENCES = "REFERENCES"       # Message → any entity

    # Org structure
    MEMBER_OF = "MEMBER_OF"         # Person → Team (carries valid_from, valid_until)


# Edge property schema — applied to ALL edges
# Created at runtime, not enforced by Neo4j schema, but validated in code
EDGE_PROPERTIES = {
    "created_at": "datetime",
    "updated_at": "datetime",
    "confidence": "float",       # 0.0–1.0; edges below 0.7 excluded from answers
    "source_event_id": "str",    # The NormalizedEvent.event_id that created this edge
    "created_by": "str",         # "pipeline" | "llm_inference" | "human:{user_id}"
}

# Edges created by LLM inference are stored at this initial confidence level
# They are excluded from direct answers until confirmed by human review or
# corroborated by additional events
LLM_INFERRED_EDGE_CONFIDENCE = 0.6

# Edges with confidence below this threshold are excluded from query answers
ANSWER_CONFIDENCE_THRESHOLD = 0.7
