"""
AI Knowledge Centre - API Gateway
FastAPI application factory with JWT auth, rate limiting, and request routing.

This module is intentionally thin — all business logic lives in routers/,
auth.py, and rate_limiter.py.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, generate_latest

from config.settings import Settings
from dependencies import set_settings
from middleware.metrics import MetricsMiddleware
from rate_limiter import RateLimiter
from routers import auth, documents, query

# ── Logging ────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"api-gateway","event":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# ── Prometheus Metrics (app-level) ──────────
INGESTION_COUNT = Counter("kc_ingestion_total", "Total ingestion jobs", ["status"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle for the application."""
    settings: Settings = app.state.settings

    # ── Startup ─────────────────────────────
    logger.info("Starting API Gateway...")

    # Database pool
    app.state.db_pool = await asyncpg.create_pool(
        settings.database_url, min_size=5, max_size=20
    )
    logger.info("Connected to PostgreSQL")

    # Redis
    app.state.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await app.state.redis_client.ping()
    logger.info("Connected to Redis")

    # Rate limiter
    app.state.rate_limiter = RateLimiter(app.state.redis_client)

    # Verify Qdrant is reachable (non-blocking)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.qdrant_url}/readyz")
            logger.info(f"Qdrant ready: {resp.status_code}")
    except Exception as e:
        logger.warning(f"Qdrant not ready at startup: {e}")

    yield

    # ── Shutdown ────────────────────────────
    logger.info("Shutting down API Gateway...")
    pool: Optional[asyncpg.Pool] = getattr(app.state, "db_pool", None)
    if pool:
        await pool.close()
    rc = getattr(app.state, "redis_client", None)
    if rc:
        await rc.close()


def create_app() -> FastAPI:
    """Application factory. Creates and configures the FastAPI app."""

    # Load and validate settings (raises ValidationError if required vars missing)
    settings = Settings()
    set_settings(settings)

    app = FastAPI(
        title="AI Knowledge Centre API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Store settings on app state for access in dependencies
    app.state.settings = settings

    # ── CORS ────────────────────────────────
    # C2 fix: use specific origins instead of wildcard
    allowed_origins = settings.get_allowed_origins_list()
    if not allowed_origins:
        logger.warning(
            "ALLOWED_ORIGINS is empty — CORS will block all cross-origin requests. "
            "Set ALLOWED_ORIGINS env var to a comma-separated list of trusted origins."
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Metrics Middleware ──────────────────
    # M6 fix: uses cardinality-safe status_class labels
    app.add_middleware(MetricsMiddleware)

    # ── Routers ─────────────────────────────
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(query.router)

    # ── Health Endpoint (H1 fix: real dependency checks) ──
    @app.get("/health")
    async def health(request: Request):
        """Check health of all critical dependencies."""
        checks = {}

        # Database check
        try:
            pool = request.app.state.db_pool
            await pool.fetchval("SELECT 1")
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "down"

        # Redis check
        try:
            rc = request.app.state.redis_client
            await rc.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "down"

        is_healthy = all(v == "ok" for v in checks.values())
        if is_healthy:
            return {"status": "ok", **checks}
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", **checks},
        )

    # ── Metrics Endpoint ───────────────────
    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type="text/plain")

    return app


# ── Module-level app instance for uvicorn ──
app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
