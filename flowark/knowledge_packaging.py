"""Knowledge packaging mode helpers."""

from __future__ import annotations


KNOWLEDGE_PACKAGING_DSL_RULE = "dsl_rule"
KNOWLEDGE_PACKAGING_EMBEDDING = "embedding"
KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG = "analysis_log_rag"
KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL = "analysis_log_rag_initial"
KNOWLEDGE_PACKAGING_MODES = {
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
}
KNOWLEDGE_PACKAGING_METADATA_KEY = "knowledge_packaging_mode"


def normalize_knowledge_packaging_mode(value: str | None) -> str:
    mode = str(value or KNOWLEDGE_PACKAGING_DSL_RULE).strip().lower()
    if mode not in KNOWLEDGE_PACKAGING_MODES:
        return KNOWLEDGE_PACKAGING_DSL_RULE
    return mode


def is_embedding_backed_packaging_mode(value: str | None) -> bool:
    return normalize_knowledge_packaging_mode(value) in {
        KNOWLEDGE_PACKAGING_EMBEDDING,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    }


def is_analysis_log_rag_packaging_mode(value: str | None) -> bool:
    return normalize_knowledge_packaging_mode(value) in {
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    }
