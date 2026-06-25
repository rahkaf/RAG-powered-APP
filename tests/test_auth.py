"""Tests for authentication utilities."""

import os
import time
import pytest
from unittest.mock import patch, MagicMock

# Set env vars before importing
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-unit-tests-64chars-long-enough!")
os.environ.setdefault("DATABASE_URL", "sqlite:///test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from auth import (
    create_access_token,
    verify_token,
    hash_password,
    verify_password,
    is_token_blacklisted,
    blacklist_token,
    get_token_hash,
)

JWT_SECRET = os.environ["JWT_SECRET"]


@pytest.fixture
def mock_redis():
    mock = MagicMock()
    mock.exists = MagicMock(return_value=0)
    mock.setex = MagicMock()
    yield mock


class TestPasswordHashing:
    def test_hash_and_verify(self):
        password = "secure_password_123"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed) is True

    def test_wrong_password(self):
        hashed = hash_password("correct_password")
        assert verify_password("wrong_password", hashed) is False

    def test_different_hashes(self):
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        # bcrypt uses random salt, so hashes should differ
        assert h1 != h2


class TestJWT:
    def test_create_and_verify_access_token(self):
        token = create_access_token(
            {"user_id": "test-user", "role": "user", "type": "access"},
            JWT_SECRET,
            expires_hours=8,
        )
        payload = verify_token(token, JWT_SECRET)
        assert payload is not None
        assert payload["user_id"] == "test-user"
        assert payload["role"] == "user"

    def test_create_and_verify_refresh_token(self):
        token = create_access_token(
            {"user_id": "test-user", "type": "refresh"},
            JWT_SECRET,
            expires_hours=72,
        )
        payload = verify_token(token, JWT_SECRET)
        assert payload is not None
        assert payload["user_id"] == "test-user"

    def test_invalid_token(self):
        payload = verify_token("invalid.token.here", JWT_SECRET)
        assert payload is None

    def test_expired_token(self):
        token = create_access_token(
            {"user_id": "test-user", "role": "user"},
            JWT_SECRET,
            expires_hours=-1,  # already expired
        )
        payload = verify_token(token, JWT_SECRET)
        assert payload is None


class TestTokenBlacklist:
    def test_token_hash(self):
        token = "some-jwt-token"
        h = get_token_hash(token)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest

    @pytest.mark.asyncio
    async def test_blacklist_token(self, mock_redis):
        token = create_access_token(
            {"user_id": "test-user", "role": "user"},
            JWT_SECRET,
            expires_hours=1,
        )
        await blacklist_token(token, mock_redis, expires_hours=1)
        mock_redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_blacklisted(self, mock_redis):
        mock_redis.exists.return_value = 1
        result = await is_token_blacklisted("some-token", mock_redis)
        assert result is True

        mock_redis.exists.return_value = 0
        result = await is_token_blacklisted("some-token", mock_redis)
        assert result is False
