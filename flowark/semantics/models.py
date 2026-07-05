"""Platform-agnostic semantic models for FlowArk adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_DSL_RULE,
    normalize_knowledge_packaging_mode,
)
from flowark.runtime.config import (
    RUNTIME_INJECTION_CONTEXT_AWARE,
    normalize_runtime_injection_mode,
)

from flowark.types import AnalysisRequest, KnowledgeCandidate, ValidationResult


class Phase(str, Enum):
    ANALYSIS = "analysis"
    FINAL_REPORT = "final_report"
    KNOWLEDGE_SYNTH = "knowledge_synth"
    KNOWLEDGE_RULE_REPAIR = "knowledge_rule_repair"


@dataclass(slots=True)
class SessionHandle:
    adapter_name: str
    session_id: str
    lineage_id: str | None = None


@dataclass(slots=True)
class PhasePolicy:
    phase: Phase
    allow_request_submit_augment: bool
    allow_after_tool_augment: bool
    trace_channel: str


@dataclass(slots=True)
class TurnContract:
    turn_name: str | None = None
    transcript_prefix: str | None = None
    echo: bool = False
    expect_json: bool = False
    timeout_seconds: int | None = None
    max_turns: int | None = None


@dataclass(slots=True)
class AnalysisRunContext:
    run_dir: Path | None
    prompt: str
    turn_name: str
    echo: bool = True


@dataclass(slots=True)
class AugmentRuntimeConfig:
    knowledge_mode: str = "warm"
    skills_dir: Path | None = None
    runtime_injection_mode: str = RUNTIME_INJECTION_CONTEXT_AWARE
    knowledge_packaging_mode: str = KNOWLEDGE_PACKAGING_DSL_RULE
    knowledge_top_k: int = 3
    knowledge_recall_top_m: int = 8
    knowledge_min_score: float = 1.0
    knowledge_injection_char_budget: int = 4000
    knowledge_delta_char_budget: int | None = None
    knowledge_allow_repeat_within_session: bool = True
    knowledge_repeat_summary_hook_gap: int = 3
    knowledge_repeat_full_hook_gap: int = 10
    knowledge_repeat_summary_react_gap: int = 0
    knowledge_repeat_full_react_gap: int = 1
    knowledge_realtime_min_interval_ms: int = 1500
    knowledge_injection_log_path: Path | None = None
    reuse_embed_base_url: str | None = None
    reuse_embed_api_key: str | None = None
    reuse_embed_model: str | None = None
    reuse_embed_verify_ssl: bool = False


def augment_runtime_config_to_payload(config: AugmentRuntimeConfig) -> dict[str, object]:
    return {
        "knowledge_mode": config.knowledge_mode,
        "skills_dir": str(config.skills_dir) if config.skills_dir is not None else None,
        "runtime_injection_mode": normalize_runtime_injection_mode(
            config.runtime_injection_mode
        ),
        "knowledge_packaging_mode": normalize_knowledge_packaging_mode(
            config.knowledge_packaging_mode
        ),
        "knowledge_top_k": config.knowledge_top_k,
        "knowledge_recall_top_m": config.knowledge_recall_top_m,
        "knowledge_min_score": config.knowledge_min_score,
        "knowledge_injection_char_budget": config.knowledge_injection_char_budget,
        "knowledge_delta_char_budget": config.knowledge_delta_char_budget,
        "knowledge_allow_repeat_within_session": config.knowledge_allow_repeat_within_session,
        "knowledge_repeat_summary_hook_gap": config.knowledge_repeat_summary_hook_gap,
        "knowledge_repeat_full_hook_gap": config.knowledge_repeat_full_hook_gap,
        "knowledge_repeat_summary_react_gap": config.knowledge_repeat_summary_react_gap,
        "knowledge_repeat_full_react_gap": config.knowledge_repeat_full_react_gap,
        "knowledge_realtime_min_interval_ms": config.knowledge_realtime_min_interval_ms,
        "knowledge_injection_log_path": (
            str(config.knowledge_injection_log_path)
            if config.knowledge_injection_log_path is not None
            else None
        ),
        "reuse_embed_base_url": config.reuse_embed_base_url,
        "reuse_embed_api_key": config.reuse_embed_api_key,
        "reuse_embed_model": config.reuse_embed_model,
        "reuse_embed_verify_ssl": bool(config.reuse_embed_verify_ssl),
    }


def augment_runtime_config_from_payload(payload: dict[str, object]) -> AugmentRuntimeConfig:
    def _path(key: str) -> Path | None:
        value = payload.get(key)
        text = str(value or "").strip()
        return Path(text).expanduser() if text else None

    def _int_value(key: str, default: int, *, minimum: int) -> int:
        value = payload.get(key)
        if value in {None, ""}:
            return default
        try:
            return max(minimum, int(value))
        except Exception:
            return default

    def _bool_value(key: str, default: bool = False) -> bool:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    return AugmentRuntimeConfig(
        knowledge_mode=str(payload.get("knowledge_mode") or "warm"),
        skills_dir=_path("skills_dir"),
        runtime_injection_mode=normalize_runtime_injection_mode(
            str(payload.get("runtime_injection_mode") or "context_aware")
        ),
        knowledge_packaging_mode=normalize_knowledge_packaging_mode(
            str(payload.get("knowledge_packaging_mode") or "dsl_rule")
        ),
        knowledge_top_k=max(1, int(payload.get("knowledge_top_k") or 3)),
        knowledge_recall_top_m=max(1, int(payload.get("knowledge_recall_top_m") or 8)),
        knowledge_min_score=float(payload.get("knowledge_min_score") or 1.0),
        knowledge_injection_char_budget=max(
            256,
            int(payload.get("knowledge_injection_char_budget") or 4000),
        ),
        knowledge_delta_char_budget=(
            max(256, int(payload["knowledge_delta_char_budget"]))
            if payload.get("knowledge_delta_char_budget") is not None
            else None
        ),
        knowledge_allow_repeat_within_session=_bool_value(
            "knowledge_allow_repeat_within_session",
            default=True,
        ),
        knowledge_repeat_summary_hook_gap=_int_value("knowledge_repeat_summary_hook_gap", 3, minimum=1),
        knowledge_repeat_full_hook_gap=_int_value("knowledge_repeat_full_hook_gap", 10, minimum=2),
        knowledge_repeat_summary_react_gap=_int_value("knowledge_repeat_summary_react_gap", 0, minimum=0),
        knowledge_repeat_full_react_gap=_int_value("knowledge_repeat_full_react_gap", 1, minimum=0),
        knowledge_realtime_min_interval_ms=max(
            0,
            int(payload.get("knowledge_realtime_min_interval_ms") or 1500),
        ),
        knowledge_injection_log_path=_path("knowledge_injection_log_path"),
        reuse_embed_base_url=str(payload.get("reuse_embed_base_url") or "").strip() or None,
        reuse_embed_api_key=str(payload.get("reuse_embed_api_key") or "").strip() or None,
        reuse_embed_model=str(payload.get("reuse_embed_model") or "").strip() or None,
        reuse_embed_verify_ssl=_bool_value("reuse_embed_verify_ssl", default=False),
    )


@dataclass(slots=True)
class RequestSubmitContext:
    session: SessionHandle
    phase: Phase
    user_prompt: str
    transcript_path: str | None = None
    app_name: str | None = None
    source: str | None = None
    sink_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AfterToolContext:
    session: SessionHandle
    phase: Phase
    tool_name: str
    tool_input: Any
    tool_output: Any
    transcript_path: str | None = None
    delta_context: str | None = None
    app_name: str | None = None
    source: str | None = None
    sink_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AugmentationPayload:
    text: str
    matched_ids: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AugmentDecision:
    should_deliver: bool
    payload: AugmentationPayload | None = None
    reason: str | None = None


class DeliveryStatus(str, Enum):
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class AugmentDeliveryResult:
    status: DeliveryStatus
    reason: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FinalReportPhaseInput:
    request: AnalysisRequest


@dataclass(slots=True)
class KnowledgeSynthPhaseInput:
    request: AnalysisRequest
    current_case_profile: dict[str, Any] | None = None
    validated_skill_catalog: list[dict[str, str]] | None = None
    repairable_skill_catalog: list[dict[str, str]] | None = None
    historical_reuse_digest_block: str = ""


@dataclass(slots=True)
class KnowledgeRuleRepairPhaseInput:
    request: AnalysisRequest
    candidate: KnowledgeCandidate
    static_result: ValidationResult
    issue_types: list[str] = field(default_factory=list)


PhaseInput = (
    FinalReportPhaseInput
    | KnowledgeSynthPhaseInput
    | KnowledgeRuleRepairPhaseInput
)


@dataclass(slots=True)
class PhaseSpec:
    phase: Phase
    phase_input: PhaseInput
    instruction: str
    policy: PhasePolicy
    turn_contract: TurnContract


@dataclass(slots=True)
class TurnOutcome:
    raw_text: str | None
    messages: list[str] = field(default_factory=list)
    turn_metrics: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class AnalysisRunResult:
    session: SessionHandle
    outcome: TurnOutcome
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PhaseRunResult:
    phase: Phase
    session: SessionHandle
    outcome: TurnOutcome
