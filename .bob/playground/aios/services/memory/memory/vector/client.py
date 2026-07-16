"""
Qdrant Cloud Vector Store client + Hybrid Retrieval (BM25 + dense).

Connects to Qdrant Cloud (cloud.qdrant.io) — no Docker required.
TLS-encrypted connection using API key from QDRANT_API_KEY env var.

Architecture:
  - Dense search:  IBM watsonx.ai slate-125m vectors (768-dim)
  - Sparse search: BM25 over the chunk corpus (in-memory, rebuilt on restart)
  - Fusion:        Reciprocal Rank Fusion (RRF) to merge both result sets
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)
from rank_bm25 import BM25Okapi

from aios_core.config import settings
from aios_core.schemas.entities import AccessTags
from aios_core.schemas.tasks import RetrievedChunk, ContextBundle, RetrievalMetadata

log = logging.getLogger(__name__)

COLLECTION = settings.qdrant_collection_name
VECTOR_SIZE = settings.qdrant_vector_size  # 768 for slate-125m-english-rtrvr-v2


@dataclass
class ChunkDoc:
    chunk_id: str
    source_artifact_id: str
    content: str
    source_system: str
    source_url: str | None
    timestamp: datetime
    entity_refs: list[str]
    access_tags: dict[str, Any]
    embedding: list[float]


class VectorClient:
    """
    Manages the Qdrant Cloud connection and the in-memory BM25 corpus.
    Connects via TLS using QDRANT_API_KEY — no Docker, no local Qdrant.
    """

    def __init__(self) -> None:
        self._client: AsyncQdrantClient | None = None
        self._bm25: BM25Okapi | None = None
        self._bm25_corpus: list[ChunkDoc] = []

    async def connect(self) -> None:
        """
        Connect to Qdrant Cloud.
        Uses QDRANT_HOST, QDRANT_PORT, QDRANT_API_KEY from settings.
        TLS is always enabled for Qdrant Cloud (QDRANT_USE_TLS=true).
        """
        if settings.qdrant_use_tls and settings.qdrant_api_key:
            # Qdrant Cloud: TLS + API key authentication
            self._client = AsyncQdrantClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                api_key=settings.qdrant_api_key,
                https=True,
            )
        else:
            # Local fallback (e.g. development without Qdrant Cloud)
            self._client = AsyncQdrantClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                https=False,
            )
        await self._ensure_collection()
        log.info(
            "Connected to Qdrant at %s:%s (TLS=%s)",
            settings.qdrant_host,
            settings.qdrant_port,
            settings.qdrant_use_tls,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def _ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if COLLECTION not in names:
            await self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            log.info("Created Qdrant collection: %s (dim=%d)", COLLECTION, VECTOR_SIZE)
        else:
            log.info("Qdrant collection '%s' already exists", COLLECTION)

    # ─────────────────────────────────────────────────────────────
    # Indexing
    # ─────────────────────────────────────────────────────────────

    async def upsert_chunk(self, doc: ChunkDoc) -> None:
        point = PointStruct(
            id=doc.chunk_id,
            vector=doc.embedding,
            payload={
                "source_artifact_id": doc.source_artifact_id,
                "content": doc.content,
                "source_system": doc.source_system,
                "source_url": doc.source_url,
                "timestamp": doc.timestamp.isoformat(),
                "entity_refs": doc.entity_refs,
                "access_tags": doc.access_tags,
            },
        )
        await self._client.upsert(collection_name=COLLECTION, points=[point])
        self._bm25_corpus.append(doc)
        self._rebuild_bm25()

    async def upsert_chunks_batch(self, docs: list[ChunkDoc]) -> None:
        points = [
            PointStruct(
                id=d.chunk_id,
                vector=d.embedding,
                payload={
                    "source_artifact_id": d.source_artifact_id,
                    "content": d.content,
                    "source_system": d.source_system,
                    "source_url": d.source_url,
                    "timestamp": d.timestamp.isoformat(),
                    "entity_refs": d.entity_refs,
                    "access_tags": d.access_tags,
                },
            )
            for d in docs
        ]
        await self._client.upsert(collection_name=COLLECTION, points=points)
        self._bm25_corpus.extend(docs)
        self._rebuild_bm25()
        log.debug("Indexed %d chunks (total: %d)", len(docs), len(self._bm25_corpus))

    def _rebuild_bm25(self) -> None:
        tokenized = [doc.content.lower().split() for doc in self._bm25_corpus]
        if tokenized:
            self._bm25 = BM25Okapi(tokenized)

    # ─────────────────────────────────────────────────────────────
    # Hybrid Retrieval
    # ─────────────────────────────────────────────────────────────

    async def hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        user_scopes: list[str],
        user_id: str,
        user_grants: list[str],
        top_k: int = 10,
        rrf_k: int = 60,
    ) -> list[RetrievedChunk]:
        dense_results  = await self._dense_search(query_vector, top_k=top_k * 2)
        sparse_results = self._bm25_search(query_text, top_k=top_k * 2)
        fused          = self._rrf_fuse(dense_results, sparse_results, k=rrf_k)

        allowed: list[RetrievedChunk] = []
        for chunk, score in fused[: top_k * 2]:
            tags = AccessTags(**chunk.get("access_tags", {}))
            if tags.is_accessible_by(user_scopes, user_id, user_grants):
                allowed.append(
                    RetrievedChunk(
                        chunk_id=chunk.get("chunk_id", ""),
                        source_artifact_id=chunk.get("source_artifact_id", ""),
                        content=chunk.get("content", ""),
                        score=score,
                        retrieval_method="hybrid",
                        source_system=chunk.get("source_system", ""),
                        source_url=chunk.get("source_url"),
                        timestamp=datetime.fromisoformat(
                            chunk.get("timestamp", datetime.utcnow().isoformat())
                        ),
                        entity_refs=chunk.get("entity_refs", []),
                        access_tags_verified=True,
                    )
                )
            if len(allowed) >= top_k:
                break
        return allowed

    async def _dense_search(
        self, query_vector: list[float], top_k: int
    ) -> list[tuple[dict, float]]:
        results = await self._client.search(
            collection_name=COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [({**r.payload, "chunk_id": str(r.id)}, r.score) for r in results]

    def _bm25_search(
        self, query: str, top_k: int
    ) -> list[tuple[dict, float]]:
        if not self._bm25 or not self._bm25_corpus:
            return []
        tokens = query.lower().split()
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            (
                {
                    "chunk_id": self._bm25_corpus[i].chunk_id,
                    "source_artifact_id": self._bm25_corpus[i].source_artifact_id,
                    "content": self._bm25_corpus[i].content,
                    "source_system": self._bm25_corpus[i].source_system,
                    "source_url": self._bm25_corpus[i].source_url,
                    "timestamp": self._bm25_corpus[i].timestamp.isoformat(),
                    "entity_refs": self._bm25_corpus[i].entity_refs,
                    "access_tags": self._bm25_corpus[i].access_tags,
                },
                float(score),
            )
            for i, score in ranked
            if score > 0
        ]

    @staticmethod
    def _rrf_fuse(
        dense: list[tuple[dict, float]],
        sparse: list[tuple[dict, float]],
        k: int = 60,
    ) -> list[tuple[dict, float]]:
        scores: dict[str, float] = {}
        chunks: dict[str, dict] = {}
        for rank, (chunk, _) in enumerate(dense):
            cid = chunk.get("chunk_id", "")
            scores[cid] = scores.get(cid, 0) + 1 / (k + rank + 1)
            chunks[cid] = chunk
        for rank, (chunk, _) in enumerate(sparse):
            cid = chunk.get("chunk_id", "")
            scores[cid] = scores.get(cid, 0) + 1 / (k + rank + 1)
            chunks[cid] = chunk
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(chunks[cid], score) for cid, score in ranked]


_vector_client: VectorClient | None = None


def get_vector_client() -> VectorClient:
    global _vector_client
    if _vector_client is None:
        _vector_client = VectorClient()
    return _vector_client
