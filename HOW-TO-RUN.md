# AI OS for Companies — Run Guide
# No Docker · IBM Cloud · Pure Python

> **One-sentence summary:** Set up 5 free cloud accounts, fill in `.env`, run `.\run-local.ps1`.

---

## What Runs Where

| Component | Where | Docker? |
|-----------|-------|---------|
| LLM (Granite 3.3) | IBM watsonx.ai (cloud) | ❌ No |
| Embeddings (768-dim) | IBM watsonx.ai (cloud) | ❌ No |
| Knowledge Graph | Neo4j AuraDB (cloud) | ❌ No |
| Vector Search | Qdrant Cloud (cloud) | ❌ No |
| Message Bus (Redis) | IBM ICD Redis (cloud) | ❌ No |
| App Services (7 services) | Local Python via `run-local.ps1` | ❌ No |

**Total RAM needed on your machine:** ~500 MB  
**Total Disk needed:** ~200 MB (Python packages only)

---

## Software You Need on Your Machine

Only **two things**:

| Tool | Why | Install |
|------|-----|---------|
| Python 3.11+ | Runs all 7 app services | https://www.python.org/downloads/ |
| `uv` | Python package manager | See below |

You already have: ✅ Python 3.14 · ✅ uv 0.11

Install `uv` if not already installed:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

That's it. No Docker. No Node. No database installs.

---

## Part A — First-Time Setup (do this once)

### A1 — Set up 5 cloud services

Follow **IBM-CLOUD-SETUP.md** step by step. It takes ~30 minutes total and every service has a free tier.

Services to create (in order):
1. IBM watsonx.ai (LLM + Embeddings)
2. Neo4j AuraDB Free (knowledge graph)
3. Qdrant Cloud Free (vector search)
4. IBM Cloud Databases for Redis Lite (message bus)
5. GitHub Personal Access Token (source data)

### A2 — Create your .env file

```powershell
cd "C:\Users\SettiYeswanth\OneDrive - IBM\Desktop\AI Operating System for Companies\.bob\playground\aios"
Copy-Item .env.ibmcloud.example .env
```

Open `.env` and fill in every value marked `← FILL THIS IN`.  
The file has inline instructions for every field.

### A3 — Install Python dependencies

```powershell
cd "C:\Users\SettiYeswanth\OneDrive - IBM\Desktop\AI Operating System for Companies\.bob\playground\aios"
uv sync
```

### A4 — Verify all cloud services

```powershell
uv run python scripts/verify_ibmcloud.py
```

All 6 checks must show ✅ before continuing.

### A5 — Seed the knowledge graph schema (run once)

```powershell
uv run python scripts/seed_graph.py
```

Expected:
```
INFO  Connecting to Neo4j AuraDB...
INFO  Creating schema (constraints + indices)...
INFO  Graph schema bootstrap complete
INFO  Qdrant collection 'aios_chunks' ready
INFO  ✓ Seed complete
```

---

## Part B — Running the Application

### Start all 7 services

```powershell
.\run-local.ps1
```

Expected output:
```
✅ .env file found
✅ uv found: 0.11.28
✅ Neo4j AuraDB connected
✅ Qdrant Cloud connected
✅ IBM ICD Redis connected
✅ Dependencies verified
✅ Started memory      → http://localhost:8002  [PID 1234]
✅ Started agents      → background              [PID 1235]
✅ Started connectors  → background              [PID 1236]
✅ Started ingestion   → background              [PID 1237]
✅ Started enrichment  → background              [PID 1238]
✅ Started interface   → http://localhost:8001   [PID 1239]
✅ Started gateway     → http://localhost:8000   [PID 1240]

AI OS is Running!
  Chat:     http://localhost:8001
  Gateway:  http://localhost:8000
```

### Stop all services

```powershell
.\run-local.ps1 -Stop
```

### Check status

```powershell
.\run-local.ps1 -Status
```

### View logs

```powershell
.\run-local.ps1 -Logs              # all services
.\run-local.ps1 -Logs enrichment   # one service
```

---

## Part C — Ingest Data and Query

### Ingest historical data (first time)

```powershell
uv run python scripts/backfill.py --days 30
```

This fetches the last 30 days from your GitHub repo (and Linear/Slack if configured) and pushes everything through the pipeline:

```
INFO  backfill  Running GitHub backfill (last 30 days)...
INFO  backfill  GitHub: 124 events published
INFO  backfill  Backfill complete — 124 total events
```

Wait 1–2 minutes for the enrichment pipeline to process events into Neo4j and Qdrant.

### Send your first query

**Windows PowerShell:**
```powershell
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test-key"} `
  -ContentType "application/json" `
  -Body '{"query":"What has been worked on this week?","stream":false}' | ConvertTo-Json
```

**curl (Mac/Linux/WSL):**
```bash
curl -s -X POST http://localhost:8001/v1/chat \
  -H "Authorization: Bearer test-key" \
  -H "Content-Type: application/json" \
  -d '{"query": "What has been worked on this week?", "stream": false}' | python -m json.tool
```

Expected response:
```json
{
  "answer": "Based on the knowledge graph, the following work was done this week...",
  "sources": [{"source_system": "github", "source_url": "..."}],
  "verdict": "pass",
  "confidence": 0.87
}
```

---

## Part D — More Queries

```powershell
# Who worked on what?
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test-key"} `
  -ContentType "application/json" `
  -Body '{"query":"Who worked on the authentication service?","stream":false}'

# What decisions were made?
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test-key"} `
  -ContentType "application/json" `
  -Body '{"query":"What decisions were made last month?","stream":false}'

# What is blocked?
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test-key"} `
  -ContentType "application/json" `
  -Body '{"query":"What features are currently blocked?","stream":false}'

# Check misalignment alerts
Invoke-RestMethod http://localhost:8001/v1/alerts `
  -Headers @{"Authorization"="Bearer test-key"}
```

---

## Part E — Run Tests

```powershell
uv run pytest tests/unit/ -v
```

Expected: 15+ tests pass in ~3 seconds. No network connections needed.

---

## Part F — Service Ports

| Service | Port | URL |
|---------|------|-----|
| Gateway (entry point) | 8000 | http://localhost:8000 |
| Interface (chat API) | 8001 | http://localhost:8001 |
| Memory API | 8002 | http://localhost:8002 |
| Agents (background) | — | Internal Redis queue |
| Connectors (background) | — | Polls GitHub/Linear/Slack |
| Ingestion (background) | — | Redis subscriber |
| Enrichment (background) | — | Redis subscriber |

---

## Part G — Troubleshooting

### `run-local.ps1` fails with "execution policy" error

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\run-local.ps1
```

### Services start but queries return empty answers

The enrichment pipeline hasn't processed any events yet:

```powershell
# Run backfill first
uv run python scripts/backfill.py --days 30

# Watch enrichment process events
.\run-local.ps1 -Logs enrichment
```

### IBM watsonx.ai errors

```powershell
uv run python scripts/verify_ibmcloud.py
```

See `IBM-CLOUD-SETUP.md § Troubleshooting` for specific error codes.

### Neo4j connection fails

- Check `NEO4J_URI` uses `neo4j+s://` not `bolt://`
- AuraDB Free requires TLS (`neo4j+s://`)
- Password: check cloud.neo4j.com → your instance → Reset password if needed

### Redis connection fails

- Check `REDIS_URL` uses `rediss://` (double s = TLS)
- Format: `rediss://:<password>@<host>:<port>/0`
- Note: there's a colon before the password (no username field)

### View logs for any service

```powershell
# All logs
Get-Content .aios-local-logs\*.log -Wait

# One service
Get-Content .aios-local-logs\enrichment.log -Wait -Tail 50
```

---

## Part H — Quick Reference Summary

```
FIRST TIME (30 min):
  1. Create 5 cloud accounts — follow IBM-CLOUD-SETUP.md
  2. cp .env.ibmcloud.example .env → fill in values
  3. uv sync
  4. uv run python scripts/verify_ibmcloud.py  ← must all pass
  5. uv run python scripts/seed_graph.py

EVERY RUN:
  1. .\run-local.ps1
  2. uv run python scripts/backfill.py --days 7  ← first time only
  3. http://localhost:8001/v1/chat

STOP:
  .\run-local.ps1 -Stop
```
