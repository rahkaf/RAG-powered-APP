"""
AI Knowledge Centre - Application Settings
Centralized configuration with validation via Pydantic BaseSettings.
"""

from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Required ──────────────────────────────
    database_url: str = Field(..., description="PostgreSQL connection string (asyncpg)")
    jwt_secret: str = Field(
        ..., min_length=32, description="JWT signing secret (min 32 characters)"
    )

    # ── Redis / Celery ────────────────────────
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"

    # ── External Services ────────────────────
    qdrant_url: str = "http://qdrant:6333"
    ollama_url: str = "http://ollama:11434"

    # ── Auth ──────────────────────────────────
    jwt_expire_hours: int = 8

    # ── Rate Limiting ────────────────────────
    rate_limit_per_minute: int = 20

    # ── CORS ─────────────────────────────────
    allowed_origins: str = ""

    # ── Logging ───────────────────────────────
    log_level: str = "INFO"

    # ── Storage ───────────────────────────────
    docs_directory: str = "/app/docs"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: str) -> str:
        """Accept comma-separated origins string."""
        return v

    def get_allowed_origins_list(self) -> List[str]:
        """Return CORS origins as a list, filtering empty entries."""
        if not self.allowed_origins.strip():
            return []
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]
