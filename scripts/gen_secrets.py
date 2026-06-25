"""Generate cryptographically secure secrets for .env files."""
import secrets

pairs = [
    ("POSTGRES_PASSWORD", secrets.token_urlsafe(24)),
    ("MINIO_PASSWORD", secrets.token_urlsafe(24)),
    ("JWT_SECRET", secrets.token_hex(32)),
    ("WEBUI_SECRET_KEY", secrets.token_hex(32)),
    ("ADMIN_SECRET", secrets.token_urlsafe(32)),
    ("LANGFUSE_SECRET_KEY", secrets.token_urlsafe(32)),
    ("LANGFUSE_PUBLIC_KEY", secrets.token_urlsafe(16)),
    ("LANGFUSE_SALT", secrets.token_urlsafe(32)),
    ("GRAFANA_PASSWORD", secrets.token_urlsafe(16)),
]
for key, value in pairs:
    print(f"{key}={value}")
