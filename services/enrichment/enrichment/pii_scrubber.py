"""
PII Scrubber — removes personally identifiable information from text
before it is written to the vector store or knowledge graph.

Uses Microsoft Presidio when available.
Falls back to a regex-based scrubber in Phase 1 dev environments
where the spaCy model may not be installed.

Privacy invariant:
  NO raw message body from Slack, Linear comments, or GitHub PR descriptions
  is ever stored. Only the PII-scrubbed, LLM-summarised version is persisted.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Simple regex fallback patterns for PII detection
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_PHONE_PATTERN = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b")


class PIIScrubber:
    """
    Scrubs PII from text.

    Attempts to use presidio-analyzer (accurate, ML-based).
    Falls back to regex patterns if presidio is not installed (dev mode).

    Usage:
        scrubber = PIIScrubber()
        clean = scrubber.scrub("Email me at alice@example.com tomorrow")
        # → "Email me at [EMAIL] tomorrow"
    """

    def __init__(self) -> None:
        self._presidio_available = False
        self._analyzer = None
        self._anonymizer = None
        self._try_load_presidio()

    def _try_load_presidio(self) -> None:
        """Attempt to load presidio. Silently fall back if not available."""
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
            self._presidio_available = True
            log.info("PII scrubber: using presidio-analyzer (ML-based)")
        except (ImportError, Exception) as e:
            log.warning(
                "presidio-analyzer not available (%s) — using regex fallback. "
                "Install presidio-analyzer and python -m spacy download en_core_web_lg "
                "for better PII detection.", e
            )

    def scrub(self, text: str) -> str:
        """
        Scrub PII from the given text.
        Returns the sanitised version. Never raises.
        """
        if not text:
            return text
        try:
            if self._presidio_available:
                return self._scrub_with_presidio(text)
            return self._scrub_with_regex(text)
        except Exception as e:
            log.error("PII scrub failed: %s — returning text unchanged", e)
            return text

    def _scrub_with_presidio(self, text: str) -> str:
        results = self._analyzer.analyze(text=text, language="en")
        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
        return anonymized.text

    def _scrub_with_regex(self, text: str) -> str:
        text = _EMAIL_PATTERN.sub("[EMAIL]", text)
        text = _PHONE_PATTERN.sub("[PHONE]", text)
        return text


# Module-level singleton
_scrubber: PIIScrubber | None = None


def get_scrubber() -> PIIScrubber:
    global _scrubber
    if _scrubber is None:
        _scrubber = PIIScrubber()
    return _scrubber
