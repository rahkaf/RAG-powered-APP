"""
Admin Panel — System administration dashboard.
Provides endpoints for user CRUD, document management, system health, and backup/restore.
"""

import os
import subprocess
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from redis import Redis
import httpx
import bcrypt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://kcadmin:knowledge_pass@postgres:5432/knowledge_centre",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
JWT_SECRET = os.environ.get("JWT_SECRET", "change-me")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "admin-secret-key")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio_admin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio_secret_key")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")

logger = logging.getLogger("admin-panel")
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"admin-panel","event":"%(message)s"}',
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

engine = create_engine(DATABASE_URL.replace("+asyncpg", ""), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Admin Panel", version="1.0.0")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=120)
    role: str = Field(default="engineer", pattern="^(admin|engineer|viewer)$")
    department: Optional[str] = None


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    role: Optional[str] = Field(default=None, pattern="^(admin|engineer|viewer)$")
    department: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordChange(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=120)


class BackupRequest(BaseModel):
    backup_type: str = Field(default="full", pattern="^(full|incremental)$")


class RestoreRequest(BaseModel):
    backup_id: str


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_admin(request: Request):
    """Verify admin secret from header."""
    admin_key = request.headers.get("X-Admin-Secret", "")
    if admin_key != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "admin-panel"}


@app.get("/api/admin/system/health")
async def system_health(request: Request, _=Depends(_require_admin)):
    """Comprehensive system health check across all services."""
    health_status = {
        "postgres": "unknown",
        "redis": "unknown",
        "qdrant": "unknown",
        "minio": "unknown",
        "ollama": "unknown",
    }

    # Postgres
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        health_status["postgres"] = "healthy"
    except Exception:
        health_status["postgres"] = "unhealthy"

    # Redis
    try:
        redis_client.ping()
        health_status["redis"] = "healthy"
    except Exception:
        health_status["redis"] = "unhealthy"

    # Qdrant
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://qdrant:6333/healthz")
            if resp.status_code == 200:
                health_status["qdrant"] = "healthy"
    except Exception:
        health_status["qdrant"] = "unhealthy"

    # MinIO
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{MINIO_ENDPOINT}/minio/health/live")
            if resp.status_code == 200:
                health_status["minio"] = "healthy"
    except Exception:
        health_status["minio"] = "unhealthy"

    # Ollama
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://ollama:11434/api/tags")
            if resp.status_code == 200:
                health_status["ollama"] = "healthy"
    except Exception:
        health_status["ollama"] = "unhealthy"

    overall = "healthy" if all(v == "healthy" for v in health_status.values()) else "degraded"
    return {"status": overall, "services": health_status}


# ---------------------------------------------------------------------------
# Routes — User Management
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
async def list_users(request: Request, _=Depends(_require_admin), db: Session = Depends(get_db)):
    """List all users."""
    result = db.execute(
        text(
            "SELECT id, username, email, role, department, is_active, created_at "
            "FROM users ORDER BY created_at DESC"
        )
    )
    users = []
    for row in result:
        users.append({
            "id": str(row[0]),
            "username": row[1],
            "email": row[2],
            "role": row[3],
            "department": row[4],
            "is_active": row[5],
            "created_at": row[6].isoformat() if row[6] else None,
        })
    return {"users": users, "total": len(users)}


@app.post("/api/admin/users")
async def create_user(request: Request, user: UserCreate, _=Depends(_require_admin), db: Session = Depends(get_db)):
    """Create a new user."""
    # Check if username exists
    existing = db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": user.username})
    if existing.fetchone():
        raise HTTPException(status_code=409, detail="Username already exists")

    # Hash password
    password_hash = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt(rounds=12)).decode()

    result = db.execute(
        text(
            """INSERT INTO users (username, email, password, role, department, is_active, created_at)
               VALUES (:u, :e, :pw, :r, :d, true, :ts) RETURNING id"""
        ),
        {
            "u": user.username,
            "e": user.email,
            "pw": password_hash,
            "r": user.role,
            "d": user.department,
            "ts": datetime.now(timezone.utc),
        },
    )
    db.commit()
    user_id = result.fetchone()[0]
    return {"id": str(user_id), "username": user.username, "role": user.role}


@app.put("/api/admin/users/{user_id}")
async def update_user(
    request: Request,
    user_id: str,
    update: UserUpdate,
    _=Depends(_require_admin),
    db: Session = Depends(get_db),
):
    """Update user fields."""
    updates = []
    params = {"id": user_id}
    if update.email is not None:
        updates.append("email = :email")
        params["email"] = update.email
    if update.role is not None:
        updates.append("role = :role")
        params["role"] = update.role
    if update.department is not None:
        updates.append("department = :dept")
        params["dept"] = update.department
    if update.is_active is not None:
        updates.append("is_active = :active")
        params["active"] = update.is_active

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    db.execute(text(f"UPDATE users SET {', '.join(updates)} WHERE id = :id"), params)
    db.commit()
    return {"status": "updated"}


@app.delete("/api/admin/users/{user_id}")
async def delete_user(request: Request, user_id: str, _=Depends(_require_admin), db: Session = Depends(get_db)):
    """Soft-delete a user (set is_active = false)."""
    db.execute(text("UPDATE users SET is_active = false WHERE id = :id::uuid"), {"id": user_id})
    db.commit()
    return {"status": "deactivated"}


# ---------------------------------------------------------------------------
# Routes — Document Management
# ---------------------------------------------------------------------------

@app.get("/api/admin/documents")
async def list_documents(
    request: Request,
    _=Depends(_require_admin),
    db: Session = Depends(get_db),
):
    """List all documents with metadata."""
    result = db.execute(
        text(
            """SELECT d.id, d.filename, d.file_type, d.file_size_mb, d.status,
                      d.department, d.version, u.username, d.uploaded_at, d.indexed_at
               FROM documents d LEFT JOIN users u ON d.uploaded_by = u.id
               WHERE d.is_latest = TRUE
               ORDER BY d.uploaded_at DESC"""
        )
    )
    docs = []
    for row in result:
        docs.append({
            "id": str(row[0]),
            "filename": row[1],
            "file_type": row[2],
            "file_size_mb": float(row[3]) if row[3] is not None else None,
            "status": row[4],
            "department": row[5],
            "version": row[6],
            "uploaded_by": row[7],
            "uploaded_at": row[8].isoformat() if row[8] else None,
            "indexed_at": row[9].isoformat() if row[9] else None,
        })
    return {"documents": docs, "total": len(docs)}


@app.get("/api/admin/stats")
async def system_stats(request: Request, _=Depends(_require_admin), db: Session = Depends(get_db)):
    """Return system-wide statistics."""
    doc_count = db.execute(text("SELECT COUNT(*) FROM documents")).fetchone()[0]
    user_count = db.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    query_count = db.execute(text("SELECT COUNT(*) FROM queries")).fetchone()[0]
    total_size = db.execute(
        text("SELECT COALESCE(SUM(file_size_mb), 0) FROM documents WHERE is_latest = TRUE")
    ).fetchone()[0]

    return {
        "documents": doc_count,
        "users": user_count,
        "queries": query_count,
        "total_storage_mb": float(total_size),
    }


# ---------------------------------------------------------------------------
# Routes — Backup / Restore
# ---------------------------------------------------------------------------

@app.post("/api/admin/backup")
async def create_backup(request: Request, req: BackupRequest, _=Depends(_require_admin)):
    """Trigger a PostgreSQL backup."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = f"{BACKUP_DIR}/backup_{req.backup_type}_{timestamp}.sql.gz"

    try:
        result = subprocess.run(
            [
                "pg_dump",
                "-h", "postgres",
                "-U", os.environ.get("POSTGRES_USER", "kcadmin"),
                "-d", os.environ.get("POSTGRES_DB", "knowledge_centre"),
                "--format=custom",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "PGPASSWORD": os.environ.get("POSTGRES_PASSWORD", "knowledge_pass")},
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Backup failed: {result.stderr}")

        # Write compressed backup
        import gzip
        with gzip.open(backup_file, "wb") as f:
            f.write(result.stdout.encode())

        return {"status": "completed", "file": backup_file, "timestamp": timestamp}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Backup timed out after 5 minutes")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")


@app.post("/api/admin/restore")
async def restore_backup(request: Request, req: RestoreRequest, _=Depends(_require_admin)):
    """Restore from a backup file."""
    backup_file = f"{BACKUP_DIR}/{req.backup_id}"
    if not os.path.exists(backup_file):
        raise HTTPException(status_code=404, detail="Backup file not found")

    try:
        import gzip
        with gzip.open(backup_file, "rb") as f:
            dump_data = f.read()

        result = subprocess.run(
            [
                "pg_restore",
                "-h", "postgres",
                "-U", os.environ.get("POSTGRES_USER", "kcadmin"),
                "-d", os.environ.get("POSTGRES_DB", "knowledge_centre"),
                "--clean",
                "--if-exists",
            ],
            input=dump_data,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PGPASSWORD": os.environ.get("POSTGRES_PASSWORD", "knowledge_pass")},
        )

        return {"status": "restored", "warnings": result.stderr[:500] if result.stderr else None}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Restore timed out after 10 minutes")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")


@app.get("/api/admin/backups")
async def list_backups(request: Request, _=Depends(_require_admin)):
    """List available backup files."""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        return {"backups": []}

    files = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith(".sql.gz"):
            path = os.path.join(BACKUP_DIR, f)
            files.append({
                "filename": f,
                "size_bytes": os.path.getsize(path),
                "created_at": datetime.fromtimestamp(os.path.getctime(path), tz=timezone.utc).isoformat(),
            })
    return {"backups": files}


# ---------------------------------------------------------------------------
# Routes — Audit Logs
# ---------------------------------------------------------------------------

@app.get("/api/admin/audit-logs")
async def audit_logs(
    request: Request,
    limit: int = 100,
    _=Depends(_require_admin),
    db: Session = Depends(get_db),
):
    """Retrieve recent audit logs (audit_logs table not yet provisioned)."""
    logger.warning("audit_logs table not in schema; returning empty list")
    return {"logs": [], "total": 0}


# ---------------------------------------------------------------------------
# HTML Dashboard (simple)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple admin dashboard HTML page."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }
        h1 { color: #333; }
        .card { background: white; border-radius: 8px; padding: 20px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .endpoint { font-family: monospace; background: #f0f0f0; padding: 4px 8px; border-radius: 4px; }
        .method { font-weight: bold; color: #2563eb; }
        .status { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
        .status.healthy { background: #22c55e; }
        .status.degraded { background: #eab308; }
    </style>
</head>
<body>
    <h1>AI Knowledge Centre — Admin Dashboard</h1>
    <div class="card">
        <h2>System Health</h2>
        <p>Check <span class="endpoint"><span class="method">GET</span> /api/admin/system/health</span></p>
    </div>
    <div class="card">
        <h2>User Management</h2>
        <p><span class="endpoint"><span class="method">GET</span> /api/admin/users</span> — List users</p>
        <p><span class="endpoint"><span class="method">POST</span> /api/admin/users</span> — Create user</p>
        <p><span class="endpoint"><span class="method">PUT</span> /api/admin/users/{id}</span> — Update user</p>
    </div>
    <div class="card">
        <h2>Document Management</h2>
        <p><span class="endpoint"><span class="method">GET</span> /api/admin/documents</span> — List documents</p>
        <p><span class="endpoint"><span class="method">GET</span> /api/admin/stats</span> — System statistics</p>
    </div>
    <div class="card">
        <h2>Backup & Restore</h2>
        <p><span class="endpoint"><span class="method">POST</span> /api/admin/backup</span> — Create backup</p>
        <p><span class="endpoint"><span class="method">POST</span> /api/admin/restore</span> — Restore backup</p>
        <p><span class="endpoint"><span class="method">GET</span> /api/admin/backups</span> — List backups</p>
    </div>
    <div class="card">
        <h2>Audit Logs</h2>
        <p><span class="endpoint"><span class="method">GET</span> /api/admin/audit-logs</span> — Recent logs</p>
    </div>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)
