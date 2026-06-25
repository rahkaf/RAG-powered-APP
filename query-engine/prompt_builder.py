"""
AI Knowledge Centre - Prompt Builder
Constructs the RAG prompt with context chunks and citations.
"""

from typing import List, Dict, Any

SYSTEM_PROMPT = """You are a precise assistant for engineering, operations, and maintenance teams.
Rules:
1. Answer ONLY using the provided context. Never use external knowledge.
2. Always cite: [Source: {filename}, Page {page}, Section: {section}]
3. If the answer is not in the context, respond exactly:
   "This information was not found in the available documents.
    Recommended documents to check manually: {suggest_category}"
4. For safety-critical procedures, add: WARNING: Always verify with
   the original document before performing this procedure.
5. Be concise. Use numbered steps for procedures. Use bullet points for lists."""


def build_prompt(
    question: str,
    context_chunks: List[Dict[str, Any]],
    suggest_category: str = "general",
) -> List[Dict[str, str]]:
    """
    Build the RAG prompt with context and question.

    Args:
        question: User's question
        context_chunks: Reranked chunks with scores and metadata
        suggest_category: Category to suggest if answer not found

    Returns:
        List of message dicts (system + user) for LLM
    """
    # Build context section
    context_parts = []
    for i, chunk in enumerate(context_chunks, 1):
        score = chunk.get("score", 0)
        text = chunk.get("text", "")
        filename = chunk.get("filename", "unknown")
        page = chunk.get("page", 0)
        section = chunk.get("section", "N/A")

        context_parts.append(
            f"[Chunk {i} - Score: {score:.2f}]: {text} "
            f"[Source: {filename}, Page {page}]"
        )

    context_block = "\n\n".join(context_parts)

    # Format system prompt with suggestion category
    system = SYSTEM_PROMPT.replace("{suggest_category}", suggest_category)

    # Build full prompt
    user_prompt = f"""CONTEXT:
{context_block}

QUESTION: {question}

ANSWER:"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]


def extract_sources(context_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extract unique sources from context chunks for the response.

    Returns:
        List of source dicts with filename, page, section, score
    """
    sources = []
    seen = set()

    for chunk in context_chunks:
        key = (
            chunk.get("filename", ""),
            chunk.get("page", 0),
            chunk.get("section", ""),
        )

        if key not in seen:
            seen.add(key)
            sources.append({
                "filename": chunk.get("filename", ""),
                "page": chunk.get("page", 0),
                "section": chunk.get("section", "N/A"),
                "score": round(chunk.get("score", 0), 4),
                "text_preview": chunk.get("text", "")[:200],
            })

    return sources


def suggest_document_category(question: str) -> str:
    """
    Suggest a document category based on the question keywords.

    Returns:
        Category string (e.g., 'SOP', 'manual', 'policy')
    """
    question_lower = question.lower()

    if any(kw in question_lower for kw in ["procedure", "step", "how to", "process"]):
        return "SOP or procedure manual"
    elif any(kw in question_lower for kw in ["specification", "technical", "diagram"]):
        return "technical manual"
    elif any(kw in question_lower for kw in ["policy", "rule", "compliance"]):
        return "policy document"
    elif any(kw in question_lower for kw in ["maintenance", "repair", "service"]):
        return "maintenance manual"
    elif any(kw in question_lower for kw in ["safety", "hazard", "warning"]):
        return "safety documentation"
    else:
        return "relevant departmental documents"
