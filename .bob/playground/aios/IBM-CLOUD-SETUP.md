# IBM Cloud + Cloud Services Setup Guide
# AI Operating System for Companies — No Docker Required

**Total time:** ~30 minutes  
**Docker needed:** ❌ None  
**GPU needed:** ❌ None  
**Credit card needed:** ❌ None (all free tiers)

Everything runs as managed cloud services. You configure 5 cloud accounts once, paste the connection details into your `.env` file, and run the app locally with `run-local.ps1`.

---

## What You Need to Set Up

| # | Service | Provider | What It Does | Cost | Time |
|---|---------|----------|-------------|------|------|
| 1 | LLM + Embeddings | **IBM watsonx.ai** | Answers questions, generates embeddings | Free Lite | 10 min |
| 2 | Knowledge Graph | **Neo4j AuraDB** | Stores entity relationships | Free Forever | 5 min |
| 3 | Vector Search | **Qdrant Cloud** | Semantic search index | Free (1 GB) | 5 min |
| 4 | Message Bus | **IBM ICD Redis** | Event queue between services | Free Lite | 5 min |
| 5 | Source Data | **GitHub** | Your repos, PRs, issues | Free | 3 min |

---

## Step 1 — IBM watsonx.ai (LLM + Embeddings)

### 1A — IBM Cloud API Key

1. Go to **https://cloud.ibm.com** and log in (or create a free account)
2. Click your **profile icon** (top-right corner) → **"Profile and settings"**
3. Click the **"API keys"** tab
4. Click **"Create an IBM Cloud API key"**
5. Name it `aios-dev` → click **"Create"**
6. ⚠️ **COPY IT IMMEDIATELY** — it shows only once
7. If you miss it, create a new one — you cannot retrieve it

→ Paste into `.env` as `IBM_API_KEY=...`

### 1B — Provision watsonx.ai

1. From the IBM Cloud dashboard, click **"Catalog"** (top nav)
2. Search for **"watsonx.ai"** and click it
3. Select the **"Lite"** plan (free, no credit card)
4. Choose a region — pick **Dallas (us-south)** unless you're in Europe (use Frankfurt)
5. Click **"Create"**

→ Set `WATSONX_URL=https://us-south.ml.cloud.ibm.com` in `.env` (or your region)

### 1C — Create a watsonx.ai Project

1. Go to **https://dataplatform.cloud.ibm.com** (or click "Launch watsonx" from your service)
2. Click **"New project"** → **"Create an empty project"**
3. Give it a name (e.g. `aios-prototype`)
4. If asked for storage: click "Create" next to Cloud Object Storage → it creates a free Lite instance automatically
5. Click **"Create project"**

### 1D — Get Your Project ID

1. Inside your project, click the **"Manage"** tab
2. Look for **"Project ID"** — a UUID like `a1b2c3d4-e5f6-7890-abcd-ef1234567890`
3. Copy it

→ Paste into `.env` as `WATSONX_PROJECT_ID=...`

### Verify IBM watsonx.ai

```powershell
cd "C:\Users\SettiYeswanth\OneDrive - IBM\Desktop\AI Operating System for Companies\.bob\playground\aios"
uv run python scripts/verify_ibmcloud.py
```

Expected output:
```
✅ IBM IAM token acquired  (expires in 3600s)
✅ Text generation:  "The capital of France is Paris."
✅ Embeddings:       768-dimensional vector [0.0234, ...]
✅ IBM watsonx.ai — all checks passed
```

---

## Step 2 — Neo4j AuraDB (Knowledge Graph)

1. Go to **https://cloud.neo4j.com**
2. Sign up for a free account (no credit card)
3. Click **"New instance"**
4. Choose **"AuraDB Free"** (the free forever tier)
5. Select a region close to you
6. Click **"Create"**
7. Wait ~2 minutes while provisioning

When it finishes, a credentials dialog appears — **copy ALL three values immediately**:

```
Username:           neo4j
Password:           <random generated — copy it now>
Connection URI:     neo4j+s://abcd1234.databases.neo4j.io
```

⚠️ **If you close the dialog without saving, you must reset the password from the instance dashboard.**

→ Paste into `.env`:
```dotenv
NEO4J_URI=neo4j+s://abcd1234.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=<the generated password>
```

**Limits of AuraDB Free:**
- 200,000 nodes, 400,000 relationships
- 1 free instance per account
- No backups (fine for prototype)

---

## Step 3 — Qdrant Cloud (Vector Search)

1. Go to **https://cloud.qdrant.io**
2. Sign up (Google/GitHub/email — no credit card)
3. Click **"Create cluster"**
4. Choose the **"Free"** tier
5. Name it `aios` → pick a region → click **"Create"**
6. Wait ~1 minute

Get your connection details:
1. Click on your cluster name
2. Click the **"Dashboard"** tab
3. Find **"Connection details"**:
   - **Host:** looks like `xxxxxxxx.us-east4-0.gcp.cloud.qdrant.io`
   - **Port:** `6333`
4. Click **"API Keys"** tab → **"Create API Key"** → copy the key

→ Paste into `.env`:
```dotenv
QDRANT_HOST=xxxxxxxx.us-east4-0.gcp.cloud.qdrant.io
QDRANT_PORT=6333
QDRANT_API_KEY=<your api key>
QDRANT_USE_TLS=true
```

**Limits of Qdrant Cloud Free:**
- 1 GB storage (holds ~500,000 vectors at 768-dim)
- 1 cluster

---

## Step 4 — IBM Cloud Databases for Redis (Message Bus)

1. Go to **https://cloud.ibm.com/catalog/services/databases-for-redis**
2. Choose the **"Lite"** plan (free)
3. Pick a region → click **"Create"**
4. Wait ~5 minutes (you'll see a loading indicator)
5. Once ready, open your instance
6. Click **"Credentials"** tab → **"New credential"** → leave defaults → click **"Add"**
7. Expand the credential you just created → click **"View credential JSON"**
8. Find the connection section — it looks like:

```json
{
  "connection": {
    "rediss": {
      "hosts": [{ "hostname": "abc123.databases.appdomain.cloud", "port": 12345 }],
      "authentication": { "password": "yourpassword" }
    }
  }
}
```

Build your Redis URL:
```
rediss://:<password>@<hostname>:<port>/0
```

Note: `rediss://` with **double s** = TLS mode. The colon before the password is required.

Example:
```
rediss://:mypassword@abc123.databases.appdomain.cloud:12345/0
```

→ Paste into `.env` as `REDIS_URL=rediss://:<password>@<host>:<port>/0`

**Optional — TLS Certificate:**
- From your ICD Redis instance, click **"Overview"** → **"TLS Certificate"** → **"Download"**
- Save to somewhere like `C:\Users\YourName\aios-redis.pem`
- Set `REDIS_TLS_CERT_PATH=C:\Users\YourName\aios-redis.pem` in `.env`
- If you skip this, Python uses system CAs which works for most ICD Redis instances

---

## Step 5 — GitHub (Source Data)

1. Go to **https://github.com/settings/tokens**
2. Click **"Generate new token (classic)"**
3. Name it `aios-prototype`
4. Check these scopes: `repo` and `read:org`
5. Set expiration to 90 days or "No expiration"
6. Click **"Generate token"**
7. Copy the `ghp_...` token

→ Paste into `.env`:
```dotenv
GITHUB_APP_PRIVATE_KEY=ghp_your_token_here
GITHUB_TARGET_REPOS=your-github-username/your-repo-name
```

You can use any public or private repo you have access to.

---

## Step 6 — Create Your .env File

```powershell
cd "C:\Users\SettiYeswanth\OneDrive - IBM\Desktop\AI Operating System for Companies\.bob\playground\aios"
Copy-Item .env.ibmcloud.example .env
```

Open `.env` in VS Code or Notepad and fill in all values marked `← FILL THIS IN`.

The file has step-by-step comments for every value.

---

## Step 7 — Verify Everything Works

```powershell
cd "C:\Users\SettiYeswanth\OneDrive - IBM\Desktop\AI Operating System for Companies\.bob\playground\aios"
uv run python scripts/verify_ibmcloud.py
```

This checks all 5 services:
```
✅ IBM IAM token acquired
✅ Text generation working
✅ Embeddings working
✅ Neo4j AuraDB connected
✅ Qdrant Cloud connected
✅ IBM ICD Redis connected
✅ All services verified — run .\run-local.ps1 to start
```

---

## Step 8 — Run the Application

```powershell
# Seed the knowledge graph schema (run once after Step 7)
uv run python scripts/seed_graph.py

# Start all 7 application services
.\run-local.ps1

# Ingest your GitHub data (run once to populate the knowledge graph)
uv run python scripts/backfill.py --days 30

# Query the system
Invoke-RestMethod http://localhost:8001/v1/chat -Method Post `
  -Headers @{"Authorization"="Bearer test"} `
  -ContentType "application/json" `
  -Body '{"query":"What has been worked on this week?","stream":false}'
```

---

## Troubleshooting

### IBM watsonx.ai — `401 Unauthorized`
Your `IBM_API_KEY` is invalid or has been deleted.  
→ Create a new key at cloud.ibm.com → Profile → API keys

### IBM watsonx.ai — `400 project_id not found`
Your `WATSONX_PROJECT_ID` is wrong or does not exist.  
→ Open your project at dataplatform.cloud.ibm.com → Manage → General → copy the UUID

### Neo4j — `ServiceUnavailable`
Your `NEO4J_URI` or credentials are wrong.  
→ Check cloud.neo4j.com → your instance → Connect → URI  
→ Make sure you use `neo4j+s://` (TLS) not `bolt://`  
→ If you lost the password, click "Reset password" on the instance dashboard

### Qdrant — `Connection refused` or `Unauthorized`
- Check `QDRANT_HOST` is the full hostname (not just the cluster ID)
- Check `QDRANT_API_KEY` is set correctly
- Check `QDRANT_USE_TLS=true`

### Redis — `Connection refused`
- Check `REDIS_URL` uses `rediss://` (double s) for TLS
- Check the URL format: `rediss://:<password>@<host>:<port>/0`
- The colon before the password is **required** even with no username

### Redis — `SSL certificate verify failed`
Download the TLS certificate from your ICD Redis instance and set:
```dotenv
REDIS_TLS_CERT_PATH=C:\path\to\certificate.pem
```

### `verify_ibmcloud.py` shows all ✅ but the app fails to start
Run with verbose logging:
```powershell
$env:LOG_LEVEL="DEBUG"; .\run-local.ps1
```
Then check the log files in `.aios-local-logs\`.

---

## Service URLs (after `run-local.ps1` starts everything)

| Service | URL |
|---------|-----|
| Chat interface | http://localhost:8001 |
| API gateway | http://localhost:8000 |
| Memory API | http://localhost:8002 |
| Neo4j AuraDB browser | https://browser.neo4j.io |
| Qdrant Cloud dashboard | https://cloud.qdrant.io |
