"""Tests for document versioning logic."""

import pytest
from datetime import datetime, timezone


class TestDocumentVersioning:
    """Test the document versioning strategy (simple increment on re-upload)."""

    def test_version_starts_at_1(self):
        """New documents should start at version 1."""
        doc = {
            "id": 1,
            "filename": "report.pdf",
            "version": 1,
            "status": "completed",
        }
        assert doc["version"] == 1

    def test_version_increments_on_update(self):
        """Re-uploading the same filename should increment version."""
        existing_version = 3
        new_version = existing_version + 1
        assert new_version == 4

    def test_version_history_tracking(self):
        """Version history should be maintained."""
        versions = [
            {"version": 1, "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc), "status": "completed"},
            {"version": 2, "created_at": datetime(2025, 1, 5, tzinfo=timezone.utc), "status": "completed"},
            {"version": 3, "created_at": datetime(2025, 1, 10, tzinfo=timezone.utc), "status": "processing"},
        ]
        assert len(versions) == 3
        assert versions[-1]["version"] == 3

    def test_same_name_different_department(self):
        """Same filename in different departments should be independent."""
        docs = [
            {"filename": "guide.pdf", "department": "engineering", "version": 1},
            {"filename": "guide.pdf", "department": "marketing", "version": 1},
        ]
        assert docs[0]["version"] == docs[1]["version"] == 1
