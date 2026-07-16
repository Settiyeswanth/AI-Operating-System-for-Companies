# AI OS for Companies — Engineering Execution Blueprint

## Document Status
- **Type**: Internal Engineering Execution Blueprint  
- **Basis**: Research Report (Parts 1–2) + Principal Reference Architecture  
- **Scope**: Phase 1 Prototype → Phase 2 Production  
- **Team**: Solo founder / 1–3 engineers  
- **LLM Stack**: Ollama (local) → OpenAI/Anthropic (production)  
- **Live Integrations (Phase 1)**: GitHub + Linear + Slack  

---

## Architecture Validation Summary

The reference architecture is sound. The following gaps are identified and addressed in this blueprint:

| Gap | Risk | Resolution |
|-----|------|------------|
| No LLM abstraction layer specified | High — locks prototype to one provider | `LLMGateway` service wrapping Ollama/OpenAI/Anthropic |
| No graph bootstrap strategy for Phase 1 | Medium — cold start problem | Seed script + historical backfill connector job |
| No API contract between agents and memory | High — agents will drift in implementation | Typed `ContextBundle` and `TaskEnvelope` schemas defined in §6 |
| VerificationAgent depends on same LLM as producing agents | Medium — correlated failures | Use different model/temperature for verification in Phase 2 |
| No local development environment spec | High — onboarding friction | Docker Compose stack defined in §9 |
| Ontology bootstrapping not defined | Medium — graph starts empty | Starter ontology + seed loader in §4 |

---

## Part 1 — System Decomposition

### 1.1 Service Boundaries (Phase 1)

Eight services. Each runs as an independent process. All communicate through a message broker in production; in Phase 1, direct HTTP calls are acceptable between co-located services to reduce ops complexity.

```
┌─────────────────────────────────────────────────────────────────┐
│  aios/                                                          │
│  ├── gateway/          API Gateway + Auth + Rate Limiting       │
│  ├── connectors/       GitHub + Linear + Slack connectors       │
│  ├── ingestion/        Normalization + Entity Resolution        │
│  ├── enrichment/       Classification + Embedding + PII scrub   │
│  ├── memory/           Knowledge Graph + Vector Store + Ledger  │
│  ├── agents/           QueryAgent + MonitorAgent + Synth + Verify│
│  ├── interface/        Chat API + Alert webhooks + Draft API    │
│  └── observability/    Metrics + Tracing + Quality dashboards   │
```

### 1.2 Phase 1 vs Phase 2 Service Map

| Service | Phase 1 Implementation | Phase 2 Upgrade |
|---------|----------------------|-----------------|
| gateway | FastAPI + API key auth | Kong/Traefik + OAuth2 + RBAC |
| connectors | Polling (5-min intervals) + webhooks | CDC + Kafka + Airbyte |
| ingestion | In-process pipeline, SQLite ER index | Kafka Streams + PostgreSQL ER index |
| enrichment | Synchronous LLM calls (Ollama) | Async queue + fine-tuned embedding models |
| memory | Neo4j (local) + Qdrant (local) + SQLite ledger | Neo4j Aura + Pinecone/Weaviate + S3 Parquet |
| agents | LangGraph state machines, single process | Containerized agents + Celery task queues |
| interface | REST + SSE for streaming | WebSocket + Slack bot integration |
| observability | Structured JSON logs + basic metrics | OpenTelemetry + Grafana + custom eval harness |

---

## Part 2 — Repository Structure

```
aios/
├── README.md
├── docker-compose.yml           # Full local stack
├── docker-compose.dev.yml       # Minimal dev stack (no GPU required)
├── pyproject.toml               # Root project config (uv workspace)
├── .env.example                 # All required env vars documented
│
├── packages/                    # Shared internal packages
│   ├── aios-core/               # Shared types, schemas, config
│   │   ├── schemas/
│   │   │   ├── events.py        # Normalized event types
│   │   │   ├── entities.py      # Canonical entity models
│   │   │   ├── tasks.py         # TaskEnvelope, ContextBundle
│   │   │   └── ontology.py      # Graph node/edge type registry
│   │   ├── config.py            # Settings (pydantic-settings)
│   │   └── llm_gateway.py       # LLM provider abstraction
│   │
│   └── aios-testing/            # Shared test fixtures and factories
│
├── services/
│   ├── gateway/
│   │   ├── main.py
│   │   ├── auth.py
│   │   ├── routes/
│   │   └── Dockerfile
│   │
│   ├── connectors/
│   │   ├── github/
│   │   │   ├── connector.py
│   │   │   ├── webhook_handler.py
│   │   │   ├── poller.py
│   │   │   └── schema_map.py    # GitHub event → NormalizedEvent
│   │   ├── linear/
│   │   │   └── ...
│   │   ├── slack/
│   │   │   └── ...
│   │   ├── base.py              # ConnectorBase abstract class
│   │   └── Dockerfile
│   │
│   ├── ingestion/
│   │   ├── pipeline.py          # Main ingestion orchestrator
│   │   ├── normalizer.py
│   │   ├── entity_resolution/
│   │   │   ├── resolver.py
│   │   │   ├── er_index.py      # SQLite/Postgres ER mapping store
│   │   │   └── review_queue.py  # Human review interface
│   │   └── Dockerfile
│   │
│   ├── enrichment/
│   │   ├── pipeline.py
│   │   ├── pii_scrubber.py
│   │   ├── classifier.py
│   │   ├── embedder.py
│   │   ├── cross_ref_extractor.py
│   │   └── Dockerfile
│   │
│   ├── memory/
│   │   ├── graph/
│   │   │   ├── client.py        # Neo4j wrapper
│   │   │   ├── queries.py       # Cypher query library
│   │   │   ├── mutations.py     # Graph write operations
│   │   │   └── seed.py          # Ontology + seed data loader
│   │   ├── vector/
│   │   │   ├── client.py        # Qdrant wrapper
│   │   │   ├── indexer.py
│   │   │   └── retriever.py     # Hybrid BM25 + dense retrieval
│   │   ├── ledger/
│   │   │   ├── store.py         # Append-only event ledger
│   │   │   └── replay.py        # Ledger replay for recovery
│   │   └── Dockerfile
│   │
│   ├── agents/
│   │   ├── query_agent/
│   │   │   ├── agent.py         # LangGraph state machine
│   │   │   ├── retriever.py     # Hybrid retrieval planner
│   │   │   └── decomposer.py    # Query decomposition
│   │   ├── monitor_agent/
│   │   │   ├── agent.py
│   │   │   ├── rules.py         # Misalignment detection rules
│   │   │   └── scheduler.py
│   │   ├── synthesis_agent/
│   │   │   ├── agent.py
│   │   │   └── templates.py     # Output format templates
│   │   ├── verification_agent/
│   │   │   ├── agent.py
│   │   │   └── faithfulness.py  # Citation grounding checker
│   │   ├── base_agent.py        # AgentBase abstract class
│   │   ├── task_queue.py        # In-process task queue (Phase 1)
│   │   └── Dockerfile
│   │
│   ├── interface/
│   │   ├── chat_api.py          # REST + SSE streaming chat
│   │   ├── alert_api.py         # Misalignment alert endpoints
│   │   ├── draft_api.py         # Spec/summary draft endpoints
│   │   └── Dockerfile
│   │
│   └── observability/
│       ├── metrics.py
│       ├── tracing.py
│       ├── eval_harness.py      # Answer quality evaluation
│       └── Dockerfile
│
├── scripts/
│   ├── seed_graph.py            # Bootstrap ontology and seed data
│   ├── backfill.py              # Historical data ingestion
│   ├── eval_run.py              # Run evaluation suite
│   └── er_review.py             # CLI for ER candidate review
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── eval/                    # Answer quality benchmarks
│
└── docs/
    ├── architecture/
    ├── api/
    ├── runbooks/
    └── adr/                     # Architecture Decision Records
```

---

## Part 3 — Data Architecture

### 3.1 Canonical Entity Models (aios-core/schemas/entities.py)

```python
# All entities inherit from BaseEntity
class BaseEntity(BaseModel):
    id: str              # Canonical UUID (system-generated)
    created_at: datetime
    updated_at: datetime
    source_ids: dict[str, str]   # {"github": "abc123", "linear": "def456"}
    confidence: float = 1.0      # For inferred entities
    is_stale: bool = False
    stale_since: datetime | None = None
    access_tags: AccessTags

class Person(BaseEntity):
    canonical_email: str
    display_names: list[str]
    team_ids: list[str]
    roles: list[str]

class Feature(BaseEntity):
    title: str
    status: FeatureStatus          # PLANNED|IN_PROGRESS|SHIPPED|ABANDONED
    priority: Priority
    spec_url: str | None
    linked_requirement_ids: list[str]
    linked_decision_ids: list[str]

class Decision(BaseEntity):
    summary: str
    rationale: str
    alternatives_rejected: list[str]
    made_by_ids: list[str]
    made_at: datetime
    superseded_by_id: str | None

class Incident(BaseEntity):
    severity: Severity             # P0|P1|P2|P3
    status: IncidentStatus
    affected_feature_ids: list[str]
    root_cause: str | None
    resolved_by_ids: list[str]

class Message(BaseEntity):
    channel_type: ChannelType      # SLACK|EMAIL|PR_COMMENT|TICKET_COMMENT
    author_id: str
    timestamp: datetime
    content_summary: str           # LLM-generated summary, not raw content
    source_url: str | None
    referenced_entity_ids: list[str]
```

### 3.2 Normalized Event Schema (aios-core/schemas/events.py)

```python
class NormalizedEvent(BaseModel):
    event_id: str                  # UUID
    idempotency_key: str           # source_system:event_type:source_id:timestamp
    source_system: SourceSystem    # GITHUB|LINEAR|SLACK
    event_type: str                # "pr.opened", "issue.created", "message.posted"
    actor_source_id: str           # Pre-resolution actor identifier
    actor_canonical_id: str | None # Populated after entity resolution
    entity_source_id: str          # The primary entity this event concerns
    entity_canonical_id: str | None
    timestamp: datetime
    raw_payload: dict              # Original webhook payload
    schema_version: str
    received_at: datetime
    processing_status: ProcessingStatus  # PENDING|RESOLVED|ENRICHED|FAILED
```

### 3.3 Task Envelope (aios-core/schemas/tasks.py)

```python
class TaskEnvelope(BaseModel):
    task_id: str                   # UUID
    task_type: TaskType            # QUERY|SYNTHESIS|VERIFY|MONITOR_CHECK
    originator: str                # agent_id or user_id
    priority: int = 3              # 1 (highest) to 5 (lowest)
    deadline_ms: int = 5000
    audit_required: bool = True
    context: TaskContext
    payload: dict                  # Task-type-specific

class TaskContext(BaseModel):
    user_identity: str
    access_scopes: list[str]
    session_id: str
    parent_task_id: str | None = None
    trace_id: str                  # OpenTelemetry trace ID

class ContextBundle(BaseModel):
    """Passed from retrieval to synthesis to verification — never modified"""
    query: str
    retrieved_chunks: list[RetrievedChunk]
    graph_context: list[GraphResult]
    retrieval_metadata: RetrievalMetadata
    access_scopes: list[str]

class RetrievedChunk(BaseModel):
    chunk_id: str
    source_artifact_id: str
    content: str
    score: float
    access_tags: AccessTags
    timestamp: datetime
    source_system: str
```

---

## Part 4 — Graph Database Schema (Ontology)

### 4.1 Neo4j Node Labels and Properties

```cypher
// Core node types
CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT feature_id IF NOT EXISTS FOR (f:Feature) REQUIRE f.id IS UNIQUE;
CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT incident_id IF NOT EXISTS FOR (i:Incident) REQUIRE i.id IS UNIQUE;
CREATE CONSTRAINT message_id IF NOT EXISTS FOR (m:Message) REQUIRE m.id IS UNIQUE;
CREATE CONSTRAINT team_id IF NOT EXISTS FOR (t:Team) REQUIRE t.id IS UNIQUE;
CREATE CONSTRAINT codeunit_id IF NOT EXISTS FOR (c:Codeunit) REQUIRE c.id IS UNIQUE;

// Relationship types (all carry: created_at, confidence, source_event_id, created_by)
// Person -[AUTHORED]-> Feature | Decision | Codeunit | Message
// Feature -[IMPLEMENTS]-> Requirement
// Feature -[DEPENDS_ON]-> Feature
// Feature -[DIVERGES_FROM]-> Requirement | Decision   (R3 signal edge)
// Decision -[CONSTRAINS]-> Feature | Project
// Incident -[AFFECTED]-> Feature | Codeunit
// Message -[REFERENCES]-> Feature | Decision | Incident
// Person -[MEMBER_OF {valid_from, valid_until}]-> Team

// Phase 1 seed query — creates starter ontology nodes
MERGE (ghost:Person {id: 'system', display_names: ['AI OS System'], canonical_email: 'system@internal'})
```

### 4.2 Cypher Query Library (memory/graph/queries.py)

Key queries that agents invoke directly:

```python
QUERIES = {
    "find_feature_authors": """
        MATCH (p:Person)-[:AUTHORED]->(f:Feature {id: $feature_id})
        RETURN p.id, p.display_names, p.canonical_email
    """,
    "find_diverging_features": """
        MATCH (f:Feature)-[d:DIVERGES_FROM]->(r)
        WHERE d.confidence >= $min_confidence
        AND d.created_at >= $since
        RETURN f, d, r ORDER BY d.confidence DESC LIMIT $limit
    """,
    "find_decision_context": """
        MATCH (d:Decision)-[:CONSTRAINS]->(f:Feature)
        WHERE f.id IN $feature_ids
        OPTIONAL MATCH (p:Person)-[:AUTHORED]->(d)
        RETURN d, collect(p) as authors, collect(f) as constrained_features
    """,
    "find_incident_blast_radius": """
        MATCH (i:Incident {id: $incident_id})-[:AFFECTED]->(f:Feature)
        OPTIONAL MATCH (p:Person)-[:AUTHORED]->(f)
        OPTIONAL MATCH (d:Decision)-[:CONSTRAINS]->(f)
        RETURN i, collect(DISTINCT f) as features, 
               collect(DISTINCT p) as authors,
               collect(DISTINCT d) as decisions
    """
}
```

---

## Part 5 — LLM Gateway (aios-core/llm_gateway.py)

The single most important abstraction for Phase 1 → Phase 2 migration. Everything uses this — nothing calls LLM APIs directly.

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel

class LLMMessage(BaseModel):
    role: str          # "system" | "user" | "assistant"
    content: str

class LLMResponse(BaseModel):
    content: str
    model: str
    usage: dict        # token counts
    raw: dict          # provider-specific response

class LLMGateway(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
    ) -> AsyncIterator[str]: ...

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]: ...

# Phase 1 implementation
class OllamaGateway(LLMGateway):
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url
        self.default_model = "llama3.1:8b"
        self.embed_model = "nomic-embed-text"

# Phase 2 implementation  
class OpenAIGateway(LLMGateway):
    def __init__(self, api_key: str):
        self.default_model = "gpt-4o"
        self.embed_model = "text-embedding-3-small"

# Factory — swap by environment variable
def get_llm_gateway() -> LLMGateway:
    provider = settings.LLM_PROVIDER  # "ollama" | "openai" | "anthropic"
    if provider == "ollama":
        return OllamaGateway(base_url=settings.OLLAMA_BASE_URL)
    elif provider == "openai":
        return OpenAIGateway(api_key=settings.OPENAI_API_KEY)
    raise ValueError(f"Unknown LLM provider: {provider}")
```

---

## Part 6 — Agent Architecture (LangGraph)

### 6.1 QueryAgent State Machine

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

class QueryAgentState(TypedDict):
    query: str
    session_context: TaskContext
    sub_queries: list[str]
    retrieved_chunks: list[RetrievedChunk]
    graph_results: list[GraphResult]
    draft_answer: str | None
    verification_verdict: VerificationVerdict | None
    final_answer: str | None
    error: str | None

def build_query_agent_graph() -> StateGraph:
    graph = StateGraph(QueryAgentState)
    
    graph.add_node("decompose_query", decompose_query_node)
    graph.add_node("retrieve_vector", retrieve_vector_node)
    graph.add_node("retrieve_graph", retrieve_graph_node)
    graph.add_node("fuse_results", fuse_results_node)         # RRF fusion
    graph.add_node("generate_answer", generate_answer_node)
    graph.add_node("verify_answer", verify_answer_node)       # VerificationAgent
    graph.add_node("format_response", format_response_node)
    
    graph.set_entry_point("decompose_query")
    graph.add_edge("decompose_query", "retrieve_vector")
    graph.add_edge("decompose_query", "retrieve_graph")       # Parallel
    graph.add_edge("retrieve_vector", "fuse_results")
    graph.add_edge("retrieve_graph", "fuse_results")
    graph.add_edge("fuse_results", "generate_answer")
    graph.add_edge("generate_answer", "verify_answer")
    graph.add_conditional_edges(
        "verify_answer",
        route_on_verdict,
        {
            "pass": "format_response",
            "uncertain": "format_response",
            "fail": END,  # Logs to audit, returns structured error
        }
    )
    graph.add_edge("format_response", END)
    return graph.compile()
```

### 6.2 MonitorAgent — Misalignment Detection Loop

```python
class MonitorAgentState(TypedDict):
    check_window_start: datetime
    check_window_end: datetime
    divergence_candidates: list[DivergenceCandidate]
    evaluated_candidates: list[EvaluatedCandidate]
    alerts_generated: list[MisalignmentAlert]

MISALIGNMENT_RULES = [
    # Rule 1: Feature blocked > N days with no linked decision explaining the block
    BlockedWithoutDecisionRule(threshold_days=5),
    
    # Rule 2: Commits touching scope not in the linked Feature definition
    ScopeDriftRule(confidence_threshold=0.7),
    
    # Rule 3: Customer support tickets referencing features with no linked Requirement
    OrphanedRequirementRule(),
    
    # Rule 4: Decision made > N days ago with no linked Feature showing implementation
    UnimplementedDecisionRule(threshold_days=14),
    
    # Rule 5: Feature marked complete but linked Requirements still show OPEN status
    CompletionMismatchRule(),
]
```

### 6.3 VerificationAgent — Faithfulness Check

```python
VERIFICATION_PROMPT = """
You are a strict fact-checker. You will be given an answer and the exact source documents
used to produce it. Your job is to verify that every factual claim in the answer is 
directly supported by one of the source documents.

For each claim in the answer:
1. Identify the source document(s) that support it
2. If no source supports a claim, mark it UNSUPPORTED
3. If a source contradicts a claim, mark it CONTRADICTED

Answer to verify:
{answer}

Source documents:
{sources}

Respond in JSON:
{
  "verdict": "PASS" | "UNCERTAIN" | "FAIL",
  "claim_annotations": [
    {"claim": "...", "status": "SUPPORTED|UNSUPPORTED|CONTRADICTED", "source_id": "..."}
  ],
  "reasoning": "..."
}

PASS = all claims supported
UNCERTAIN = some claims have weak or indirect support  
FAIL = one or more claims contradicted or entirely unsupported
"""
```

---

## Part 7 — Connector Architecture

### 7.1 ConnectorBase Abstract Class

```python
class ConnectorBase(ABC):
    source_system: SourceSystem
    
    @abstractmethod
    async def poll_recent(self, since: datetime) -> list[RawEvent]:
        """Pull events since last successful poll. Used in Phase 1."""
        ...
    
    @abstractmethod
    async def handle_webhook(self, payload: dict, headers: dict) -> list[RawEvent]:
        """Process an incoming webhook from the source system."""
        ...
    
    @abstractmethod
    def map_to_normalized(self, raw_event: RawEvent) -> NormalizedEvent:
        """Map source-specific event schema to canonical NormalizedEvent."""
        ...
    
    @abstractmethod
    async def validate_webhook_signature(self, payload: bytes, headers: dict) -> bool:
        """Verify the webhook came from the expected source."""
        ...
```

### 7.2 GitHub Connector — Schema Mapping (Key Events)

```python
GITHUB_EVENT_MAP = {
    "push": lambda p: NormalizedEvent(
        source_system=SourceSystem.GITHUB,
        event_type="code.pushed",
        actor_source_id=p["pusher"]["email"],
        entity_source_id=p["repository"]["full_name"],
        # ... 
    ),
    "pull_request": lambda p: NormalizedEvent(
        source_system=SourceSystem.GITHUB,
        event_type=f"pr.{p['action']}",
        # ...
    ),
    "issues": lambda p: NormalizedEvent(
        source_system=SourceSystem.GITHUB,
        event_type=f"issue.{p['action']}",
        # ...
    ),
}
```

### 7.3 Linear Connector — Key Events

```python
LINEAR_EVENT_MAP = {
    "Issue": {
        "create": "ticket.created",
        "update": "ticket.updated", 
        "remove": "ticket.deleted",
    },
    "Comment": {"create": "comment.created"},
    "Cycle": {"create": "sprint.created", "update": "sprint.updated"},
    "Project": {"create": "project.created", "update": "project.updated"},
}
```

### 7.4 Slack Connector — Key Events

```python
SLACK_EVENT_MAP = {
    "message": "message.posted",
    "message_changed": "message.edited",
    "channel_created": "channel.created",
    "member_joined_channel": "channel.member_joined",
    # Reactions as signal of decision acknowledgment
    "reaction_added": "reaction.added",
}

# Special handling: thread replies are critical for decision context
# Threads are ingested as Message entities with parent_message_id
```

---

## Part 8 — Entity Resolution Pipeline

### 8.1 Resolution Strategy (services/ingestion/entity_resolution/resolver.py)

```python
class EntityResolver:
    """
    Two-stage resolution: deterministic email match → probabilistic fallback.
    Ambiguous cases → human review queue. Never auto-merge below confidence threshold.
    """
    
    DETERMINISTIC_CONFIDENCE = 1.0
    PROBABILISTIC_THRESHOLD = 0.85    # Below this → human review queue
    
    async def resolve_person(self, event: NormalizedEvent) -> str | None:
        """Returns canonical_id if resolved, None if routed to review queue."""
        
        # Stage 1: Deterministic — email match
        email = self._extract_email(event)
        if email:
            canonical_id = await self.er_index.lookup_by_email(email)
            if canonical_id:
                return canonical_id
            # New entity — create canonical record
            return await self._create_canonical_person(email, event)
        
        # Stage 2: Probabilistic — display name + username patterns
        candidates = await self._find_candidates(event)
        if not candidates:
            return await self._create_canonical_person(None, event)
        
        best_match = max(candidates, key=lambda c: c.confidence)
        if best_match.confidence >= self.PROBABILISTIC_THRESHOLD:
            return best_match.canonical_id
        
        # Below threshold → human review queue
        await self.review_queue.submit(
            ReviewCandidate(
                source_event=event,
                candidates=candidates,
                reason="Confidence below threshold"
            )
        )
        return None  # Event held pending review
    
    def _compute_similarity_score(self, a: dict, b: dict) -> float:
        """
        Weighted combination:
        - Email domain match: 0.4
        - Display name similarity (normalized Levenshtein): 0.3
        - Username pattern match: 0.2
        - Temporal co-occurrence: 0.1
        """
        score = 0.0
        if a.get("email_domain") and a["email_domain"] == b.get("email_domain"):
            score += 0.4
        if a.get("display_name") and b.get("display_name"):
            score += 0.3 * (1 - normalized_levenshtein(a["display_name"], b["display_name"]))
        # ... etc
        return score
```

---

## Part 9 — Local Development Stack (Docker Compose)

### 9.1 docker-compose.yml

```yaml
version: "3.9"

services:
  # Core infrastructure
  neo4j:
    image: neo4j:5.18-community
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: "neo4j/password"
      NEO4J_PLUGINS: '["apoc"]'
    volumes: ["neo4j_data:/data"]
    healthcheck:
      test: ["CMD", "neo4j", "status"]
      interval: 10s

  qdrant:
    image: qdrant/qdrant:v1.9.0
    ports: ["6333:6333"]
    volumes: ["qdrant_data:/qdrant/storage"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    # Used for: task queue, session cache, rate limiting

  ollama:
    image: ollama/ollama:latest
    ports: ["11434:11434"]
    volumes: ["ollama_models:/root/.ollama"]
    # Pull models on first start:
    # docker exec aios-ollama-1 ollama pull llama3.1:8b
    # docker exec aios-ollama-1 ollama pull nomic-embed-text

  # Application services
  gateway:
    build: ./services/gateway
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [redis]

  connectors:
    build: ./services/connectors
    env_file: .env
    depends_on: [redis, neo4j, qdrant]

  ingestion:
    build: ./services/ingestion
    env_file: .env
    depends_on: [neo4j, redis]

  enrichment:
    build: ./services/enrichment
    env_file: .env
    depends_on: [neo4j, qdrant, ollama]

  agents:
    build: ./services/agents
    env_file: .env
    depends_on: [neo4j, qdrant, redis, ollama]

  interface:
    build: ./services/interface
    ports: ["8001:8001"]
    env_file: .env
    depends_on: [agents]

volumes:
  neo4j_data:
  qdrant_data:
  ollama_models:
```

### 9.2 .env.example

```env
# LLM Provider
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_DEFAULT_MODEL=llama3.1:8b
OLLAMA_EMBED_MODEL=nomic-embed-text

# Phase 2 (leave blank for Phase 1)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Infrastructure
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
QDRANT_HOST=qdrant
QDRANT_PORT=6333
REDIS_URL=redis://redis:6379

# GitHub Connector
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=
GITHUB_TARGET_REPOS=org/repo1,org/repo2

# Linear Connector
LINEAR_API_KEY=
LINEAR_WEBHOOK_SECRET=
LINEAR_TEAM_IDS=

# Slack Connector
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
SLACK_TARGET_CHANNELS=

# Auth
API_KEY_SALT=change-me-in-production
JWT_SECRET=change-me-in-production

# Operational
LOG_LEVEL=INFO
ENVIRONMENT=development
ER_CONFIDENCE_THRESHOLD=0.85
MONITOR_POLL_INTERVAL_MINUTES=5
STALENESS_THRESHOLD_DAYS=30
```

---

## Part 10 — API Contracts

### 10.1 Chat API (interface service)

```
POST /v1/chat
Authorization: Bearer <api_key>
Content-Type: application/json

Request:
{
  "query": "What was the last decision made about the auth service?",
  "session_id": "uuid",
  "stream": true
}

Response (streaming SSE):
data: {"type": "thinking", "content": "Retrieving relevant context..."}
data: {"type": "chunk", "content": "The last decision about..."}
data: {"type": "sources", "sources": [{"id": "...", "type": "Decision", "url": "..."}]}
data: {"type": "done", "verdict": "PASS", "confidence": 0.92}

Response (non-streaming):
{
  "answer": "The last decision about the auth service...",
  "sources": [...],
  "verdict": "PASS" | "UNCERTAIN" | "FAIL",
  "confidence": 0.92,
  "trace_id": "uuid"
}
```

### 10.2 Alerts API

```
GET /v1/alerts?since=<iso8601>&severity=high&limit=20
Authorization: Bearer <api_key>

Response:
{
  "alerts": [
    {
      "alert_id": "uuid",
      "type": "scope_drift" | "delivery_delay" | "contradiction_detected" | ...,
      "severity": "high" | "medium" | "low",
      "summary": "Feature PROJ-412 has diverged from Decision DEC-89",
      "feature_id": "uuid",
      "evidence": [...],
      "detected_at": "2024-01-15T10:30:00Z",
      "acknowledged": false
    }
  ]
}

POST /v1/alerts/{alert_id}/acknowledge
POST /v1/alerts/{alert_id}/dismiss
```

### 10.3 Internal Agent Task Queue API

```
POST /internal/tasks
{
  "task_type": "QUERY",
  "payload": {...},
  "context": {...}
}

GET /internal/tasks/{task_id}
{
  "status": "PENDING" | "IN_PROGRESS" | "COMPLETE" | "FAILED",
  "result": {...} | null,
  "error": null | {...}
}
```

---

## Part 11 — Testing Strategy

### 11.1 Test Pyramid

```
                    ┌────────────────────┐
                    │   E2E Tests (5%)   │
                    │  Full user flow     │
                    │  against real stack │
                    ├────────────────────┤
                    │ Integration (25%)  │
                    │ Service boundaries │
                    │ Agent pipelines    │
                    │ ER pipeline        │
                    ├────────────────────┤
                    │  Unit Tests (70%)  │
                    │  Schema validation │
                    │  ER logic          │
                    │  Query decomp.     │
                    │  Access control    │
                    │  Cypher queries    │
                    └────────────────────┘
```

### 11.2 Critical Test Cases

```python
# tests/unit/test_entity_resolution.py
class TestEntityResolution:
    def test_deterministic_email_match_resolves_correctly(self): ...
    def test_same_person_different_display_names_routes_to_review(self): ...
    def test_two_different_people_not_merged(self): ...  # Critical negative test
    def test_merge_is_reversible(self): ...

# tests/unit/test_access_control.py
class TestAccessControl:
    def test_restricted_artifact_excluded_from_answer(self): ...
    def test_excluded_artifact_not_disclosed_in_response(self): ...
    def test_pii_flag_requires_pii_grant(self): ...

# tests/integration/test_query_agent.py
class TestQueryAgent:
    def test_simple_factual_query_returns_cited_answer(self): ...
    def test_unknown_query_returns_uncertainty_rather_than_hallucination(self): ...
    def test_verification_fail_blocks_delivery(self): ...

# tests/eval/benchmark.py — Answer quality evaluation
EVAL_DATASET = [
    {
        "query": "Who last modified the payment module?",
        "expected_entity_types": ["Person", "Codeunit"],
        "expected_source_types": ["github"],
        "acceptable_uncertainty": False,
    },
    # ... 50+ eval cases seeded from Phase 1 real usage
]
```

### 11.3 Evaluation Harness Metrics

```python
QUALITY_METRICS = {
    "faithfulness": "Fraction of answer claims supported by cited sources",
    "retrieval_precision": "Fraction of retrieved chunks relevant to query",
    "retrieval_recall": "Fraction of relevant chunks retrieved",
    "entity_resolution_precision": "Fraction of auto-resolved merges that are correct",
    "alert_precision": "Fraction of misalignment alerts that humans confirm as real",
    "latency_p50_ms": "Median query response latency",
    "latency_p95_ms": "95th percentile query response latency",
}
```

---

## Part 12 — Engineering Milestones

### Milestone 0: Infrastructure Foundation (Target: Day 1–3)
- [ ] Repository initialized with monorepo structure
- [ ] docker-compose.yml boots Neo4j + Qdrant + Redis + Ollama cleanly
- [ ] `aios-core` package with all schemas compiles with zero errors
- [ ] `seed_graph.py` bootstraps ontology in Neo4j
- [ ] Ollama serving `llama3.1:8b` and `nomic-embed-text` locally
- **Validation**: `docker compose up` starts all infra services with passing healthchecks

### Milestone 1: Ingestion Pipeline Live (Target: Day 4–7)
- [ ] GitHub connector polls last 7 days of events for target repos
- [ ] Linear connector polls last 7 days of issues and comments
- [ ] Slack connector polls last 7 days of target channel messages
- [ ] NormalizedEvent schema validates all three source types
- [ ] Entity resolution resolves ≥80% of Person entities via email
- [ ] Events written to Temporal Event Ledger (SQLite)
- **Validation**: 100+ events ingested, ≥10 Person nodes in Neo4j, no duplicate nodes for known individuals

### Milestone 2: Knowledge Graph Populated (Target: Day 8–12)
- [ ] Enrichment pipeline classifies events and generates embeddings
- [ ] 500+ document chunks indexed in Qdrant
- [ ] Knowledge Graph populated: Feature, Decision, Incident node types present
- [ ] AUTHORED, MEMBER_OF, REFERENCES edge types present with confidence scores
- [ ] Hybrid retrieval returns relevant chunks for 5 test queries
- **Validation**: Graph traversal query "Who worked on [known feature]?" returns correct Person

### Milestone 3: Query Agent Working (Target: Day 13–18)
- [ ] QueryAgent answers simple factual questions with citations
- [ ] VerificationAgent returns PASS/UNCERTAIN/FAIL verdicts
- [ ] FAIL verdicts block delivery and log to audit
- [ ] Chat API endpoint returns streaming SSE responses
- [ ] Access control excludes restricted artifacts from answers
- **Validation**: 10-query benchmark: ≥7/10 queries return correct cited answers, 0 hallucinations on known-fact queries

### Milestone 4: MonitorAgent Live (Target: Day 19–24)
- [ ] MonitorAgent runs on 5-minute schedule
- [ ] BlockedWithoutDecisionRule produces correct alerts on seed data
- [ ] ScopeDriftRule detects at least 1 real drift in target repos
- [ ] Alerts API returns structured alerts with evidence
- [ ] Alert false positive rate measured on first 50 alerts
- **Validation**: At least 1 genuine misalignment detected in real data. Alert precision ≥60% (humans confirm as real)

### Milestone 5: End-to-End Prototype Complete (Target: Day 25–30)
- [ ] All 4 agents running
- [ ] Webhook handlers live for all 3 connectors (real-time ingestion)
- [ ] Chat interface accessible at localhost:8001
- [ ] ER review CLI operational
- [ ] Basic observability dashboard (structured logs + metrics endpoint)
- [ ] Evaluation harness running against 20-query benchmark
- **Validation**: Full demo flow — ingest real event → it appears in graph → query agent answers question about it → monitor agent detects a real drift. All within 5 minutes end-to-end.

---

## Part 13 — Technical Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Ollama too slow for interactive query latency | High | Medium | Use 7B models for enrichment, 13B+ only for query synthesis. Cache frequent queries in Redis. |
| Entity resolution splits same person across connectors | High | High | Prioritize email-based resolution. Instrument ER precision from day 1. |
| LangGraph state machine hangs on LLM timeout | Medium | High | Add per-node timeout (5s default), circuit breaker on LLM gateway. |
| Neo4j graph query slow on large traversals | Low | Medium | Add query complexity limits (max 4 hops), index key node properties on day 1. |
| Slack webhook signature validation fails due to timing | Medium | Low | Use 5-minute tolerance on timestamp check. Log all validation failures. |
| MonitorAgent alert fatigue from high false positive rate | High | High | Start with only 1–2 high-precision rules. Add rules incrementally after measuring precision. |
| LLM cross-reference extraction introduces spurious graph edges | Medium | Medium | All LLM-extracted edges get confidence < 0.7 and are flagged in answers. |

---

## Part 14 — Phase 2 Production Upgrade Path

### What Changes (without throwing away Phase 1 work)

| Component | Phase 1 | Phase 2 Upgrade | Upgrade Cost |
|-----------|---------|-----------------|--------------|
| LLM provider | Ollama (local) | OpenAI GPT-4o | Change env var + test |
| Event broker | Redis Pub/Sub | Apache Kafka | Swap task_queue.py backend |
| ER index | SQLite | PostgreSQL | Schema migration only |
| Vector store | Qdrant (local) | Qdrant Cloud / Pinecone | Change Qdrant client config |
| Graph DB | Neo4j (local) | Neo4j Aura (managed) | Change connection string |
| Event ledger | SQLite | S3 Parquet + Athena | Add new ledger backend |
| Deployment | Docker Compose | Kubernetes (EKS/GKE) | Helm charts + CI/CD |
| Auth | API key | OAuth2 + RBAC | Extend gateway auth module |
| Observability | Structured logs | OpenTelemetry + Grafana | Instrumentation already present |

### New Connectors to Add (in priority order)
1. Notion (documentation layer — adds declarative knowledge)
2. Zoom / Google Meet (meeting transcripts — highest tacit→explicit value)
3. Salesforce (customer signal → requirement feedback loop)
4. Jira (for teams not using Linear)
5. PagerDuty / OpsGenie (incident signal)

---

## Part 15 — Success Metrics

### Phase 1 Prototype Success Criteria

| Metric | Target | Measurement Method |
|--------|--------|-------------------|
| Query faithfulness | ≥80% claims cited | VerificationAgent + manual sample review |
| Entity resolution precision | ≥85% auto-merges correct | Weekly sample audit of resolved pairs |
| Alert precision | ≥60% confirmed real | Human acknowledgment rate on alerts |
| P50 query latency (Ollama) | ≤8 seconds | Interface service timing middleware |
| Ingestion lag | ≤10 minutes from event to graph | Event timestamp vs. graph node timestamp |
| ER queue drain time | ≤48 hours | Queue depth monitoring |

### Phase 2 Production Success Criteria

| Metric | Target |
|--------|--------|
| Query faithfulness | ≥92% |
| P50 query latency | ≤2 seconds (OpenAI) |
| Alert precision | ≥80% |
| Entity resolution precision | ≥95% |
| System uptime | ≥99.5% |
| Knowledge freshness | ≥95% of events in graph within 60 seconds |

---

## Appendix A — Architecture Decision Records (ADR Index)

ADRs to be written before implementation begins:

- **ADR-001**: Monorepo vs. polyrepo — Monorepo chosen for Phase 1 simplicity; revisit at 5+ engineers
- **ADR-002**: LangGraph vs. custom agent orchestration — LangGraph chosen for built-in state management and debugging; raw Python acceptable alternative
- **ADR-003**: Neo4j vs. Amazon Neptune — Neo4j chosen for local development parity; Neptune for Phase 2 if AWS-native
- **ADR-004**: Qdrant vs. Weaviate vs. Pinecone — Qdrant chosen for local-first with managed cloud path
- **ADR-005**: SQLite vs. PostgreSQL for Phase 1 ER index — SQLite for simplicity; PostgreSQL upgrade path is schema-compatible
- **ADR-006**: Redis for task queue vs. Celery+RabbitMQ — Redis chosen for Phase 1; Kafka for Phase 2

---

## Appendix B — Key Dependencies

```toml
# pyproject.toml (root)
[tool.uv.workspace]
members = ["packages/*", "services/*"]

[project]
requires-python = ">=3.11"

# packages/aios-core
dependencies = [
  "pydantic>=2.0",
  "pydantic-settings>=2.0",
  "httpx>=0.27",
  "python-dotenv>=1.0",
]

# services/agents
dependencies = [
  "aios-core",
  "langgraph>=0.1.0",
  "langchain-core>=0.2.0",
  "neo4j>=5.0",
  "qdrant-client>=1.9",
  "redis>=5.0",
  "rank-bm25>=0.2",       # BM25 for hybrid retrieval
]

# services/connectors
dependencies = [
  "aios-core",
  "PyGithub>=2.0",
  "linear-python>=0.1",    # or httpx-based custom client
  "slack-sdk>=3.0",
  "apscheduler>=3.10",     # Polling scheduler
]

# services/ingestion
dependencies = [
  "aios-core",
  "rapidfuzz>=3.0",        # Fast string distance for ER
  "presidio-analyzer>=2.0", # PII detection
  "presidio-anonymizer>=2.0",
]

# services/enrichment
dependencies = [
  "aios-core",
  "sentence-transformers>=3.0",
]
```
