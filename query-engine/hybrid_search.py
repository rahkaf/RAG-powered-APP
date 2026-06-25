"""
AI Knowledge Centre - Hybrid Search Engine
Combines vector search (Qdrant) with BM25 keyword search using Reciprocal Rank Fusion.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
import redis
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/3")
TOP_K_VECTOR = int(os.getenv("TOP_K_VECTOR", "20"))
TOP_K_BM25 = int(os.getenv("TOP_K_BM25", "20"))
RRF_K = 60


class HybridSearch:
    """Hybrid search combining vector and BM25 retrieval with RRF."""

    def __init__(self):
        self.redis_client = None
        self.bm25_indices = {}
        self._connect_redis()

    def _connect_redis(self):
        """Connect to Redis for BM25 index storage."""
        try:
            self.redis_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True
            )
            self.redis_client.ping()
            logger.info("Connected to Redis for BM25 indices")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")

    def _scan_keys(self, pattern: str) -> List[str]:
        """Iterate Redis keys using SCAN instead of KEYS."""
        if not self.redis_client:
            return []

        keys: List[str] = []
        cursor = 0
        while True:
            cursor, batch = self.redis_client.scan(cursor=cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        return keys

    def search(
        self,
        query_embedding: List[float],
        query_text: str,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """Perform hybrid search combining vector and BM25 results."""
        vector_results = self._vector_search(query_embedding, filters)
        bm25_results = self._bm25_search(query_text)
        merged = self._reciprocal_rank_fusion(vector_results, bm25_results)
        merged.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return merged[:top_k]

    def _vector_search(
        self,
        query_embedding: List[float],
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search Qdrant for similar vectors."""
        try:
            search_payload = {
                "vector": query_embedding,
                "limit": TOP_K_VECTOR,
                "with_payload": True,
            }

            if filters:
                search_payload["filter"] = self._build_qdrant_filter(filters)

            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{QDRANT_URL}/collections/documents/points/search",
                    json=search_payload,
                )

                if resp.status_code != 200:
                    logger.error(f"Qdrant search failed: {resp.text}")
                    return []

                results = resp.json().get("result", [])
                return [
                    {
                        "text": r.get("payload", {}).get("text", ""),
                        "filename": r.get("payload", {}).get("filename", ""),
                        "page": r.get("payload", {}).get("page", 0),
                        "section": r.get("payload", {}).get("section", ""),
                        "document_id": r.get("payload", {}).get("document_id", ""),
                        "department": r.get("payload", {}).get("department", ""),
                        "file_type": r.get("payload", {}).get("file_type", ""),
                        "score": r.get("score", 0),
                        "source": "vector",
                    }
                    for r in results
                ]
        except Exception as e:
            logger.error(f"Vector search error: {e}")
            return []

    def _build_qdrant_filter(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Build Qdrant filter payload from query filters."""
        must_conditions = []

        if filters.get("department"):
            must_conditions.append({
                "key": "department",
                "match": {"value": filters["department"]},
            })

        if filters.get("file_type"):
            must_conditions.append({
                "key": "file_type",
                "match": {"value": filters["file_type"]},
            })

        if filters.get("document_id"):
            must_conditions.append({
                "key": "document_id",
                "match": {"value": filters["document_id"]},
            })

        if must_conditions:
            return {"must": must_conditions}
        return {}

    def _bm25_search(self, query_text: str) -> List[Dict[str, Any]]:
        """Search using BM25 indices stored in Redis (JSON, no pickle)."""
        if not self.redis_client:
            return []

        try:
            query_tokens = query_text.lower().split()
            if not query_tokens:
                return []

            all_results = []

            for key in self._scan_keys("bm25_tokens:*"):
                doc_id = key.replace("bm25_tokens:", "")
                try:
                    tokens_data = self.redis_client.get(key)
                    chunks_data = self.redis_client.get(f"bm25_chunks:{doc_id}")

                    if not tokens_data or not chunks_data:
                        continue

                    tokenized_corpus = json.loads(tokens_data)
                    chunks = json.loads(chunks_data)
                    bm25 = BM25Okapi(tokenized_corpus)
                    scores = bm25.get_scores(query_tokens)

                    import numpy as np

                    top_indices = np.argsort(scores)[::-1][:TOP_K_BM25]

                    for idx in top_indices:
                        if scores[idx] > 0 and idx < len(chunks):
                            chunk = chunks[idx]
                            all_results.append({
                                "text": chunk.get("text", ""),
                                "filename": chunk.get("filename", ""),
                                "page": chunk.get("page", 0),
                                "section": chunk.get("section", ""),
                                "document_id": doc_id,
                                "department": chunk.get("department", ""),
                                "file_type": chunk.get("file_type", ""),
                                "score": float(scores[idx]),
                                "source": "bm25",
                            })
                except Exception as e:
                    logger.warning(f"BM25 search error for {doc_id}: {e}")
                    continue

            return all_results

        except Exception as e:
            logger.error(f"BM25 search error: {e}")
            return []

    def _reciprocal_rank_fusion(
        self,
        vector_results: List[Dict[str, Any]],
        bm25_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge results using Reciprocal Rank Fusion (RRF)."""
        merged = {}

        for rank, result in enumerate(vector_results):
            key = self._result_key(result)
            rrf_score = 1.0 / (RRF_K + rank + 1)

            if key in merged:
                merged[key]["rrf_score"] += rrf_score
                merged[key]["vector_rank"] = rank + 1
            else:
                merged[key] = {**result, "rrf_score": rrf_score, "vector_rank": rank + 1}

        for rank, result in enumerate(bm25_results):
            key = self._result_key(result)
            rrf_score = 1.0 / (RRF_K + rank + 1)

            if key in merged:
                merged[key]["rrf_score"] += rrf_score
                merged[key]["bm25_rank"] = rank + 1
            else:
                merged[key] = {**result, "rrf_score": rrf_score, "bm25_rank": rank + 1}

        return list(merged.values())

    def _result_key(self, result: Dict[str, Any]) -> str:
        """Generate a deduplication key for a search result."""
        return f"{result.get('text', '')[:100]}:{result.get('filename', '')}:{result.get('page', 0)}"

    def load_bm25_index(self, document_id: str) -> bool:
        """Load a specific BM25 index from Redis into memory."""
        if not self.redis_client:
            return False

        try:
            tokens_data = self.redis_client.get(f"bm25_tokens:{document_id}")
            chunks_data = self.redis_client.get(f"bm25_chunks:{document_id}")

            if tokens_data and chunks_data:
                self.bm25_indices[document_id] = {
                    "tokens": json.loads(tokens_data),
                    "chunks": json.loads(chunks_data),
                }
                return True
            return False
        except Exception as e:
            logger.warning(f"Failed to load BM25 index for {document_id}: {e}")
            return False

    def rebuild_all_indices(self):
        """Reload all BM25 indices from Redis."""
        if not self.redis_client:
            return

        loaded = 0
        for key in self._scan_keys("bm25_tokens:*"):
            doc_id = key.replace("bm25_tokens:", "")
            if self.load_bm25_index(doc_id):
                loaded += 1

        logger.info(f"Loaded {loaded} BM25 indices from Redis")
