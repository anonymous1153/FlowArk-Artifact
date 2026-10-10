"""FlowArk package exports."""

from flowark.reporting import ReportNormalizer
from flowark.runtime.config import AnalysisRequest, RunArtifacts, RunConfig
from flowark.types import (
    DataflowPath,
    EvidenceRef,
    FinalDataflowReport,
    KnowledgeCandidate,
    KnowledgeInjectionRecord,
    KnowledgeMatch,
    SinkFinding,
    ValidationResult,
)

__all__ = [
    "AnalysisRequest",
    "RunConfig",
    "RunArtifacts",
    "ReportNormalizer",
    "DataflowPath",
    "EvidenceRef",
    "FinalDataflowReport",
    "KnowledgeCandidate",
    "KnowledgeInjectionRecord",
    "KnowledgeMatch",
    "SinkFinding",
    "ValidationResult",
]

try:  # pragma: no cover - runner imports are optional for lightweight package imports
    from flowark.runtime.runner import FlowArkRunner
except Exception:  # pragma: no cover
    FlowArkRunner = None  # type: ignore[assignment]
else:
    __all__.append("FlowArkRunner")
