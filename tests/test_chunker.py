"""Tests for document chunking module."""

import pytest
from chunker import chunk_document


class TestChunker:
    def test_short_text_single_chunk(self):
        sections = [{"text": "This is a short document.", "page": 1, "section": "Intro", "filename": "test.txt"}]
        chunks = chunk_document(sections, "txt")
        assert len(chunks) == 1
        assert chunks[0]["text"] == "This is a short document."
        assert chunks[0]["filename"] == "test.txt"

    def test_empty_text(self):
        sections = [{"text": "", "page": 1, "section": "", "filename": "test.txt"}]
        chunks = chunk_document(sections, "txt")
        assert len(chunks) == 0

    def test_long_text_multiple_chunks(self):
        long_text = " ".join(["word"] * 2000)
        sections = [{"text": long_text, "page": 1, "section": "", "filename": "test.txt"}]
        chunks = chunk_document(sections, "txt")
        assert len(chunks) > 1

    def test_metadata_preserved(self):
        text = "Test content " * 100
        sections = [{"text": text, "page": 5, "section": "Chapter 1", "filename": "report.pdf"}]
        chunks = chunk_document(sections, "pdf")
        for c in chunks:
            assert c["filename"] == "report.pdf"
            assert "text" in c
            assert "page" in c
            assert "section" in c

    def test_excel_row_chunks(self):
        sections = [
            {"text": f"col_a: val{i} | col_b: data{i}", "page": 0, "section": "Sheet1", "filename": "data.xlsx"}
            for i in range(100)
        ]
        chunks = chunk_document(sections, "xlsx")
        assert len(chunks) >= 1
        # Excel chunks should be smaller
        for c in chunks:
            assert "col_a" in c["text"] or c["word_count"] <= 256

    def test_multiple_sections(self):
        sections = [
            {"text": "Section one content here.", "page": 1, "section": "Intro", "filename": "doc.txt"},
            {"text": "Section two content here.", "page": 2, "section": "Body", "filename": "doc.txt"},
        ]
        chunks = chunk_document(sections, "txt")
        assert len(chunks) == 2
        assert chunks[0]["section"] == "Intro"
        assert chunks[1]["section"] == "Body"

    def test_word_count_present(self):
        sections = [{"text": "Hello world this is a test.", "page": 1, "section": "", "filename": "test.txt"}]
        chunks = chunk_document(sections, "txt")
        assert len(chunks) == 1
        assert chunks[0]["word_count"] == 6
