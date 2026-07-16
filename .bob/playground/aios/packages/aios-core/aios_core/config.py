"""
Application settings — loaded from environment / .env file.

All services import from this module.

NO DOCKER REQUIRED.
Every infrastructure dependency runs as a managed cloud service:

  LLM + Embeddings  →  IBM watsonx.ai         (IBM Cloud)
  Knowledge Graph   →  Neo4j AuraDB            (cloud.neo4j.com — free tier)
  Vector Search     →  Qdrant Cloud            (cloud.qdrant.io — free tier)
  Message Bus       →  IBM Cloud Databases for Redis  (ICD Redis — Lite plan)

All connection strings are plain URLs read from environment variables.
No container orchestration. No local databases. No GPU.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Environment ───────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # ── LLM Provider ──────────────────────────────────────────────
    # "watsonx" = IBM watsonx.ai (no Docker, no GPU, recommended)
    # "openai" or "anthropic" = alternative cloud providers
    llm_provider: Literal["ollama", "openai", "anthropic", "watsonx"] = "watsonx"

    # Legacy Ollama settings (only used if llm_provider=ollama)
    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "llama3.1:8b"
    ollama_embed_model: str = "nomic-embed-text"

    openai_api_key: str = ""
    openai_default_model: str = "gpt-4o"
    openai_embed_model: str = "text-embedding-3-small"

    anthropic_api_key: str = ""
    anthropic_default_model: str = "claude-3-5-sonnet-20241022"

    # ── IBM watsonx.ai ────────────────────────────────────────────
    # Get these from IBM-CLOUD-SETUP.md
    ibm_api_key: str = ""
    # Region endpoints:
    #   Dallas (us-south) : https://us-south.ml.cloud.ibm.com
    #   Frankfurt (eu-de) : https://eu-de.ml.cloud.ibm.com
    #   Tokyo (jp-tok)    : https://jp-tok.ml.cloud.ibm.com
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_project_id: str = ""
    watsonx_default_model: str = "ibm/granite-3-3-8b-instruct"
    watsonx_embed_model: str = "ibm/slate-125m-english-rtrvr-v2"
    # slate-125m produces 768-dim vectors → matches qdrant_vector_size below

    # ── Neo4j AuraDB (cloud.neo4j.com — free tier) ────────────────
    # Format:  neo4j+s://<instanceid>.databases.neo4j.io
    # Get from: cloud.neo4j.com → your instance → Connect → URI
    neo4j_uri: str = "neo4j+s://xxxxxxxx.databases.neo4j.io"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""    # Set in .env — from AuraDB instance credentials

    # ── Qdrant Cloud (cloud.qdrant.io — free tier) ────────────────
    # Format for host: <cluster-id>.<region>.aws.cloud.qdrant.io
    # Get from: cloud.qdrant.io → your cluster → Dashboard → Connection details
    qdrant_host: str = "xxxxxxxx.us-east4-0.gcp.cloud.qdrant.io"
    qdrant_port: int = 6333
    qdrant_api_key: str = ""    # Set in .env — from Qdrant Cloud dashboard
    qdrant_use_tls: bool = True # Always True for Qdrant Cloud
    qdrant_collection_name: str = "aios_chunks"
    qdrant_vector_size: int = 768   # slate-125m-english-rtrvr-v2 output dimension

    # ── IBM Cloud Databases for Redis (ICD Redis) ─────────────────
    # Format: rediss://:<password>@<host>:<port>/0
    # "rediss://" (with double-s) = TLS-encrypted Redis connection
    # Get from: cloud.ibm.com → Databases for Redis → your instance → Credentials
    redis_url: str = "rediss://:password@host.databases.appdomain.cloud:port/0"
    # IBM ICD Redis also provides a CA certificate for TLS verification.
    # Set REDIS_TLS_CERT_PATH to the path of the downloaded cert, or leave empty
    # to use the system CA bundle (works for most ICD Redis instances).
    redis_tls_cert_path: str = ""

    # ── Source Systems ────────────────────────────────────────────
    github_app_id: str = ""
    github_app_private_key: str = ""        # GitHub PAT or App private key
    github_webhook_secret: str = ""
    github_target_repos: str = ""           # comma-separated: org/repo1,org/repo2

    linear_api_key: str = ""
    linear_webhook_secret: str = ""
    linear_team_ids: str = ""               # comma-separated

    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_target_channels: str = ""         # comma-separated channel names

    # ── Security ─────────────────────────────────────────────────
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    api_key_salt: str = "change-me-in-production"
    jwt_secret: str = "change-me-in-production"

    # ── Operational ───────────────────────────────────────────────
    er_confidence_threshold: float = 0.85
    monitor_poll_interval_minutes: int = 5
    staleness_threshold_days: int = 30
    max_retrieval_hops: int = 4
    answer_confidence_threshold: float = 0.7

    # ── Inter-service URLs (local Python processes) ───────────────
    # When running with run-local.ps1, all services run on localhost
    memory_service_url: str = "http://localhost:8002"
    agents_service_url: str = "http://localhost:8003"
    interface_service_url: str = "http://localhost:8001"
    connectors_service_url: str = "http://localhost:8010"

    # ── Computed helpers ──────────────────────────────────────────
    @property
    def github_repos(self) -> list[str]:
        return [r.strip() for r in self.github_target_repos.split(",") if r.strip()]

    @property
    def linear_teams(self) -> list[str]:
        return [t.strip() for t in self.linear_team_ids.split(",") if t.strip()]

    @property
    def slack_channels(self) -> list[str]:
        return [c.strip() for c in self.slack_target_channels.split(",") if c.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Convenience singleton — `from aios_core.config import settings`
settings = get_settings()
