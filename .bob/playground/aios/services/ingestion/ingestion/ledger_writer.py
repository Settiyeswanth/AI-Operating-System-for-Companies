"""
Ledger Writer — thin wrapper around EventLedger for the ingestion service.

Provides deduplication check (ledger.exists) before insert.
"""

from __future__ import annotations

import logging

from aios_core.schemas.events import NormalizedEvent

log = logging.getLogger(__name__)

# Import the ledger from memory service package
# In Phase 1 all services share the same ledger file path
try:
    from memory.ledger.store import EventLedger, DEFAULT_LEDGER_PATH
except ImportError:
    # Fallback when running ingestion in isolation
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "memory"))
    from memory.ledger.store import EventLedger, DEFAULT_LEDGER_PATH


class LedgerWriter:
    def __init__(self) -> None:
        self._ledger = EventLedger(db_path=DEFAULT_LEDGER_PATH)

    def exists(self, idempotency_key: str) -> bool:
        """Check if an event with this key is already in the ledger."""
        with self._ledger._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE idempotency_key=? LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            return row is not None

    def append(self, event: NormalizedEvent) -> bool:
        """Append event to ledger. Returns True if new, False if duplicate."""
        appended = self._ledger.append(event)
        if not appended:
            log.debug("Duplicate in ledger: %s", event.idempotency_key)
        return appended
