"""
AI Knowledge Centre - Reranker Module
Cross-encoder reranking using ms-marco-MiniLM-L-6-v2.
"""

import os
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

RERANKER_MODEL = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
TOP_K_RERANKED = int(os.getenv("TOP_K_RERANKED", "5"))


class Reranker:
    """Cross-encoder reranker for search results."""

    def __init__(self):
        self.model = None
        self._load_model()

    def _load_model(self):
        """Load the cross-encoder model."""
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"Loading reranker model: {RERANKER_MODEL}")
            self.model = CrossEncoder(RERANKER_MODEL)
            logger.info("Reranker model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load reranker model: {e}")
            self.model = None

    def rerank(
        self,
        query: str,
        results: List[Dict[str, Any]],
        top_k: int = None,
    ) -> List[Dict[str, Any]]:
        """
        Rerank search results using cross-encoder scoring.

        Args:
            query: The original query text
            results: List of search results with text field
            top_k: Number of results to return (defaults to TOP_K_RERANKED)

        Returns:
            Reranked results with cross-encoder scores
        """
        if not self.model:
            logger.warning("Reranker model not loaded, returning original results")
            return results[:top_k or TOP_K_RERANKED]

        if not results:
            return []

        top_k = top_k or TOP_K_RERANKED

        # Build query-document pairs
        pairs = [(query, r.get("text", "")) for r in results]

        # Score all pairs
        scores = self.model.predict(pairs)

        # Attach scores to results
        for result, score in zip(results, scores):
            result["rerank_score"] = float(score)

        # Sort by rerank score descending
        results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)

        # Return top-k
        reranked = results[:top_k]

        # Update the main score to be the rerank score
        for r in reranked:
            r["score"] = r.get("rerank_score", r.get("score", 0))

        logger.info(
            f"Reranked {len(results)} results → top {len(reranked)} "
            f"(score range: {reranked[0]['score']:.4f} to {reranked[-1]['score']:.4f})"
        )

        return reranked
