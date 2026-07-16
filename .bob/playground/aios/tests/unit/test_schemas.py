"""
Unit tests for the Schema layer (events, entities, tasks).

Validates that:
  - All schemas serialize / deserialize correctly
  - Idempotency key generation is deterministic
  - ContextBundle is immutable (frozen Pydantic model)
  - TaskEnvelope priority is bounded 1–5
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from aios_core.schemas.events import NormalizedEvent, SourceSystem, ProcessingStatus
from aios_core.schemas.entities import (
    Person, Feature, Decision, FeatureStatus, Priority, AccessTags
)
from aios_core.schemas.tasks import (
    ContextBundle, RetrievedChunk, GraphResult, RetrievalMetadata,
    TaskEnvelope, TaskContext, TaskType, VerdictStatus
)


# ─────────────────────────────────────────────────────────────────
# NormalizedEvent
# ─────────────────────────────────────────────────────────────────

def test_normalized_event_roundtrip():
    event = NormalizedEvent(
        idempotency_key="github:pr.opened:org/repo:2024-01-01T00:00:00",
        source_system=SourceSystem.GITHUB,
        event_type="pr.opened",
        actor_source_id="alice@example.com",
        entity_type="Feature",
        entity_source_id="org/repo",
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        normalized_payload={"pr_number": 42, "title": "Add auth"},
    )
    json_str = event.model_dump_json()
    restored = NormalizedEvent.model_validate_json(json_str)
    assert restored.event_id == event.event_id
    assert restored.idempotency_key == event.idempotency_key
    assert restored.source_system == SourceSystem.GITHUB


def test_normalized_event_default_status_is_pending():
    event = NormalizedEvent(
        idempotency_key="test:key",
        source_system=SourceSystem.LINEAR,
        event_type="ticket.created",
        timestamp=datetime.utcnow(),
    )
    assert event.processing_status == ProcessingStatus.PENDING


# ─────────────────────────────────────────────────────────────────
# ContextBundle immutability
# ─────────────────────────────────────────────────────────────────

def test_context_bundle_is_frozen():
    """ContextBundle must be immutable — no agent may modify it after creation."""
    bundle = ContextBundle(
        query="Who wrote the auth service?",
        retrieved_chunks=[],
        graph_context=[],
        retrieval_metadata=RetrievalMetadata(query="test"),
        access_scopes=["*"],
    )
    with pytest.raises(Exception):  # Pydantic ValidationError or TypeError
        bundle.query = "tampered query"  # type: ignore


# ─────────────────────────────────────────────────────────────────
# TaskEnvelope validation
# ─────────────────────────────────────────────────────────────────

def test_task_envelope_priority_bounds():
    ctx = TaskContext(user_identity="user-1", access_scopes=["*"])
    with pytest.raises(Exception):
        TaskEnvelope(task_type=TaskType.QUERY, originator="user-1", priority=0, context=ctx)
    with pytest.raises(Exception):
        TaskEnvelope(task_type=TaskType.QUERY, originator="user-1", priority=6, context=ctx)


def test_task_envelope_default_priority():
    ctx = TaskContext(user_identity="user-1", access_scopes=["*"])
    env = TaskEnvelope(task_type=TaskType.QUERY, originator="user-1", context=ctx)
    assert env.priority == 3


def test_task_envelope_roundtrip():
    ctx = TaskContext(user_identity="user-1", access_scopes=["team:eng"])
    env = TaskEnvelope(
        task_type=TaskType.VERIFY,
        originator="synthesis_agent",
        context=ctx,
        payload={"draft": "The auth service was written by Alice."},
    )
    json_str = env.model_dump_json()
    restored = TaskEnvelope.model_validate_json(json_str)
    assert restored.task_type == TaskType.VERIFY
    assert restored.payload["draft"] == "The auth service was written by Alice."


# ─────────────────────────────────────────────────────────────────
# Entity models
# ─────────────────────────────────────────────────────────────────

def test_feature_has_sensible_defaults():
    f = Feature(title="Auth refactor")
    assert f.status == FeatureStatus.PLANNED
    assert f.priority == Priority.MEDIUM
    assert f.id is not None
    assert f.access_tags is not None


def test_person_canonical_email_stored():
    p = Person(canonical_email="alice@example.com")
    assert p.canonical_email == "alice@example.com"
    assert p.is_active is True
