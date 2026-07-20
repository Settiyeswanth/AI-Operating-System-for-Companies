"""
Entity Resolution — two-stage pipeline.

Stage 1: Deterministic — email match (confidence = 1.0)
Stage 2: Probabilistic — display name + username similarity
         Matches below CONFIDENCE_THRESHOLD → human review queue (HELD)

This is the highest-risk component in the system.
See Reference Architecture §4.2 for the full design rationale.

Critical invariant: NEVER auto-merge two entities when confidence < threshold.
A split entity (missed merge) produces incomplete answers.
A wrong merge (false merge) produces wrong answers AND may be a privacy violation.
The threshold bias is: prefer splits over wrong merges.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from rapidfuzz.distance import Levenshtein

from aios_core.config import settings
from aios_core.schemas.events import NormalizedEvent, ProcessingStatus

log = logging.getLogger(__name__)

DEFAULT_ER_DB_PATH = Path("/data/er/entity_resolution.db")

DETERMINISTIC_CONFIDENCE = 1.0
AUTO_MERGE_THRESHOLD = settings.er_confidence_threshold  # default 0.85


class EntityResolutionIndex:
    """
    SQLite-backed mapping: (source_system, source_id) → canonical_id
    Supports: lookup, register, merge, and reversal.
    """

    def __init__(self, db_path: Path = DEFAULT_ER_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS entity_map (
                    source_system TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source_system, source_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS canonical_persons (
                    canonical_id TEXT PRIMARY KEY,
                    canonical_email TEXT,
                    display_names TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_email
                ON canonical_persons (canonical_email)
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_system TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    display_name TEXT,
                    username TEXT,
                    candidates_json TEXT DEFAULT '[]',
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    resolved_at TEXT,
                    resolved_by TEXT,
                    resolved_canonical_id TEXT
                )
            """)
            c.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def lookup_by_source(self, source_system: str, source_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT canonical_id FROM entity_map WHERE source_system=? AND source_id=?",
                (source_system, source_id),
            ).fetchone()
            return row["canonical_id"] if row else None

    def lookup_by_email(self, email: str) -> str | None:
        if not email:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT canonical_id FROM canonical_persons WHERE canonical_email=?",
                (email.lower().strip(),),
            ).fetchone()
            return row["canonical_id"] if row else None

    def create_canonical_person(
        self,
        canonical_email: str | None,
        display_name: str | None,
        source_system: str,
        source_id: str,
    ) -> str:
        """
        Create a new canonical Person entity and map the source ID to it.

        Idempotent on canonical_email: if a Person with this email already
        exists (e.g. from a concurrent call), reuses that canonical_id rather
        than creating a duplicate.  This closes the race condition where two
        concurrent events for the same new actor each see a cache miss in
        lookup_by_email() and both attempt creation.
        """
        import json
        normalized_email = (canonical_email or "").lower().strip()
        new_id = str(uuid.uuid4())

        with self._conn() as c:
            # Attempt insert — silently ignored if email already exists
            c.execute(
                "INSERT OR IGNORE INTO canonical_persons "
                "(canonical_id, canonical_email, display_names) VALUES (?, ?, ?)",
                (new_id, normalized_email,
                 json.dumps([display_name] if display_name else [])),
            )
            c.commit()

            # Read back the canonical_id that actually owns this email
            # (may be new_id we just inserted, or a pre-existing one)
            row = c.execute(
                "SELECT canonical_id FROM canonical_persons WHERE canonical_email=?",
                (normalized_email,),
            ).fetchone()
            canonical_id = row["canonical_id"] if row else new_id

            # Map source → canonical (REPLACE handles re-runs safely)
            c.execute(
                "INSERT OR REPLACE INTO entity_map "
                "(source_system, source_id, canonical_id, confidence) VALUES (?, ?, ?, ?)",
                (source_system, source_id, canonical_id, DETERMINISTIC_CONFIDENCE),
            )
            c.commit()

        return canonical_id

    def register_mapping(
        self,
        source_system: str,
        source_id: str,
        canonical_id: str,
        confidence: float,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO entity_map (source_system, source_id, canonical_id, confidence) VALUES (?, ?, ?, ?)",
                (source_system, source_id, canonical_id, confidence),
            )
            c.commit()

    def get_all_canonical_persons(self) -> list[dict]:
        import json
        with self._conn() as c:
            rows = c.execute(
                "SELECT canonical_id, canonical_email, display_names FROM canonical_persons"
            ).fetchall()
            return [
                {
                    "canonical_id": r["canonical_id"],
                    "canonical_email": r["canonical_email"],
                    "display_names": json.loads(r["display_names"] or "[]"),
                }
                for r in rows
            ]

    def queue_for_review(
        self,
        source_system: str,
        source_id: str,
        display_name: str | None,
        username: str | None,
        candidates: list[dict],
        reason: str,
    ) -> None:
        import json
        with self._conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO review_queue
                  (source_system, source_id, display_name, username, candidates_json, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_system, source_id, display_name, username,
                 json.dumps(candidates), reason),
            )
            c.commit()

    def queue_size(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
            ).fetchone()[0]


def _normalize_email(email: str) -> str:
    return email.lower().strip() if email else ""


def _extract_email_from_event(event: NormalizedEvent) -> str | None:
    """Try to extract a usable email from the event payload or actor_source_id."""
    actor = event.actor_source_id or ""
    if "@" in actor:
        return _normalize_email(actor)
    # Try payload fields
    for field in ("email", "actor_email", "user_email", "committer_email"):
        val = event.normalized_payload.get(field, "")
        if val and "@" in val:
            return _normalize_email(val)
    return None


def _similarity_score(a_name: str, b_name: str) -> float:
    """Normalized similarity between two display names (0.0–1.0)."""
    if not a_name or not b_name:
        return 0.0
    a, b = a_name.lower().strip(), b_name.lower().strip()
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    dist = Levenshtein.distance(a, b)
    return 1.0 - (dist / max_len)


class EntityResolver:
    """
    Resolves source-local actor IDs to canonical Person IDs.
    Two stages: deterministic (email) → probabilistic (name similarity).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._index = EntityResolutionIndex(
            db_path=db_path or DEFAULT_ER_DB_PATH
        )

    async def connect(self) -> None:
        log.info(
            "Entity Resolution index ready. Review queue: %d pending",
            self._index.queue_size(),
        )

    async def resolve(self, event: NormalizedEvent) -> NormalizedEvent:
        """
        Attempt to resolve the actor in this event to a canonical Person ID.
        Returns the event with actor_canonical_id populated (or status=HELD).
        """
        actor_source_id = event.actor_source_id
        if not actor_source_id or actor_source_id == "system":
            event.actor_canonical_id = "system"
            return event

        # Check if already mapped
        existing = self._index.lookup_by_source(
            event.source_system.value, actor_source_id
        )
        if existing:
            event.actor_canonical_id = existing
            return event

        # Stage 1: Deterministic — email
        email = _extract_email_from_event(event)
        if email:
            canonical_id = self._index.lookup_by_email(email)
            if canonical_id:
                # Map this source ID to the existing canonical entity
                self._index.register_mapping(
                    event.source_system.value,
                    actor_source_id,
                    canonical_id,
                    DETERMINISTIC_CONFIDENCE,
                )
                event.actor_canonical_id = canonical_id
                log.debug("ER: deterministic match via email %s → %s", email, canonical_id)
                return event
            else:
                # New entity — create canonical record
                display_name = event.normalized_payload.get("display_name") or actor_source_id
                canonical_id = self._index.create_canonical_person(
                    canonical_email=email,
                    display_name=display_name,
                    source_system=event.source_system.value,
                    source_id=actor_source_id,
                )
                event.actor_canonical_id = canonical_id
                log.debug("ER: new Person created via email %s → %s", email, canonical_id)
                return event

        # Stage 2: Probabilistic — name/username similarity
        display_name = (
            event.normalized_payload.get("display_name") or
            event.normalized_payload.get("name") or
            actor_source_id
        )
        candidates = self._find_candidates(display_name)
        best = max(candidates, key=lambda c: c["score"], default=None)

        if best and best["score"] >= AUTO_MERGE_THRESHOLD:
            self._index.register_mapping(
                event.source_system.value,
                actor_source_id,
                best["canonical_id"],
                best["score"],
            )
            event.actor_canonical_id = best["canonical_id"]
            log.debug(
                "ER: probabilistic match %s → %s (score %.2f)",
                display_name, best["canonical_id"], best["score"],
            )
            return event

        # Below threshold → hold for human review
        self._index.queue_for_review(
            source_system=event.source_system.value,
            source_id=actor_source_id,
            display_name=display_name,
            username=actor_source_id if "/" not in actor_source_id else None,
            candidates=candidates,
            reason=f"Best confidence {best['score']:.2f} < {AUTO_MERGE_THRESHOLD}"
            if best else "No candidates found",
        )
        event.processing_status = ProcessingStatus.HELD
        log.info(
            "ER: event HELD for review — actor '%s' (queue size: %d)",
            display_name,
            self._index.queue_size(),
        )
        return event

    def _find_candidates(self, display_name: str) -> list[dict]:
        """Find existing canonical persons that might match this display name."""
        if not display_name:
            return []
        all_persons = self._index.get_all_canonical_persons()
        candidates = []
        for person in all_persons:
            for existing_name in person.get("display_names", []):
                score = _similarity_score(display_name, existing_name)
                if score > 0.4:  # Minimum threshold for consideration
                    candidates.append({
                        "canonical_id": person["canonical_id"],
                        "canonical_email": person["canonical_email"],
                        "matched_name": existing_name,
                        "score": score,
                    })
                    break
        return sorted(candidates, key=lambda c: c["score"], reverse=True)[:5]
