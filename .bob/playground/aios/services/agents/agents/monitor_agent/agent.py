"""
MonitorAgent — Proactive misalignment detection.

Runs on a configurable schedule (default: every 5 minutes).
Applies misalignment rules to the current state of the Knowledge Graph.
Generates MisalignmentAlert objects for confirmed divergences.

Phase 1 rules (2 only — high precision, low false positive):
  Rule 1: BlockedWithoutDecision — Feature blocked > 5 days, no linked Decision
  Rule 2: CompletionMismatch   — Feature SHIPPED but Requirements still OPEN

Do NOT add more rules until Phase 1 rules achieve ≥60% alert precision
(measured by human acknowledgment rate). Alert fatigue is the primary
failure mode for this agent.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aios_core.config import settings
from aios_core.llm_gateway import get_llm_gateway, LLMMessage
from aios_core.schemas.tasks import (
    AlertSeverity,
    AlertType,
    MisalignmentAlert,
)

log = logging.getLogger(__name__)

ALERTS_CHANNEL = "aios:alerts"


class MonitorAgent:
    """
    Scheduled agent that evaluates misalignment rules against the Knowledge Graph.
    Publishes MisalignmentAlert objects to Redis on detection.
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        interval = settings.monitor_poll_interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run_checks,
            "interval",
            minutes=interval,
            id="monitor_agent",
            next_run_time=datetime.now(),  # Run immediately on start
        )
        self._scheduler.start()
        log.info("MonitorAgent started (interval: %d min)", interval)

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown()
        if self._redis:
            await self._redis.aclose()

    async def _run_checks(self) -> None:
        log.info("MonitorAgent: running checks at %s", datetime.utcnow().isoformat())
        try:
            from memory.graph.client import get_graph_client
            graph = get_graph_client()

            alerts: list[MisalignmentAlert] = []
            alerts.extend(await self._check_blocked_without_decision(graph))
            alerts.extend(await self._check_completion_mismatch(graph))

            for alert in alerts:
                await self._publish_alert(alert)

            log.info("MonitorAgent: %d alert(s) generated", len(alerts))
        except Exception as e:
            log.error("MonitorAgent check failed: %s", e, exc_info=True)

    # ─────────────────────────────────────────────────────────────
    # Rule 1: Blocked features without explaining Decisions
    # ─────────────────────────────────────────────────────────────

    async def _check_blocked_without_decision(self, graph) -> list[MisalignmentAlert]:
        threshold_days = 5
        try:
            features = await graph.find_blocked_features_without_decisions(threshold_days)
        except Exception as e:
            log.error("Rule 1 query failed: %s", e)
            return []

        alerts = []
        for f in features:
            days = f.get("days_blocked", 0)
            severity = AlertSeverity.HIGH if days > 10 else AlertSeverity.MEDIUM
            alerts.append(MisalignmentAlert(
                alert_type=AlertType.BLOCKED_WITHOUT_DECISION,
                severity=severity,
                summary=f"Feature '{f.get('title', f.get('feature_id'))}' has been blocked for {days} days with no linked decision explaining the block.",
                detail=(
                    f"Feature ID: {f.get('feature_id')}\n"
                    f"Blocked since: {f.get('blocked_since')}\n"
                    f"Days blocked: {days}\n"
                    f"No Decision node is linked via CONSTRAINS relationship.\n"
                    f"Action: Create a Decision record explaining why this is blocked, "
                    f"or unblock the feature."
                ),
                feature_id=f.get("feature_id"),
                rule_id="blocked_without_decision_v1",
            ))
        return alerts

    # ─────────────────────────────────────────────────────────────
    # Rule 2: Features marked SHIPPED with open Requirements
    # ─────────────────────────────────────────────────────────────

    async def _check_completion_mismatch(self, graph) -> list[MisalignmentAlert]:
        try:
            mismatches = await graph.find_completion_mismatches()
        except Exception as e:
            log.error("Rule 2 query failed: %s", e)
            return []

        alerts = []
        for m in mismatches:
            alerts.append(MisalignmentAlert(
                alert_type=AlertType.COMPLETION_MISMATCH,
                severity=AlertSeverity.HIGH,
                summary=f"Feature '{m.get('feature_title')}' is marked SHIPPED but linked requirement '{m.get('requirement_text', m.get('requirement_id'))[:80]}' is still OPEN.",
                detail=(
                    f"Feature ID: {m.get('feature_id')}\n"
                    f"Requirement ID: {m.get('requirement_id')}\n"
                    f"This may indicate the feature shipped without fully addressing "
                    f"the original requirement, or the requirement status was not updated."
                ),
                feature_id=m.get("feature_id"),
                rule_id="completion_mismatch_v1",
            ))
        return alerts

    # ─────────────────────────────────────────────────────────────
    # Publishing
    # ─────────────────────────────────────────────────────────────

    async def _is_duplicate_alert(self, alert: MisalignmentAlert) -> bool:
        """
        Returns True if an identical alert (same rule + same feature) was already
        published within the last 24 hours.

        Without this, the MonitorAgent fires a new alert every 5-minute poll cycle
        for the same condition, producing hundreds of duplicate alerts per day.
        Redis SETNX with 24h TTL acts as the deduplication gate.
        """
        if not self._redis:
            return False
        # Unique key per (rule_id, feature_id). Null feature_id uses "global".
        feature_key = alert.feature_id or "global"
        dedup_key = f"aios:alert:dedup:{alert.rule_id}:{feature_key}"
        # SETNX returns 1 if key was newly set, 0 if it already existed
        was_new = await self._redis.setnx(dedup_key, "1")
        if was_new:
            # First time seeing this alert — set 24-hour expiry
            await self._redis.expire(dedup_key, 86400)
            return False  # NOT a duplicate — go ahead and publish
        return True  # Already published within 24h — suppress

    async def _publish_alert(self, alert: MisalignmentAlert) -> None:
        if not self._redis:
            return

        # Deduplication check — suppress if same alert was published within 24h
        if await self._is_duplicate_alert(alert):
            log.debug(
                "Alert suppressed (duplicate within 24h): [%s] feature=%s",
                alert.rule_id,
                alert.feature_id or "global",
            )
            return

        await self._redis.publish(ALERTS_CHANNEL, alert.model_dump_json())
        await self._redis.lpush("aios:alerts:list", alert.model_dump_json())  # Persist for polling
        log.info(
            "Alert published: [%s/%s] %s",
            alert.severity.value,
            alert.alert_type.value,
            alert.summary[:80],
        )
