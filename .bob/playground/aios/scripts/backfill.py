#!/usr/bin/env python3
"""
Historical data backfill script.

Polls all configured connectors for historical events and publishes them
to the ingestion pipeline. Run this once after first boot to seed the
Knowledge Graph with recent activity.

Usage (inside connectors container):
    python scripts/backfill.py --days 7

Usage (local, with PYTHONPATH set):
    cd aios && python scripts/backfill.py --days 7

What it does:
  1. Instantiates GitHubConnector, LinearConnector, SlackConnector
  2. Connects each connector to Redis
  3. Calls poll_and_publish(since=now - N days) for each
  4. Prints a summary of events published

After this runs, the ingestion service will pick up the events from
Redis and process them through entity resolution → enrichment → graph.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running directly with: python scripts/backfill.py
# Adds the service directories to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "aios-core"))
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "connectors"))
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "memory"))

from aios_core.config import settings
from aios_core.logging import configure_logging
from connectors.github.connector import GitHubConnector
from connectors.linear.connector import LinearConnector
from connectors.slack.connector import SlackConnector

configure_logging(settings.log_level, "backfill")
log = logging.getLogger("backfill")


async def run_backfill(days: int) -> None:
    since = datetime.utcnow() - timedelta(days=days)
    log.info("=" * 60)
    log.info("AIOS Backfill — ingesting last %d days of data", days)
    log.info("Since: %s UTC", since.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    connectors = [
        ("GitHub", GitHubConnector()),
        ("Linear", LinearConnector()),
        ("Slack",  SlackConnector()),
    ]

    total = 0
    results: list[tuple[str, int, str]] = []

    for name, connector in connectors:
        try:
            await connector.connect_redis()
            log.info("Running %s backfill...", name)
            count = await connector.poll_and_publish(since)
            results.append((name, count, "ok"))
            total += count
            log.info("%s: %d events published", name, count)
        except Exception as e:
            results.append((name, 0, str(e)))
            log.error("%s backfill failed: %s", name, e)
        finally:
            try:
                await connector.close()
            except Exception:
                pass

    log.info("=" * 60)
    log.info("Backfill complete — %d total events published", total)
    log.info("")
    log.info("Results:")
    for name, count, status in results:
        icon = "✓" if status == "ok" else "✗"
        log.info("  %s %-10s %3d events  %s", icon, name, count, "" if status == "ok" else f"({status})")
    log.info("")
    log.info("Next: watch the ingestion service logs to see events being processed:")
    log.info("  docker compose logs -f ingestion enrichment")
    log.info("")
    log.info("Then check the graph:")
    log.info("  Open http://localhost:7474 (neo4j / password)")
    log.info("  Run: MATCH (n) RETURN n LIMIT 50")
    log.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historical data from GitHub, Linear, and Slack."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    args = parser.parse_args()

    if args.days < 1 or args.days > 90:
        print("ERROR: --days must be between 1 and 90")
        sys.exit(1)

    asyncio.run(run_backfill(args.days))


if __name__ == "__main__":
    main()
