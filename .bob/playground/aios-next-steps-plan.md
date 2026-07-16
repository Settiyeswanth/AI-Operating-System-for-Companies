# AI OS for Companies — Engineering Execution Plan
# NO DOCKER — Full Cloud Architecture

> **Status:** Code-complete and runnable. Zero Docker required.
> All infrastructure runs as free-tier managed cloud services.

---

## Architecture: Everything in the Cloud

```
┌─────────────────────────────────────────────────────────────────────┐
│  YOUR MACHINE (only Python processes, no containers)                │
│                                                                     │
│  .\run-local.ps1 starts these 7 Python processes:                  │
│    gateway     :8000  (FastAPI — auth, rate limit, proxy)           │
│    interface   :8001  (FastAPI — chat API, alerts API)              │
│    memory      :8002  (FastAPI — graph + vector queries)            │
│    agents      (background — QueryAgent, MonitorAgent, etc.)       │
│    connectors  (background — GitHub/Linear/Slack polling)           │
│    ingestion   (background — dedup, entity resolution)              │
│    enrichment  (background — PII scrub, embed, write)               │
└──────────────┬──────────────────────────────────────────────────────┘
               │ connects to
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  MANAGED CLOUD SERVICES (all free tiers, no Docker, no GPU)        │
│                                                                     │
│  IBM watsonx.ai        LLM (Granite 3.3 8B) + Embeddings (768-dim) │
│  Neo4j AuraDB Free     Knowledge Graph (nodes, edges, Cypher)       │
│  Qdrant Cloud Free     Vector Index (hybrid BM25 + dense search)    │
│  IBM ICD Redis Lite    Message Bus (Pub/Sub, rate limits, dedup)    │
│                                                                     │
│  GitHub / Linear / Slack   Source data connectors                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Completion Matrix (All Done)

| Component | Status | Cloud Service / File |
|-----------|--------|---------------------|
| LLM + Embeddings | ✅ | IBM watsonx.ai — `llm_gateway.py:WatsonxGateway` |
| Knowledge Graph | ✅ | Neo4j AuraDB — `neo4j+s://` TLS connection |
| Vector Search | ✅ | Qdrant Cloud — TLS + API key in `vector/client.py` |
| Message Bus | ✅ | IBM ICD Redis — `rediss://` TLS in all services |
| Gateway service | ✅ | `services/gateway/` — no Docker hostnames |
| Enrichment service | ✅ | `services/enrichment/` — PII → embed → graph |
| MonitorAgent dedup | ✅ | Redis SETNX 24h TTL |
| IBM Cloud config | ✅ | `config.py` — all 5 cloud services configured |
| .env template | ✅ | `.env.ibmcloud.example` — numbered setup instructions |
| Setup guide | ✅ | `IBM-CLOUD-SETUP.md` — step-by-step all 5 services |
| All-service verifier | ✅ | `scripts/verify_ibmcloud.py` — checks all 5 |
| Windows launcher | ✅ | `run-local.ps1` — cloud verify + start/stop/logs |
| Mac/Linux launcher | ✅ | `run-local.sh` — same |
| Run guide | ✅ | `HOW-TO-RUN.md` — no Docker, pure cloud |
| docker-compose.yml | ✅ | Empty (services: {}) — Docker not needed |
| Backfill script | ✅ | `scripts/backfill.py` |
| Unit tests (15) | ✅ | `tests/unit/` |

---

## What You Need to Set Up (One Time, ~30 Minutes)

All 5 services are **free tier** — no credit card required:

| # | Service | URL | What It Does |
|---|---------|-----|-------------|
| 1 | IBM watsonx.ai | cloud.ibm.com | LLM + Embeddings |
| 2 | Neo4j AuraDB | cloud.neo4j.com | Knowledge Graph |
| 3 | Qdrant Cloud | cloud.qdrant.io | Vector Search |
| 4 | IBM ICD Redis | cloud.ibm.com/catalog/services/databases-for-redis | Event Bus |
| 5 | GitHub PAT | github.com/settings/tokens | Source data |

**Full guide:** `IBM-CLOUD-SETUP.md`

---

## Start Sequence (After One-Time Setup)

```powershell
# 1. Fill in .env (one time)
Copy-Item .env.ibmcloud.example .env
# Edit .env — fill in all ← FILL THIS IN values

# 2. Verify all cloud services (one time)
uv run python scripts/verify_ibmcloud.py
# Must show 6 green ✅

# 3. Create knowledge graph schema (one time)
uv run python scripts/seed_graph.py

# 4. Start all 7 app services (every time)
.\run-local.ps1

# 5. Ingest historical data (first time only)
uv run python scripts/backfill.py --days 30

# 6. Query
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test"} `
  -ContentType "application/json" `
  -Body '{"query":"What has been worked on?","stream":false}'
```

---

## Cloud Service Connection Details

### IBM watsonx.ai
- **Auth:** IBM IAM API key → Bearer token (auto-refreshed every ~55 min)
- **Text gen endpoint:** `{WATSONX_URL}/ml/v1/text/generation?version=2024-05-01`
- **Embeddings endpoint:** `{WATSONX_URL}/ml/v1/text/embeddings?version=2024-05-01`
- **Model:** `ibm/granite-3-3-8b-instruct` (8B, fast, instruction-tuned)
- **Embed model:** `ibm/slate-125m-english-rtrvr-v2` → 768-dim vectors
- **Prompt format:** Granite `<|system|>`, `<|user|>`, `<|assistant|>` tokens

### Neo4j AuraDB
- **URI format:** `neo4j+s://xxxxxxxx.databases.neo4j.io` (TLS required)
- **Driver:** `neo4j` Python async driver
- **Schema:** constraints + indices created by `scripts/seed_graph.py`
- **Cypher queries:** in `services/memory/memory/graph/client.py`

### Qdrant Cloud
- **Connection:** `AsyncQdrantClient(host=..., api_key=..., https=True)`
- **Collection:** `aios_chunks` (768-dim cosine similarity)
- **Search:** hybrid BM25 + dense, fused with Reciprocal Rank Fusion
- **Config changed:** `config.py` now has `qdrant_api_key` and `qdrant_use_tls`

### IBM ICD Redis
- **URL format:** `rediss://:password@host:port/0` (double-s = TLS)
- **Channels:** `aios:events`, `aios:enrichment`, `aios:tasks`, `aios:alerts`
- **Keys:** `aios:ratelimit:*`, `aios:alert:dedup:*`

---

## Files Changed in This No-Docker Rewrite

| File | Change |
|------|--------|
| `packages/aios-core/aios_core/config.py` | Default `llm_provider=watsonx`, added `qdrant_api_key`, `qdrant_use_tls`, `redis_tls_cert_path`, fixed service URLs to `localhost` |
| `services/memory/memory/vector/client.py` | TLS + API key auth for Qdrant Cloud |
| `services/gateway/gateway/main.py` | Replaced hardcoded `http://interface:8001` with `settings.interface_service_url` |
| `docker-compose.yml` | Emptied — `services: {}` with explanatory comment |
| `.env.ibmcloud.example` | Complete rewrite — numbered setup steps for all 5 services |
| `IBM-CLOUD-SETUP.md` | Complete rewrite — Neo4j + Qdrant Cloud + ICD Redis + watsonx |
| `HOW-TO-RUN.md` | No-Docker only guide |
| `run-local.ps1` | No Docker checks; cloud credential verification instead |
| `run-local.sh` | Same for Mac/Linux |
| `scripts/verify_ibmcloud.py` | Now checks all 5 services: watsonx + Neo4j + Qdrant + Redis + GitHub |

---

## Phase 2 — Production Hardening (Next Steps)

### P2.1 Security
- [ ] Replace Bearer token auth with OAuth2 / JWT
- [ ] RBAC enforcement at Memory Service layer
- [ ] Webhook verification for Linear + Slack
- [ ] IBM Secrets Manager instead of `.env` file

### P2.2 Reliability
- [ ] Circuit breakers on all external API calls
- [ ] Dead-letter queue for failed enrichment events
- [ ] Retry with exponential backoff (IBM IAM, watsonx.ai, graph writes)

### P2.3 Observability
- [ ] Structured JSON logging with correlation IDs
- [ ] Prometheus metrics on each service
- [ ] OpenTelemetry distributed tracing

### P2.4 Scale
- [ ] Replace IBM ICD Redis Pub/Sub with IBM Event Streams (Kafka)
- [ ] Async batch embedding in enrichment pipeline
- [ ] Neo4j AuraDB Business tier (auto-scaling, backups)
- [ ] Qdrant Cloud Production tier (dedicated cluster)

---

## Success Criteria

- [ ] `uv run python scripts/verify_ibmcloud.py` → 6 green ✅
- [ ] `.\run-local.ps1` starts all 7 services without errors
- [ ] `scripts/backfill.py --days 7` ingests ≥1 event
- [ ] Neo4j AuraDB shows >0 nodes after enrichment
- [ ] Qdrant Cloud shows >0 vectors after enrichment
- [ ] Natural-language query returns grounded answer with sources
- [ ] `uv run pytest tests/unit/ -v` → 15+ tests pass
