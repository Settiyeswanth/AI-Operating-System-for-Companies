"""
Canonical entity models.

These are the first-class citizens of the Knowledge Graph.
Every piece of organizational knowledge resolves to one of these types.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────

class FeatureStatus(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    SHIPPED = "shipped"
    ABANDONED = "abandoned"
    BLOCKED = "blocked"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Severity(str, Enum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"


class ChannelType(str, Enum):
    SLACK = "slack"
    EMAIL = "email"
    PR_COMMENT = "pr_comment"
    TICKET_COMMENT = "ticket_comment"
    MEETING_TRANSCRIPT = "meeting_transcript"


# ─────────────────────────────────────────────────────────────────
# Access Control
# ─────────────────────────────────────────────────────────────────

class AccessTags(BaseModel):
    """
    Access control metadata attached to every stored artifact.
    Evaluated at READ time on every retrieval — not cached across requests.
    """
    min_scope: str | None = None           # e.g. "team:engineering"
    restricted_to: list[str] = Field(default_factory=list)  # explicit user IDs
    pii_flag: bool = False
    sensitivity_level: str = "internal"   # "public" | "internal" | "confidential" | "restricted"

    def is_accessible_by(self, user_scopes: list[str], user_id: str, user_grants: list[str]) -> bool:
        """
        Returns True if the user is permitted to see this artifact.
        Rule: deny if any condition fails. Exclusions are never disclosed.
        """
        # Explicit deny list takes priority
        if user_id in self.restricted_to:
            return False

        # PII requires explicit grant
        if self.pii_flag and "pii" not in user_grants:
            return False

        # Minimum scope check
        if self.min_scope and self.min_scope not in user_scopes:
            return False

        return True


# ─────────────────────────────────────────────────────────────────
# Base Entity
# ─────────────────────────────────────────────────────────────────

class BaseEntity(BaseModel):
    """
    All knowledge graph entities inherit from this.
    source_ids maps source system name → source-local identifier.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    source_ids: dict[str, str] = Field(default_factory=dict)
    confidence: float = 1.0
    is_stale: bool = False
    stale_since: datetime | None = None
    access_tags: AccessTags = Field(default_factory=AccessTags)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────
# Concrete Entity Types
# ─────────────────────────────────────────────────────────────────

class Person(BaseEntity):
    """
    A human actor in the organization.
    canonical_email is the primary resolution key — must be normalized (lowercase).
    """
    canonical_email: str = ""
    display_names: list[str] = Field(default_factory=list)
    team_ids: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    is_active: bool = True


class Team(BaseEntity):
    name: str
    parent_team_id: str | None = None
    area: str | None = None           # "engineering" | "product" | "gtm" etc.


class Feature(BaseEntity):
    """
    A unit of product work — maps to a Linear issue, GitHub issue, or equivalent.
    The DIVERGES_FROM edge hangs off this node (R3 signal).
    """
    title: str
    description: str = ""
    status: FeatureStatus = FeatureStatus.PLANNED
    priority: Priority = Priority.MEDIUM
    spec_url: str | None = None
    linked_requirement_ids: list[str] = Field(default_factory=list)
    linked_decision_ids: list[str] = Field(default_factory=list)
    blocked_since: datetime | None = None


class Decision(BaseEntity):
    """
    A recorded organizational decision — created from Linear comments,
    Slack threads, or explicit decision documents.
    """
    summary: str
    rationale: str = ""
    alternatives_rejected: list[str] = Field(default_factory=list)
    made_by_ids: list[str] = Field(default_factory=list)
    made_at: datetime = Field(default_factory=datetime.utcnow)
    superseded_by_id: str | None = None
    source_url: str | None = None


class Incident(BaseEntity):
    severity: Severity = Severity.P2
    status: IncidentStatus = IncidentStatus.OPEN
    title: str = ""
    affected_feature_ids: list[str] = Field(default_factory=list)
    root_cause: str | None = None
    resolved_by_ids: list[str] = Field(default_factory=list)
    resolved_at: datetime | None = None


class Message(BaseEntity):
    """
    A captured communication artifact.
    IMPORTANT: content_summary stores an LLM-generated summary.
    Raw content is NEVER stored in the graph — privacy by design.
    """
    channel_type: ChannelType = ChannelType.SLACK
    author_id: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    content_summary: str = ""
    source_url: str | None = None
    parent_message_id: str | None = None      # For thread replies
    referenced_entity_ids: list[str] = Field(default_factory=list)
    reaction_counts: dict[str, int] = Field(default_factory=dict)


class Codeunit(BaseEntity):
    """
    A tracked code artifact — typically a file or module.
    Created from GitHub push/PR events.
    """
    path: str
    language: str | None = None
    repository: str = ""
    last_modified_by_id: str | None = None
    linked_feature_ids: list[str] = Field(default_factory=list)
