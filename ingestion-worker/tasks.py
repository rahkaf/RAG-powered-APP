"""
AI Knowledge Centre - Celery Application & Tasks
Handles document ingestion, vector management, backups, and evaluations.
"""

import os
import json
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery.schedules import crontab

# ── Logging ────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"ingestion-worker","event":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# ── Celery App ─────────────────────────────
app = Celery(
    "knowledge_centre",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/2"),
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=1800,  # 30 min soft limit
    task_time_limit=1830,  # 30.5 min hard limit
    task_default_queue="ingestion",
)

# ── Beat Schedule ──────────────────────────
app.conf.beat_schedule = {
    "backup-postgres-nightly": {
        "task": "tasks.backup_postgres",
        "schedule": crontab(hour=3, minute=0),
    },
    "backup-qdrant-weekly": {
        "task": "tasks.backup_qdrant",
        "schedule": crontab(hour=3, minute=30, day_of_week=0),
    },
    "ragas-evaluation-weekly": {
        "task": "tasks.run_ragas_evaluation",
        "schedule": crontab(hour=2, minute=0, day_of_week=0),
    },
}

# ── Configuration ──────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
MINIO_URL = os.getenv("MINIO_URL", "http://minio:9000")
MINIO_USER = os.getenv("MINIO_USER", "minioadmin")
MINIO_PASSWORD = os.getenv("MINIO_PASSWORD", "minioadmin")
DATABASE_URL = os.getenv("DATABASE_URL")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "60"))
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")


# ── Helper: Database Connection ────────────
async def get_db_connection():
    """Create an async database connection."""
    import asyncpg
    return await asyncpg.connect(DATABASE_URL)


# ── Helper: MinIO Upload ──────────────────
def upload_to_minio(file_path: str, object_name: str) -> bool:
    """Upload a file to MinIO bucket."""
    import boto3
    from botocore.client import Config

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_URL,
            aws_access_key_id=MINIO_USER,
            aws_secret_access_key=MINIO_PASSWORD,
            config=Config(signature_version="s3v4"),
        )
        # Ensure bucket exists
        try:
            s3.head_bucket(Bucket="documents")
        except Exception:
            s3.create_bucket(Bucket="documents")

        s3.upload_file(file_path, "documents", object_name)
        return True
    except Exception as e:
        logger.error(f"MinIO upload failed: {e}")
        return False


# ── Helper: MinIO Download ─────────────────
def download_from_minio(object_name: str, local_path: str) -> bool:
    """Download a file from MinIO."""
    import boto3
    from botocore.client import Config

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_URL,
            aws_access_key_id=MINIO_USER,
            aws_secret_access_key=MINIO_PASSWORD,
            config=Config(signature_version="s3v4"),
        )
        s3.download_file("documents", object_name, local_path)
        return True
    except Exception as e:
        logger.error(f"MinIO download failed: {e}")
        return False


# ── Helper: Qdrant Operations ──────────────
def delete_qdrant_vectors(document_id: str) -> bool:
    """Delete all Qdrant points for a document."""
    import httpx

    try:
        with httpx.Client(timeout=30) as client:
            # Scroll to find all points for this document
            resp = client.post(
                f"{QDRANT_URL}/collections/documents/points/scroll",
                json={
                    "filter": {
                        "must": [
                            {"key": "document_id", "match": {"value": document_id}}
                        ]
                    },
                    "limit": 1000,
                    "with_payload": False,
                },
            )
            if resp.status_code != 200:
                return False

            points = resp.json().get("result", {}).get("points", [])
            if not points:
                return True

            point_ids = [p["id"] for p in points]
            del_resp = client.post(
                f"{QDRANT_URL}/collections/documents/points/delete",
                json={"points": point_ids},
            )
            return del_resp.status_code == 200
    except Exception as e:
        logger.error(f"Qdrant delete failed: {e}")
        return False


def get_qdrant_snapshot() -> bytes:
    """Create a Qdrant collection snapshot."""
    import httpx

    with httpx.Client(timeout=120) as client:
        resp = client.put(f"{QDRANT_URL}/collections/documents/snapshots")
        resp.raise_for_status()
        snapshot_url = resp.json()["result"]["snapshot_url"]

        # Download the snapshot
        snapshot_resp = client.get(f"{QDRANT_URL}{snapshot_url}")
        return snapshot_resp.content


# ── Main Ingestion Task ────────────────────
@app.task(
    bind=True,
    name="tasks.ingest_document",
    max_retries=MAX_RETRIES,
    default_retry_delay=RETRY_BACKOFF,
    retry_backoff=True,
    retry_jitter=True,
)
def ingest_document(self, document_id: str, file_path: str, department: str, file_type: str):
    """
    Main document ingestion pipeline:
    1. Parse document
    2. Chunk text
    3. Generate embeddings
    4. Upload to Qdrant
    5. Build BM25 index
    6. Upload to MinIO
    7. Update database status
    """
    import asyncio
    from parsers import parse_document
    from chunker import chunk_document

    logger.info(f"Starting ingestion: document_id={document_id} file={file_path}")

    try:
        # ── Step 1: Parse document ──
        logger.info(f"Parsing document: {file_path}")
        parsed_content = parse_document(file_path)

        if not parsed_content:
            raise ValueError("Failed to parse document - no content extracted")

        # ── Step 2: Chunk text ──
        logger.info(f"Chunking document: {len(parsed_content)} sections")
        chunks = chunk_document(parsed_content, file_type)
        logger.info(f"Created {len(chunks)} chunks")

        if not chunks:
            raise ValueError("No chunks created from document")

        # ── Step 3: Generate embeddings ──
        logger.info("Generating embeddings via Ollama")
        embeddings = []
        for chunk in chunks:
            try:
                import httpx as req
                resp = req.post(
                    f"{OLLAMA_URL}/api/embeddings",
                    json={
                        "model": "nomic-embed-text",
                        "prompt": chunk["text"],
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    embedding = resp.json()["embedding"]
                    embeddings.append(embedding)
                else:
                    logger.warning(f"Embedding failed for chunk: {chunk['text'][:50]}...")
                    embeddings.append([0.0] * 768)
            except Exception as e:
                logger.warning(f"Embedding error: {e}")
                embeddings.append([0.0] * 768)

        # ── Step 4: Upload to Qdrant ──
        logger.info("Uploading vectors to Qdrant")
        points = []
        for chunk, embedding in zip(chunks, embeddings):
            points.append({
                "id": str(uuid.uuid4()),
                "vector": embedding,
                "payload": {
                    "text": chunk["text"],
                    "filename": chunk.get("filename", ""),
                    "page": chunk.get("page", 0),
                    "section": chunk.get("section", ""),
                    "document_id": document_id,
                    "department": department or "",
                    "file_type": file_type,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "version": 1,
                },
            })

        # Ensure collection exists
        import httpx as req
        try:
            req.put(
                f"{QDRANT_URL}/collections/documents",
                json={
                    "vectors": {"size": 768, "distance": "Cosine"},
                },
                timeout=10,
            )
        except Exception:
            pass  # Collection might already exist

        # Upsert in batches
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            resp = req.post(
                f"{QDRANT_URL}/collections/documents/points/upsert",
                json={"points": batch},
                timeout=30,
            )
            if resp.status_code != 200:
                raise ValueError(f"Qdrant upsert failed: {resp.text}")

        logger.info(f"Uploaded {len(points)} points to Qdrant")

        # ── Step 5: Build BM25 Index ──
        logger.info("Building BM25 index")
        try:
            from rank_bm25 import BM25Okapi
            import redis

            tokenized_corpus = [chunk["text"].lower().split() for chunk in chunks]

            r = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/3"), decode_responses=True
            )
            r.set(f"bm25_tokens:{document_id}", json.dumps(tokenized_corpus))
            r.set(f"bm25_chunks:{document_id}", json.dumps(chunks))
            r.delete(f"bm25:{document_id}")
            r.close()
            logger.info("BM25 index saved to Redis")
        except Exception as e:
            logger.warning(f"BM25 index build failed: {e}")

        # ── Step 6: Upload to MinIO ──
        logger.info("Uploading original file to MinIO")
        minio_path = f"{department or 'general'}/{os.path.basename(file_path)}"
        upload_to_minio(file_path, minio_path)

        # ── Step 7: Update database status ──
        logger.info("Updating database status to 'indexed'")
        asyncio.run(
            _update_document_status(document_id, "indexed", len(chunks), minio_path)
        )

        logger.info(
            f"Ingestion complete: document_id={document_id} "
            f"chunks={len(chunks)} minio_path={minio_path}"
        )

        return {
            "document_id": document_id,
            "status": "indexed",
            "chunk_count": len(chunks),
            "minio_path": minio_path,
        }

    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        asyncio.run(
            _update_document_status(document_id, "failed", error_msg=str(e))
        )

        # Send alert on final retry exhaustion
        if self.request.retries >= self.max_retries - 1:
            send_alert(
                level="critical",
                title=f"Ingestion failed — all {self.max_retries} retries exhausted",
                message=str(e),
                metadata={
                    "document_id": document_id,
                    "file_path": file_path,
                    "department": department,
                    "file_type": file_type,
                    "retries": self.request.retries,
                },
            )
        else:
            send_alert(
                level="warning",
                title=f"Ingestion retry {self.request.retries + 1}/{self.max_retries}",
                message=str(e),
                metadata={
                    "document_id": document_id,
                    "retry_count": self.request.retries + 1,
                },
            )

        raise self.retry(exc=e)


async def _update_document_status(
    document_id: str,
    status: str,
    chunk_count: int = 0,
    minio_path: str = "",
    error_msg: str = "",
):
    """Update document status in PostgreSQL."""
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if status == "indexed":
            await conn.execute(
                """UPDATE documents
                   SET status = $1, chunk_count = $2, minio_path = $3,
                       indexed_at = NOW(), error_msg = NULL
                   WHERE id = $4""",
                status,
                chunk_count,
                minio_path,
                document_id,
            )
        else:
            await conn.execute(
                """UPDATE documents
                   SET status = $1, error_msg = $2, retry_count = retry_count + 1
                   WHERE id = $3""",
                status,
                error_msg,
                document_id,
            )
    finally:
        await conn.close()


# ── Delete Vectors Task ────────────────────
@app.task(name="tasks.delete_document_vectors")
def delete_document_vectors(document_id: str):
    """Delete all Qdrant points and BM25 data for a document."""
    logger.info(f"Deleting vectors for document: {document_id}")

    # Delete from Qdrant
    success = delete_qdrant_vectors(document_id)
    if success:
        logger.info(f"Deleted Qdrant vectors for document: {document_id}")
    else:
        logger.warning(f"Failed to delete Qdrant vectors for document: {document_id}")

    # Delete BM25 index from Redis
    try:
        import redis

        r = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/3"), decode_responses=True
        )
        r.delete(f"bm25_tokens:{document_id}")
        r.delete(f"bm25_chunks:{document_id}")
        r.delete(f"bm25:{document_id}")
        r.close()
    except Exception as e:
        logger.warning(f"Failed to delete BM25 index: {e}")

    return {"document_id": document_id, "deleted": success}


# ── Alert Helper ──────────────────────────
def send_alert(level: str, title: str, message: str, metadata: dict = None):
    """Send an alert to the configured webhook URL."""
    if not ALERT_WEBHOOK_URL:
        logger.warning(f"Alert suppressed (no webhook URL): {title} — {message}")
        return False

    try:
        import httpx

        payload = {
            "level": level,
            "title": title,
            "message": message,
            "service": "ingestion-worker",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }

        resp = httpx.post(ALERT_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code in (200, 201, 202):
            logger.info(f"Alert sent: {title}")
            return True
        else:
            logger.warning(f"Alert webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Failed to send alert: {e}")
        return False


# ── RAGAS Evaluation Task ──────────────────
@app.task(name="tasks.run_ragas_evaluation")
def run_ragas_evaluation():
    """
    Weekly RAGAS evaluation: sample answered queries and compute quality metrics.
    Uses the ragas library with Ollama (OpenAI-compatible endpoint).
    """
    import asyncio

    logger.info("Starting RAGAS evaluation")

    try:
        # Get recent queries with feedback
        rows = asyncio.run(_get_evaluation_samples(50))

        if not rows or len(rows) == 0:
            logger.warning("No queries available for evaluation")
            return {"status": "skipped", "reason": "no queries"}

        # Build dataset for ragas
        questions = []
        answers = []
        contexts_list = []
        ground_truths = []

        for row in rows:
            questions.append(row["question"])
            answers.append(row["answer"])
            # Parse sources JSON to extract context text
            sources = row["sources"]
            if isinstance(sources, str):
                try:
                    sources = json.loads(sources)
                except Exception:
                    sources = []
            if isinstance(sources, list) and len(sources) > 0:
                chunk_texts = [s.get("text", "") if isinstance(s, dict) else str(s) for s in sources]
                contexts_list.append(chunk_texts)
            else:
                contexts_list.append([""])  # placeholder
            ground_truths.append("")  # No ground truth available; faithfulness will still be computed

        if len(questions) == 0:
            return {"status": "skipped", "reason": "no valid samples"}

        # Compute metrics using ragas library with Ollama backend
        try:
            from ragas import evaluate
            from ragas.metrics import (
                faithfulness,
                answer_relevancy,
                context_precision,
                context_recall,
            )
            from datasets import Dataset
            from langchain_community.chat_models import ChatOllama

            # Build HuggingFace dataset
            data = {
                "question": questions,
                "answer": answers,
                "contexts": contexts_list,
            }
            if any(g for g in ground_truths):
                data["ground_truth"] = ground_truths

            dataset = Dataset.from_dict(data)

            # Configure LangChain + Ollama for ragas
            llm = ChatOllama(
                model=os.getenv("OLLAMA_MODEL", "llama3.2:latest"),
                base_url=OLLAMA_URL,
                temperature=0,
            )

            # Run evaluation
            result = evaluate(
                dataset,
                metrics=[
                    faithfulness,
                    answer_relevancy,
                    context_precision,
                    context_recall,
                ],
                llm=llm,
            )

            # Extract scores from the result DataFrame
            df = result.to_pandas()
            import pandas as _pd
            evaluation = {
                "sample_size": len(questions),
                "faithfulness": round(float(df["faithfulness"].mean()), 4) if "faithfulness" in df.columns else 0.0,
                "answer_relevancy": round(float(df["answer_relevancy"].mean()), 4) if "answer_relevancy" in df.columns else 0.0,
                "context_precision": round(float(df["context_precision"].mean()), 4) if "context_precision" in df.columns else 0.0,
                "context_recall": round(float(df["context_recall"].mean()), 4) if "context_recall" in df.columns else 0.0,
                "avg_latency_ms": 0.0,  # RAGAS doesn't output latency natively
            }

        except ImportError as ie:
            logger.warning(f"RAGAS library not available: {ie}")
            return {"status": "skipped", "reason": "ragas not installed"}
        except Exception as ragas_err:
            logger.error(f"RAGAS computation failed: {ragas_err}")
            send_alert("error", "RAGAS evaluation failed", str(ragas_err), {"task": "run_ragas_evaluation"})
            return {"status": "failed", "error": str(ragas_err)}

        asyncio.run(_store_evaluation(evaluation))

        logger.info(f"RAGAS evaluation complete: {evaluation}")
        return evaluation

    except Exception as e:
        logger.error(f"RAGAS evaluation failed: {e}")
        send_alert("error", "RAGAS evaluation failed", str(e), {"task": "run_ragas_evaluation"})
        return {"status": "failed", "error": str(e)}


async def _get_evaluation_samples(limit: int):
    """Get recent queries with feedback for evaluation."""
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """SELECT id, question, answer, sources, feedback
               FROM queries
               WHERE feedback IS NOT NULL
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )
        return rows
    finally:
        await conn.close()


async def _store_evaluation(evaluation: dict):
    """Store RAGAS evaluation results."""
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """INSERT INTO ragas_evaluations
               (evaluated_at, sample_size, faithfulness, answer_relevancy,
                context_precision, context_recall, avg_latency_ms)
               VALUES (NOW(), $1, $2, $3, $4, $5, $6)""",
            evaluation["sample_size"],
            evaluation["faithfulness"],
            evaluation["answer_relevancy"],
            evaluation["context_precision"],
            evaluation["context_recall"],
            evaluation["avg_latency_ms"],
        )
    finally:
        await conn.close()


def _cleanup_old_backups(bucket: str, prefix: str, retention_days: int = 30) -> int:
    """Delete backup objects older than retention_days from MinIO/S3."""
    import boto3
    from botocore.client import Config

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_URL,
            aws_access_key_id=MINIO_USER,
            aws_secret_access_key=MINIO_PASSWORD,
            config=Config(signature_version="s3v4"),
        )
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"].replace(tzinfo=timezone.utc) < cutoff:
                    s3.delete_object(Bucket=bucket, Key=obj["Key"])
                    deleted += 1
                    logger.info(f"Deleted expired backup: {obj['Key']}")
    except Exception as e:
        logger.warning(f"Backup retention cleanup failed: {e}")

    return deleted


# ── Backup Tasks ───────────────────────────
@app.task(name="tasks.backup_postgres")
def backup_postgres():
    """Nightly PostgreSQL backup: pg_dump gzip → MinIO, retain 30 days."""
    import subprocess
    import gzip
    from datetime import datetime

    logger.info("Starting PostgreSQL backup")

    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_file = f"/tmp/pg_backup_{date_str}.sql.gz"
        minio_path = f"postgres/{date_str}.sql.gz"

        # pg_dump
        cmd = [
            "pg_dump",
            "-h", "postgres",
            "-U", os.getenv("POSTGRES_USER", "kcadmin"),
            "-d", os.getenv("POSTGRES_DB", "knowledge_centre"),
            "-F", "plain",
            "--no-owner",
            "--no-privileges",
        ]

        env = os.environ.copy()
        env["PGPASSWORD"] = os.getenv("POSTGRES_PASSWORD", "")

        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=600
        )

        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {result.stderr}")

        # Compress
        with open(backup_file, "wb") as f_out:
            with gzip.open(f_out, "wb") as f_gz:
                f_gz.write(result.stdout.encode())

        # Verify backup size
        backup_size = os.path.getsize(backup_file)
        if backup_size < 100:
            raise RuntimeError(f"Backup too small ({backup_size} bytes), likely empty")

        # Upload to MinIO backups bucket
        import boto3
        from botocore.client import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_URL,
            aws_access_key_id=MINIO_USER,
            aws_secret_access_key=MINIO_PASSWORD,
            config=Config(signature_version="s3v4"),
        )
        try:
            s3.head_bucket(Bucket="backups")
        except Exception:
            s3.create_bucket(Bucket="backups")

        with open(backup_file, "rb") as f:
            s3.put_object(Bucket="backups", Key=minio_path, Body=f.read())

        _cleanup_old_backups("backups", "postgres/", retention_days=30)

        # Cleanup local file
        os.remove(backup_file)

        logger.info(
            f"PostgreSQL backup complete: {minio_path} size={backup_size} bytes"
        )
        return {"status": "success", "path": minio_path, "size": backup_size}

    except Exception as e:
        logger.error(f"PostgreSQL backup failed: {e}")
        return {"status": "failed", "error": str(e)}


@app.task(name="tasks.backup_qdrant")
def backup_qdrant():
    """Weekly Qdrant snapshot backup → MinIO."""
    import gzip
    from datetime import datetime

    logger.info("Starting Qdrant backup")

    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshot_data = get_qdrant_snapshot()
        minio_path = f"qdrant/{date_str}.snapshot.gz"

        # Compress
        compressed = gzip.compress(snapshot_data)

        # Upload to MinIO
        import boto3
        from botocore.client import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_URL,
            aws_access_key_id=MINIO_USER,
            aws_secret_access_key=MINIO_PASSWORD,
            config=Config(signature_version="s3v4"),
        )

        try:
            s3.head_bucket(Bucket="backups")
        except Exception:
            s3.create_bucket(Bucket="backups")

        s3.put_object(Bucket="backups", Key=minio_path, Body=compressed)

        _cleanup_old_backups("backups", "qdrant/", retention_days=30)

        # Verify size
        if len(compressed) < 1024:
            logger.warning(f"Qdrant snapshot suspiciously small: {len(compressed)} bytes")

        logger.info(f"Qdrant backup complete: {minio_path} size={len(compressed)} bytes")
        return {"status": "success", "path": minio_path, "size": len(compressed)}

    except Exception as e:
        logger.error(f"Qdrant backup failed: {e}")
        return {"status": "failed", "error": str(e)}
