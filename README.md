# AI Knowledge Centre

A production-grade Retrieval-Augmented Generation (RAG) system for enterprise document management and intelligent querying.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Nginx (443)                         │
│              Edge Security + Reverse Proxy               │
└────────┬──────────┬──────────┬──────────┬───────────────┘
         │          │          │          │
    ┌────▼───┐ ┌───▼────┐ ┌──▼─────┐ ┌─▼────────┐
    │OpenWeb │ │ API    │ │ Admin  │ │ Grafana  │
    │  UI    │ │Gateway │ │ Panel  │ │  :3000   │
    │ :8080  │ │ :8000  │ │ :8080  │ │          │
    └────────┘ └───┬────┘ └───┬────┘ └──────────┘
                   │          │
         ┌─────────┴──────────┴─────────┐
         │        Core Services          │
         │  ┌──────────┐  ┌──────────┐  │
         │  │ Ingestion│  │  Query   │  │
         │  │ Worker   │  │  Engine  │  │
         │  │ (Celery) │  │  :8001   │  │
         │  └────┬─────┘  └────┬─────┘  │
         │       │              │        │
         │  ┌────▼──────────────▼────┐   │
         │  │    Data Layer          │   │
         │  │ Postgres │ Qdrant │ MinIO│  │
         │  │ Redis    │ Ollama │      │  │
         │  └────────────────────────┘   │
         └──────────────────────────────┘
```

## Quick Start

### Prerequisites
- Docker & Docker Compose v2+
- 16GB+ RAM recommended
- 50GB+ free disk space

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env with your settings (at minimum, set JWT_SECRET and POSTGRES_PASSWORD)
```

### 2. Generate SSL certificates (optional for dev)

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/ssl/key.pem -out nginx/ssl/cert.pem \
  -subj "/CN=localhost"
```

### 3. Start all services

```bash
docker compose up -d
```

### 4. Pull the LLM model

```bash
docker compose exec ollama ollama pull llama3.2:latest
```

### 5. Access the services

| Service | URL | Description |
|---------|-----|-------------|
| Web UI | https://localhost/ | Chat interface for querying documents |
| Admin Panel | https://localhost/admin/ | User & document management |
| API Gateway | https://localhost/api/ | REST API for programmatic access |
| Grafana | https://localhost/grafana/ | Observability dashboards |
| Langfuse | https://localhost/langfuse/ | LLM tracing & evaluation |

### Default Admin Credentials
- **Username:** `admin`
- **Password:** `admin123` (change immediately!)

## API Usage

### Authentication
```bash
# Login
curl -X POST https://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'

# Use the returned token
export TOKEN="your-jwt-token"
```

### Upload a Document
```bash
curl -X POST https://localhost/api/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@document.pdf" \
  -F "department=engineering"
```

### Query Documents
```bash
curl -X POST https://localhost/api/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "What are our API rate limits?", "department": "engineering"}'
```

## Backup & Restore

### Create Backup
```bash
curl -X POST https://localhost/api/admin/backup \
  -H "X-Admin-Secret: your-admin-secret" \
  -d '{"backup_type": "full"}'
```

### Restore from Backup
```bash
curl -X POST https://localhost/api/admin/restore \
  -H "X-Admin-Secret: your-admin-secret" \
  -d '{"backup_id": "backup_full_20250101_120000.sql.gz"}'
```

## Supported Document Types

| Format | Extension | Parser |
|--------|-----------|--------|
| PDF | `.pdf` | PyMuPDF + OCR fallback |
| Word | `.docx` | python-docx |
| Excel | `.xlsx` | openpyxl |
| PowerPoint | `.pptx` | python-pptx |
| Plain Text | `.txt` | Native |
| Markdown | `.md` | Native |
| CSV | `.csv` | pandas |
| Images | `.png/.jpg/.tiff` | Tesseract OCR |
| HTML | `.html` | BeautifulSoup |

## Environment Variables

See `.env.example` for the complete list. Key variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `JWT_SECRET` | JWT signing key (64-char hex) | Required |
| `POSTGRES_PASSWORD` | Database password | Required |
| `OLLAMA_MODEL` | LLM model for generation | `llama3.2:latest` |
| `EMBEDDING_MODEL` | Sentence transformer model | `all-MiniLM-L6-v2` |
| `RATE_LIMIT_PER_MINUTE` | API rate limit | `20` |

## Architecture Decisions

- **Ollama** for local LLM inference (no external API keys needed)
- **Qdrant** for vector storage (supports filtering, fast similarity search)
- **Hybrid Search** (BM25 + vector) with RRF fusion for best retrieval quality
- **Celery** for async document ingestion with retry logic
- **Langfuse** for LLM observability and prompt versioning
- **Grafana + Prometheus + Loki** for full observability stack

## License

Private — Internal Use Only
# RAG-powered-APP
