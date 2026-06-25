"""
AI Knowledge Centre - Document Chunker
Chunks documents based on file type with configurable token sizes and overlaps.
"""

import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# Chunking configuration by document type
CHUNK_CONFIGS = {
    # SOPs and policies: larger chunks with overlap
    "sop": {"chunk_size": 512, "overlap": 50},
    "policy": {"chunk_size": 512, "overlap": 50},
    # Manuals: even larger chunks
    "manual": {"chunk_size": 768, "overlap": 100},
    "docx": {"chunk_size": 768, "overlap": 100},
    # Excel: small chunks, no overlap (row-wise)
    "xlsx": {"chunk_size": 256, "overlap": 0},
    "xls": {"chunk_size": 256, "overlap": 0},
    # Notes and text files
    "txt": {"chunk_size": 384, "overlap": 64},
    "md": {"chunk_size": 384, "overlap": 64},
    "csv": {"chunk_size": 384, "overlap": 64},
    # Default
    "default": {"chunk_size": 512, "overlap": 50},
}


def chunk_document(
    sections: List[Dict[str, Any]],
    file_type: str,
) -> List[Dict[str, Any]]:
    """
    Chunk parsed document sections into appropriately sized pieces.

    Args:
        sections: List of parsed sections with text, page, section metadata
        file_type: File extension/type for chunk config selection

    Returns:
        List of chunks with text and metadata
    """
    config = CHUNK_CONFIGS.get(file_type.lower(), CHUNK_CONFIGS["default"])
    chunk_size = config["chunk_size"]
    overlap = config["overlap"]

    chunks = []

    for section in sections:
        text = section.get("text", "")
        if not text:
            continue

        # If text is short enough, keep as single chunk
        word_count = len(text.split())
        if word_count <= chunk_size:
            chunks.append({
                "text": text,
                "page": section.get("page", 0),
                "section": section.get("section", ""),
                "filename": section.get("filename", ""),
                "word_count": word_count,
            })
            continue

        # Split using recursive character splitting
        section_chunks = _recursive_split(
            text, chunk_size, overlap, section
        )
        chunks.extend(section_chunks)

    logger.info(
        f"Chunked {len(sections)} sections into {len(chunks)} chunks "
        f"(type={file_type}, size={chunk_size}, overlap={overlap})"
    )

    return chunks


def _recursive_split(
    text: str,
    chunk_size: int,
    overlap: int,
    metadata: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Recursively split text into chunks by paragraphs, sentences, then words.
    """
    # Try splitting by paragraphs first
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 1:
        chunks = []
        current_chunk = []

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            current_chunk.append(para)
            current_text = "\n\n".join(current_chunk)

            if len(current_text.split()) > chunk_size:
                if len(current_chunk) > 1:
                    # Remove last paragraph and save current chunk
                    current_chunk.pop()
                    chunk_text = "\n\n".join(current_chunk)
                    chunks.append({
                        "text": chunk_text,
                        "page": metadata.get("page", 0),
                        "section": metadata.get("section", ""),
                        "filename": metadata.get("filename", ""),
                        "word_count": len(chunk_text.split()),
                    })
                    # Start new chunk with overlap
                    if overlap > 0:
                        overlap_text = _get_overlap_text(chunk_text, overlap)
                        current_chunk = [overlap_text, para] if overlap_text else [para]
                    else:
                        current_chunk = [para]
                else:
                    # Single paragraph too large, split by sentences
                    sentence_chunks = _split_by_sentences(para, chunk_size, overlap, metadata)
                    chunks.extend(sentence_chunks)
                    current_chunk = []

        # Save remaining text
        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            if chunk_text.strip():
                chunks.append({
                    "text": chunk_text,
                    "page": metadata.get("page", 0),
                    "section": metadata.get("section", ""),
                    "filename": metadata.get("filename", ""),
                    "word_count": len(chunk_text.split()),
                })

        return chunks
    else:
        # Single paragraph, split by sentences
        return _split_by_sentences(text, chunk_size, overlap, metadata)


def _split_by_sentences(
    text: str,
    chunk_size: int,
    overlap: int,
    metadata: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Split text by sentences."""
    import re

    # Simple sentence splitting
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current_chunk = []

    for sentence in sentences:
        current_chunk.append(sentence)
        current_text = " ".join(current_chunk)

        if len(current_text.split()) > chunk_size:
            if len(current_chunk) > 1:
                current_chunk.pop()
                chunk_text = " ".join(current_chunk)
                chunks.append({
                    "text": chunk_text,
                    "page": metadata.get("page", 0),
                    "section": metadata.get("section", ""),
                    "filename": metadata.get("filename", ""),
                    "word_count": len(chunk_text.split()),
                })
                if overlap > 0:
                    overlap_text = _get_overlap_text(chunk_text, overlap)
                    current_chunk = [overlap_text, sentence] if overlap_text else [sentence]
                else:
                    current_chunk = [sentence]
            else:
                # Single sentence too large, split by words
                word_chunks = _split_by_words(sentence, chunk_size, metadata)
                chunks.extend(word_chunks)
                current_chunk = []

    # Save remaining text
    if current_chunk:
        chunk_text = " ".join(current_chunk)
        if chunk_text.strip():
            chunks.append({
                "text": chunk_text,
                "page": metadata.get("page", 0),
                "section": metadata.get("section", ""),
                "filename": metadata.get("filename", ""),
                "word_count": len(chunk_text.split()),
            })

    return chunks


def _split_by_words(
    text: str,
    chunk_size: int,
    metadata: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Split text by words (fallback for very long sentences)."""
    words = text.split()
    chunks = []

    for i in range(0, len(words), chunk_size):
        chunk_words = words[i : i + chunk_size]
        chunk_text = " ".join(chunk_words)
        chunks.append({
            "text": chunk_text,
            "page": metadata.get("page", 0),
            "section": metadata.get("section", ""),
            "filename": metadata.get("filename", ""),
            "word_count": len(chunk_words),
        })

    return chunks


def _get_overlap_text(text: str, overlap_words: int) -> str:
    """Get the last N words of text for overlap."""
    words = text.split()
    if len(words) <= overlap_words:
        return text
    return " ".join(words[-overlap_words:])
