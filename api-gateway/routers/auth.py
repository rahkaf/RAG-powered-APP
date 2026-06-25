"""
AI Knowledge Centre - Auth Router
Endpoints for login, token refresh, and logout.
"""

import asyncpg
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import (
    blacklist_token,
    constant_time_user_not_found_stub,
    create_access_token,
    get_current_user,
    verify_password,
    verify_token,
)
from dependencies import get_db_pool, get_rate_limiter, get_redis_client, get_settings
from models.auth import LoginRequest, RefreshRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    body: LoginRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    redis_client: aioredis.Redis = Depends(get_redis_client),
    rate_limiter=Depends(get_rate_limiter),
    settings=Depends(get_settings),
):
    """Authenticate user and return JWT."""
    client_ip = request.client.host if request.client else "unknown"

    # Rate limit: 5/15min per IP
    if not await rate_limiter.check(f"login:{client_ip}", 5, 900):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password, role, is_active FROM users WHERE username = $1",
            body.username,
        )

    # Constant-time stub to prevent timing side-channel if user not found
    if not row:
        constant_time_user_not_found_stub()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, row["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")

    # Update last login
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login = NOW() WHERE id = $1", row["id"]
        )

    token = create_access_token(
        {"user_id": str(row["id"]), "username": row["username"], "role": row["role"]},
        settings.jwt_secret,
        settings.jwt_expire_hours,
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_hours * 3600,
        user_id=str(row["id"]),
        role=row["role"],
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest,
    current_user=Depends(get_current_user),
    redis_client: aioredis.Redis = Depends(get_redis_client),
    settings=Depends(get_settings),
):
    """Refresh an existing JWT.

    Requires authentication. The submitted token's user_id must match the
    authenticated user to prevent token theft abuse.
    """
    # Decode the submitted token
    payload = verify_token(body.token, settings.jwt_secret)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Verify token ownership: the refresh token must belong to the authenticated user
    if payload.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Token does not belong to authenticated user")

    # Check blacklist
    from auth import is_token_blacklisted

    if await is_token_blacklisted(body.token, redis_client):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    new_token = create_access_token(
        {
            "user_id": payload["user_id"],
            "username": payload["username"],
            "role": payload["role"],
        },
        settings.jwt_secret,
        settings.jwt_expire_hours,
    )

    return TokenResponse(
        access_token=new_token,
        expires_in=settings.jwt_expire_hours * 3600,
        user_id=payload["user_id"],
        role=payload["role"],
    )


@router.post("/logout")
async def logout(
    body: RefreshRequest,
    current_user=Depends(get_current_user),
    redis_client: aioredis.Redis = Depends(get_redis_client),
    settings=Depends(get_settings),
):
    """Blacklist the submitted JWT token on logout.

    Verifies that the token belongs to the authenticated user before blacklisting.
    """
    # Decode the submitted token to verify ownership
    payload = verify_token(body.token, settings.jwt_secret)
    if not payload:
        raise HTTPException(status_code=400, detail="Invalid token")

    if payload.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Token does not belong to authenticated user")

    await blacklist_token(body.token, redis_client, settings.jwt_secret, settings.jwt_expire_hours)
    return {"status": "ok"}
