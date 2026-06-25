"""
AI Knowledge Centre - Sliding Window Rate Limiter
Uses Redis sorted sets for accurate sliding window rate limiting.

Includes fail-open behavior: if Redis is unavailable, requests are allowed
rather than causing 500 errors.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding window rate limiter backed by Redis sorted sets."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def check(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """
        Check if a request is allowed under the rate limit.

        Args:
            key: Unique identifier (e.g. "user:123" or "ip:192.168.1.1")
            max_requests: Maximum number of requests allowed in the window
            window_seconds: Time window in seconds

        Returns:
            True if request is allowed, False if rate limited.
            Returns True (fail-open) if Redis is unavailable.
        """
        try:
            now = time.time()
            window_start = now - window_seconds

            pipe = self.redis.pipeline()

            # Remove expired entries
            pipe.zremrangebyscore(key, 0, window_start)

            # Count current entries in window
            pipe.zcard(key)

            # Add current request
            pipe.zadd(key, {f"{now}": now})

            # Set expiry on the key
            pipe.expire(key, window_seconds)

            results = await pipe.execute()
            current_count = results[1]

            # If count exceeds max, remove the entry we just added
            if current_count >= max_requests:
                await self.redis.zrem(key, f"{now}")
                return False

            return True

        except Exception:
            logger.warning(
                "Redis unavailable for rate limit check, allowing request (fail-open): key=%s",
                key,
                exc_info=True,
            )
            return True

    async def get_remaining(self, key: str, max_requests: int, window_seconds: int) -> int:
        """Get remaining requests in the current window."""
        try:
            now = time.time()
            window_start = now - window_seconds

            # Remove expired entries
            await self.redis.zremrangebyscore(key, 0, window_start)

            # Count current entries
            count = await self.redis.zcard(key)

            return max(0, max_requests - count)

        except Exception:
            logger.warning(
                "Redis unavailable for rate limit remaining check, returning max: key=%s",
                key,
                exc_info=True,
            )
            return max_requests

    async def reset(self, key: str) -> None:
        """Reset the rate limit for a key."""
        try:
            await self.redis.delete(key)
        except Exception:
            logger.warning(
                "Redis unavailable for rate limit reset: key=%s",
                key,
                exc_info=True,
            )
