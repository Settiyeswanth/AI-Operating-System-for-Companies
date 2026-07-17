"""
Normalized event schemas.

Every piece of data that enters the system — from any source — is
immediately converted to a NormalizedEvent before any processing.
This is the canonical data contract for the ingestion pipeline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


class SourceSystem(str, Enum):
    GITHUB = "github"
    LINEAR = "linear"
    SLACK = "slack"
    NOTION = "notion"       # Phase 2
    SALESFORCE = "salesforce"  # Phase 2
    ZOOM = "zoom"           # Phase 2
    INTERNAL = "internal"   # System-generated events


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    NORMALIZED = "normalized"
    RESOLVED = "resolved"       # Entity resolution complete
    ENRICHED = "enriched"       # Embeddings + classification done
    FAILED = "failed"
    HELD = "held"               # Awaiting ER human review


class RawEvent(BaseModel):
    """
    Opaque envelope from a connector before any normalization.
    Written by connector, consumed by ingestion pipeline.
    """
    source_system: SourceSystem
    event_type: str              # Source-specific type, e.g. "push", "IssueCreate"
    received_at: datetime = Field(default_factory=datetime.utcnow)
    raw_payload: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str = ""    # Connector sets this; ingestion deduplicates on it


class NormalizedEvent(BaseModel):
    """
    The canonical event model. All downstream processing works on this type.

    After connector mapping:  actor_canonical_id = None (ER not run yet)
    After entity resolution:  actor_canonical_id = canonical UUID
    After enrichment:         processing_status = ENRICHED
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str            # Deduplication key — reject duplicates at ingestion
    source_system: SourceSystem
    event_type: str                 # Normalized type, e.g. "pr.opened", "ticket.created"

    # Actor (the human or system that caused this event)
    actor_source_id: str = ""       # Source-local identity (email, username, user_id)
    actor_canonical_id: str | None = None  # Populated after entity resolution

    # Primary entity this event concerns
    entity_type: str = ""           # "Feature", "Message", "Codeunit", etc.
    entity_source_id: str = ""      # Source-local identifier
    entity_canonical_id: str | None = None

    # Timing
    timestamp: datetime             # When the event happened in the source system
    received_at: datetime = Field(default_factory=datetime.utcnow)

    # Payload (source-specific fields after initial normalization)
    normalized_payload: dict[str, Any] = Field(default_factory=dict)

    # Pipeline state
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    schema_version: str = "1.0"
    error_message: str | None = None

    def make_idempotency_key(source: SourceSystem, event_type: str,
                              entity_id: str, timestamp: datetime) -> str:
        """Canonical idempotency key construction — call from connectors."""
        return f"{source.value}:{event_type}:{entity_id}:{timestamp.isoformat()}"
