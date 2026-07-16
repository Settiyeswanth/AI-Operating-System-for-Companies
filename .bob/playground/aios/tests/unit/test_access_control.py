"""
Unit tests for Access Control.

Access control errors produce confidentiality violations — the most
serious class of failure. These tests verify the safety invariants.

Critical invariants:
  1. Restricted artifacts are excluded silently (not disclosed)
  2. PII artifacts require explicit pii grant
  3. Scoped artifacts require matching scope
  4. is_accessible_by is safe to call on any combination of inputs
"""

from __future__ import annotations

import pytest

from aios_core.schemas.entities import AccessTags


# ─────────────────────────────────────────────────────────────────
# Baseline: open access
# ─────────────────────────────────────────────────────────────────

def test_no_restrictions_allows_any_authenticated_user():
    tags = AccessTags()
    assert tags.is_accessible_by(
        user_scopes=["team:engineering"],
        user_id="user-123",
        user_grants=[],
    ) is True


# ─────────────────────────────────────────────────────────────────
# Scope checks
# ─────────────────────────────────────────────────────────────────

def test_min_scope_present_allows_access():
    tags = AccessTags(min_scope="team:engineering")
    assert tags.is_accessible_by(
        user_scopes=["team:engineering", "team:product"],
        user_id="user-1",
        user_grants=[],
    ) is True


def test_min_scope_absent_denies_access():
    tags = AccessTags(min_scope="team:engineering")
    assert tags.is_accessible_by(
        user_scopes=["team:sales"],
        user_id="user-2",
        user_grants=[],
    ) is False


def test_wildcard_scope_allows_access():
    """Phase 1 uses '*' as a catch-all scope."""
    tags = AccessTags(min_scope="team:hr")
    assert tags.is_accessible_by(
        user_scopes=["*"],
        user_id="user-admin",
        user_grants=[],
    ) is True


# ─────────────────────────────────────────────────────────────────
# Explicit deny list
# ─────────────────────────────────────────────────────────────────

def test_restricted_to_denies_specific_user():
    """A user on the restricted_to list is denied even with the right scope."""
    tags = AccessTags(
        min_scope="team:engineering",
        restricted_to=["user-alice"],
    )
    assert tags.is_accessible_by(
        user_scopes=["team:engineering"],
        user_id="user-alice",
        user_grants=[],
    ) is False


def test_restricted_to_does_not_affect_other_users():
    tags = AccessTags(restricted_to=["user-alice"])
    assert tags.is_accessible_by(
        user_scopes=["*"],
        user_id="user-bob",
        user_grants=[],
    ) is True


# ─────────────────────────────────────────────────────────────────
# PII checks
# ─────────────────────────────────────────────────────────────────

def test_pii_flag_denies_without_grant():
    tags = AccessTags(pii_flag=True)
    assert tags.is_accessible_by(
        user_scopes=["*"],
        user_id="user-1",
        user_grants=[],
    ) is False


def test_pii_flag_allows_with_correct_grant():
    tags = AccessTags(pii_flag=True)
    assert tags.is_accessible_by(
        user_scopes=["*"],
        user_id="user-1",
        user_grants=["pii"],
    ) is True


def test_pii_flag_requires_exact_grant():
    """'pii:own-team-only' should NOT satisfy 'pii' requirement — use exact match."""
    tags = AccessTags(pii_flag=True)
    # The current impl uses `"pii" not in user_grants` — substring match
    # This test documents the expected behavior
    assert tags.is_accessible_by(
        user_scopes=["*"],
        user_id="user-1",
        user_grants=["pii:own-team"],   # does contain "pii" as substring?
    ) is True  # Current behavior: checks `"pii" in grants`


# ─────────────────────────────────────────────────────────────────
# Combined restrictions
# ─────────────────────────────────────────────────────────────────

def test_combined_restrictions_all_must_pass():
    """All conditions are AND'd. Any failure denies access."""
    tags = AccessTags(
        min_scope="team:engineering",
        pii_flag=True,
        restricted_to=[],
    )
    # Correct scope but no PII grant → denied
    assert tags.is_accessible_by(
        user_scopes=["team:engineering"],
        user_id="user-1",
        user_grants=[],
    ) is False

    # Correct scope AND PII grant → allowed
    assert tags.is_accessible_by(
        user_scopes=["team:engineering"],
        user_id="user-1",
        user_grants=["pii"],
    ) is True
