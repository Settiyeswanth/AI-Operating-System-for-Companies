"""
SynthesisAgent — Produces structured multi-source artifacts.

Called by QueryAgent (complex queries) and MonitorAgent (alert reports).
Never initiates work independently — always receives a synthesis task spec.

Outputs are always forwarded to VerificationAgent before delivery.

Structural rule: SynthesisAgent MUST NOT be merged with VerificationAgent.
The verifier must have no stake in the output it is checking.
"""

from __future__ import annotations
 
import json
import logging
from typing import Any

from aios_core.config import settings
from aios_core.llm_gateway import get_llm_gateway, LLMMessage
from aios_core.schemas.tasks import (
    ContextBundle,
    VerificationVerdict,
    VerdictStatus,
)

log = logging.getLogger(__name__)


# Output format templates — SynthesisAgent selects based on task type
TEMPLATES = {
    "decision_summary": """
Summarize the following organizational decisions clearly and concisely.
For each decision include: what was decided, why, and who made it.
Use bullet points. Cite [Source N] for each claim.

Sources:
{sources}

Question / Context: {query}
""",
    "incident_timeline": """
Produce a clear incident timeline from the following sources.
Include: detection, impact, response steps, resolution, and root cause if known.
Cite [Source N] for each event.

Sources:
{sources}

Incident context: {query}
""",
    "spec_draft": """
Draft a concise engineering specification based on the following organizational context.
Include: background, goal, scope, key decisions already made, and open questions.
Cite [Source N] for each factual claim.

Sources:
{sources}

Spec topic: {query}
""",
    "default": """
Synthesize the following organizational information into a clear, structured answer.
Cite [Source N] for every factual claim. Do not invent facts.

Sources:
{sources}

Task: {query}
""",
}


class SynthesisAgent:
    """
    Produces structured artifacts from a ContextBundle.
    Stateless — create a new instance per task or reuse as a singleton.
    """

    async def synthesize(
        self,
        bundle: ContextBundle,
        output_format: str = "default",
    ) -> str:
        """
        Synthesize a structured artifact from the context bundle.
        Returns the raw text artifact (not yet verified).
        Caller must pass this to VerificationAgent before delivery.
        """
        llm = get_llm_gateway()
        template = TEMPLATES.get(output_format, TEMPLATES["default"])

        # Build sources block (same format used in QueryAgent for consistency)
        source_texts: list[str] = []
        for i, chunk in enumerate(bundle.retrieved_chunks[:10]):
            source_texts.append(
                f"[Source {i + 1}] "
                f"({chunk.source_system}, {chunk.timestamp.date()})\n"
                f"{chunk.content}"
            )
        for gr in bundle.graph_context[:3]:
            if gr.summary:
                source_texts.append(f"[Graph: {gr.query_name}]\n{gr.summary}")

        sources_block = "\n\n".join(source_texts) if source_texts else "No sources available."

        prompt = template.format(sources=sources_block, query=bundle.query).strip()

        try:
            response = await llm.complete(
                [LLMMessage(role="user", content=prompt)],
                temperature=0.2,   # Slightly creative for synthesis, still grounded
                max_tokens=2048,
            )
            return response.content.strip()
        except Exception as e:
            log.error("SynthesisAgent failed for query '%s': %s", bundle.query[:60], e)
            return ""


# Module-level singleton
_synthesis_agent: SynthesisAgent | None = None


def get_synthesis_agent() -> SynthesisAgent:
    global _synthesis_agent
    if _synthesis_agent is None:
        _synthesis_agent = SynthesisAgent()
    return _synthesis_agent
