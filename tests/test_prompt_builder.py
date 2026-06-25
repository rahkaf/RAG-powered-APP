"""Tests for the RAG prompt builder."""

import pytest
from prompt_builder import build_prompt, extract_sources, suggest_document_category


class TestPromptBuilder:
    def test_basic_build(self):
        context_chunks = [
            {"text": "Rate limit is 20 req/min per user.", "score": 0.95, "filename": "policy.pdf", "page": 3, "section": "Limits"}
        ]
        messages = build_prompt("What is the API rate limit?", context_chunks)
        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "20 req/min" in messages[1]["content"]
        assert "API rate limit" in messages[1]["content"]

    def test_multiple_chunks(self):
        chunks = [
            {"text": "The rate limit is 20 requests per minute.", "score": 0.9, "filename": "p1.pdf", "page": 1, "section": "Limits"},
            {"text": "Exceeding the limit returns HTTP 429.", "score": 0.8, "filename": "p1.pdf", "page": 2, "section": "Errors"},
            {"text": "Rate limits are per-user, tracked by JWT.", "score": 0.7, "filename": "p1.pdf", "page": 3, "section": "Auth"},
        ]
        messages = build_prompt("What are the rate limiting rules?", chunks)
        user_content = messages[1]["content"]
        for chunk in chunks:
            assert chunk["text"] in user_content

    def test_suggest_category(self):
        messages = build_prompt(
            "How do I deploy the service?",
            [{"text": "Deploy via docker compose up -d", "score": 0.9, "filename": "guide.md", "page": 1, "section": "Deploy"}],
            suggest_category="engineering",
        )
        assert "engineering" in messages[0]["content"]

    def test_no_context(self):
        messages = build_prompt("What is the meaning of life?", [])
        assert "What is the meaning of life?" in messages[1]["content"]

    def test_system_prompt_contains_instructions(self):
        messages = build_prompt("test", [{"text": "test context", "score": 1.0, "filename": "t.txt", "page": 1, "section": ""}])
        system = messages[0]["content"]
        assert len(system) > 50
        assert "cite" in system.lower() or "source" in system.lower()


class TestExtractSources:
    def test_unique_sources(self):
        chunks = [
            {"text": "a", "score": 0.9, "filename": "f1.pdf", "page": 1, "section": "A"},
            {"text": "b", "score": 0.8, "filename": "f1.pdf", "page": 1, "section": "A"},  # duplicate
            {"text": "c", "score": 0.7, "filename": "f1.pdf", "page": 2, "section": "B"},
        ]
        sources = extract_sources(chunks)
        assert len(sources) == 2
        assert sources[0]["page"] == 1
        assert sources[1]["page"] == 2

    def test_empty_chunks(self):
        sources = extract_sources([])
        assert sources == []


class TestSuggestDocumentCategory:
    def test_procedure_keywords(self):
        assert "procedure" in suggest_document_category("How do I perform this step?").lower()

    def test_technical_keywords(self):
        assert "technical" in suggest_document_category("Show me the specification diagram").lower()

    def test_safety_keywords(self):
        assert "safety" in suggest_document_category("What are the hazard warnings?").lower()

    def test_default_category(self):
        result = suggest_document_category("random unrelated question")
        assert isinstance(result, str)
        assert len(result) > 0
