"""
Query Engine — RAG orchestration service.
Provides hybrid search (BM25 + vector), reranking, and LLM-backed answer generation.
"""

import os
import time
import uuid
import hashlib
import json
import logging
from typing import Optional
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, generate_latest
from redis import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hybrid_search import HybridSearch
from prompt_builder import build_prompt, extract_sources
from reranker import Reranker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://kcadmin:knowledge_pass@postgres:5432/knowledge_centre",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))
TOP_K = int(os.environ.get("TOP_K", "10"))
FINAL_K = int(os.environ.get("FINAL_K", "5"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse:3000")

logger = logging.getLogger("query-engine")
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"query-engine","event":"%(message)s"}',
)

# ---------------------------------------------------------------------------
# Database (async SQLAlchemy)
# ---------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

QUERY_CACHE_TTL = int(os.environ.get("QUERY_CACHE_TTL", "3600"))

# ── Prometheus Metrics ─────────────────────
QUERY_COUNT = Counter("kc_queries_total", "Total queries", ["status"])
QUERY_LATENCY = Histogram("kc_query_latency_seconds", "Query latency", buckets=[1, 5, 10, 15, 20, 30, 60])
CACHE_HITS = Counter("kc_cache_hits_total", "Query cache hits")
LLM_LATENCY = Histogram("kc_llm_latency_seconds", "LLM generation latency", buckets=[2, 5, 10, 15, 20, 30, 60])

# ---------------------------------------------------------------------------
# Core components
# ---------------------------------------------------------------------------

search_engine = HybridSearch()
reranker = Reranker()

# ---------------------------------------------------------------------------
# Langfuse (optional telemetry)
# ---------------------------------------------------------------------------

langfuse_client = None
if LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY:
    try:
        from langfuse import Langfuse

        langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
        )
        logger.info("Langfuse tracing enabled")
    except Exception as e:
        logger.warning(f"Langfuse init failed: {e}")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Query Engine", version="1.0.0")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    department: Optional[str] = None
    use_history: bool = True
    top_k: int = Field(default=TOP_K, ge=1, le=50)
    final_k: int = Field(default=FINAL_K, ge=1, le=20)


class SourceDocument(BaseModel):
    document_id: int
    filename: str
    chunk_index: int
    score: float
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceDocument]
    query_id: str
    latency_ms: float
    model_used: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_id(request: Request) -> Optional[str]:
    """Extract user ID from auth header if present."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = pyjwt.decode(
                auth[7:],
                os.environ.get("JWT_SECRET", "change-me"),
                algorithms=["HS256"],
            )
            return payload.get("user_id") or payload.get("sub")
        except Exception:
            pass
    return None


async def _log_query(
    db: AsyncSession,
    user_id: Optional[str],
    question: str,
    answer: str,
    sources: list,
    department: Optional[str],
    latency_ms: float,
):
    """Persist query log to PostgreSQL."""
    try:
        await db.execute(
            text(
                """INSERT INTO queries (id, user_id, question, answer, sources, filters_applied,
                   latency_ms, created_at)
                   VALUES (:id, NULLIF(:uid, '')::uuid, :q, :a, :sources::jsonb, :filters::jsonb, :lat, :ts)"""
            ),
            {
                "id": str(uuid.uuid4()),
                "uid": user_id,
                "q": question,
                "a": answer,
                "sources": json.dumps(sources),
                "filters": json.dumps({"department": department} if department else {}),
                "lat": int(latency_ms),
                "ts": datetime.now(timezone.utc),
            },
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to log query: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "query-engine"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type="text/plain")


@app.post("/api/query", response_model=QueryResponse)
async def query(request: Request, req: QueryRequest, db: AsyncSession = Depends(get_db)):
    """
    RAG query endpoint:
    1. Hybrid search (BM25 + vector) → top_k results
    2. Cross-encoder rerank → final_k results
    3. Prompt assembly with context
    4. LLM generation via Ollama
    5. Log query + telemetry
    """
    start = time.time()
    query_id = str(uuid.uuid4())
    user_id = _get_user_id(request)

    query_str = f"{req.question}:{req.department}:{req.top_k}:{req.final_k}"
    cache_key = f"query_cache:{hashlib.sha256(query_str.encode()).hexdigest()}"
    cached = redis_client.get(cache_key)
    if cached:
        cached_data = json.loads(cached)
        logger.info(f"Query cache HIT: {cache_key[:16]}...")
        CACHE_HITS.inc()
        QUERY_COUNT.labels(status="success").inc()
        return QueryResponse(**cached_data)
    logger.info(f"Query cache MISS: {cache_key[:16]}...")

    query_embedding = [0.0] * EMBEDDING_DIMENSION
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            emb_resp = await client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": req.question},
            )
            if emb_resp.status_code == 200:
                query_embedding = emb_resp.json()["embedding"]
    except Exception:
        logger.warning("Embedding generation failed, using zero vector")

    try:
        filters = {}
        if req.department:
            filters["department"] = req.department
        search_results = search_engine.search(
            query_embedding=query_embedding,
            query_text=req.question,
            filters=filters,
            top_k=req.top_k,
        )
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")

    if len(search_results) > req.final_k:
        reranked = reranker.rerank(req.question, search_results, top_k=req.final_k)
    else:
        reranked = search_results

    messages = build_prompt(
        question=req.question,
        context_chunks=reranked,
        suggest_category=req.department or "general",
    )

    answer_parts = []
    model_used = OLLAMA_MODEL
    llm_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": TEMPERATURE},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer_parts.append(data["message"]["content"])
    except Exception as e:
        logger.error(f"LLM generation failed: {e}")
        QUERY_COUNT.labels(status="error").inc()
        raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")
    LLM_LATENCY.observe(time.time() - llm_start)

    answer = "\n".join(answer_parts)
    latency_ms = (time.time() - start) * 1000

    source_list = extract_sources(reranked)
    sources = [
        SourceDocument(
            document_id=0,
            filename=s.get("filename", "unknown"),
            chunk_index=s.get("page", 0),
            score=s.get("score", 0.0),
            snippet=s.get("text_preview", ""),
        )
        for s in source_list
    ]

    await _log_query(
        db,
        user_id,
        req.question,
        answer,
        [s.model_dump() for s in sources],
        req.department,
        latency_ms,
    )
    QUERY_COUNT.labels(status="success").inc()
    QUERY_LATENCY.observe(latency_ms / 1000)

    if langfuse_client:
        try:
            trace = langfuse_client.trace(
                name="rag-query",
                metadata={"query_id": query_id, "user_id": user_id or "anonymous"},
            )
            trace.generation(
                name="ollama-generate",
                model=model_used,
                prompt=req.question,
                completion=answer,
                usage={"totalTokens": len(answer.split())},
            )
        except Exception as e:
            logger.warning(f"Langfuse trace failed: {e}")

    response_data = {
        "answer": answer,
        "sources": [s.model_dump() for s in sources],
        "query_id": query_id,
        "latency_ms": latency_ms,
        "model_used": model_used,
    }
    redis_client.setex(cache_key, QUERY_CACHE_TTL, json.dumps(response_data))

    return QueryResponse(
        answer=answer,
        sources=sources,
        query_id=query_id,
        latency_ms=latency_ms,
        model_used=model_used,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
