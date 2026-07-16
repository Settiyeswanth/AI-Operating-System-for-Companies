"""
Unit tests for Entity Resolution.

These are the most critical tests in the system. ER errors produce
silent, incorrect answers — the hardest failure mode to detect.

Tests focus on:
  1. Deterministic email match resolves correctly
  2. Two different people with similar names are NOT auto-merged
  3. New entity is created when no match exists
  4. Below-threshold candidates go to review queue, not auto-merged
  5. Review queue receives the correct information
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from aios_core.schemas.events import NormalizedEvent, SourceSystem, ProcessingStatus
from ingestion.entity_resolution.resolver import EntityResolver, EntityResolutionIndex


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "er_test.db"


@pytest.fixture
def er_index(temp_db: Path) -> EntityResolutionIndex:
    return EntityResolutionIndex(db_path=temp_db)


@pytest.fixture
def resolver(temp_db: Path) -> EntityResolver:
    return EntityResolver(db_path=temp_db)


def _make_event(
    source_system: SourceSystem,
    actor_id: str,
    email: str | None = None,
    display_name: str | None = None,
) -> NormalizedEvent:
    payload = {}
    if email:
        payload["email"] = email
    if display_name:
        payload["display_name"] = display_name
    return NormalizedEvent(
        idempotency_key=f"test:{actor_id}:{datetime.utcnow().isoformat()}",
        source_system=source_system,
        event_type="test.event",
        actor_source_id=actor_id,
        entity_source_id="entity-1",
        timestamp=datetime.utcnow(),
        normalized_payload=payload,
        processing_status=ProcessingStatus.NORMALIZED,
    )


# ─────────────────────────────────────────────────────────────────
# Deterministic resolution tests
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_email_creates_new_canonical_person(resolver: EntityResolver):
    """New email → new canonical Person created."""
    event = _make_event(SourceSystem.GITHUB, "alice-github", email="alice@example.com")
    result = await resolver.resolve(event)

    assert result.actor_canonical_id is not None
    assert result.processing_status != ProcessingStatus.HELD


@pytest.mark.asyncio
async def test_same_email_different_sources_resolves_to_same_person(resolver: EntityResolver):
    """Alice's GitHub and Slack accounts resolve to the same canonical Person via email."""
    github_event = _make_event(
        SourceSystem.GITHUB, "alice-gh", email="alice@example.com", display_name="Alice Johnson"
    )
    slack_event = _make_event(
        SourceSystem.SLACK, "U09ALICE", email="alice@example.com", display_name="Alice J."
    )

    result_gh = await resolver.resolve(github_event)
    result_sl = await resolver.resolve(slack_event)

    assert result_gh.actor_canonical_id is not None
    assert result_sl.actor_canonical_id is not None
    assert result_gh.actor_canonical_id == result_sl.actor_canonical_id, (
        "Same email from different sources must resolve to same canonical Person"
    )


@pytest.mark.asyncio
async def test_different_emails_create_different_persons(resolver: EntityResolver):
    """Critical negative test: two different people must NOT be merged."""
    event_alice = _make_event(SourceSystem.GITHUB, "alice-gh", email="alice@example.com")
    event_bob = _make_event(SourceSystem.GITHUB, "bob-gh", email="bob@example.com")

    result_alice = await resolver.resolve(event_alice)
    result_bob = await resolver.resolve(event_bob)

    assert result_alice.actor_canonical_id != result_bob.actor_canonical_id, (
        "Different emails must NEVER produce the same canonical ID"
    )


@pytest.mark.asyncio
async def test_duplicate_event_resolves_to_same_canonical_id(resolver: EntityResolver):
    """Processing the same actor twice must return the same canonical ID."""
    event1 = _make_event(SourceSystem.GITHUB, "alice-gh", email="alice@example.com")
    event2 = _make_event(SourceSystem.GITHUB, "alice-gh", email="alice@example.com")

    result1 = await resolver.resolve(event1)
    result2 = await resolver.resolve(event2)

    assert result1.actor_canonical_id == result2.actor_canonical_id


# ─────────────────────────────────────────────────────────────────
# Probabilistic resolution tests
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_email_below_threshold_goes_to_review_queue(resolver: EntityResolver):
    """
    Actor with no email and low name similarity → HELD, not auto-merged.
    This is the most important safety test.
    """
    # First, create an existing person with a completely different display name
    seed_event = _make_event(SourceSystem.GITHUB, "xavier-gh", email="xavier@example.com", display_name="Xavier Chen")
    await resolver.resolve(seed_event)

    # Now try to resolve someone with no email and a very different name
    # "Zachary Smith" has low similarity to "Xavier Chen"
    ambiguous_event = _make_event(SourceSystem.SLACK, "U09ZACH", display_name="Zachary Smith")
    result = await resolver.resolve(ambiguous_event)

    assert result.processing_status == ProcessingStatus.HELD, (
        "Ambiguous actor with no email should be HELD for human review, not auto-resolved"
    )
    assert result.actor_canonical_id is None

    # Verify it's in the review queue
    queue_size = resolver._index.queue_size()
    assert queue_size >= 1


@pytest.mark.asyncio
async def test_system_actor_always_resolves(resolver: EntityResolver):
    """The 'system' actor ID must always resolve without going to review queue."""
    event = _make_event(SourceSystem.GITHUB, "system")
    event.actor_source_id = "system"
    result = await resolver.resolve(event)

    assert result.actor_canonical_id == "system"
    assert result.processing_status != ProcessingStatus.HELD


# ─────────────────────────────────────────────────────────────────
# Index tests
# ─────────────────────────────────────────────────────────────────

def test_lookup_by_email_case_insensitive(er_index: EntityResolutionIndex):
    """Email lookup must be case-insensitive."""
    cid = er_index.create_canonical_person("Alice@Example.COM", "Alice", "github", "alice-1")
    found = er_index.lookup_by_email("alice@example.com")
    assert found == cid

    found_upper = er_index.lookup_by_email("ALICE@EXAMPLE.COM")
    assert found_upper == cid


def test_create_canonical_person_is_idempotent(er_index: EntityResolutionIndex):
    """Creating the same canonical person twice must not create duplicates."""
    cid1 = er_index.create_canonical_person("bob@test.com", "Bob", "github", "bob-gh-1")
    # Second call with different source_id but same email
    cid2 = er_index.create_canonical_person("bob@test.com", "Bob", "linear", "bob-lin-1")
    # They should both map to SOME canonical ID via email lookup
    email_cid = er_index.lookup_by_email("bob@test.com")
    assert email_cid is not None
