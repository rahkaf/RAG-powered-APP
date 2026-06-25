"""Tests for the sliding window rate limiter."""

import time
import os
import pytest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def mock_redis():
    mock = AsyncMock()
    pipeline = AsyncMock()
    pipeline.execute = AsyncMock(return_value=[None, None, 0, None])
    mock.pipeline.return_value = pipeline
    mock.zremrangebyscore = AsyncMock()
    mock.zcard = AsyncMock(return_value=0)
    mock.zadd = AsyncMock()
    mock.expire = AsyncMock()
    mock.zrem = AsyncMock()
    mock.delete = AsyncMock()
    yield mock


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_request_under_limit(self, mock_redis):
        from rate_limiter import RateLimiter

        rl = RateLimiter(mock_redis)
        # count = 3, limit = 20 → allowed
        mock_redis.pipeline.return_value.execute.return_value = [None, 3, None, None]
        result = await rl.check("user1", 20, 60)
        assert result is True

    @pytest.mark.asyncio
    async def test_blocks_request_over_limit(self, mock_redis):
        from rate_limiter import RateLimiter

        rl = RateLimiter(mock_redis)
        # count = 20, limit = 20 → blocked (>= means blocked)
        mock_redis.pipeline.return_value.execute.return_value = [None, 20, None, None]
        result = await rl.check("user1", 20, 60)
        assert result is False

    @pytest.mark.asyncio
    async def test_different_users_independent(self, mock_redis):
        from rate_limiter import RateLimiter

        rl = RateLimiter(mock_redis)

        # User1 under limit
        mock_redis.pipeline.return_value.execute.return_value = [None, 5, None, None]
        assert await rl.check("user1", 20, 60) is True

        # User2 also under limit
        mock_redis.pipeline.return_value.execute.return_value = [None, 3, None, None]
        assert await rl.check("user2", 20, 60) is True

    @pytest.mark.asyncio
    async def test_get_remaining(self, mock_redis):
        from rate_limiter import RateLimiter

        rl = RateLimiter(mock_redis)
        mock_redis.zcard.return_value = 7
        remaining = await rl.get_remaining("user1", 20, 60)
        assert remaining == 13

    @pytest.mark.asyncio
    async def test_reset(self, mock_redis):
        from rate_limiter import RateLimiter

        rl = RateLimiter(mock_redis)
        await rl.reset("user1")
        mock_redis.delete.assert_called_once_with("user1")
