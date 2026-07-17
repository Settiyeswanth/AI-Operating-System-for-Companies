"""
Agents service — main entry point.

Starts all four agents as concurrent async tasks in one process:

  ┌─────────────────────────────────────────────────────────────┐
  │  AGENTS SERVICE (this file)                                 │
  │                                                             │
  │  QueryAgent     ─── answers on-demand questions             │
  │  MonitorAgent   ─── detects misalignment on a schedule      │
  │  SynthesisAgent ─── produces structured artifacts           │
  │  VerificationAgent─ checks every answer before delivery     │
  │                                                             │
  │  TaskRouter     ─── Redis subscriber that routes            │
  │                      TaskEnvelope messages to the correct   │
  │                      agent and returns results              │
  └─────────────────────────────────────────────────────────────┘

Inter-agent communication rule:
  Agents NEVER call each other directly.
  All communication goes through the Redis task queue.
  TaskRouter is the only component that knows about all four agents.
  Each agent only knows its own inputs and outputs.
"""

from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis

from aios_core.config import settings
from aios_core.logging import configure_logging
from aios_core.schemas.tasks import (
    TaskEnvelope,
    TaskType,
    VerificationVerdict,
    VerdictStatus,
)

# ── Import all four agents ──────────────────────────────────────
from agents.query_agent.agent import get_query_agent, QueryAgentState
from agents.monitor_agent.agent import MonitorAgent
from agents.synthesis_agent.agent import get_synthesis_agent
from agents.verification_agent.agent import get_verification_agent

configure_logging(settings.log_level, "agents")
log = logging.getLogger(__name__)

# Redis channels
TASK_CHANNEL = "aios:tasks"          # Inbound tasks for this service
RESULT_CHANNEL = "aios:task:results" # Outbound results back to interface
AUDIT_CHANNEL = "aios:audit"         # FAIL verdicts and errors


# ─────────────────────────────────────────────────────────────────
# Task Router — connects all four agents via the queue
# ─────────────────────────────────────────────────────────────────

class TaskRouter:
    """
    Subscribes to the task queue channel.
    Routes each TaskEnvelope to the correct agent.
    Publishes results back to the result channel.

    Routing table:
      QUERY         → QueryAgent
      SYNTHESIS     → SynthesisAgent
      VERIFY        → VerificationAgent
      MONITOR_CHECK → MonitorAgent (manual trigger)
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._query_agent = None
        self._synthesis_agent = get_synthesis_agent()
        self._verification_agent = get_verification_agent()

    async def start(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self._query_agent = get_query_agent()
        log.info("TaskRouter ready — all four agents loaded")
        await self._listen()

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(TASK_CHANNEL)
        log.info("TaskRouter subscribed to %s", TASK_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            asyncio.create_task(self._handle_message(message["data"]))

    async def _handle_message(self, raw: str) -> None:
        """Process one task envelope. Runs as a concurrent task."""
        try:
            data = json.loads(raw)
            envelope = TaskEnvelope.model_validate(data)
        except Exception as e:
            log.error("TaskRouter: failed to parse envelope: %s", e)
            return

        log.info(
            "TaskRouter: routing %s task %s from %s",
            envelope.task_type.value,
            envelope.task_id[:8],
            envelope.originator,
        )

        try:
            result = await self._dispatch(envelope)
            await self._publish_result(envelope, result)
        except Exception as e:
            log.error("TaskRouter: dispatch failed for %s: %s", envelope.task_id[:8], e)
            await self._publish_error(envelope, str(e))

    async def _dispatch(self, envelope: TaskEnvelope) -> dict:
        match envelope.task_type:

            case TaskType.QUERY:
                # Route to QueryAgent (LangGraph)
                from aios_core.schemas.tasks import TaskContext
                state: QueryAgentState = {
                    "query": envelope.payload.get("query", ""),
                    "task_context": envelope.context,
                    "sub_queries": [],
                    "retrieved_chunks": [],
                    "graph_results": [],
                    "context_bundle": None,
                    "draft_answer": None,
                    "verification_verdict": None,
                    "final_answer": None,
                    "sources": [],
                    "error": None,
                }
                final = await self._query_agent.ainvoke(state)
                verdict = final.get("verification_verdict")
                if verdict and verdict.verdict == VerdictStatus.FAIL:
                    await self._send_to_audit(envelope, final)
                return {
                    "task_id": envelope.task_id,
                    "status": "complete",
                    "answer": final.get("final_answer") or final.get("draft_answer", ""),
                    "sources": final.get("sources", []),
                    "verdict": verdict.verdict.value if verdict else "uncertain",
                }

            case TaskType.SYNTHESIS:
                # Route to SynthesisAgent
                bundle_data = envelope.payload.get("context_bundle")
                if not bundle_data:
                    return {"task_id": envelope.task_id, "status": "error", "error": "No context_bundle in payload"}
                from aios_core.schemas.tasks import ContextBundle
                bundle = ContextBundle.model_validate(bundle_data)
                output_format = envelope.payload.get("output_format", "default")
                artifact = await self._synthesis_agent.synthesize(bundle, output_format)
                return {
                    "task_id": envelope.task_id,
                    "status": "complete",
                    "artifact": artifact,
                    "output_format": output_format,
                }

            case TaskType.VERIFY:
                # Route to VerificationAgent
                draft = envelope.payload.get("draft_answer", "")
                bundle_data = envelope.payload.get("context_bundle")
                if not bundle_data:
                    return {"task_id": envelope.task_id, "status": "error", "error": "No context_bundle in payload"}
                from aios_core.schemas.tasks import ContextBundle
                bundle = ContextBundle.model_validate(bundle_data)
                verdict: VerificationVerdict = await self._verification_agent.verify(draft, bundle)
                if verdict.verdict == VerdictStatus.FAIL:
                    await self._send_to_audit(envelope, {"verdict": verdict.model_dump()})
                return {
                    "task_id": envelope.task_id,
                    "status": "complete",
                    "verdict": verdict.verdict.value,
                    "claim_annotations": [a.model_dump() for a in verdict.claim_annotations],
                    "reasoning": verdict.reasoning,
                }

            case TaskType.MONITOR_CHECK:
                # Manual trigger for MonitorAgent (normally runs on schedule)
                return {
                    "task_id": envelope.task_id,
                    "status": "acknowledged",
                    "message": "MonitorAgent runs on its own schedule. Use the alerts API to view results.",
                }

            case _:
                return {
                    "task_id": envelope.task_id,
                    "status": "error",
                    "error": f"Unknown task type: {envelope.task_type}",
                }

    async def _publish_result(self, envelope: TaskEnvelope, result: dict) -> None:
        result_key = f"aios:task:result:{envelope.task_id}"
        await self._redis.setex(result_key, 300, json.dumps(result))  # TTL 5 min
        await self._redis.publish(RESULT_CHANNEL, json.dumps(result))

    async def _publish_error(self, envelope: TaskEnvelope, error: str) -> None:
        await self._publish_result(envelope, {
            "task_id": envelope.task_id,
            "status": "error",
            "error": error,
        })

    async def _send_to_audit(self, envelope: TaskEnvelope, data: dict) -> None:
        """Route FAIL verdicts to the audit channel for human review."""
        audit_entry = {
            "task_id": envelope.task_id,
            "task_type": envelope.task_type.value,
            "originator": envelope.originator,
            "user_identity": envelope.context.user_identity,
            "data": data,
        }
        await self._redis.lpush("aios:audit:queue", json.dumps(audit_entry))
        await self._redis.publish(AUDIT_CHANNEL, json.dumps(audit_entry))
        log.warning(
            "FAIL verdict sent to audit queue — task %s from user %s",
            envelope.task_id[:8],
            envelope.context.user_identity,
        )


# ─────────────────────────────────────────────────────────────────
# Main — starts all four agents as concurrent async tasks
# ─────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("=" * 60)
    log.info("AIOS Agents Service starting")
    log.info("  → QueryAgent      (on-demand query answering)")
    log.info("  → MonitorAgent    (scheduled misalignment detection)")
    log.info("  → SynthesisAgent  (multi-source artifact generation)")
    log.info("  → VerificationAgent (answer faithfulness checking)")
    log.info("  → TaskRouter      (inter-agent communication hub)")
    log.info("=" * 60)

    # Connect memory clients once — shared across all agents in this process
    try:
        from memory.graph.client import get_graph_client
        from memory.vector.client import get_vector_client
        graph = get_graph_client()
        await graph.connect()
        vector = get_vector_client()
        await vector.connect()
        log.info("Memory clients connected (Neo4j + Qdrant)")
    except Exception as e:
        log.warning("Memory client startup warning: %s — agents will retry on first use", e)

    # Start MonitorAgent (has its own APScheduler)
    monitor = MonitorAgent()
    await monitor.start()

    # Start TaskRouter (subscribes to Redis task queue)
    router = TaskRouter()

    # Run both concurrently — neither blocks the other
    try:
        await asyncio.gather(
            router.start(),
            _keepalive(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Agents service shutting down...")
        await monitor.stop()


async def _keepalive() -> None:
    """Keeps the event loop alive. TaskRouter and MonitorAgent run as background tasks."""
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
