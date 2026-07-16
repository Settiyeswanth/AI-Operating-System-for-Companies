"""
connectors/main.py — Entry point for the connectors service.

Runs two things concurrently:
  1. APScheduler polling loop (every 5 min) for GitHub + Linear + Slack
  2. FastAPI webhook receiver for incoming real-time events
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
import uvicorn

from aios_core.config import settings
from aios_core.logging import configure_logging

from connectors.github.connector import GitHubConnector
from connectors.linear.connector import LinearConnector
from connectors.slack.connector import SlackConnector

configure_logging(settings.log_level, "connectors")
log = logging.getLogger(__name__)

# FastAPI app for webhook endpoints
app = FastAPI(title="AIOS Connectors", version="0.1.0")

# Connector instances
github = GitHubConnector()
linear = LinearConnector()
slack = SlackConnector()

# Track last successful poll time per connector
_last_poll: dict[str, datetime] = {}


async def poll_all_connectors() -> None:
    """Run all connector polls. Called by APScheduler."""
    lookback = timedelta(minutes=settings.monitor_poll_interval_minutes + 2)  # Overlap

    for name, connector in [("github", github), ("linear", linear), ("slack", slack)]:
        since = _last_poll.get(name, datetime.utcnow() - lookback)
        try:
            count = await connector.poll_and_publish(since)
            _last_poll[name] = datetime.utcnow()
            log.info("Poll complete: %s — %d events published", name, count)
        except Exception as e:
            log.error("Poll failed for %s: %s", name, e)


# ─────────────────────────────────────────────────────────────────
# Webhook endpoints
# ─────────────────────────────────────────────────────────────────

@app.post("/webhooks/github")
async def github_webhook(request: Request):
    payload = await request.body()
    headers = dict(request.headers)
    events = await github.handle_webhook(payload, headers)
    for event in events:
        normalized = github.map_to_normalized(event)
        if normalized:
            await github.publish_event(normalized)
    return Response(status_code=200)


@app.post("/webhooks/linear")
async def linear_webhook(request: Request):
    payload = await request.body()
    headers = dict(request.headers)
    events = await linear.handle_webhook(payload, headers)
    for event in events:
        normalized = linear.map_to_normalized(event)
        if normalized:
            await linear.publish_event(normalized)
    return Response(status_code=200)


@app.post("/webhooks/slack")
async def slack_webhook(request: Request):
    payload = await request.body()
    data = __import__("json").loads(payload)

    # Handle Slack URL verification challenge
    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    headers = dict(request.headers)
    events = await slack.handle_webhook(payload, headers)
    for event in events:
        normalized = slack.map_to_normalized(event)
        if normalized:
            await slack.publish_event(normalized)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "last_poll": {k: v.isoformat() for k, v in _last_poll.items()},
    }


# ─────────────────────────────────────────────────────────────────
# Startup / Shutdown
# ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    log.info("Connectors service starting...")

    # Connect Redis
    for connector in [github, linear, slack]:
        await connector.connect_redis()

    # Start polling scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_all_connectors,
        "interval",
        minutes=settings.monitor_poll_interval_minutes,
        id="poll_all",
        next_run_time=datetime.now(),
    )
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("Polling scheduler started (interval: %d min)", settings.monitor_poll_interval_minutes)


@app.on_event("shutdown")
async def shutdown():
    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown()
    for connector in [github, linear, slack]:
        await connector.close()


if __name__ == "__main__":
    uvicorn.run("connectors.main:app", host="0.0.0.0", port=8010, reload=True)
