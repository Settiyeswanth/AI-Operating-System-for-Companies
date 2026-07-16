# AI OS for Companies — Prototype

An AI intelligence layer that makes your company's knowledge queryable.
Connects GitHub, Linear, and Slack to a knowledge graph, answers organizational questions, and detects misalignment between decisions and execution.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- ~8GB RAM (for Ollama running llama3.1:8b)
- GitHub App or PAT, Linear API key, Slack Bot Token

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 2. Start infrastructure

```bash
docker compose up neo4j qdrant redis ollama -d
# Wait ~30 seconds for all services to be healthy
```

### 3. Pull Ollama models (first time only)

```bash
docker exec aios-ollama ollama pull llama3.1:8b
docker exec aios-ollama ollama pull nomic-embed-text
# This takes 5–15 minutes depending on your connection
```

### 4. Seed the graph

```bash
docker exec aios-ingestion python scripts/seed_graph.py
# Creates Neo4j schema, constraints, indices, and system node
```

### 5. Start all services

```bash
docker compose up -d
```

### 6. Run the first poll (ingest last 7 days)

```bash
# The connectors service polls automatically every 5 minutes
# To trigger an immediate historical backfill:
docker exec aios-connectors python scripts/backfill.py --days 7
```

### 7. Query the system

```bash
curl -X POST http://localhost:8001/v1/chat \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"query": "Who worked on the authentication service last sprint?", "stream": false}'
```

### 8. View alerts

```bash
curl http://localhost:8001/v1/alerts \
  -H "Authorization: Bearer your-token"
```

## Architecture

```
GitHub + Linear + Slack
    ↓ (webhooks + 5-min polling)
Connectors Service (port 8010)
    ↓ Redis Pub/Sub
Ingestion Service → Entity Resolution → Event Ledger (SQLite)
    ↓
Enrichment Service → Knowledge Graph (Neo4j) + Vector Store (Qdrant)
    ↓
Agents Service (QueryAgent + MonitorAgent)
    ↓
Interface API (port 8001) → You
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| interface | 8001 | Chat + Alerts REST API |
| connectors | 8010 | Webhook receiver + polling |
| memory | 8002 | Graph + Vector internal API |
| neo4j | 7474/7687 | Knowledge Graph |
| qdrant | 6333 | Vector store |
| redis | 6379 | Task queue + pub/sub |
| ollama | 11434 | Local LLM |

## Running Tests

```bash
cd tests
pip install -e ".[test]"
pytest unit/ -v
```

## Milestone Status

- [x] Milestone 0: Infrastructure + Schemas + LLM Gateway
- [x] Milestone 1: Ingestion Pipeline (GitHub + Linear + Slack connectors)
- [ ] Milestone 2: Knowledge Graph Populated (run backfill.py)
- [ ] Milestone 3: QueryAgent Live
- [ ] Milestone 4: MonitorAgent Live
- [ ] Milestone 5: End-to-end Demo

## Phase 2 Migration

Switch from local Ollama to OpenAI: `LLM_PROVIDER=openai` in `.env`.
All other migrations are similarly single-variable changes.
See `ai-os-engineering-blueprint.md` for the full upgrade path.
