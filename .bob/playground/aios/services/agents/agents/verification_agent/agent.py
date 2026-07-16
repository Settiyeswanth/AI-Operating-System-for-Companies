"""
VerificationAgent — Faithfulness checker.

STRUCTURAL INVARIANT: This agent MUST remain independent from all other agents.
It shares no mutable state with QueryAgent or SynthesisAgent.
It receives only: (draft_answer, context_bundle).
It has no access to the agent that produced the draft.

This separation is what makes the verification meaningful.
Merging synthesis and verification is the single most dangerous
architectural collapse possible in this system.

Verdict semantics:
  PASS      — every factual claim is directly supported by a cited source
  UNCERTAIN — some claims have weak or indirect support only
  FAIL      — any claim is unsupported or contradicted by sources

FAIL blocks delivery. UNCERTAIN delivers with an explicit caveat.
"""

from __future__ import annotations

import json
import logging

from aios_core.config import settings
from aios_core.llm_gateway import get_llm_gateway, LLMMessage
from aios_core.schemas.tasks import (
    ContextBundle,
    ClaimAnnotation,
    VerificationVerdict,
    VerdictStatus,
)

log = logging.getLogger(__name__)

VERIFICATION_PROMPT = """\
You are a strict fact-checker for an AI organizational intelligence system.
You have been given a draft answer and the EXACT source documents used to produce it.

Your task: verify that every factual claim in the answer is directly supported
by one of the provided source documents.

For each identifiable claim, classify it:
  SUPPORTED    — the source directly states or clearly implies this
  UNSUPPORTED  — no source supports this claim (possible hallucination)
  CONTRADICTED — a source explicitly contradicts this claim

Verdict rules:
  PASS      → all claims are SUPPORTED
  UNCERTAIN → at least one claim has weak/indirect support, none contradicted
  FAIL      → any claim is UNSUPPORTED or CONTRADICTED

Draft answer to verify:
{answer}

Source documents:
{sources}

Respond ONLY in valid JSON — no explanation outside the JSON:
{{
  "verdict": "PASS" | "UNCERTAIN" | "FAIL",
  "claim_annotations": [
    {{"claim": "short quote of the claim", "status": "SUPPORTED|UNSUPPORTED|CONTRADICTED", "source_id": "S1 or null"}}
  ],
  "reasoning": "one-sentence explanation of your verdict"
}}"""


class VerificationAgent:
    """
    Checks a draft answer against its source ContextBundle.
    Stateless — safe to use as a singleton.
    """

    async def verify(
        self,
        draft_answer: str,
        bundle: ContextBundle,
    ) -> VerificationVerdict:
        """
        Verify the draft_answer against bundle.retrieved_chunks + bundle.graph_context.
        Returns a VerificationVerdict. Never raises — returns UNCERTAIN on errors.
        """
        if not draft_answer.strip():
            return VerificationVerdict(
                verdict=VerdictStatus.FAIL,
                reasoning="Empty draft answer — nothing to verify.",
                verifier_model=settings.ollama_default_model,
            )

        # Build the sources payload the verifier can cite from
        sources: list[dict] = []
        for i, chunk in enumerate(bundle.retrieved_chunks[:10]):
            sources.append({
                "id": f"S{i + 1}",
                "source_system": chunk.source_system,
                "date": chunk.timestamp.date().isoformat(),
                "content": chunk.content[:800],  # Cap per-chunk to keep prompt bounded
            })
        for gr in bundle.graph_context[:3]:
            if gr.summary:
                sources.append({
                    "id": f"G_{gr.query_name}",
                    "source_system": "knowledge_graph",
                    "content": gr.summary,
                })

        if not sources:
            # No sources at all — answer cannot be grounded
            return VerificationVerdict(
                verdict=VerdictStatus.UNCERTAIN,
                reasoning="No source documents available to verify against.",
                verifier_model=settings.ollama_default_model,
            )

        llm = get_llm_gateway()
        prompt = VERIFICATION_PROMPT.format(
            answer=draft_answer,
            sources=json.dumps(sources, indent=2),
        )

        try:
            response = await llm.complete(
                [LLMMessage(role="user", content=prompt)],
                temperature=0.0,          # Deterministic — verification must be reproducible
                max_tokens=768,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(response.content)

            verdict_str = parsed.get("verdict", "UNCERTAIN").upper()
            try:
                verdict_status = VerdictStatus(verdict_str.lower())
            except ValueError:
                verdict_status = VerdictStatus.UNCERTAIN

            annotations = [
                ClaimAnnotation(
                    claim=a.get("claim", ""),
                    status=a.get("status", "UNSUPPORTED"),
                    source_chunk_id=a.get("source_id"),
                )
                for a in parsed.get("claim_annotations", [])
            ]

            return VerificationVerdict(
                verdict=verdict_status,
                claim_annotations=annotations,
                reasoning=parsed.get("reasoning", ""),
                verifier_model=settings.ollama_default_model,
            )

        except json.JSONDecodeError as e:
            log.warning("VerificationAgent: JSON parse failed (%s), defaulting to UNCERTAIN", e)
            return VerificationVerdict(
                verdict=VerdictStatus.UNCERTAIN,
                reasoning=f"Could not parse verifier response: {e}",
                verifier_model=settings.ollama_default_model,
            )
        except Exception as e:
            log.error("VerificationAgent unexpected error: %s", e)
            return VerificationVerdict(
                verdict=VerdictStatus.UNCERTAIN,
                reasoning=f"Verification error: {e}",
                verifier_model=settings.ollama_default_model,
            )


# Module-level singleton
_verification_agent: VerificationAgent | None = None


def get_verification_agent() -> VerificationAgent:
    global _verification_agent
    if _verification_agent is None:
        _verification_agent = VerificationAgent()
    return _verification_agent
