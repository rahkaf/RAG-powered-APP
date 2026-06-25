-- ============================================
-- AI Knowledge Centre - PostgreSQL Init Script
-- ============================================

-- Create the langfuse database
CREATE DATABASE langfuse;

-- Create extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Users ──────────────────────────────────
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'engineer',
    department VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES users(id)
);

-- ── Documents ──────────────────────────────
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(50) NOT NULL,
    department VARCHAR(100),
    file_size_mb NUMERIC(10,2),
    minio_path VARCHAR(1000),
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    chunk_count INTEGER DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    checksum VARCHAR(128) NOT NULL,
    is_latest BOOLEAN NOT NULL DEFAULT TRUE,
    error_msg TEXT,
    retry_count INTEGER DEFAULT 0,
    uploaded_by UUID REFERENCES users(id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    indexed_at TIMESTAMPTZ
);

-- ── Queries ────────────────────────────────
CREATE TABLE queries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id),
    session_id VARCHAR(100),
    question TEXT NOT NULL,
    answer TEXT,
    sources JSONB DEFAULT '[]'::jsonb,
    filters_applied JSONB DEFAULT '{}'::jsonb,
    latency_ms INTEGER,
    vector_search_ms INTEGER,
    reranker_ms INTEGER,
    llm_ms INTEGER,
    tokens_used INTEGER,
    feedback SMALLINT,
    feedback_note TEXT,
    langfuse_trace VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Ingestion Jobs ─────────────────────────
CREATE TABLE ingestion_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID REFERENCES documents(id),
    celery_task_id VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    attempt INTEGER DEFAULT 0,
    error_msg TEXT,
    stack_trace TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RAGAS Evaluations ──────────────────────
CREATE TABLE ragas_evaluations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sample_size INTEGER NOT NULL,
    faithfulness NUMERIC(5,4),
    answer_relevancy NUMERIC(5,4),
    context_precision NUMERIC(5,4),
    context_recall NUMERIC(5,4),
    avg_latency_ms NUMERIC(10,2)
);

-- ── Indexes ────────────────────────────────
CREATE INDEX idx_queries_user_id ON queries(user_id);
CREATE INDEX idx_queries_created_at ON queries(created_at DESC);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_department ON documents(department);
CREATE INDEX idx_documents_uploaded_by ON documents(uploaded_by);
CREATE INDEX idx_documents_checksum ON documents(checksum);
CREATE INDEX idx_ingestion_jobs_document_id ON ingestion_jobs(document_id);
CREATE INDEX idx_ingestion_jobs_celery_task_id ON ingestion_jobs(celery_task_id);
CREATE INDEX idx_queries_session_id ON queries(session_id);
CREATE INDEX idx_queries_feedback ON queries(feedback);

-- ── Default Admin User ─────────────────────
-- Password: admin123 (bcrypt hash, cost 12)
INSERT INTO users (username, email, password, role, department, is_active)
VALUES (
    'admin',
    'admin@knowledge-centre.local',
    '$2b$12$LJ3m4ys3Lg.KVxI7Ym4dWuQxKjH8x3p9R2sT5vY7bC6dE9fG0hI1j',
    'admin',
    'management',
    TRUE
);
