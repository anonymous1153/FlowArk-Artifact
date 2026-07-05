"""知识库相关模块导出。"""

from flowark.knowledge.manager import KnowledgeManager, SkillRecord
from flowark.knowledge.analysis_log_rag import AnalysisLogRagRouter
from flowark.knowledge.embedding_router import EmbeddingKnowledgeRouter
from flowark.knowledge.pipeline import KnowledgeStore, KnowledgeSynthesizer, KnowledgeValidator
from flowark.knowledge.router import KnowledgeRouter, RuleKnowledgeRouter

_RUNTIME_AUGMENT_EXPORTS = {
    "compute_after_tool_augment",
    "compute_request_submit_augment",
    "format_skills",
}

__all__ = [
    "KnowledgeManager",
    "SkillRecord",
    "KnowledgeRouter",
    "RuleKnowledgeRouter",
    "AnalysisLogRagRouter",
    "EmbeddingKnowledgeRouter",
    "KnowledgeSynthesizer",
    "KnowledgeValidator",
    "KnowledgeStore",
    "compute_after_tool_augment",
    "compute_request_submit_augment",
    "format_skills",
]


def __getattr__(name: str) -> object:
    if name in _RUNTIME_AUGMENT_EXPORTS:
        from flowark.knowledge import runtime_augment

        value = getattr(runtime_augment, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'flowark.knowledge' has no attribute {name!r}")
