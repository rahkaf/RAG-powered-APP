"""
AI Knowledge Centre - FastAPI Dependencies
Shared resource accessors via dependency injection.
"""

import asyncpg
import redis.asyncio as aioredis
from fastapi import Request

from config.settings import Settings
from rate_limiter import RateLimiter

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the application settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Set the settings singleton (called during app startup)."""
    global _settings
    _settings = settings


async def get_db_pool(request: Request) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool from app state."""
    return request.app.state.db_pool


async def get_redis_client(request: Request) -> aioredis.Redis:
    """Return the shared Redis client from app state."""
    return request.app.state.redis_client


def get_rate_limiter(request: Request) -> RateLimiter:
    """Return the shared RateLimiter from app state."""
    return request.app.state.rate_limiter
