"""Runtime context shared by the runner and knowledge hooks."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path

from flowark.semantics.models import (
    AugmentRuntimeConfig,
    augment_runtime_config_from_payload,
    augment_runtime_config_to_payload,
)
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_DSL_RULE,
    normalize_knowledge_packaging_mode,
)
from flowark.runtime.config import (
    RUNTIME_INJECTION_CONTEXT_AWARE,
    normalize_runtime_injection_mode,
)


@dataclass(slots=True)
class HookRuntimeContext:
    knowledge_mode: str = "warm"
    skills_dir: Path | None = None
    runtime_injection_mode: str = RUNTIME_INJECTION_CONTEXT_AWARE
    knowledge_packaging_mode: str = KNOWLEDGE_PACKAGING_DSL_RULE
    analysis_cwd: Path | None = None
    analysis_app_name: str | None = None
    analysis_source: str | None = None
    analysis_sink_types: list[str] = field(default_factory=list)
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
    command_hook_trace_path: Path | None = None
    session_role_override: str | None = None
    augment_runtime_config: AugmentRuntimeConfig | None = None
    reuse_embed_base_url: str | None = None
    reuse_embed_api_key: str | None = None
    reuse_embed_model: str | None = None
    reuse_embed_verify_ssl: bool = False


_HOOK_RUNTIME_CONTEXT: ContextVar[HookRuntimeContext | None] = ContextVar(
    "flowark_hook_runtime_context",
    default=None,
)


def set_hook_runtime_context(context: HookRuntimeContext) -> Token[HookRuntimeContext | None]:
    return _HOOK_RUNTIME_CONTEXT.set(context)


def reset_hook_runtime_context(token: Token[HookRuntimeContext | None]) -> None:
    _HOOK_RUNTIME_CONTEXT.reset(token)


def get_hook_runtime_context() -> HookRuntimeContext | None:
    return _HOOK_RUNTIME_CONTEXT.get()


def hook_runtime_context_to_payload(context: HookRuntimeContext) -> dict[str, object]:
    return {
        "knowledge_mode": context.knowledge_mode,
        "skills_dir": str(context.skills_dir) if context.skills_dir is not None else None,
        "runtime_injection_mode": normalize_runtime_injection_mode(
            context.runtime_injection_mode
        ),
        "knowledge_packaging_mode": normalize_knowledge_packaging_mode(
            context.knowledge_packaging_mode
        ),
        "analysis_cwd": str(context.analysis_cwd) if context.analysis_cwd is not None else None,
        "analysis_app_name": context.analysis_app_name,
        "analysis_source": context.analysis_source,
        "analysis_sink_types": list(context.analysis_sink_types),
        "knowledge_top_k": context.knowledge_top_k,
        "knowledge_recall_top_m": context.knowledge_recall_top_m,
        "knowledge_min_score": context.knowledge_min_score,
        "knowledge_injection_char_budget": context.knowledge_injection_char_budget,
        "knowledge_delta_char_budget": context.knowledge_delta_char_budget,
        "knowledge_allow_repeat_within_session": context.knowledge_allow_repeat_within_session,
        "knowledge_repeat_summary_hook_gap": context.knowledge_repeat_summary_hook_gap,
        "knowledge_repeat_full_hook_gap": context.knowledge_repeat_full_hook_gap,
        "knowledge_repeat_summary_react_gap": context.knowledge_repeat_summary_react_gap,
        "knowledge_repeat_full_react_gap": context.knowledge_repeat_full_react_gap,
        "knowledge_realtime_min_interval_ms": context.knowledge_realtime_min_interval_ms,
        "knowledge_injection_log_path": (
            str(context.knowledge_injection_log_path)
            if context.knowledge_injection_log_path is not None
            else None
        ),
        "command_hook_trace_path": (
            str(context.command_hook_trace_path)
            if context.command_hook_trace_path is not None
            else None
        ),
        "session_role_override": context.session_role_override,
        "augment_runtime_config": (
            augment_runtime_config_to_payload(context.augment_runtime_config)
            if context.augment_runtime_config is not None
            else None
        ),
        "reuse_embed_base_url": context.reuse_embed_base_url,
        "reuse_embed_api_key": context.reuse_embed_api_key,
        "reuse_embed_model": context.reuse_embed_model,
        "reuse_embed_verify_ssl": bool(context.reuse_embed_verify_ssl),
    }


def hook_runtime_context_from_payload(payload: dict[str, object]) -> HookRuntimeContext:
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

    sink_types = payload.get("analysis_sink_types")
    if not isinstance(sink_types, list):
        sink_types = []
    serialized_augment_runtime_config = payload.get("augment_runtime_config")
    return HookRuntimeContext(
        knowledge_mode=str(payload.get("knowledge_mode") or "warm"),
        skills_dir=_path("skills_dir"),
        runtime_injection_mode=normalize_runtime_injection_mode(
            str(payload.get("runtime_injection_mode") or "context_aware")
        ),
        knowledge_packaging_mode=normalize_knowledge_packaging_mode(
            str(payload.get("knowledge_packaging_mode") or "dsl_rule")
        ),
        analysis_cwd=_path("analysis_cwd"),
        analysis_app_name=str(payload.get("analysis_app_name") or "").strip() or None,
        analysis_source=str(payload.get("analysis_source") or "").strip() or None,
        analysis_sink_types=[str(item).strip() for item in sink_types if str(item).strip()],
        knowledge_top_k=max(1, int(payload.get("knowledge_top_k") or 3)),
        knowledge_recall_top_m=max(1, int(payload.get("knowledge_recall_top_m") or 8)),
        knowledge_min_score=float(payload.get("knowledge_min_score") or 1.0),
        knowledge_injection_char_budget=max(256, int(payload.get("knowledge_injection_char_budget") or 4000)),
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
        knowledge_realtime_min_interval_ms=max(0, int(payload.get("knowledge_realtime_min_interval_ms") or 1500)),
        knowledge_injection_log_path=_path("knowledge_injection_log_path"),
        command_hook_trace_path=_path("command_hook_trace_path"),
        session_role_override=str(payload.get("session_role_override") or "").strip() or None,
        augment_runtime_config=(
            augment_runtime_config_from_payload(serialized_augment_runtime_config)
            if isinstance(serialized_augment_runtime_config, dict)
            else None
        ),
        reuse_embed_base_url=str(payload.get("reuse_embed_base_url") or "").strip() or None,
        reuse_embed_api_key=str(payload.get("reuse_embed_api_key") or "").strip() or None,
        reuse_embed_model=str(payload.get("reuse_embed_model") or "").strip() or None,
        reuse_embed_verify_ssl=_bool_value("reuse_embed_verify_ssl", default=False),
    )
