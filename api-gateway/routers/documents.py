"""
AI Knowledge Centre - Documents Router
Endpoints for document listing, ingestion, and deletion.
"""

import hashlib
import logging
import os
import pathlib
import tempfile
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File

from auth import get_current_user
from dependencies import get_db_pool, get_rate_limiter, get_settings
from models.documents import DocumentResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("")
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    department: Optional[str] = None,
    status_filter: Optional[str] = None,
    current_user=Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """List documents with pagination and filters."""
    offset = (page - 1) * page_size
    conditions = ["is_latest = TRUE"]
    params = []
    param_idx = 1

    if department:
        conditions.append(f"department = ${param_idx}")
        params.append(department)
        param_idx += 1

    if status_filter:
        conditions.append(f"status = ${param_idx}")
        params.append(status_filter)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    async with db_pool.acquire() as conn:
        # Get total count
        count_query = f"SELECT COUNT(*) FROM documents WHERE {where_clause}"
        total = await conn.fetchval(count_query, *params)

        # Get page
        query = f"""SELECT id, filename, file_type, department, file_size_mb, status,
                    chunk_count, version, uploaded_at, indexed_at
                    FROM documents WHERE {where_clause}
                    ORDER BY uploaded_at DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}"""
        params.extend([page_size, offset])
        rows = await conn.fetch(query, *params)

    documents = [
        DocumentResponse(
            id=str(r["id"]),
            filename=r["filename"],
            file_type=r["file_type"],
            department=r["department"],
            status=r["status"],
            chunk_count=r["chunk_count"],
            version=r["version"],
            uploaded_at=r["uploaded_at"].isoformat() if r["uploaded_at"] else "",
            indexed_at=r["indexed_at"].isoformat() if r["indexed_at"] else None,
        ).model_dump()
        for r in rows
    ]

    return {"documents": documents, "total": total, "page": page, "page_size": page_size}


@router.post("/ingest")
async def ingest(
    request: Request,
    file: Optional[UploadFile] = File(None),
    department: Optional[str] = None,
    current_user=Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    rate_limiter=Depends(get_rate_limiter),
    settings=Depends(get_settings),
):
    """Upload and ingest a document (admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    # Rate limit: 10/h per admin
    if not await rate_limiter.check(f"ingest:{current_user['user_id']}", 10, 3600):
        raise HTTPException(status_code=429, detail="Ingestion rate limit exceeded.")

    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    content = await file.read()
    checksum = hashlib.sha256(content).hexdigest()
    raw_filename = file.filename or "unknown"

    # Sanitize filename: strip directory components to prevent path traversal (C6 fix)
    safe_filename = pathlib.Path(raw_filename).name
    unique_name = f"{uuid.uuid4().hex}_{safe_filename}"

    docs_dir = pathlib.Path(settings.docs_directory)
    docs_dir.mkdir(parents=True, exist_ok=True)
    file_path = docs_dir / unique_name
    file_type = safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else "txt"

    # Check for existing version by filename + department
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            """SELECT id, version FROM documents
               WHERE filename = $1 AND department IS NOT DISTINCT FROM $2 AND is_latest = TRUE""",
            safe_filename,
            department,
        )
        if existing:
            # Mark old version as not latest
            await conn.execute(
                "UPDATE documents SET is_latest = FALSE WHERE id = $1", existing["id"]
            )
            version = existing["version"] + 1
        else:
            version = 1

        # Insert document record
        doc_id = await conn.fetchval(
            """INSERT INTO documents (filename, file_type, department, file_size_mb, status,
               checksum, version, is_latest, uploaded_by, uploaded_at)
               VALUES ($1, $2, $3, $4, 'queued', $5, $6, TRUE, $7, NOW())
               RETURNING id""",
            safe_filename,
            file_type,
            department,
            round(len(content) / (1024 * 1024), 2),
            checksum,
            version,
            current_user["user_id"],
        )

    # Save file atomically: write to temp file then rename
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(docs_dir), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, str(file_path))
        except BaseException:
            # Clean up temp file on any error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'failed', error_msg = $1 WHERE id = $2",
                f"File write error: {e}",
                doc_id,
            )
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Enqueue Celery task
    try:
        from celery import Celery

        celery_app = Celery(broker=settings.celery_broker_url, backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/2"))
        celery_app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_serializer="json",
            timezone="UTC",
            enable_utc=True,
        )
        task = celery_app.send_task(
            "tasks.ingest_document",
            args=[str(doc_id), str(file_path), department, file_type],
            queue="ingestion",
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ingestion_jobs (document_id, celery_task_id, status, created_at)
                   VALUES ($1, $2, 'queued', NOW())""",
                doc_id,
                task.id,
            )

        logger.info(f"Document queued for ingestion: {safe_filename} doc_id={doc_id}")

        return {"document_id": str(doc_id), "status": "queued", "version": version}

    except Exception as e:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'failed', error_msg = $1 WHERE id = $2",
                str(e),
                doc_id,
            )
        logger.error(f"Ingestion enqueue error: {e}")
        raise HTTPException(status_code=500, detail="Failed to enqueue ingestion task")


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    current_user=Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    settings=Depends(get_settings),
):
    """Delete a document and its vectors (admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    async with db_pool.acquire() as conn:
        doc = await conn.fetchrow(
            "SELECT id, minio_path, filename FROM documents WHERE id = $1", document_id
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

    # Delete from Qdrant via query engine
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(f"http://query-engine:8001/vectors/{document_id}")
    except Exception as e:
        logger.warning(f"Failed to delete Qdrant vectors for {document_id}: {e}")

    # Delete the actual file from disk
    try:
        docs_dir = pathlib.Path(settings.docs_directory)
        if docs_dir.exists():
            for child in docs_dir.iterdir():
                if child.name.endswith(document_id) or document_id in child.name:
                    child.unlink()
                    logger.info(f"Deleted file: {child}")
    except Exception as e:
        logger.warning(f"Failed to delete file for {document_id}: {e}")

    # Mark as deleted in database
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET is_latest = FALSE WHERE id = $1", document_id
        )

    logger.info(f"Document deleted: {document_id} by {current_user['username']}")
    return {"status": "deleted"}
