"""FlowArk 运行配置与请求类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    is_analysis_log_rag_packaging_mode,
    is_embedding_backed_packaging_mode,
    normalize_knowledge_packaging_mode,
)
from flowark.types import AnalysisRequest, RunArtifacts


KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES = "with_selection_rules"
KNOWLEDGE_DISTILLATION_GENERIC = "generic"
KNOWLEDGE_DISTILLATION_MODES = {
    KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES,
    KNOWLEDGE_DISTILLATION_GENERIC,
}

RUNTIME_INJECTION_CONTEXT_AWARE = "context_aware"
RUNTIME_INJECTION_START_ONLY = "start_only"
RUNTIME_INJECTION_MODES = {
    RUNTIME_INJECTION_CONTEXT_AWARE,
    RUNTIME_INJECTION_START_ONLY,
}


def normalize_knowledge_distillation_mode(value: str | None) -> str:
    mode = str(value or KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES).strip().lower()
    if mode not in KNOWLEDGE_DISTILLATION_MODES:
        return KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES
    return mode


def normalize_runtime_injection_mode(value: str | None) -> str:
    mode = str(value or RUNTIME_INJECTION_CONTEXT_AWARE).strip().lower()
    if mode not in RUNTIME_INJECTION_MODES:
        return RUNTIME_INJECTION_CONTEXT_AWARE
    return mode


def normalize_auto_knowledge_validate_mode(value: str | None) -> str:
    mode = str(value or "static").strip().lower()
    if mode == "full":
        raise ValueError("auto_knowledge_validate_mode=full 已删除，请改用 static 或 off")
    if mode not in {"off", "static"}:
        raise ValueError("auto_knowledge_validate_mode 必须是 off 或 static")
    return mode


def normalize_knowledge_reuse_digest_mode(value: str | None) -> str:
    mode = str(value or "off").strip().lower()
    if mode not in {"off", "live_corridor", "live_corridor_v2"}:
        return "off"
    return mode


def normalize_knowledge_runtime_modes(
    *,
    knowledge_distillation_mode: str | None,
    auto_knowledge_validate_mode: str | None,
    knowledge_reuse_digest_mode: str | None,
) -> tuple[str, str, str]:
    distillation_mode = normalize_knowledge_distillation_mode(knowledge_distillation_mode)
    validate_mode = normalize_auto_knowledge_validate_mode(auto_knowledge_validate_mode)
    digest_mode = normalize_knowledge_reuse_digest_mode(knowledge_reuse_digest_mode)
    if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
        return distillation_mode, "off", "off"
    return distillation_mode, validate_mode, digest_mode


@dataclass(slots=True)
class RunConfig:
    """FlowArk 运行配置。"""

    cwd: Path
    out_dir: Path | None = None
    agent_adapter: str = "opencode"
    opencode_binary: str | None = None
    opencode_model: str = ""
    opencode_provider: str = "anthropic"
    opencode_after_tool_delivery: str = "no_reply_context"
    opencode_bash_policy: str = "read_only_guarded"
    opencode_post_phase_mode: str = "plain_json_same_surface"
    opencode_structured_output: bool = True
    agent_mode: str = "flowark"
    knowledge_mode: str = "warm"
    knowledge_allow_repeat_injection_within_session: bool = True
    auto_knowledge_cycle: bool = True
    runtime_injection_mode: str = RUNTIME_INJECTION_CONTEXT_AWARE
    knowledge_distillation_mode: str = KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES
    knowledge_packaging_mode: str = KNOWLEDGE_PACKAGING_DSL_RULE
    auto_knowledge_validate_mode: str = "static"
    knowledge_reuse_digest_mode: str = "off"
    knowledge_top_k: int = 3
    knowledge_recall_top_m: int = 8
    knowledge_min_score: float = 1.0
    knowledge_injection_char_budget: int = 4000
    knowledge_delta_char_budget: int | None = None
    knowledge_realtime_min_interval_ms: int = 1500
    knowledge_repeat_summary_hook_gap: int = 3
    knowledge_repeat_full_hook_gap: int = 10
    knowledge_repeat_summary_react_gap: int = 0
    knowledge_repeat_full_react_gap: int = 1
    reuse_embed_base_url: str | None = None
    reuse_embed_api_key: str | None = None
    reuse_embed_model: str | None = None
    reuse_embed_verify_ssl: bool = False
    reuse_rerank_base_url: str | None = None
    reuse_rerank_api_key: str | None = None
    reuse_rerank_model: str | None = None
    reuse_rerank_timeout_seconds: int = 60
    skills_dir: Path | None = None
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Task", "Read", "Grep", "Glob"]
    )

    def __post_init__(self) -> None:
        self.runtime_injection_mode = normalize_runtime_injection_mode(
            self.runtime_injection_mode
        )
        distillation_mode, validate_mode, digest_mode = normalize_knowledge_runtime_modes(
            knowledge_distillation_mode=self.knowledge_distillation_mode,
            auto_knowledge_validate_mode=self.auto_knowledge_validate_mode,
            knowledge_reuse_digest_mode=self.knowledge_reuse_digest_mode,
        )
        self.knowledge_distillation_mode = distillation_mode
        self.auto_knowledge_validate_mode = validate_mode
        self.knowledge_reuse_digest_mode = digest_mode
        self.knowledge_packaging_mode = normalize_knowledge_packaging_mode(
            self.knowledge_packaging_mode
        )
        agent_name = str(self.agent_mode or "flowark").strip().lower()
        flowark_enabled = agent_name != "naive"
        if (
            flowark_enabled
            and self.knowledge_distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC
            and self.knowledge_packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING
        ):
            raise ValueError(
                "knowledge_packaging_mode=embedding 不支持搭配 "
                "knowledge_distillation_mode=generic"
            )
        if self.knowledge_packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
            self.auto_knowledge_validate_mode = "off"
        if is_analysis_log_rag_packaging_mode(self.knowledge_packaging_mode):
            self.auto_knowledge_cycle = False
            self.auto_knowledge_validate_mode = "off"
            self.knowledge_reuse_digest_mode = "off"
        if self.knowledge_packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL:
            self.runtime_injection_mode = RUNTIME_INJECTION_START_ONLY
        self.agent_adapter = "opencode"
        if flowark_enabled and is_embedding_backed_packaging_mode(self.knowledge_packaging_mode):
            missing = []
            if not str(self.reuse_embed_base_url or "").strip():
                missing.append("reuse_embed_base_url")
            if not str(self.reuse_embed_api_key or "").strip():
                missing.append("reuse_embed_api_key")
            if missing:
                raise ValueError(
                    f"knowledge_packaging_mode={self.knowledge_packaging_mode} 需要配置 embedding 后端: "
                    + ", ".join(missing)
                )
