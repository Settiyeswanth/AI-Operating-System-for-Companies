"""
Task and agent communication schemas.

These are the typed contracts that flow between agents and between
the agent layer and the memory layer. Nothing is passed as raw dicts.

Architecture rule: ContextBundle is IMMUTABLE once created at retrieval time.
It is passed unchanged through QueryAgent → SynthesisAgent → VerificationAgent.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────
# Task Queue
# ─────────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    QUERY = "query"
    SYNTHESIS = "synthesis"
    VERIFY = "verify"
    MONITOR_CHECK = "monitor_check"


class TaskContext(BaseModel):
    """
    Identity and authorization context carried on every task.
    Agents may not elevate privileges beyond what this context grants.
    """
    user_identity: str
    access_scopes: list[str] = Field(default_factory=list)
    user_grants: list[str] = Field(default_factory=list)  # e.g. ["pii:own-team"]
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_task_id: str | None = None
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class TaskEnvelope(BaseModel):
    """
    The message format used for all inter-agent communication.
    Agents communicate ONLY through the task queue — never direct calls.
    """
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_type: TaskType
    originator: str                   # agent class name or user_id
    priority: int = Field(default=3, ge=1, le=5)  # 1 = highest
    deadline_ms: int = 5000
    audit_required: bool = True
    context: TaskContext
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────
# Context Bundle — The Retrieval Contract
# ─────────────────────────────────────────────────────────────────

class RetrievedChunk(BaseModel):
    """A single retrieved document chunk from the vector store."""
    chunk_id: str
    source_artifact_id: str
    content: str                      # The actual text
    score: float                      # Retrieval relevance score (0–1)
    retrieval_method: str             # "dense" | "sparse" | "hybrid"
    source_system: str
    source_url: str | None = None
    timestamp: datetime
    entity_refs: list[str] = Field(default_factory=list)  # Canonical entity IDs referenced
    access_tags_verified: bool = False  # Set True after access control check


class GraphResult(BaseModel):
    """A result from a Knowledge Graph traversal query."""
    query_name: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""                 # Short natural-language summary of the result


class RetrievalMetadata(BaseModel):
    query: str
    sub_queries: list[str] = Field(default_factory=list)
    vector_retrieved: int = 0
    graph_retrieved: int = 0
    total_after_fusion: int = 0
    retrieval_latency_ms: float = 0.0
    access_filtered_count: int = 0   # How many chunks were excluded by access control


class ContextBundle(BaseModel):
    """
    IMMUTABLE context package created at retrieval time.
    Passed unchanged through the entire agent chain:
      QueryAgent → (SynthesisAgent) → VerificationAgent

    VerificationAgent receives this + the draft answer. Nothing else.
    This structural separation ensures the verifier cannot be influenced
    by anything the producing agent did not actually use.
    """
    bundle_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    query: str
    retrieved_chunks: list[RetrievedChunk]
    graph_context: list[GraphResult]
    retrieval_metadata: RetrievalMetadata
    access_scopes: list[str]
    created_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"frozen": True}   # Pydantic v2: prevents mutation


# ─────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────

class VerdictStatus(str, Enum):
    PASS = "pass"
    UNCERTAIN = "uncertain"
    FAIL = "fail"


class ClaimAnnotation(BaseModel):
    claim: str
    status: str                       # "SUPPORTED" | "UNSUPPORTED" | "CONTRADICTED"
    source_chunk_id: str | None = None
    reasoning: str = ""


class VerificationVerdict(BaseModel):
    verdict: VerdictStatus
    claim_annotations: list[ClaimAnnotation] = Field(default_factory=list)
    reasoning: str = ""
    verified_at: datetime = Field(default_factory=datetime.utcnow)
    verifier_model: str = ""


# ─────────────────────────────────────────────────────────────────
# Misalignment Alerts (MonitorAgent output)
# ─────────────────────────────────────────────────────────────────

class AlertType(str, Enum):
    SCOPE_DRIFT = "scope_drift"
    DELIVERY_DELAY = "delivery_delay"
    CONTRADICTION_DETECTED = "contradiction_detected"
    ORPHANED_REQUIREMENT = "orphaned_requirement"
    COMPLETION_MISMATCH = "completion_mismatch"
    STALE_DECISION = "stale_decision"
    BLOCKED_WITHOUT_DECISION = "blocked_without_decision"


class AlertSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MisalignmentAlert(BaseModel):
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_type: AlertType
    severity: AlertSeverity
    summary: str
    detail: str = ""
    feature_id: str | None = None
    decision_id: str | None = None
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    acknowledged: bool = False
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    dismissed: bool = False
    rule_id: str = ""                 # Which MonitorAgent rule fired
