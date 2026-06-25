"""
AI Knowledge Centre - Query Router
Endpoint for sending queries to the RAG engine.
"""

import json
import logging
import time

import asyncpg
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from prometheus_client import Counter, Histogram

from auth import get_current_user
from dependencies import get_db_pool, get_rate_limiter, get_redis_client, get_settings
from models.queries import QueryRequest, QueryResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["query"])

# ── Prometheus Metrics ─────────────────────
QUERY_COUNT = Counter("kc_queries_total", "Total queries", ["status"])
QUERY_LATENCY = Histogram(
    "kc_query_latency_seconds",
    "Query latency",
    buckets=[1, 5, 10, 15, 20, 30, 60],
)


@router.post("/query")
async def query(
    body: QueryRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    rate_limiter=Depends(get_rate_limiter),
    settings=Depends(get_settings),
):
    """Send a query to the RAG engine."""
    # Rate limit: N/min per user (from settings)
    if not await rate_limiter.check(
        f"query:{current_user['user_id']}", settings.rate_limit_per_minute, 60
    ):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for queries.")

    start = time.time()

    try:
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "http://query-engine:8001/query",
                json={
                    "question": body.question,
                    "filters": body.filters or {},
                    "user_id": current_user["user_id"],
                },
            )

        if resp.status_code != 200:
            QUERY_COUNT.labels(status="error").inc()
            raise HTTPException(status_code=502, detail="Query engine error")

        raw_result = resp.json()
        latency_ms = int((time.time() - start) * 1000)

        # Validate response against Pydantic schema (H7 fix)
        try:
            result = QueryResponse.from_raw(raw_result)
        except Exception as e:
            logger.warning(f"Query response validation failed: {e}, returning raw response")
            # Fall back to raw result if validation fails (graceful degradation)
            result = raw_result
            sources_for_db = str(raw_result.get("sources", []))
            filters_for_db = str(body.filters or {})
            tokens_used = raw_result.get("tokens_used", 0)
            vector_search_ms = raw_result.get("vector_search_ms", 0)
            reranker_ms = raw_result.get("reranker_ms", 0)
            llm_ms = raw_result.get("llm_ms", 0)
            session_id = raw_result.get("session_id", "")
            answer = raw_result.get("answer", "")
        else:
            # Proper JSON serialization for JSONB columns (fixes str() bug)
            sources_for_db = json.dumps([s.model_dump() for s in result.sources])
            filters_for_db = json.dumps(body.filters or {})
            tokens_used = result.tokens_used or 0
            vector_search_ms = result.vector_search_ms or 0
            reranker_ms = result.reranker_ms or 0
            llm_ms = result.llm_ms or 0
            session_id = result.session_id or ""
            answer = result.answer

        # Log query to PostgreSQL
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO queries (user_id, session_id, question, answer, sources,
                   filters_applied, latency_ms, vector_search_ms, reranker_ms, llm_ms,
                   tokens_used, created_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, $11, NOW())""",
                current_user["user_id"],
                session_id,
                body.question,
                answer,
                sources_for_db,
                filters_for_db,
                latency_ms,
                vector_search_ms,
                reranker_ms,
                llm_ms,
                tokens_used,
            )

        QUERY_COUNT.labels(status="success").inc()
        QUERY_LATENCY.observe(time.time() - start)

        logger.info(
            f"Query answered: user={current_user['username']} "
            f"latency={latency_ms}ms tokens={tokens_used}"
        )

        return result.model_dump() if isinstance(result, QueryResponse) else raw_result

    except HTTPException:
        raise
    except httpx.TimeoutException:
        QUERY_COUNT.labels(status="timeout").inc()
        raise HTTPException(status_code=504, detail="Query timed out")
    except Exception as e:
        QUERY_COUNT.labels(status="error").inc()
        logger.error(f"Query error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
