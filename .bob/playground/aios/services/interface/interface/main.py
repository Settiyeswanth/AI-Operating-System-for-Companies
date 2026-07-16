"""
Interface Service — Chat API + Alerts API + Drafts API.

FastAPI application that exposes the AI OS to users.
Three endpoints:
  POST /v1/chat           — conversational query with SSE streaming
  GET  /v1/alerts         — list misalignment alerts
  POST /v1/alerts/{id}/acknowledge
  POST /v1/alerts/{id}/dismiss

Auth: API key via Authorization: Bearer header (Phase 1)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import AsyncIterator

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aios_core.config import settings
from aios_core.logging import configure_logging
from aios_core.schemas.tasks import (
    TaskContext,
    MisalignmentAlert,
    VerdictStatus,
)

configure_logging(settings.log_level, "interface")
log = logging.getLogger(__name__)

app = FastAPI(
    title="AI OS Interface",
    description="Organizational intelligence chat and alert API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in Phase 2
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────
# Redis client (shared)
# ─────────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ─────────────────────────────────────────────────────────────────
# Auth (Phase 1: simple API key)
# ─────────────────────────────────────────────────────────────────

async def verify_api_key(request: Request) -> dict:
    """
    Phase 1: Accept any non-empty Bearer token and treat it as the user identity.
    Phase 2: Replace with real OAuth2 + RBAC.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty API key")
    # Phase 1: token IS the user identity
    return {
        "user_id": token[:64],
        "scopes": ["*"],      # Phase 1: full access
        "grants": [],
    }


# ─────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str
    session_id: str | None = None
    stream: bool = True


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict]
    verdict: str
    confidence: float
    trace_id: str


# ─────────────────────────────────────────────────────────────────
# Chat endpoint
# ─────────────────────────────────────────────────────────────────

@app.post("/v1/chat")
async def chat(
    request: ChatRequest,
    auth: dict = Depends(verify_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Main conversational query endpoint.
    Supports streaming (SSE) and non-streaming responses.
    """
    session_id = request.session_id or str(uuid.uuid4())
    trace_id = str(uuid.uuid4())

    ctx = TaskContext(
        user_identity=auth["user_id"],
        access_scopes=auth["scopes"],
        user_grants=auth["grants"],
        session_id=session_id,
        trace_id=trace_id,
    )

    if request.stream:
        return StreamingResponse(
            _stream_response(request.query, ctx),
            media_type="text/event-stream",
        )

    # Non-streaming path
    result = await _run_query_agent(request.query, ctx)
    return ChatResponse(**result)


async def _stream_response(query: str, ctx: TaskContext) -> AsyncIterator[str]:
    """Yield SSE events for a streaming chat response."""

    def sse(event_type: str, data: dict) -> str:
        return f"data: {json.dumps({'type': event_type, **data})}\n\n"

    yield sse("thinking", {"content": "Retrieving organizational context..."})

    try:
        # Run the query agent
        result = await _run_query_agent(query, ctx)

        answer = result.get("final_answer", "")
        sources = result.get("sources", [])
        verdict = result.get("verdict", "uncertain")

        if not answer:
            yield sse("error", {"content": "I could not find reliable information to answer that question."})
            return

        # Stream the answer word by word (simulated streaming for Ollama non-streaming mode)
        words = answer.split(" ")
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield sse("chunk", {"content": chunk})
            if i % 10 == 0:
                await asyncio.sleep(0)  # yield control

        yield sse("sources", {"sources": sources})
        yield sse("done", {
            "verdict": verdict,
            "confidence": result.get("confidence", 0.0),
            "trace_id": ctx.trace_id,
        })

    except Exception as e:
        log.error("Stream error for query '%s': %s", query[:50], e)
        yield sse("error", {"content": "An error occurred processing your query. Please try again."})


async def _run_query_agent(query: str, ctx: TaskContext) -> dict:
    """Run the QueryAgent and return a structured result dict."""
    from agents.query_agent.agent import get_query_agent

    agent = get_query_agent()

    initial_state = {
        "query": query,
        "task_context": ctx,
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

    final_state = await agent.ainvoke(initial_state)

    verdict = final_state.get("verification_verdict")
    verdict_str = verdict.verdict.value if verdict else "uncertain"

    return {
        "final_answer": final_state.get("final_answer") or final_state.get("draft_answer", ""),
        "sources": final_state.get("sources", []),
        "verdict": verdict_str,
        "confidence": 0.9 if verdict_str == "pass" else 0.6 if verdict_str == "uncertain" else 0.0,
        "trace_id": ctx.trace_id,
    }


# ─────────────────────────────────────────────────────────────────
# Alerts endpoints
# ─────────────────────────────────────────────────────────────────

@app.get("/v1/alerts")
async def list_alerts(
    since: str | None = None,
    severity: str | None = None,
    limit: int = 20,
    auth: dict = Depends(verify_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    """List recent misalignment alerts."""
    raw_alerts = await redis.lrange("aios:alerts:list", 0, limit - 1)
    alerts = []
    for raw in raw_alerts:
        try:
            alert_data = json.loads(raw)
            if severity and alert_data.get("severity") != severity:
                continue
            alerts.append(alert_data)
        except Exception:
            continue
    return {"alerts": alerts, "total": len(alerts)}


@app.post("/v1/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    auth: dict = Depends(verify_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Mark an alert as acknowledged."""
    # Phase 1: store acknowledgment in Redis hash
    await redis.hset(f"aios:alert:ack:{alert_id}", mapping={
        "acknowledged_by": auth["user_id"],
        "acknowledged_at": datetime.utcnow().isoformat(),
    })
    return {"status": "acknowledged", "alert_id": alert_id}


@app.post("/v1/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    alert_id: str,
    auth: dict = Depends(verify_api_key),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Dismiss an alert (marks it as not actionable)."""
    await redis.hset(f"aios:alert:dismiss:{alert_id}", mapping={
        "dismissed_by": auth["user_id"],
        "dismissed_at": datetime.utcnow().isoformat(),
    })
    return {"status": "dismissed", "alert_id": alert_id}


# ─────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "interface", "timestamp": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    log.info("Interface service starting...")
    # Connect memory clients on startup
    try:
        from memory.graph.client import get_graph_client
        from memory.vector.client import get_vector_client

        graph = get_graph_client()
        await graph.connect()

        vector = get_vector_client()
        await vector.connect()

        log.info("Memory clients connected")
    except Exception as e:
        log.warning("Memory client startup warning: %s", e)


@app.on_event("shutdown")
async def shutdown():
    global _redis
    if _redis:
        await _redis.aclose()
