"""
AI Knowledge Centre - Authentication Module
JWT token management, password hashing, and Redis-based token blacklisting.

Uses PyJWT (replaces abandoned python-jose).
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

from dependencies import get_redis_client, get_settings

logger = logging.getLogger(__name__)

# ── JWT Operations ─────────────────────────


def create_access_token(
    data: dict,
    secret: str,
    expires_hours: int = 8,
) -> str:
    """Create a signed JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": secrets.token_hex(16),
    })
    return pyjwt.encode(to_encode, secret, algorithm="HS256")


def verify_token(token: str, secret: str) -> Optional[dict]:
    """Verify and decode a JWT token. Returns payload or None."""
    try:
        payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        # PyJWT already checks exp, but double-check for safety
        if payload.get("exp") and datetime.fromtimestamp(
            payload["exp"], tz=timezone.utc
        ) < datetime.now(timezone.utc):
            return None
        return payload
    except pyjwt.ExpiredSignatureError:
        return None
    except pyjwt.InvalidTokenError:
        return None


def get_token_hash(token: str) -> str:
    """Hash a token for Redis blacklist storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def get_token_remaining_seconds(token: str, secret: str) -> int:
    """Decode a token and return remaining seconds until expiry, or 0."""
    try:
        payload = pyjwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": True})
        exp = payload.get("exp")
        if exp is None:
            return 0
        remaining = exp - datetime.now(timezone.utc).timestamp()
        return max(0, int(remaining))
    except pyjwt.InvalidTokenError:
        return 0


async def blacklist_token(token: str, redis_client, secret: str, expires_hours: int = 8) -> None:
    """Add a token hash to the Redis blacklist with TTL matching token's remaining life."""
    remaining = get_token_remaining_seconds(token, secret)
    # Fallback to full expires_hours if token decode fails
    ttl = remaining if remaining > 0 else expires_hours * 3600
    token_hash = get_token_hash(token)
    await redis_client.setex(f"blacklist:{token_hash}", ttl, "1")


async def is_token_blacklisted(token: str, redis_client) -> bool:
    """Check if a token hash exists in the Redis blacklist."""
    token_hash = get_token_hash(token)
    result = await redis_client.exists(f"blacklist:{token_hash}")
    return result > 0


# ── Password Operations ────────────────────


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost factor 12)."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def constant_time_user_not_found_stub() -> None:
    """Perform a dummy bcrypt check to equalize timing when user is not found.

    Prevents timing side-channels that reveal valid usernames.
    """
    bcrypt.checkpw(b"dummy-timing-stub", bcrypt.gensalt(rounds=4))


# ── Dependency ─────────────────────────────


async def get_current_user(
    request: Request,
    redis_client=Depends(get_redis_client),
    settings=Depends(get_settings),
) -> dict:
    """FastAPI dependency that extracts and validates the JWT from the Authorization header.

    Uses the shared Redis client from app state via dependency injection (no new
    connections created per request).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    payload = verify_token(token, settings.jwt_secret)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Check blacklist using the shared Redis client
    try:
        blacklisted = await is_token_blacklisted(token, redis_client)
        if blacklisted:
            raise HTTPException(status_code=401, detail="Token has been revoked")
    except HTTPException:
        raise
    except Exception:
        # If Redis is unavailable, don't block on blacklist check (fail open)
        logger.warning("Redis unavailable for blacklist check, allowing request")

    return {
        "user_id": payload.get("user_id"),
        "username": payload.get("username"),
        "role": payload.get("role"),
    }
