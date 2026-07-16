"""
Gateway Service — API authentication, rate limiting, and reverse proxy.

All external traffic enters through port 8000.
Authenticated requests are forwarded to the interface service (port 8001).

Phase 1:
  - Bearer token auth (any non-empty token accepted)
  - Simple Redis-based rate limiting (60 requests/minute per token)
  - Reverse proxy to interface service via httpx

Phase 2:
  - Replace auth with OAuth2 + RBAC via Keycloak
  - Replace rate limiter with Kong/Traefik
"""

from __future__ import annotations

import logging
import time

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from aios_core.config import settings
from aios_core.logging import configure_logging

configure_logging(settings.log_level, "gateway")
log = logging.getLogger(__name__)

app = FastAPI(title="AIOS Gateway", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Interface service URL — all /v1/* requests are forwarded here
# Reads from settings so it works with run-local.ps1 (localhost) and any other deployment
INTERFACE_URL = settings.interface_service_url


# Rate limit: max requests per minute per API key
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ─────────────────────────────────────────────────────────────────
# Auth helper
# ─────────────────────────────────────────────────────────────────

async def authenticate(request: Request) -> str:
    """
    Phase 1: Extract Bearer token. Any non-empty token is accepted.
    Returns the token (used as user identity downstream).
    Phase 2: validate JWT, extract claims, enforce RBAC.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header. Use: Bearer <token>")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")
    return token


# ─────────────────────────────────────────────────────────────────
# Rate limiter
# ─────────────────────────────────────────────────────────────────

async def check_rate_limit(token: str, redis: aioredis.Redis) -> None:
    """
    Sliding-window rate limit: max RATE_LIMIT requests per RATE_WINDOW_SECONDS.
    Uses a Redis counter with expiry. Raises 429 if limit exceeded.
    """
    key = f"aios:ratelimit:{token}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, RATE_WINDOW_SECONDS)
    if count > RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {RATE_LIMIT} requests per minute.",
        )


# ─────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway"}


# ─────────────────────────────────────────────────────────────────
# Reverse proxy — forward all /v1/* and /webhooks/* to interface/connectors
# ─────────────────────────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(request: Request, path: str):
    """
    Authenticate, rate-limit, then forward the request to the interface service.
    Webhook endpoints (/webhooks/*) are forwarded to the connectors service.
    """
    # Skip auth for health checks and Slack URL verification
    if path in ("health", "favicon.ico"):
        return {"status": "ok"}

    token = await authenticate(request)
    redis = await get_redis()
    await check_rate_limit(token, redis)

    # Determine upstream target
    if path.startswith("webhooks/"):
        upstream = settings.connectors_service_url
    else:
        upstream = INTERFACE_URL

    url = f"{upstream}/{path}"
    method = request.method
    headers = dict(request.headers)
    # Forward the original Authorization header
    headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers={k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")},
                content=body,
                params=dict(request.query_params),
            )
        # Stream the response back
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            media_type=resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        log.error("Gateway: cannot reach upstream %s", upstream)
        raise HTTPException(status_code=503, detail="Upstream service unavailable. Is it running?")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream service timed out.")


# ─────────────────────────────────────────────────────────────────
# Startup / Shutdown
# ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    log.info("Gateway service starting on port 8000...")
    log.info("Forwarding /v1/*        → %s", settings.interface_service_url)
    log.info("Forwarding /webhooks/*  → %s", settings.connectors_service_url)


@app.on_event("shutdown")
async def shutdown():
    global _redis
    if _redis:
        await _redis.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8000, reload=True)
