"""
QueryAgent — LangGraph state machine.

Handles on-demand organizational questions.
Flow: decompose → parallel retrieve → RRF fuse → generate → verify → format

Architecture note: The ContextBundle is created at the 'fuse' node and
passed UNCHANGED through the remainder of the chain. VerificationAgent
receives exactly what the GenerateAnswer node used — no more, no less.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any, AsyncIterator, TypedDict
import operator

from langgraph.graph import StateGraph, END

from aios_core.config import settings
from aios_core.llm_gateway import get_llm_gateway, LLMMessage
from aios_core.schemas.tasks import (
    ContextBundle,
    RetrievalMetadata,
    RetrievedChunk,
    GraphResult,
    TaskContext,
    VerificationVerdict,
    VerdictStatus,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────

class QueryAgentState(TypedDict):
    query: str
    task_context: TaskContext

    # Retrieval
    sub_queries: list[str]
    retrieved_chunks: list[RetrievedChunk]
    graph_results: list[GraphResult]
    context_bundle: ContextBundle | None

    # Generation
    draft_answer: str | None

    # Verification
    verification_verdict: VerificationVerdict | None

    # Output
    final_answer: str | None
    sources: list[dict]
    error: str | None


# ─────────────────────────────────────────────────────────────────
# Node functions
# ─────────────────────────────────────────────────────────────────

async def decompose_query_node(state: QueryAgentState) -> dict:
    """
    Decompose the user's query into sub-queries for parallel retrieval.
    Simple queries return as-is. Complex queries are broken into 2–3 sub-queries.
    """
    query = state["query"]
    llm = get_llm_gateway()

    DECOMPOSE_PROMPT = """You are a query decomposer for an organizational intelligence system.
Given a user's question, decompose it into 1-3 specific retrieval queries.
If the question is simple and self-contained, return just the original question.
If it's complex, split it into simpler sub-queries that together answer the full question.

Question: {query}

Respond in JSON: {{"sub_queries": ["query1", "query2", ...]}}
Keep sub-queries short and specific. Maximum 3 sub-queries."""

    try:
        response = await llm.complete(
            [LLMMessage(role="user", content=DECOMPOSE_PROMPT.format(query=query))],
            temperature=0.0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.content)
        sub_queries = parsed.get("sub_queries", [query])
        if not sub_queries:
            sub_queries = [query]
    except Exception as e:
        log.warning("Query decomposition failed (%s), using original query", e)
        sub_queries = [query]

    return {"sub_queries": sub_queries}


async def retrieve_vector_node(state: QueryAgentState) -> dict:
    """Dense + sparse retrieval from the Vector Store."""
    from memory.vector.client import get_vector_client

    ctx = state["task_context"]
    llm = get_llm_gateway()
    vector_client = get_vector_client()

    # Embed the original query (not sub-queries, for vector search)
    try:
        embeddings = await llm.embed([state["query"]])
        query_vector = embeddings[0]
    except Exception as e:
        log.error("Embedding failed: %s", e)
        return {"retrieved_chunks": []}

    try:
        chunks = await vector_client.hybrid_search(
            query_text=state["query"],
            query_vector=query_vector,
            user_scopes=ctx.access_scopes,
            user_id=ctx.user_identity,
            user_grants=ctx.user_grants,
            top_k=10,
        )
    except Exception as e:
        log.error("Vector search failed: %s", e)
        chunks = []

    return {"retrieved_chunks": chunks}


async def retrieve_graph_node(state: QueryAgentState) -> dict:
    """Graph traversal queries based on entities mentioned in the query."""
    from memory.graph.client import get_graph_client

    graph = get_graph_client()
    ctx = state["task_context"]
    results: list[GraphResult] = []

    query_lower = state["query"].lower()

    try:
        # Heuristic entity detection — Phase 2: use NER
        if any(kw in query_lower for kw in ["who", "author", "wrote", "worked", "built"]):
            # Try to extract a feature reference from sub-queries
            feature_mentions = _extract_entity_mentions(state["sub_queries"])
            for mention in feature_mentions[:3]:
                authors = await graph.find_feature_authors(mention)
                if authors:
                    results.append(GraphResult(
                        query_name="feature_authors",
                        nodes=authors,
                        summary=f"Authors of {mention}: {', '.join(a.get('names', [['?']])[0] if a.get('names') else ['?'] for a in authors)}",
                    ))

        if any(kw in query_lower for kw in ["decision", "decided", "why", "rationale", "chose"]):
            recent_decisions = await graph.run_raw(
                "MATCH (d:Decision) RETURN d ORDER BY d.made_at DESC LIMIT 5"
            )
            if recent_decisions:
                results.append(GraphResult(
                    query_name="recent_decisions",
                    nodes=recent_decisions,
                    summary=f"Found {len(recent_decisions)} recent decisions",
                ))

        if any(kw in query_lower for kw in ["incident", "outage", "bug", "broken", "down"]):
            recent_incidents = await graph.run_raw(
                "MATCH (i:Incident) RETURN i ORDER BY i.created_at DESC LIMIT 5"
            )
            if recent_incidents:
                results.append(GraphResult(
                    query_name="recent_incidents",
                    nodes=recent_incidents,
                    summary=f"Found {len(recent_incidents)} recent incidents",
                ))

    except Exception as e:
        log.error("Graph retrieval failed: %s", e)

    return {"graph_results": results}


async def fuse_results_node(state: QueryAgentState) -> dict:
    """
    Create the immutable ContextBundle from retrieval results.
    This bundle is passed unchanged to GenerateAnswer and VerificationAgent.
    """
    ctx = state["task_context"]
    chunks = state.get("retrieved_chunks", [])
    graph = state.get("graph_results", [])

    bundle = ContextBundle(
        query=state["query"],
        retrieved_chunks=chunks,
        graph_context=graph,
        retrieval_metadata=RetrievalMetadata(
            query=state["query"],
            sub_queries=state.get("sub_queries", []),
            vector_retrieved=len(chunks),
            graph_retrieved=len(graph),
            total_after_fusion=len(chunks),
        ),
        access_scopes=ctx.access_scopes,
    )

    return {"context_bundle": bundle}


async def generate_answer_node(state: QueryAgentState) -> dict:
    """Generate an answer from the context bundle."""
    bundle = state["context_bundle"]
    if not bundle:
        return {"draft_answer": None, "error": "No context bundle"}

    llm = get_llm_gateway()

    # Format sources for the prompt
    source_texts = []
    for i, chunk in enumerate(bundle.retrieved_chunks[:8]):  # Cap at 8 chunks
        source_texts.append(
            f"[Source {i+1}] ({chunk.source_system}, {chunk.timestamp.date()})\n{chunk.content}"
        )
    for gr in bundle.graph_context[:3]:
        if gr.summary:
            source_texts.append(f"[Graph: {gr.query_name}]\n{gr.summary}")

    sources_block = "\n\n".join(source_texts) if source_texts else "No relevant context found."

    ANSWER_PROMPT = f"""You are an AI assistant with access to your organization's knowledge base.
Answer the following question using ONLY the provided sources.
If the sources don't contain enough information, say so clearly — do NOT invent facts.
Cite sources by their number [Source N] when making factual claims.

Question: {bundle.query}

Sources:
{sources_block}

Answer concisely and accurately. If uncertain, say so."""

    try:
        response = await llm.complete(
            [LLMMessage(role="user", content=ANSWER_PROMPT)],
            temperature=0.1,
            max_tokens=1024,
        )
        draft = response.content.strip()
    except Exception as e:
        log.error("Answer generation failed: %s", e)
        draft = None

    return {"draft_answer": draft}


async def verify_answer_node(state: QueryAgentState) -> dict:
    """
    VerificationAgent call — checks draft answer against the ContextBundle.
    This node is the safety gate between generation and delivery.
    """
    draft = state.get("draft_answer")
    bundle = state.get("context_bundle")

    if not draft or not bundle:
        return {"verification_verdict": VerificationVerdict(
            verdict=VerdictStatus.FAIL,
            reasoning="No draft answer or context bundle to verify.",
        )}

    llm = get_llm_gateway()

    # Format sources for verification (same chunks the generator used)
    sources_for_verification = []
    for i, chunk in enumerate(bundle.retrieved_chunks[:8]):
        sources_for_verification.append({
            "id": f"S{i+1}",
            "chunk_id": chunk.chunk_id,
            "content": chunk.content,
        })
    for gr in bundle.graph_context[:3]:
        sources_for_verification.append({
            "id": f"G_{gr.query_name}",
            "content": gr.summary,
        })

    VERIFY_PROMPT = f"""You are a strict fact-checker for an AI system.
You will be given an answer and the exact source documents used to produce it.
Your job: verify that every factual claim in the answer is directly supported by
one of the provided sources.

For each claim:
  SUPPORTED = source directly states or clearly implies this
  UNSUPPORTED = no source supports this claim
  CONTRADICTED = a source contradicts this claim

Answer to verify:
{draft}

Source documents:
{json.dumps(sources_for_verification, indent=2)}

Respond ONLY in JSON:
{{
  "verdict": "PASS" or "UNCERTAIN" or "FAIL",
  "claim_annotations": [
    {{"claim": "...", "status": "SUPPORTED|UNSUPPORTED|CONTRADICTED", "source_id": "S1 or null"}}
  ],
  "reasoning": "brief explanation"
}}

PASS = all claims supported
UNCERTAIN = some claims have weak/indirect support only
FAIL = any claim is UNSUPPORTED or CONTRADICTED"""

    try:
        response = await llm.complete(
            [LLMMessage(role="user", content=VERIFY_PROMPT)],
            temperature=0.0,   # Deterministic verification
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.content)
        verdict = VerificationVerdict(
            verdict=VerdictStatus(parsed.get("verdict", "UNCERTAIN").lower()),
            reasoning=parsed.get("reasoning", ""),
            verifier_model=settings.ollama_default_model,
        )
    except Exception as e:
        log.error("Verification failed: %s", e)
        verdict = VerificationVerdict(
            verdict=VerdictStatus.UNCERTAIN,
            reasoning=f"Verification error: {e}",
        )

    return {"verification_verdict": verdict}


async def format_response_node(state: QueryAgentState) -> dict:
    """Assemble the final response with sources and uncertainty caveats."""
    verdict = state.get("verification_verdict")
    draft = state.get("draft_answer", "")
    bundle = state.get("context_bundle")

    # Add uncertainty caveat if needed
    final_answer = draft or ""
    if verdict and verdict.verdict == VerdictStatus.UNCERTAIN:
        final_answer = (
            f"{final_answer}\n\n"
            "⚠️ *Note: Some information in this answer has limited source support. "
            "Please verify with the cited sources before acting on it.*"
        )

    # Build sources list
    sources = []
    if bundle:
        seen = set()
        for chunk in bundle.retrieved_chunks[:8]:
            if chunk.source_artifact_id not in seen:
                sources.append({
                    "chunk_id": chunk.chunk_id,
                    "artifact_id": chunk.source_artifact_id,
                    "source_system": chunk.source_system,
                    "source_url": chunk.source_url,
                    "timestamp": chunk.timestamp.isoformat(),
                })
                seen.add(chunk.source_artifact_id)

    return {"final_answer": final_answer, "sources": sources}


def route_on_verdict(state: QueryAgentState) -> str:
    verdict = state.get("verification_verdict")
    if verdict and verdict.verdict == VerdictStatus.FAIL:
        return "fail"
    return "deliver"


# ─────────────────────────────────────────────────────────────────
# Graph assembly
# ─────────────────────────────────────────────────────────────────

def build_query_agent():
    graph = StateGraph(QueryAgentState)

    graph.add_node("decompose",      decompose_query_node)
    graph.add_node("vector_search",  retrieve_vector_node)
    graph.add_node("graph_search",   retrieve_graph_node)
    graph.add_node("fuse",           fuse_results_node)
    graph.add_node("generate",       generate_answer_node)
    graph.add_node("verify",         verify_answer_node)
    graph.add_node("format",         format_response_node)

    graph.set_entry_point("decompose")
    graph.add_edge("decompose",     "vector_search")
    graph.add_edge("decompose",     "graph_search")
    graph.add_edge("vector_search", "fuse")
    graph.add_edge("graph_search",  "fuse")
    graph.add_edge("fuse",          "generate")
    graph.add_edge("generate",      "verify")
    graph.add_conditional_edges(
        "verify",
        route_on_verdict,
        {"deliver": "format", "fail": END},
    )
    graph.add_edge("format", END)

    return graph.compile()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _extract_entity_mentions(texts: list[str]) -> list[str]:
    """Naive entity mention extractor. Phase 2: replace with NER."""
    mentions = []
    for text in texts:
        words = text.split()
        for i, word in enumerate(words):
            if word.isupper() and len(word) > 2:
                mentions.append(word)
            if "-" in word and any(c.isdigit() for c in word):
                mentions.append(word)
    return list(set(mentions))[:5]


# Module-level compiled graph
_query_agent = None


def get_query_agent():
    global _query_agent
    if _query_agent is None:
        _query_agent = build_query_agent()
    return _query_agent
