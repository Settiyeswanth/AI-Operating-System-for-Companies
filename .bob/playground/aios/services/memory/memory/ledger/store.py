"""
Temporal Event Ledger — immutable append-only event store.

Phase 1: SQLite (zero ops, sufficient for prototype scale)
Phase 2: Swap backend to S3 Parquet + Athena (change store.py only)

The ledger serves two purposes:
  1. Audit trail — every event that entered the system, original form
  2. Recovery — replay events to rebuild Knowledge Graph or Vector Store
     after a data quality issue or schema migration
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from aios_core.schemas.events import NormalizedEvent, SourceSystem, ProcessingStatus

log = logging.getLogger(__name__)

# Default ledger path — override via env for production
DEFAULT_LEDGER_PATH = Path("/data/ledger/events.db")


class EventLedger:
    """
    Append-only SQLite ledger. Never UPDATE or DELETE — only INSERT and SELECT.
    Correction events are separate INSERT rows, not overwrites.
    """

    def __init__(self, db_path: Path | str = DEFAULT_LEDGER_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id        TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source_system   TEXT NOT NULL,
                    event_type      TEXT NOT NULL,
                    actor_source_id TEXT,
                    entity_source_id TEXT,
                    event_timestamp TEXT NOT NULL,
                    received_at     TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    schema_version  TEXT NOT NULL DEFAULT '1.0',
                    payload_json    TEXT NOT NULL,
                    inserted_at     TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_source_system
                ON events (source_system, event_timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_event_id
                ON events (event_id)
            """)
            conn.commit()
        log.debug("Ledger schema ready at %s", self.db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def append(self, event: NormalizedEvent) -> bool:
        """
        Append an event to the ledger. Returns True if appended, False if
        idempotency key already exists (duplicate — safe to ignore).
        """
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, idempotency_key, source_system, event_type,
                        actor_source_id, entity_source_id, event_timestamp,
                        received_at, processing_status, schema_version, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.idempotency_key,
                        event.source_system.value,
                        event.event_type,
                        event.actor_source_id,
                        event.entity_source_id,
                        event.timestamp.isoformat(),
                        event.received_at.isoformat(),
                        event.processing_status.value,
                        event.schema_version,
                        json.dumps(event.normalized_payload),
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Duplicate idempotency_key — not an error, just a duplicate
            return False

    def update_status(self, event_id: str, status: ProcessingStatus) -> None:
        """
        Update the processing status of an event.
        NOTE: This is the ONLY permitted mutation on the ledger.
        All other fields are immutable after insert.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE events SET processing_status = ? WHERE event_id = ?",
                (status.value, event_id),
            )
            conn.commit()

    def get_event(self, event_id: str) -> NormalizedEvent | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._row_to_event(row) if row else None

    def replay(
        self,
        source_system: SourceSystem | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        batch_size: int = 100,
    ) -> Iterator[NormalizedEvent]:
        """
        Yield events for replay. Used during:
          - Knowledge Graph rebuild after schema migration
          - Recovery after data quality correction
        """
        clauses = []
        params: list = []
        if source_system:
            clauses.append("source_system = ?")
            params.append(source_system.value)
        if since:
            clauses.append("event_timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("event_timestamp <= ?")
            params.append(until.isoformat())

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = 0

        while True:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM events {where} "
                    f"ORDER BY event_timestamp ASC LIMIT ? OFFSET ?",
                    [*params, batch_size, offset],
                ).fetchall()
            if not rows:
                break
            for row in rows:
                event = self._row_to_event(row)
                if event:
                    yield event
            offset += batch_size

    def count(self, source_system: SourceSystem | None = None) -> int:
        with self._conn() as conn:
            if source_system:
                return conn.execute(
                    "SELECT COUNT(*) FROM events WHERE source_system = ?",
                    (source_system.value,),
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def _row_to_event(self, row: sqlite3.Row) -> NormalizedEvent | None:
        try:
            return NormalizedEvent(
                event_id=row["event_id"],
                idempotency_key=row["idempotency_key"],
                source_system=SourceSystem(row["source_system"]),
                event_type=row["event_type"],
                actor_source_id=row["actor_source_id"] or "",
                entity_source_id=row["entity_source_id"] or "",
                timestamp=datetime.fromisoformat(row["event_timestamp"]),
                received_at=datetime.fromisoformat(row["received_at"]),
                processing_status=ProcessingStatus(row["processing_status"]),
                schema_version=row["schema_version"],
                normalized_payload=json.loads(row["payload_json"]),
            )
        except Exception as e:
            log.warning("Could not deserialize ledger row %s: %s", row["event_id"], e)
            return None


# Module-level singleton
_ledger: EventLedger | None = None


def get_ledger(db_path: Path | str | None = None) -> EventLedger:
    global _ledger
    if _ledger is None:
        path = db_path or DEFAULT_LEDGER_PATH
        _ledger = EventLedger(db_path=path)
    return _ledger
