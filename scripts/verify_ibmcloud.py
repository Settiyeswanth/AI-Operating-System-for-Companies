"""
verify_ibmcloud.py — Verify ALL cloud services before starting the AI OS.

Checks all 5 required services:
  1. IBM watsonx.ai — IAM auth + text generation + embeddings
  2. Neo4j AuraDB   — graph database connection
  3. Qdrant Cloud   — vector database connection
  4. IBM ICD Redis  — message bus connection
  5. GitHub         — source data API (optional but recommended)

Usage:
    uv run python scripts/verify_ibmcloud.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# ── Load .env before importing aios_core ──────────────────────────────────
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "packages" / "aios-core"))

env_file = project_root / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if value:
                os.environ.setdefault(key, value)
else:
    print("⚠️  No .env file found at:", env_file)
    print("   Run:  Copy-Item .env.ibmcloud.example .env")
    sys.exit(1)

RESET  = "\033[0m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"


def ok(msg: str)   -> None: print(f"  {GREEN}✅{RESET} {msg}")
def fail(msg: str) -> None: print(f"  {RED}❌{RESET} {msg}")
def warn(msg: str) -> None: print(f"  {YELLOW}⚠️ {RESET} {msg}")
def info(msg: str) -> None: print(f"     {msg}")
def hdr(msg: str)  -> None: print(f"\n{CYAN}── {msg} ──{RESET}")


# ──────────────────────────────────────────────────────────────────────────────
# 1. IBM watsonx.ai
# ──────────────────────────────────────────────────────────────────────────────

async def check_ibm_watsonx(api_key: str, base_url: str, project_id: str, model: str, embed_model: str) -> bool:
    import httpx
    hdr("IBM watsonx.ai")

    # Step 1: IAM token
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://iam.cloud.ibm.com/identity/token",
                data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": api_key},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            expires = resp.json().get("expires_in", 3600)
        ok(f"IBM IAM token acquired (expires in {expires}s)")
    except Exception as e:
        fail(f"IBM IAM auth failed: {e}")
        info("Fix: check IBM_API_KEY in .env — create at cloud.ibm.com → Profile → API keys")
        return False

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Step 2: Text generation
    try:
        prompt = "<|system|>\nYou are helpful.\n<|end_of_text|>\n<|user|>\nSay hello in 5 words.\n<|end_of_text|>\n<|assistant|>"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/ml/v1/text/generation?version=2024-05-01",
                json={"model_id": model, "input": prompt, "project_id": project_id,
                      "parameters": {"decoding_method": "greedy", "max_new_tokens": 32}},
                headers=headers,
            )
            resp.raise_for_status()
            text = resp.json()["results"][0]["generated_text"].strip()
        ok(f'Text generation: "{text[:60]}"')
    except Exception as e:
        fail(f"Text generation failed: {e}")
        info("Fix: check WATSONX_PROJECT_ID and WATSONX_URL in .env")
        return False

    # Step 3: Embeddings
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/ml/v1/text/embeddings?version=2024-05-01",
                json={"model_id": embed_model, "inputs": ["test sentence"], "project_id": project_id},
                headers=headers,
            )
            resp.raise_for_status()
            dim = len(resp.json()["results"][0]["embedding"])
        ok(f"Embeddings: {dim}-dimensional vector")
        if dim != 768:
            warn(f"Expected 768-dim but got {dim} — update QDRANT_VECTOR_SIZE in .env if changed")
    except Exception as e:
        fail(f"Embeddings failed: {e}")
        return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# 2. Neo4j AuraDB
# ──────────────────────────────────────────────────────────────────────────────

async def check_neo4j(uri: str, user: str, password: str) -> bool:
    hdr("Neo4j AuraDB")
    if "xxxxxxxx" in uri or not password:
        fail("NEO4J_URI or NEO4J_PASSWORD not set — fill in .env")
        info("Get from: cloud.neo4j.com → your instance → Connect")
        return False
    try:
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        await driver.verify_connectivity()
        async with driver.session() as s:
            result = await s.run("RETURN 1 AS n")
            record = await result.single()
            assert record["n"] == 1
        await driver.close()
        ok(f"Connected: {uri}")
        return True
    except Exception as e:
        fail(f"Neo4j connection failed: {e}")
        info("Fix: check NEO4J_URI uses neo4j+s:// (TLS) and NEO4J_PASSWORD is correct")
        info("    If password forgotten: cloud.neo4j.com → instance → Reset password")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 3. Qdrant Cloud
# ──────────────────────────────────────────────────────────────────────────────

async def check_qdrant(host: str, port: int, api_key: str, use_tls: bool) -> bool:
    hdr("Qdrant Cloud")
    if "xxxxxxxx" in host or not api_key:
        fail("QDRANT_HOST or QDRANT_API_KEY not set — fill in .env")
        info("Get from: cloud.qdrant.io → your cluster → Dashboard → Connection details")
        return False
    try:
        from qdrant_client import AsyncQdrantClient
        client = AsyncQdrantClient(host=host, port=port, api_key=api_key, https=use_tls)
        collections = await client.get_collections()
        await client.close()
        names = [c.name for c in collections.collections]
        ok(f"Connected: {host}:{port}  (collections: {names or 'none yet'})")
        return True
    except Exception as e:
        fail(f"Qdrant connection failed: {e}")
        info("Fix: check QDRANT_HOST is the full hostname (not just cluster ID)")
        info("     check QDRANT_API_KEY and QDRANT_USE_TLS=true")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 4. IBM ICD Redis
# ──────────────────────────────────────────────────────────────────────────────

async def check_redis(redis_url: str, cert_path: str) -> bool:
    hdr("IBM Cloud Databases for Redis")
    if "host.databases" in redis_url or "yourpassword" in redis_url:
        fail("REDIS_URL not configured — fill in .env")
        info("Get from: cloud.ibm.com → Databases for Redis → your instance → Credentials")
        info("Format:   rediss://:<password>@<host>:<port>/0")
        return False
    try:
        import redis.asyncio as aioredis
        import ssl

        kwargs: dict = {}
        if cert_path and Path(cert_path).exists():
            ssl_ctx = ssl.create_default_context(cafile=cert_path)
            kwargs["ssl_context"] = ssl_ctx
        elif redis_url.startswith("rediss://"):
            # Use system CAs — works for most ICD Redis instances
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE  # acceptable for dev
            kwargs["ssl_context"] = ssl_ctx

        r = aioredis.from_url(redis_url, decode_responses=True, **kwargs)
        pong = await r.ping()
        await r.aclose()
        ok(f"Connected (PING → {pong})")
        return True
    except Exception as e:
        fail(f"Redis connection failed: {e}")
        info("Fix: check REDIS_URL format: rediss://:<password>@<host>:<port>/0")
        info("     Note the colon before the password — no username")
        info("     If SSL error: download TLS cert from ICD Redis instance and set REDIS_TLS_CERT_PATH")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 5. GitHub (optional)
# ──────────────────────────────────────────────────────────────────────────────

async def check_github(token: str, repos: str) -> bool:
    hdr("GitHub (source data)")
    if not token:
        warn("GITHUB_APP_PRIVATE_KEY not set — no GitHub data will be ingested")
        info("Optional: github.com → Settings → Developer Settings → Personal Access Tokens")
        return True  # not fatal — warn only
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "aios/0.1"},
            )
            resp.raise_for_status()
            login = resp.json().get("login", "?")
        ok(f"GitHub authenticated as: {login}")
        if repos and "your-org" not in repos:
            ok(f"Target repos: {repos}")
        else:
            warn("GITHUB_TARGET_REPOS not set — set to your-username/your-repo in .env")
        return True
    except Exception as e:
        fail(f"GitHub auth failed: {e}")
        info("Fix: check GITHUB_APP_PRIVATE_KEY (starts with ghp_)")
        return False  # only warn, not fatal


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  AI OS — Cloud Services Verification         {RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════{RESET}")

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    # Read all settings
    api_key      = get("IBM_API_KEY")
    wx_url       = get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com").rstrip("/")
    project_id   = get("WATSONX_PROJECT_ID")
    model        = get("WATSONX_DEFAULT_MODEL", "ibm/granite-3-3-8b-instruct")
    embed_model  = get("WATSONX_EMBED_MODEL",   "ibm/slate-125m-english-rtrvr-v2")
    neo4j_uri    = get("NEO4J_URI",  "neo4j+s://xxxxxxxx.databases.neo4j.io")
    neo4j_user   = get("NEO4J_USER", "neo4j")
    neo4j_pw     = get("NEO4J_PASSWORD")
    qdrant_host  = get("QDRANT_HOST", "xxxxxxxx.cloud.qdrant.io")
    qdrant_port  = int(get("QDRANT_PORT", "6333"))
    qdrant_key   = get("QDRANT_API_KEY")
    qdrant_tls   = get("QDRANT_USE_TLS", "true").lower() == "true"
    redis_url    = get("REDIS_URL", "rediss://:password@host.databases.appdomain.cloud:12345/0")
    redis_cert   = get("REDIS_TLS_CERT_PATH")
    gh_token     = get("GITHUB_APP_PRIVATE_KEY")
    gh_repos     = get("GITHUB_TARGET_REPOS")

    # Pre-flight: required vars
    missing = []
    if not api_key:    missing.append("IBM_API_KEY")
    if not project_id: missing.append("WATSONX_PROJECT_ID")

    if missing:
        print()
        for m in missing:
            fail(f"{m} is not set in .env")
        print(f"\n{RED}Fill in your .env file and re-run. See IBM-CLOUD-SETUP.md.{RESET}")
        sys.exit(1)

    # Run all checks
    results: dict[str, bool] = {}
    results["IBM watsonx.ai"]  = await check_ibm_watsonx(api_key, wx_url, project_id, model, embed_model)
    results["Neo4j AuraDB"]    = await check_neo4j(neo4j_uri, neo4j_user, neo4j_pw)
    results["Qdrant Cloud"]    = await check_qdrant(qdrant_host, qdrant_port, qdrant_key, qdrant_tls)
    results["IBM ICD Redis"]   = await check_redis(redis_url, redis_cert)
    results["GitHub"]          = await check_github(gh_token, gh_repos)

    # Summary
    print(f"\n{CYAN}── Summary ──────────────────────────────────────{RESET}")
    all_pass = True
    for name, passed in results.items():
        if passed:
            ok(name)
        else:
            fail(name)
            all_pass = False

    print()
    if all_pass:
        print(f"{GREEN}{BOLD}✅ All services verified! You can now run:{RESET}")
        print(f"   {BOLD}uv run python scripts/seed_graph.py{RESET}")
        print(f"   {BOLD}.\\run-local.ps1{RESET}")
    else:
        print(f"{RED}{BOLD}❌ Some services failed. Fix the errors above and re-run.{RESET}")
        print("   See IBM-CLOUD-SETUP.md for detailed setup instructions.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
