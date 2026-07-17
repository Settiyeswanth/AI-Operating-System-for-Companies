"""
Logging configuration for all AIOS services.

Call configure_logging() at the top of every service's main.py.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO", service_name: str = "aios") -> None:
    """Configure structured JSON-compatible logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "neo4j", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger(service_name)
    log.info("Logging configured", extra={"level": level, "service": service_name})
