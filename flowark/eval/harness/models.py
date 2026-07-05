"""Core data models for evaluation harness."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flowark.runtime.config import (
    RUNTIME_INJECTION_CONTEXT_AWARE,
    RUNTIME_INJECTION_START_ONLY,
    KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES,
    KNOWLEDGE_DISTILLATION_GENERIC,
    normalize_knowledge_runtime_modes,
    normalize_runtime_injection_mode,
)
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    is_analysis_log_rag_packaging_mode,
    is_embedding_backed_packaging_mode,
    normalize_knowledge_packaging_mode,
)

from .common import (
    DEFAULT_SINK_CATEGORIES,
    DEFAULT_TASK_TIMEOUT_SECONDS,
    _normalize_optional_positive_int,
    normalize_classification_filter,
)

FLOWARK_STUDIO_REQUESTED_PARAMS_ENV = "FLOWARK_STUDIO_REQUESTED_PARAMS_JSON"
FLOWARK_STUDIO_EFFECTIVE_PARAMS_ENV = "FLOWARK_STUDIO_EFFECTIVE_PARAMS_JSON"
FLOWARK_STUDIO_NORMALIZATION_WARNINGS_ENV = "FLOWARK_STUDIO_NORMALIZATION_WARNINGS_JSON"


@dataclass(slots=True)
class EvalCase:
    flow_id: str
    dataset: str
    app_name: str
    apk_name: str
    source_dir: str
    source_id: str | None = None
    benchmark_family: str | None = None
    target_sink_categories: list[str] = field(default_factory=list)
    classification: str | None = None
    source_method: str | None = None
    source_classname: str | None = None
    source_statement: str | None = None
    sink_method: str | None = None
    sink_classname: str | None = None
    sink_statement: str | None = None
    sink_entries: list[dict[str, Any]] = field(default_factory=list)
    ground_truth_sink_categories: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EvaluationConfig:
    input_path: Path
    out_dir: Path
    agent_adapter: str = "opencode"
    opencode_binary: str | None = None
    opencode_model: str = ""
    opencode_provider: str = "anthropic"
    opencode_after_tool_delivery: str = "no_reply_context"
    opencode_bash_policy: str = "read_only_guarded"
    opencode_post_phase_mode: str = "plain_json_same_surface"
    opencode_structured_output: bool = True
    modes: list[str] = field(default_factory=lambda: ["naive", "flowark"])
    parallel: int = 2
    serialize_within_app: bool = True
    repeats: int = 1
    max_cases: int | None = None  # backward-compatible alias of max_sources
    max_apps: int | None = None
    max_sources: int | None = None
    app_names: list[str] = field(default_factory=list)
    classification_filter: str = "all"
    dummy_run: bool = False
    knowledge_mode: str = "warm"
    knowledge_allow_repeat_injection_within_session: bool = True
    auto_knowledge_cycle: bool = True
    runtime_injection_mode: str = RUNTIME_INJECTION_CONTEXT_AWARE
    knowledge_distillation_mode: str = KNOWLEDGE_DISTILLATION_WITH_SELECTION_RULES
    knowledge_packaging_mode: str = KNOWLEDGE_PACKAGING_DSL_RULE
    auto_knowledge_validate_mode: str = "static"
    knowledge_reuse_digest_mode: str = "off"
    knowledge_repeat_summary_react_gap: int = 0
    knowledge_repeat_full_react_gap: int = 1
    code_recall_intensity: str = "normal"
    knowledge_top_k: int = 3
    knowledge_recall_top_m: int = 8
    sink_categories: list[str] = field(default_factory=lambda: list(DEFAULT_SINK_CATEGORIES))
    timeout_seconds: int | None = DEFAULT_TASK_TIMEOUT_SECONDS
    runtime_backend_profile: str | None = None
    runtime_backend_mode: str = "single"
    runtime_backend_pool: str | None = None
    runtime_backend_pool_candidates: list[dict[str, Any]] = field(default_factory=list)
    runtime_backend_base_url: str | None = None
    runtime_backend_auth_token: str | None = None
    runtime_backend_model: str | None = None
    llm_judge_enabled: bool = True
    llm_judge_base_url: str = ""
    llm_judge_api_key: str = ""
    llm_judge_model: str = ""
    llm_judge_timeout_seconds: int = 120
    llm_judge_max_retries: int = 2
    reuse_embed_base_url: str | None = None
    reuse_embed_api_key: str | None = None
    reuse_embed_model: str | None = None
    reuse_embed_verify_ssl: bool = False
    reuse_rerank_base_url: str | None = None
    reuse_rerank_api_key: str | None = None
    reuse_rerank_model: str | None = None
    reuse_rerank_timeout_seconds: int = 60
    requested_params: dict[str, Any] = field(default_factory=dict)
    effective_params: dict[str, Any] = field(default_factory=dict)
    normalization_warnings: list[str] = field(default_factory=list)

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
        flowark_enabled = "flowark" in self.normalized_modes()
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
            if flowark_enabled:
                self.serialize_within_app = True
                self.repeats = 1
        if self.knowledge_packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL:
            self.runtime_injection_mode = RUNTIME_INJECTION_START_ONLY
        self.agent_adapter = "opencode"
        flowark_modes = {str(mode or "").strip().lower() for mode in self.modes or []}
        if "native" in flowark_modes:
            flowark_modes.discard("native")
            flowark_modes.add("naive")
        self.runtime_backend_mode = "single"
        self.runtime_backend_pool = None
        self.runtime_backend_pool_candidates = []

    def normalized_modes(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for mode in self.modes:
            m = str(mode or "").strip().lower()
            if m == "native":
                m = "naive"
            if m not in {"naive", "flowark"}:
                raise ValueError(f"不支持的模式: {mode}")
            if m in seen:
                continue
            seen.add(m)
            result.append(m)
        if not result:
            raise ValueError("modes 不能为空")
        return result

    def normalized_max_apps(self) -> int | None:
        return _normalize_optional_positive_int(self.max_apps, field_name="max_apps")

    def effective_max_sources(self) -> int | None:
        if self.max_sources is not None:
            return _normalize_optional_positive_int(self.max_sources, field_name="max_sources")
        if self.max_cases is not None:
            return _normalize_optional_positive_int(self.max_cases, field_name="max_cases")
        return None

    def normalized_classification_filter(self) -> str:
        return normalize_classification_filter(self.classification_filter)

    def normalized_app_names(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in self.app_names or []:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out


def validate_embedding_packaging_backend_config(config: EvaluationConfig) -> None:
    if "flowark" not in config.normalized_modes():
        return
    packaging_mode = normalize_knowledge_packaging_mode(config.knowledge_packaging_mode)
    if not is_embedding_backed_packaging_mode(packaging_mode):
        return
    missing = []
    if not str(config.reuse_embed_base_url or "").strip():
        missing.append("reuse_embed_base_url")
    if not str(config.reuse_embed_api_key or "").strip():
        missing.append("reuse_embed_api_key")
    if missing:
        raise ValueError(
            f"knowledge_packaging_mode={packaging_mode} 需要配置 embedding 后端: "
            + ", ".join(missing)
        )


@dataclass(slots=True)
class EvalTask:
    case: EvalCase
    mode: str
    repeat_idx: int
    repeat_dir: Path
    task_index: int = 0
    task_total: int = 0
