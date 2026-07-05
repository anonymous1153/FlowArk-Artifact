"""Platform-agnostic runtime knowledge augmentation logic."""

from __future__ import annotations

from contextvars import ContextVar, Token
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from flowark.knowledge.content_utils import SUMMARY_PREFIX, ensure_core_conclusion
from flowark.knowledge.analysis_log_rag import AnalysisLogRagRouter
from flowark.knowledge.embedding_router import EmbeddingKnowledgeRouter
from flowark.knowledge.manager import SkillRecord
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
    KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    is_analysis_log_rag_packaging_mode,
    normalize_knowledge_packaging_mode,
)
from flowark.runtime.config import normalize_runtime_injection_mode
from flowark.knowledge.reuse_recall import build_reuse_embedding_config
from flowark.knowledge.router import RuleKnowledgeRouter
from flowark.knowledge.rule_matcher import match_egress_case
from flowark.knowledge.signals import (
    extract_react_tool_turn_ids,
    read_transcript_delta,
    stringify_tool_response,
)
from flowark.semantics.models import (
    AfterToolContext,
    AugmentDecision,
    AugmentRuntimeConfig,
    AugmentationPayload,
    RequestSubmitContext,
)
from flowark.timeutil import now_tz8_iso
from flowark.types import KnowledgeInjectionRecord, RealtimeKnowledgeState, to_jsonable

_REALTIME_STATE_BY_SESSION: dict[str, RealtimeKnowledgeState] = {}
_REALTIME_LOCK_BY_SESSION: dict[str, threading.RLock] = {}
_DEFAULT_REALTIME_MIN_INTERVAL_MS = 1500
_DEFAULT_REPEAT_SUMMARY_HOOK_GAP = 3
_DEFAULT_REPEAT_FULL_HOOK_GAP = 10
_DEFAULT_REPEAT_SUMMARY_REACT_GAP = 0
_DEFAULT_REPEAT_FULL_REACT_GAP = 1
_AUGMENT_RUNTIME_CONFIG: ContextVar[AugmentRuntimeConfig | None] = ContextVar(
    "flowark_augment_runtime_config",
    default=None,
)
_INJECTION_RECORD_EXTRAS: ContextVar[dict[str, Any] | None] = ContextVar(
    "flowark_injection_record_extras",
    default=None,
)
_FLOWARK_INJECTED_CONTEXT_BLOCK_RE = re.compile(
    r"<(?P<tag>flowark-(?:runtime-context|knowledge-injection))\b[^>]*>.*?</(?P=tag)>",
    flags=re.S | re.I,
)
_ANALYSIS_LOG_RAG_TITLE = (
    "Retrieved historical analysis snippets from prior same-app sessions"
)
_TRANSCRIPT_BOUNDARY_RE = re.compile(
    r"^(?:\[?(?:assistant|user|tool|system|runner|phase:[^\]]+)\]?[:\s]|"
    r"(?:Assistant|User|Tool|System)Message\b|"
    r"(?:ToolUse|ToolResult)Block\b)",
    flags=re.I,
)

WrapPayloadFn = Callable[[AugmentationPayload], str]


def _runtime_context() -> AugmentRuntimeConfig | None:
    return _AUGMENT_RUNTIME_CONFIG.get()


def _set_runtime_context(config: AugmentRuntimeConfig) -> Token[AugmentRuntimeConfig | None]:
    return _AUGMENT_RUNTIME_CONFIG.set(config)


def _reset_runtime_context(token: Token[AugmentRuntimeConfig | None]) -> None:
    _AUGMENT_RUNTIME_CONFIG.reset(token)


def _require_runtime_context() -> AugmentRuntimeConfig:
    runtime = _runtime_context()
    if runtime is None:
        raise RuntimeError("缺少 augment runtime config，无法解析知识链路配置")
    return runtime


def _runtime_text(value: str | None, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _build_match_text(*parts: str | None) -> str:
    return "\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _strip_injected_context_for_rag_query(text: str) -> str:
    """Remove previous FlowArk injection blocks before embedding a RAG query."""
    value = str(text or "")
    if not value.strip():
        return ""
    stripped = _FLOWARK_INJECTED_CONTEXT_BLOCK_RE.sub("\n", value)
    if _ANALYSIS_LOG_RAG_TITLE not in stripped:
        return stripped.strip()

    lines: list[str] = []
    skipping_rag_block = False
    for line in stripped.splitlines():
        if _ANALYSIS_LOG_RAG_TITLE in line:
            skipping_rag_block = True
            continue
        if skipping_rag_block and _TRANSCRIPT_BOUNDARY_RE.match(line.strip()):
            skipping_rag_block = False
        if skipping_rag_block:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _dedupe_preserve_order(values: list[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


def _knowledge_mode() -> str:
    runtime = _require_runtime_context()
    return _runtime_text(runtime.knowledge_mode, default="warm").lower()


def _knowledge_packaging_mode() -> str:
    runtime = _require_runtime_context()
    return normalize_knowledge_packaging_mode(runtime.knowledge_packaging_mode)


def _runtime_injection_mode() -> str:
    runtime = _require_runtime_context()
    return normalize_runtime_injection_mode(
        getattr(runtime, "runtime_injection_mode", "context_aware")
    )


def _current_run_id_from_injection_log() -> str | None:
    runtime = _require_runtime_context()
    if runtime.knowledge_injection_log_path is None:
        return None
    return Path(runtime.knowledge_injection_log_path).expanduser().parent.name.strip() or None


def _append_injection_log(record: KnowledgeInjectionRecord | dict[str, Any]) -> None:
    runtime = _require_runtime_context()
    log_path = (
        str(runtime.knowledge_injection_log_path)
        if runtime.knowledge_injection_log_path is not None
        else None
    )
    if not log_path:
        return
    path = Path(log_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")


def set_injection_record_extras(extras: dict[str, Any]) -> Token[dict[str, Any] | None]:
    return _INJECTION_RECORD_EXTRAS.set(dict(extras))


def reset_injection_record_extras(token: Token[dict[str, Any] | None]) -> None:
    _INJECTION_RECORD_EXTRAS.reset(token)


def _skills_dir_from_runtime() -> Path | None:
    runtime = _require_runtime_context()
    if runtime.skills_dir is not None:
        return Path(runtime.skills_dir).expanduser()
    return None


def _runtime_router() -> RuleKnowledgeRouter | EmbeddingKnowledgeRouter | AnalysisLogRagRouter:
    skills_dir = _skills_dir_from_runtime()
    if skills_dir is None:
        raise RuntimeError("当前运行未绑定知识 scope 的 skills_dir，无法执行知识注入")
    runtime = _require_runtime_context()
    packaging_mode = _knowledge_packaging_mode()
    if packaging_mode in {
        KNOWLEDGE_PACKAGING_EMBEDDING,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    }:
        embedding_config = build_reuse_embedding_config(
            base_url=runtime.reuse_embed_base_url,
            api_key=runtime.reuse_embed_api_key,
            model=runtime.reuse_embed_model,
            verify_ssl=runtime.reuse_embed_verify_ssl,
        )
        if embedding_config is None:
            raise RuntimeError(f"knowledge_packaging_mode={packaging_mode} 缺少 embedding 后端配置")
        if is_analysis_log_rag_packaging_mode(packaging_mode):
            return AnalysisLogRagRouter(
                skills_dir=skills_dir,
                embedding_config=embedding_config,
            )
        return EmbeddingKnowledgeRouter(
            skills_dir=skills_dir,
            embedding_config=embedding_config,
            validated_only=False,
            disable_legacy_task_specific=True,
        )
    return RuleKnowledgeRouter(
        skills_dir=skills_dir,
        validated_only=False,
        disable_legacy_task_specific=True,
    )


def _get_realtime_state(session_id: str | None, *, transcript_path: str | None = None) -> RealtimeKnowledgeState | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    state = _REALTIME_STATE_BY_SESSION.get(sid)
    if state is None:
        state = RealtimeKnowledgeState(session_id=sid, transcript_path=transcript_path or None)
        _REALTIME_STATE_BY_SESSION[sid] = state
    if transcript_path:
        state.transcript_path = transcript_path
    return state


def _get_realtime_lock(session_id: str | None) -> threading.RLock | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    lock = _REALTIME_LOCK_BY_SESSION.get(sid)
    if lock is None:
        lock = threading.RLock()
        _REALTIME_LOCK_BY_SESSION[sid] = lock
    return lock


def _realtime_min_interval_ms() -> int:
    runtime = _require_runtime_context()
    try:
        return max(0, int(runtime.knowledge_realtime_min_interval_ms))
    except Exception:
        return _DEFAULT_REALTIME_MIN_INTERVAL_MS


def _delta_char_budget(default_budget: int) -> int:
    runtime = _require_runtime_context()
    if runtime.knowledge_delta_char_budget is not None:
        try:
            return max(256, int(runtime.knowledge_delta_char_budget))
        except Exception:
            pass
    return max(600, min(1800, default_budget))


def _allow_repeat_injection_within_session() -> bool:
    runtime = _require_runtime_context()
    return bool(runtime.knowledge_allow_repeat_within_session)


def _hydrate_no_repeat_state_from_log(state: RealtimeKnowledgeState | None) -> None:
    if state is None:
        return
    packaging_mode = _knowledge_packaging_mode()
    if (
        not is_analysis_log_rag_packaging_mode(packaging_mode)
        and _allow_repeat_injection_within_session()
    ):
        return
    runtime = _require_runtime_context()
    log_path = runtime.knowledge_injection_log_path
    if log_path is None:
        return
    path = Path(log_path).expanduser()
    if not path.exists():
        return
    skill_counts: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        delivery_status = str(record.get("delivery_status") or "delivered")
        record_session_id = str(
            record.get("opencode_session_id") or record.get("session_id") or ""
        ).strip()
        if record_session_id and state.session_id and record_session_id != state.session_id:
            continue
        if (
            packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL
            and record_session_id
            and record_session_id == state.session_id
            and str(record.get("knowledge_packaging_mode") or "").strip()
            == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL
            and str(record.get("hook_event_name") or "").strip() == "UserPromptSubmit"
            and delivery_status != "failed"
        ):
            state.analysis_log_rag_initial_request_submit_attempted = True
        if delivery_status in {"skipped", "failed"}:
            continue
        for raw_skill_id in record.get("injected_skill_ids") or []:
            skill_id = str(raw_skill_id or "").strip()
            if not skill_id:
                continue
            skill_counts[skill_id] = int(skill_counts.get(skill_id, 0) or 0) + 1
    for skill_id, count in skill_counts.items():
        state.injected_skill_ids.add(skill_id)
        state.injected_skill_counts[skill_id] = max(
            int(state.injected_skill_counts.get(skill_id, 0) or 0),
            count,
        )


def _repeat_summary_hook_gap() -> int:
    runtime = _require_runtime_context()
    try:
        return max(1, int(runtime.knowledge_repeat_summary_hook_gap))
    except Exception:
        return _DEFAULT_REPEAT_SUMMARY_HOOK_GAP


def _repeat_full_hook_gap() -> int:
    runtime = _require_runtime_context()
    try:
        value = max(1, int(runtime.knowledge_repeat_full_hook_gap))
    except Exception:
        value = _DEFAULT_REPEAT_FULL_HOOK_GAP
    return max(value, _repeat_summary_hook_gap() + 1)


def _repeat_summary_react_gap() -> int:
    runtime = _require_runtime_context()
    try:
        return max(0, int(getattr(runtime, "knowledge_repeat_summary_react_gap", _DEFAULT_REPEAT_SUMMARY_REACT_GAP)))
    except Exception:
        return _DEFAULT_REPEAT_SUMMARY_REACT_GAP


def _repeat_full_react_gap() -> int:
    runtime = _require_runtime_context()
    try:
        return max(0, int(getattr(runtime, "knowledge_repeat_full_react_gap", _DEFAULT_REPEAT_FULL_REACT_GAP)))
    except Exception:
        return _DEFAULT_REPEAT_FULL_REACT_GAP


def _analysis_cwd() -> Path | None:
    runtime = _require_runtime_context()
    raw_value = getattr(runtime, "analysis_cwd", None)
    if raw_value is None:
        return None
    return Path(raw_value).expanduser()


def _analysis_app_name() -> str | None:
    runtime = _require_runtime_context()
    value = _runtime_text(getattr(runtime, "analysis_app_name", None))
    return value or None


def _analysis_source() -> str | None:
    runtime = _require_runtime_context()
    value = _runtime_text(getattr(runtime, "analysis_source", None))
    return value or None


def _analysis_sink_types() -> list[str]:
    runtime = _require_runtime_context()
    return [
        str(part).strip()
        for part in (getattr(runtime, "analysis_sink_types", None) or [])
        if str(part).strip()
    ]


def _analysis_log_rag_initial_match_text(
    *,
    prompt: str,
    app_name: str | None,
    source_desc: str | None,
    sink_types: list[str],
) -> str:
    sink_text = ", ".join(str(value).strip() for value in sink_types if str(value).strip())
    return _build_match_text(
        _strip_injected_context_for_rag_query(prompt),
        f"app: {app_name}" if str(app_name or "").strip() else None,
        f"source: {source_desc}" if str(source_desc or "").strip() else None,
        f"sink_categories: {sink_text}" if sink_text else None,
    )


def _knowledge_top_k() -> int:
    packaging_mode = _knowledge_packaging_mode()
    if packaging_mode in {KNOWLEDGE_PACKAGING_EMBEDDING, KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG}:
        return 3
    runtime = _require_runtime_context()
    try:
        value = int(runtime.knowledge_top_k or 3)
    except Exception:
        value = 3
    return max(1, value)


def _knowledge_recall_top_m(top_k: int) -> int:
    if _knowledge_packaging_mode() in {
        KNOWLEDGE_PACKAGING_EMBEDDING,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG,
        KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL,
    }:
        return max(1, int(top_k or 3))
    runtime = _require_runtime_context()
    try:
        value = int(runtime.knowledge_recall_top_m or 8)
    except Exception:
        value = max(top_k * 3, 8)
    return max(top_k, value)


def _knowledge_min_score() -> float:
    runtime = _require_runtime_context()
    try:
        return float(runtime.knowledge_min_score)
    except Exception:
        return 1.0


def _knowledge_injection_char_budget() -> int:
    runtime = _require_runtime_context()
    try:
        return max(256, int(runtime.knowledge_injection_char_budget))
    except Exception:
        return 4000


def _request_submit_char_budget() -> int:
    return _knowledge_injection_char_budget()


def _router_mode_for_packaging(packaging_mode: str) -> str:
    if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
        return "embedding"
    if packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG:
        return "analysis_log_rag"
    if packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL:
        return "analysis_log_rag_initial"
    return "dsl_rule"


def _mark_injected(
    state: RealtimeKnowledgeState | None,
    injected_skill_ids: list[str],
    *,
    injected_note_case_keys: list[str] | None = None,
    rendered_skill_modes: dict[str, set[str]] | None = None,
    react_turn_index: int | None = None,
    touch_delta_timestamp: bool,
) -> None:
    if state is None:
        return
    now_ts = time.monotonic()
    current_react_turn = (
        _current_react_turn_index(state)
        if react_turn_index is None
        else int(react_turn_index)
    )
    for sid in injected_skill_ids:
        sid_text = str(sid)
        state.injected_skill_ids.add(sid_text)
        state.injected_skill_counts[sid_text] = int(state.injected_skill_counts.get(sid_text, 0) or 0) + 1
        state.last_injected_ts_by_skill[sid_text] = now_ts
        state.last_injected_hook_by_skill[sid_text] = int(state.hook_index)
        modes = set((rendered_skill_modes or {}).get(sid_text) or {"full"})
        if "full" in modes:
            state.last_full_react_turn_by_skill[sid_text] = current_react_turn
        if "summary" in modes:
            state.last_summary_react_turn_by_skill[sid_text] = current_react_turn
    for case_key in list(injected_note_case_keys or []):
        case_text = str(case_key).strip()
        if not case_text:
            continue
        state.injected_note_case_keys.add(case_text)
        state.injected_note_case_counts[case_text] = int(state.injected_note_case_counts.get(case_text, 0) or 0) + 1
        state.last_injected_ts_by_note_case[case_text] = now_ts
        state.last_injected_hook_by_note_case[case_text] = int(state.hook_index)
    if touch_delta_timestamp:
        state.last_delta_injection_ts = now_ts
        state.delta_injection_count += 1


def _advance_hook_index(state: RealtimeKnowledgeState | None) -> int:
    if state is None:
        return 0
    state.hook_index = int(state.hook_index or 0) + 1
    return state.hook_index


def _advance_react_turn_index(state: RealtimeKnowledgeState | None, turn_ids: list[str]) -> int:
    if state is None:
        return 0
    for turn_id in turn_ids:
        turn_text = str(turn_id or "").strip()
        if not turn_text or turn_text in state.seen_react_turn_ids:
            continue
        state.seen_react_turn_ids.add(turn_text)
        state.react_turn_index = int(state.react_turn_index or 0) + 1
    return int(state.react_turn_index or 0)


def _current_react_turn_index(state: RealtimeKnowledgeState | None) -> int:
    if state is None:
        return 0
    react_index = int(state.react_turn_index or 0)
    if react_index > 0:
        return react_index
    return int(state.hook_index or 0)


def _rendered_skill_modes(details: list[dict[str, Any]]) -> dict[str, set[str]]:
    modes: dict[str, set[str]] = {}
    for item in details:
        if str(item.get("type") or "") != "skill":
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        render_mode = str(item.get("render_mode") or "").strip()
        if not skill_id or not render_mode:
            continue
        modes.setdefault(skill_id, set()).add(render_mode)
    return modes


def _throttle_realtime_injection(state: RealtimeKnowledgeState | None) -> bool:
    if state is None:
        return False
    min_interval_ms = _realtime_min_interval_ms()
    if min_interval_ms <= 0:
        return False
    elapsed_ms = (time.monotonic() - float(state.last_delta_injection_ts or 0.0)) * 1000.0
    return elapsed_ms < float(min_interval_ms)


def _extract_json_object_text(text: str) -> str | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.S)
    if fenced:
        return fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start:end + 1].strip()
    return None


def _trim_text(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _summarize_match_reasons(matches: list[Any], *, limit: int = 24) -> list[str]:
    reasons: list[str] = []
    for match in matches:
        reasons.extend([str(value).strip() for value in (getattr(match, "reasons", []) or []) if str(value).strip()])
    return _dedupe_preserve_order(reasons, limit=limit)


def _skill_file_snapshot(skill: SkillRecord) -> str:
    try:
        return skill.file_path.read_text(encoding="utf-8")
    except Exception:
        return str(skill.content or "")


def _note_execution_line(skill: SkillRecord, *, repeat: bool) -> str:
    if skill.is_validated():
        reread_requirement = "非必要不要回读其覆盖的内部实现。"
        return (
            f"- 执行要求: 默认相信并遵循这条机制层指导，{reread_requirement}"
            if not repeat
            else f"- 执行要求: 本 session 已注入过该知识；继续按既有机制层指导分析，{reread_requirement}"
        )
    return (
        "- 执行要求: 这是一条未验证机制层指导，不应默认作为运行时知识使用；若被手动查看，仅能作保守参考。"
        if not repeat
        else "- 执行要求: 本 session 已查看过该未验证机制层指导；不要默认依赖它替代代码证据。"
    )


def _selector_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _resolve_note_egress_cases(
    skill: SkillRecord,
    *,
    match_text: str,
    limit: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    egress = skill.get_egress_map()
    if egress is None:
        return [], []
    if not str(match_text or "").strip():
        return [], []
    selected_cases: list[dict[str, Any]] = []
    selected_selector_values: list[str] = []
    for case in list(egress.cases or []):
        case_selectors = [str(v).strip() for v in (case.selectors or []) if str(v).strip()]
        case_negative_selectors = [
            str(v).strip()
            for v in (case.negative_selectors or [])
            if str(v).strip()
        ]
        matched_values, allowed = match_egress_case(case_selectors, case_negative_selectors, match_text)
        if not allowed:
            continue
        if not matched_values:
            continue
        selected_cases.append(
            {
                "matched_selectors": matched_values[:3],
                "next_hops": [str(v).strip() for v in (case.next_hops or []) if str(v).strip()][:4],
                "summary": str(case.summary or "").strip(),
                "boundary_summary": skill.get_boundary_summary(),
                "key_apis": skill.get_key_apis(),
                "note_case_keys": [f"{skill.id}:{_selector_key(v)}" for v in matched_values[:3]],
            }
        )
        selected_selector_values.extend(matched_values[:3])
        if len(selected_cases) >= limit:
            break
    deduped_selectors: list[str] = []
    seen: set[str] = set()
    for value in selected_selector_values:
        key = _selector_key(value)
        if key in seen:
            continue
        seen.add(key)
        deduped_selectors.append(value)
    return selected_cases, deduped_selectors


def _format_egress_lines(selected_cases: list[dict[str, Any]], *, compact: bool = False) -> list[str]:
    if not selected_cases:
        return []
    lines: list[str] = []
    if compact:
        lines.append("- 出口表命中项：")
    else:
        lines.append("- 出口表命中项（仅注入当前 selector 对应条目）：")
    for case in selected_cases:
        selectors = ", ".join(case.get("matched_selectors") or []) or "selector"
        next_hops = ", ".join(case.get("next_hops") or []) or "（未给出下一跳）"
        summary = str(case.get("summary") or "").strip()
        line = f"  - {selectors} -> {next_hops}"
        if summary:
            line += f"；{summary}"
        lines.append(line)
    return lines


def _render_note_block(
    skill: SkillRecord,
    *,
    matched_anchor_reasons: list[str],
    selected_cases: list[dict[str, Any]],
) -> tuple[str, str]:
    validation_status = skill.get_validation_status()
    mode = "trusted_note" if skill.is_validated() else "auto_synth_note"
    key_apis = skill.get_key_apis()
    key_api_line = f"- 关键 API / 边界点: {', '.join(key_apis[:6])}\n" if key_apis else ""
    egress_lines = _format_egress_lines(selected_cases)
    egress_block = ("\n".join(egress_lines) + "\n") if egress_lines else ""
    block = (
        f"### NOTE: {skill.name}\n"
        f"- ID: `{skill.id}`\n"
        f"- validation_status: `{validation_status}`\n"
        f"- entry_condition: {skill.get_entry_condition() or '（无）'}\n"
        f"- 匹配原因: {'; '.join(matched_anchor_reasons[:3]) or 'anchor 命中'}\n"
        f"{_note_execution_line(skill, repeat=False)}\n\n"
        f"{key_api_line}"
        f"{egress_block}"
        f"{ensure_core_conclusion(skill.content, fallback=skill.name).strip()}\n"
    )
    return block, mode


def _gap_satisfied(*, current_index: int, last_index: int, required_gap: int) -> bool:
    if current_index <= 0 or last_index < 0:
        return False
    intervening_turns = int(current_index) - int(last_index) - 1
    return intervening_turns >= int(required_gap)


def _select_repeat_mode(
    *,
    count: int,
    last_full_react_turn: int | None,
    last_summary_react_turn: int | None,
    current_react_turn: int,
) -> str:
    if count <= 0:
        return "full"
    if last_full_react_turn is None:
        if last_summary_react_turn == current_react_turn and current_react_turn > 0:
            return "skip"
        return "full"
    if _gap_satisfied(
        current_index=current_react_turn,
        last_index=last_full_react_turn,
        required_gap=_repeat_full_react_gap(),
    ):
        return "full"
    if last_full_react_turn == current_react_turn and current_react_turn > 0:
        return "skip"
    if last_summary_react_turn == current_react_turn and current_react_turn > 0:
        return "skip"
    if last_summary_react_turn is None or _gap_satisfied(
        current_index=current_react_turn,
        last_index=last_summary_react_turn,
        required_gap=_repeat_summary_react_gap(),
    ):
        return "summary"
    return "skip"


def _select_skill_render_mode(skill: SkillRecord, *, state: RealtimeKnowledgeState | None) -> str:
    if state is None:
        return "full"
    skill_id = str(skill.id)
    return _select_repeat_mode(
        count=int(state.injected_skill_counts.get(skill_id, 0) or 0),
        last_full_react_turn=(
            int(state.last_full_react_turn_by_skill[skill_id])
            if skill_id in state.last_full_react_turn_by_skill
            else None
        ),
        last_summary_react_turn=(
            int(state.last_summary_react_turn_by_skill[skill_id])
            if skill_id in state.last_summary_react_turn_by_skill
            else None
        ),
        current_react_turn=_current_react_turn_index(state),
    )


def _render_note_summary(
    skill: SkillRecord,
    *,
    selected_cases: list[dict[str, Any]],
    repeated: bool,
) -> tuple[str, str]:
    egress_lines = _format_egress_lines(selected_cases, compact=True)
    summary_line = skill.get_core_conclusion() or skill.name
    block = (
        f"### NOTE_SUMMARY: {skill.name}\n"
        f"- ID: `{skill.id}`\n"
        + (
            f"- knowledge://{skill.id} 再次命中，本 session 已完整注入过。\n"
            if repeated
            else ""
        )
        + f"- {SUMMARY_PREFIX}{summary_line}\n"
        + ("\n".join(egress_lines) + "\n" if egress_lines else "")
    )
    return block.strip() + "\n", "trusted_note_summary" if skill.is_validated() else "auto_synth_note_summary"


def format_skills(skills: list[SkillRecord]) -> str:
    sections: list[str] = []
    for skill in skills:
        if not skill.is_note():
            continue
        section, _ = _render_note_block(
            skill,
            matched_anchor_reasons=["手动格式化"],
            selected_cases=[],
        )
        sections.append(section)
    return "\n\n".join(sections)


def _pick_notes(matches: list[Any], *, state: RealtimeKnowledgeState | None, limit: int) -> list[Any]:
    notes: list[Any] = []
    for match in matches:
        if state is not None and not _allow_repeat_injection_within_session():
            if str(match.skill_id) in state.injected_skill_ids:
                continue
        notes.append(match)
        if len(notes) >= limit:
            break
    return notes


def _render_selected_knowledge(
    *,
    router: RuleKnowledgeRouter | EmbeddingKnowledgeRouter,
    selected_note_matches: list[Any],
    char_budget: int,
    state: RealtimeKnowledgeState | None,
    match_text: str,
    current_app_name: str | None,
) -> tuple[str, list[str], list[dict[str, Any]], list[str], bool, list[str], dict[str, str], int]:
    sections: list[str] = []
    injected_ids: list[str] = []
    details: list[dict[str, Any]] = []
    injected_note_case_keys: list[str] = []
    dropped_skill_ids: list[str] = []
    dropped_skill_reasons: dict[str, str] = {}
    used_chars = 0
    any_full_render = False

    def append_block(
        skill: SkillRecord,
        block: str,
        *,
        mode: str,
        render_mode: str,
        egress_cases: list[dict[str, Any]] | None = None,
        selected_selectors: list[str] | None = None,
        match_score: float | None = None,
        match_stage: str | None = None,
        match_reasons: list[str] | None = None,
    ) -> bool:
        nonlocal used_chars, any_full_render
        extra_len = len(block) + (2 if sections else 0)
        if sections and used_chars + extra_len > char_budget:
            return False
        if not sections and extra_len > char_budget:
            return False
        sections.append(block)
        injected_ids.append(skill.id)
        used_chars += extra_len
        if render_mode == "full":
            any_full_render = True
        details.append(
            {
                "type": "skill",
                "skill_id": skill.id,
                "validation_status": skill.get_validation_status(),
                "mode": mode,
                "render_mode": render_mode,
                "egress_case_count": len(egress_cases or []),
                "selected_selectors": list(selected_selectors or []),
                "match_score": match_score,
                "match_stage": match_stage,
                "match_reasons": list(match_reasons or []),
                "skill_file_path": str(skill.file_path),
                "skill_file_snapshot": _skill_file_snapshot(skill),
                "injected_prompt_block": block,
            }
        )
        for case in list(egress_cases or []):
            for case_key in list(case.get("note_case_keys") or []):
                key_text = str(case_key).strip()
                if key_text and key_text not in injected_note_case_keys:
                    injected_note_case_keys.append(key_text)
        return True

    def record_budget_drop(skill: SkillRecord, *, attempted_mode: str, fallback_attempted: bool) -> None:
        skill_id = str(skill.id)
        if skill_id not in dropped_skill_ids:
            dropped_skill_ids.append(skill_id)
        dropped_skill_reasons[skill_id] = (
            f"char_budget_exceeded:{attempted_mode}->summary"
            if fallback_attempted
            else f"char_budget_exceeded:{attempted_mode}"
        )

    def skill_seen_in_session(skill: SkillRecord) -> bool:
        if state is None:
            return False
        return int(state.injected_skill_counts.get(skill.id, 0) or 0) > 0

    def try_append_with_summary_fallback(
        skill: SkillRecord,
        *,
        primary_block: str,
        primary_mode: str,
        primary_render_mode: str,
        primary_egress_cases: list[dict[str, Any]] | None,
        primary_selected_selectors: list[str] | None,
        egressless_block_factory: Callable[[], tuple[str, str]] | None = None,
        summary_factory: Callable[[], tuple[str, str]] | None = None,
        summary_egress_cases: list[dict[str, Any]] | None = None,
        match_score: float | None = None,
        match_stage: str | None = None,
        match_reasons: list[str] | None = None,
    ) -> bool:
        if append_block(
            skill,
            primary_block,
            mode=primary_mode,
            render_mode=primary_render_mode,
            egress_cases=primary_egress_cases,
            selected_selectors=primary_selected_selectors,
            match_score=match_score,
            match_stage=match_stage,
            match_reasons=match_reasons,
        ):
            return True
        if egressless_block_factory is not None and primary_egress_cases:
            egressless_block, egressless_mode = egressless_block_factory()
            if append_block(
                skill,
                egressless_block,
                mode=egressless_mode,
                render_mode=primary_render_mode,
                egress_cases=[],
                selected_selectors=primary_selected_selectors,
                match_score=match_score,
                match_stage=match_stage,
                match_reasons=match_reasons,
            ):
                return True
        if summary_factory is not None and primary_render_mode == "full":
            summary_block, summary_mode = summary_factory()
            if append_block(
                skill,
                summary_block,
                mode=summary_mode,
                render_mode="summary",
                egress_cases=summary_egress_cases,
                selected_selectors=primary_selected_selectors,
                match_score=match_score,
                match_stage=match_stage,
                match_reasons=match_reasons,
            ):
                return True
            record_budget_drop(skill, attempted_mode=primary_render_mode, fallback_attempted=True)
            return False
        record_budget_drop(skill, attempted_mode=primary_render_mode, fallback_attempted=False)
        return False

    def select_note_render(skill: SkillRecord, selected_cases: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        note_mode = _select_skill_render_mode(skill, state=state)
        if note_mode == "skip":
            return note_mode, []
        rendered_cases: list[dict[str, Any]] = []
        for case in selected_cases:
            case["render_mode"] = note_mode
            rendered_cases.append(case)
        return note_mode, rendered_cases

    for match in selected_note_matches:
        skill = router.manager.get_skill_by_id(str(match.skill_id), current_app_name=current_app_name)
        if skill is None or not skill.is_note():
            continue
        selected_cases, selected_selector_values = _resolve_note_egress_cases(
            skill,
            match_text=match_text,
            limit=2,
        )
        render_mode, rendered_cases = select_note_render(skill, selected_cases)
        if render_mode == "skip":
            continue
        if render_mode == "full":
            block, mode = _render_note_block(
                skill,
                matched_anchor_reasons=list(match.reasons or []),
                selected_cases=selected_cases,
            )
        else:
            block, mode = _render_note_summary(
                skill,
                selected_cases=rendered_cases,
                repeated=True,
            )
        try_append_with_summary_fallback(
            skill,
            primary_block=block,
            primary_mode=mode,
            primary_render_mode=render_mode,
            primary_egress_cases=selected_cases if render_mode == "full" else rendered_cases,
            primary_selected_selectors=selected_selector_values,
            egressless_block_factory=(
                lambda s=skill, reasons=list(match.reasons or []): _render_note_block(
                    s,
                    matched_anchor_reasons=reasons,
                    selected_cases=[],
                )
            )
            if _knowledge_packaging_mode() == KNOWLEDGE_PACKAGING_EMBEDDING
            and render_mode == "full"
            and selected_cases
            else None,
            summary_factory=(
                lambda s=skill, sc=(
                    [] if _knowledge_packaging_mode() == KNOWLEDGE_PACKAGING_EMBEDDING and selected_cases else selected_cases
                ), repeated=skill_seen_in_session(skill): _render_note_summary(
                    s,
                    selected_cases=sc,
                    repeated=repeated,
                )
            ) if render_mode == "full" else None,
            summary_egress_cases=(
                []
                if _knowledge_packaging_mode() == KNOWLEDGE_PACKAGING_EMBEDDING and selected_cases
                else (selected_cases if render_mode == "full" else rendered_cases)
            ),
            match_score=float(getattr(match, "score", 0.0) or 0.0),
            match_stage=str(getattr(match, "match_stage", "") or ""),
            match_reasons=[str(v) for v in list(getattr(match, "reasons", []) or [])],
        )

    return (
        "\n\n".join(sections),
        injected_ids,
        details,
        injected_note_case_keys,
        (not any_full_render and bool(sections)),
        dropped_skill_ids,
        dropped_skill_reasons,
        used_chars,
    )


def _render_analysis_log_rag_matches(
    *,
    matches: list[Any],
    char_budget: int,
) -> tuple[str, list[str], list[dict[str, Any]], list[str], bool, list[str], dict[str, str], int]:
    title = (
        "### Retrieved historical analysis snippets from prior same-app sessions\n"
        "- Source: trimmed historical transcripts and final reports from completed same-app runs.\n"
        "- Use policy: treat these snippets as historical context only; verify applicability against the current code before relying on them.\n"
    )
    sections: list[str] = [title]
    injected_ids: list[str] = []
    details: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    dropped_reasons: dict[str, str] = {}
    used_chars = len(title)
    selected_matches = list(matches or [])

    for idx, match in enumerate(selected_matches, start=1):
        chunk = getattr(match, "chunk", None)
        chunk_id = str(getattr(match, "chunk_id", "") or getattr(chunk, "chunk_id", "") or "").strip()
        if chunk is None or not chunk_id:
            continue
        score = float(getattr(match, "score", 0.0) or 0.0)
        source = str(getattr(chunk, "source_id", "") or getattr(chunk, "case_id", "") or "").strip()
        provenance_parts = [
            f"app={getattr(chunk, 'app_name', '')}",
            f"origin={getattr(chunk, 'origin', '')}",
            f"run={getattr(chunk, 'run_id', '')}",
        ]
        case_id = str(getattr(chunk, "case_id", "") or "").strip()
        if case_id:
            provenance_parts.append(f"case={case_id}")
        if source:
            provenance_parts.append(f"source={source}")
        sink_types = [
            str(value).strip()
            for value in list(getattr(chunk, "sink_types", ()) or ())
            if str(value).strip()
        ]
        if sink_types:
            provenance_parts.append(f"sinks={','.join(sink_types)}")
        run_order = getattr(chunk, "run_order", None)
        if run_order is not None:
            provenance_parts.append(f"run_order={run_order}")
        body = str(getattr(chunk, "text", "") or "").strip()
        header = (
            f"#### Historical Snippet {idx}\n"
            f"- chunk_id: `{chunk_id}`\n"
            f"- provenance: {'; '.join(part for part in provenance_parts if part and not part.endswith('='))}\n"
            f"- semantic_similarity: {score:.4f}\n"
        )
        separator_len = 2 if sections else 0
        remaining_matches = max(1, len(selected_matches) - idx + 1)
        remaining_budget = max(0, int(char_budget or 0) - used_chars - separator_len)
        body_budget = remaining_budget - len(header) - 2
        if body_budget < 120:
            dropped_ids.append(chunk_id)
            dropped_reasons[chunk_id] = "char_budget_exceeded"
            continue
        fair_body_budget = max(120, body_budget // remaining_matches)
        body_limit = min(len(body), max(120, fair_body_budget))
        render_mode_line = ""
        if len(body) > body_limit:
            render_mode_line = "- render_mode: trimmed_to_budget\n"
        trimmed_body = _trim_text(body, body_limit)
        block = f"{header}{render_mode_line}\n{trimmed_body}\n"
        extra_len = len(block) + separator_len
        if used_chars + extra_len > char_budget:
            fallback_budget = max(120, int(char_budget or 0) - used_chars - separator_len - len(header) - len(render_mode_line) - 3)
            if fallback_budget < 120:
                dropped_ids.append(chunk_id)
                dropped_reasons[chunk_id] = "char_budget_exceeded"
                continue
            trimmed_body = _trim_text(body, fallback_budget)
            render_mode_line = "- render_mode: trimmed_to_budget\n"
            block = f"{header}{render_mode_line}\n{trimmed_body}\n"
            extra_len = len(block) + separator_len
            if used_chars + extra_len > char_budget:
                dropped_ids.append(chunk_id)
                dropped_reasons[chunk_id] = "char_budget_exceeded"
                continue
        sections.append(block)
        injected_ids.append(chunk_id)
        used_chars += extra_len
        details.append(
            {
                "type": "analysis_log_rag_chunk",
                "chunk_id": chunk_id,
                "origin": str(getattr(chunk, "origin", "") or ""),
                "app_name": str(getattr(chunk, "app_name", "") or ""),
                "source_id": getattr(chunk, "source_id", None),
                "case_id": getattr(chunk, "case_id", None),
                "run_id": getattr(chunk, "run_id", None),
                "sink_types": sink_types,
                "run_order": run_order,
                "artifact_path": getattr(chunk, "artifact_path", None),
                "match_score": score,
                "match_stage": str(getattr(match, "match_stage", "") or ""),
                "match_reasons": [str(v) for v in list(getattr(match, "reasons", []) or [])],
                "injected_prompt_block": block,
            }
        )

    if not injected_ids:
        return "", [], [], [], False, dropped_ids, dropped_reasons, 0
    return (
        "\n\n".join(sections).strip() + "\n",
        injected_ids,
        details,
        [],
        False,
        dropped_ids,
        dropped_reasons,
        used_chars,
    )


def _analysis_log_rag_candidate_details(matches: list[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for rank, match in enumerate(matches, start=1):
        chunk = getattr(match, "chunk", None)
        chunk_id = str(getattr(match, "chunk_id", "") or getattr(chunk, "chunk_id", "") or "").strip()
        if chunk is None or not chunk_id:
            continue
        text = str(getattr(chunk, "text", "") or "")
        candidates.append(
            {
                "rank": rank,
                "chunk_id": chunk_id,
                "score": float(getattr(match, "score", 0.0) or 0.0),
                "origin": str(getattr(chunk, "origin", "") or ""),
                "app_name": str(getattr(chunk, "app_name", "") or ""),
                "source_id": getattr(chunk, "source_id", None),
                "case_id": getattr(chunk, "case_id", None),
                "run_id": getattr(chunk, "run_id", None),
                "sink_types": list(getattr(chunk, "sink_types", ()) or ()),
                "run_order": getattr(chunk, "run_order", None),
                "artifact_path": getattr(chunk, "artifact_path", None),
                "match_stage": str(getattr(match, "match_stage", "") or ""),
                "match_reasons": [str(v) for v in list(getattr(match, "reasons", []) or [])],
                "text_chars": len(text),
                "text_excerpt": _trim_text(text, 400),
            }
        )
    return candidates


def _select_fresh_analysis_log_rag_matches(
    matches: list[Any],
    *,
    state: RealtimeKnowledgeState | None,
    limit: int,
) -> tuple[list[Any], list[str], dict[str, str]]:
    already_injected = set(state.injected_skill_ids) if state is not None else set()
    selected: list[Any] = []
    dropped_ids: list[str] = []
    dropped_reasons: dict[str, str] = {}
    for match in matches:
        chunk = getattr(match, "chunk", None)
        chunk_id = str(getattr(match, "chunk_id", "") or getattr(chunk, "chunk_id", "") or "").strip()
        if not chunk_id:
            continue
        if chunk_id in already_injected:
            dropped_ids.append(chunk_id)
            dropped_reasons[chunk_id] = "already_injected_in_session"
            continue
        if len(selected) < max(1, int(limit or 3)):
            selected.append(match)
    return selected, dropped_ids, dropped_reasons


def _merge_dropped_chunks(
    first_ids: list[str],
    first_reasons: dict[str, str],
    second_ids: list[str],
    second_reasons: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    dropped_ids = _dedupe_preserve_order([*first_ids, *second_ids])
    dropped_reasons = dict(first_reasons)
    dropped_reasons.update(second_reasons)
    return dropped_ids, dropped_reasons


def _empty_render_delivery_reason(dropped_reasons: dict[str, str]) -> str:
    reasons = {str(reason or "").strip() for reason in dict(dropped_reasons or {}).values()}
    if reasons and reasons <= {"already_injected_in_session"}:
        return "all_candidates_already_injected"
    return "empty_render"


def _append_record(
    *,
    mode: str,
    query_excerpt: str,
    matched_ids: list[str],
    injected_ids: list[str],
    dropped_ids: list[str],
    dropped_reasons: dict[str, str],
    injected_chars: int,
    details: list[dict[str, Any]],
    hook_event_name: str,
    delta: bool,
    used_summary_only: bool = False,
    matched_rules: list[str] | None = None,
    retrieval_query: str | None = None,
    retrieval_candidates: list[dict[str, Any]] | None = None,
    delivery_status: str | None = None,
    delivery_reason: str | None = None,
) -> None:
    record = to_jsonable(
        KnowledgeInjectionRecord(
            timestamp=now_tz8_iso(),
            mode=mode,
            query_excerpt=query_excerpt[:200],
            matched_skill_ids=list(matched_ids),
            selected_skill_ids=list(injected_ids),
            dropped_skill_ids=list(dropped_ids),
            dropped_skill_reasons=dict(dropped_reasons),
            injected_skill_ids=list(injected_ids),
            injected_chars=int(injected_chars or 0),
            used_summary_only=used_summary_only,
            details=list(details),
            hook_event_name=hook_event_name,
            delta=delta,
            matched_rules=list(matched_rules or []),
        )
    )
    packaging_mode = _knowledge_packaging_mode()
    record["knowledge_packaging_mode"] = packaging_mode
    record["router_mode"] = _router_mode_for_packaging(packaging_mode)
    record["runtime_injection_mode"] = _runtime_injection_mode()
    record["delivery_status"] = str(delivery_status or "delivered")
    record["delivery_reason"] = str(delivery_reason or "")
    extras = _INJECTION_RECORD_EXTRAS.get()
    if extras:
        record.update({str(key): to_jsonable(value) for key, value in extras.items()})
    _append_injection_log(record)
    if is_analysis_log_rag_packaging_mode(packaging_mode):
        _append_analysis_log_rag_audit_artifacts(
            record,
            retrieval_query=retrieval_query,
            retrieval_candidates=retrieval_candidates,
        )


def _append_analysis_log_rag_audit_artifacts(
    record: dict[str, Any],
    *,
    retrieval_query: str | None,
    retrieval_candidates: list[dict[str, Any]] | None,
) -> None:
    runtime = _require_runtime_context()
    if runtime.knowledge_injection_log_path is None:
        return
    run_dir = Path(runtime.knowledge_injection_log_path).expanduser().parent
    rag_dir = run_dir / "analysis_log_rag"
    rag_dir.mkdir(parents=True, exist_ok=True)
    query_text = str(retrieval_query or "")
    candidates = to_jsonable(list(retrieval_candidates or []))
    selected_ids = list(record.get("injected_skill_ids") or [])
    dropped_ids = list(record.get("dropped_skill_ids") or [])
    router_mode = _router_mode_for_packaging(str(record.get("knowledge_packaging_mode") or "dsl_rule"))
    retrieval_record = {
        "timestamp": record.get("timestamp"),
        "hook_event_name": record.get("hook_event_name"),
        "delta": bool(record.get("delta")),
        "router_mode": router_mode,
        "query": query_text,
        "query_chars": len(query_text),
        "candidates": candidates,
        "selected_chunk_ids": selected_ids,
        "dropped_chunk_ids": dropped_ids,
        "dropped_chunk_reasons": dict(record.get("dropped_skill_reasons") or {}),
        "delivery_status": record.get("delivery_status", "delivered"),
        "delivery_reason": record.get("delivery_reason", ""),
    }
    injection_record = {
        "timestamp": record.get("timestamp"),
        "hook_event_name": record.get("hook_event_name"),
        "delta": bool(record.get("delta")),
        "router_mode": router_mode,
        "selected_chunk_ids": selected_ids,
        "injected_chars": int(record.get("injected_chars") or 0),
        "details": list(record.get("details") or []),
        "dropped_chunk_ids": dropped_ids,
        "dropped_chunk_reasons": dict(record.get("dropped_skill_reasons") or {}),
        "delivery_status": record.get("delivery_status", "delivered"),
        "delivery_reason": record.get("delivery_reason", ""),
    }
    for filename, payload in (
        ("rag_retrieval.jsonl", retrieval_record),
        ("rag_injection.jsonl", injection_record),
    ):
        with (rag_dir / filename).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(to_jsonable(payload), ensure_ascii=False) + "\n")


def append_injection_skip_record(
    *,
    runtime_config: AugmentRuntimeConfig,
    hook_event_name: str,
    delivery_reason: str,
    delta: bool,
    query_excerpt: str = "",
    extras: dict[str, Any] | None = None,
) -> None:
    token = _set_runtime_context(runtime_config)
    try:
        mode = _knowledge_mode()
        packaging_mode = _knowledge_packaging_mode()
        record: dict[str, Any] = {
            "timestamp": now_tz8_iso(),
            "mode": mode,
            "query_excerpt": str(query_excerpt or "")[:200],
            "matched_skill_ids": [],
            "selected_skill_ids": [],
            "dropped_skill_ids": [],
            "dropped_skill_reasons": {},
            "injected_skill_ids": [],
            "injected_chars": 0,
            "used_summary_only": False,
            "details": [],
            "hook_event_name": hook_event_name,
            "delta": bool(delta),
            "matched_rules": [],
            "knowledge_packaging_mode": packaging_mode,
            "router_mode": _router_mode_for_packaging(packaging_mode),
            "runtime_injection_mode": _runtime_injection_mode(),
            "delivery_status": "skipped",
            "delivery_reason": str(delivery_reason or "skipped"),
        }
        context_extras = _INJECTION_RECORD_EXTRAS.get()
        if context_extras:
            record.update({str(key): to_jsonable(value) for key, value in context_extras.items()})
        if extras:
            record.update({str(key): to_jsonable(value) for key, value in extras.items()})
        _append_injection_log(record)
        if is_analysis_log_rag_packaging_mode(packaging_mode):
            _append_analysis_log_rag_audit_artifacts(
                record,
                retrieval_query=str(query_excerpt or ""),
                retrieval_candidates=[],
            )
    finally:
        _reset_runtime_context(token)


def _payload_rendered_chars(
    payload: AugmentationPayload,
    *,
    wrap_payload_fn: WrapPayloadFn | None,
) -> int:
    if wrap_payload_fn is None:
        return len(str(payload.text or ""))
    return len(wrap_payload_fn(payload))


def _payload_wrapper_reserve(
    payload: AugmentationPayload,
    *,
    wrap_payload_fn: WrapPayloadFn | None,
) -> int:
    if wrap_payload_fn is None:
        return 0
    return max(0, len(wrap_payload_fn(payload)) - len(str(payload.text or "")))


def _request_submit_payload(
    *,
    rendered_text: str,
    matched_ids: list[str],
    matched_rules: list[str],
) -> AugmentationPayload:
    return AugmentationPayload(
        text=str(rendered_text or "").strip(),
        matched_ids=list(matched_ids),
        matched_rules=list(matched_rules),
        metadata={
            "variant": "request_submit",
        },
    )


def _after_tool_payload(
    *,
    rendered_text: str,
    matched_ids: list[str],
    matched_rules: list[str],
    tool_name: str,
) -> AugmentationPayload:
    return AugmentationPayload(
        text=str(rendered_text or "").strip(),
        matched_ids=list(matched_ids),
        matched_rules=list(matched_rules),
        metadata={
            "variant": "after_tool",
            "tool_name": str(tool_name or "").strip() or "unknown",
        },
    )


def compute_request_submit_augment(
    *,
    runtime_config: AugmentRuntimeConfig,
    ctx: RequestSubmitContext,
    wrap_payload_fn: WrapPayloadFn | None = None,
) -> AugmentDecision:
    token = _set_runtime_context(runtime_config)
    try:
        prompt = str(ctx.user_prompt or "")
        if not prompt:
            return AugmentDecision(should_deliver=False, reason="empty_prompt")

        mode = _knowledge_mode()
        if mode in {"off", "cold"}:
            _append_record(
                mode=mode,
                query_excerpt=prompt,
                matched_ids=[],
                injected_ids=[],
                dropped_ids=[],
                dropped_reasons={},
                injected_chars=0,
                details=[],
                hook_event_name="UserPromptSubmit",
                delta=False,
                delivery_status="skipped",
                delivery_reason="mode_disabled",
            )
            return AugmentDecision(should_deliver=False, reason="mode_disabled")

        session_id = str(ctx.session.session_id or "").strip() or None
        transcript_path = str(ctx.transcript_path or "").strip() or None
        state = _get_realtime_state(session_id, transcript_path=transcript_path)
        _hydrate_no_repeat_state_from_log(state)
        _advance_hook_index(state)
        if state and state.transcript_path:
            try:
                path = Path(state.transcript_path)
                if path.exists():
                    state.last_transcript_offset = int(path.stat().st_size)
            except Exception:
                pass

        current_app_name = ctx.app_name or _analysis_app_name()
        source_desc = ctx.source or _analysis_source()
        sink_types = list(ctx.sink_types or _analysis_sink_types())
        top_k = _knowledge_top_k()
        recall_top_m = _knowledge_recall_top_m(top_k)
        packaging_mode = _knowledge_packaging_mode()
        rag_packaging = is_analysis_log_rag_packaging_mode(packaging_mode)
        if packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL:
            match_text = _analysis_log_rag_initial_match_text(
                prompt=prompt,
                app_name=current_app_name,
                source_desc=source_desc,
                sink_types=sink_types,
            )
        elif rag_packaging:
            match_text = _build_match_text(
                _strip_injected_context_for_rag_query(prompt),
                source_desc,
            )
        else:
            match_text = _build_match_text(prompt, source_desc)

        if packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL and state is not None:
            if state.analysis_log_rag_initial_request_submit_attempted:
                append_injection_skip_record(
                    runtime_config=runtime_config,
                    hook_event_name="UserPromptSubmit",
                    delivery_reason="analysis_log_rag_initial_already_attempted",
                    delta=False,
                    query_excerpt=match_text,
                )
                return AugmentDecision(
                    should_deliver=False,
                    reason="analysis_log_rag_initial_already_attempted",
                )
            state.analysis_log_rag_initial_request_submit_attempted = True

        router = _runtime_router()
        if rag_packaging:
            matches = router.recall(
                text=match_text,
                limit=top_k,
                current_app_name=current_app_name,
                current_run_id=_current_run_id_from_injection_log(),
            )
        elif packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
            matches = router.recall(
                text=match_text,
                limit=top_k,
                current_app_name=current_app_name,
            )
        else:
            min_score = _knowledge_min_score()
            matches = [
                m
                for m in router.recall(
                    text=match_text,
                    limit=recall_top_m,
                    current_app_name=current_app_name,
                )
                if float(getattr(m, "score", 0.0) or 0.0) >= min_score
            ]
        matched_ids = [
            str(getattr(m, "chunk_id", "") or getattr(m, "skill_id", ""))
            for m in matches
        ]
        matched_rules = _summarize_match_reasons(matches)
        if not matches:
            if rag_packaging:
                append_injection_skip_record(
                    runtime_config=runtime_config,
                    hook_event_name="UserPromptSubmit",
                    delivery_reason="no_matches",
                    delta=False,
                    query_excerpt=match_text,
                )
            return AugmentDecision(should_deliver=False, reason="no_matches")

        char_budget = _request_submit_char_budget()
        rag_retrieval_candidates = (
            _analysis_log_rag_candidate_details(matches)
            if rag_packaging
            else None
        )
        rag_selected_matches = matches
        rag_pre_dropped_ids: list[str] = []
        rag_pre_dropped_reasons: dict[str, str] = {}
        if rag_packaging:
            rag_selected_matches, rag_pre_dropped_ids, rag_pre_dropped_reasons = _select_fresh_analysis_log_rag_matches(
                matches,
                state=state,
                limit=top_k,
            )
            rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_analysis_log_rag_matches(
                matches=rag_selected_matches,
                char_budget=char_budget,
            )
            dropped_ids, dropped_reasons = _merge_dropped_chunks(
                rag_pre_dropped_ids,
                rag_pre_dropped_reasons,
                dropped_ids,
                dropped_reasons,
            )
        else:
            note_matches = _pick_notes(matches, state=state, limit=top_k)
            rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_selected_knowledge(
                router=router,
                selected_note_matches=note_matches,
                char_budget=char_budget,
                state=state,
                match_text=match_text,
                current_app_name=current_app_name,
            )
        if not rendered_text.strip():
            if dropped_ids:
                _append_record(
                    mode=mode,
                    query_excerpt=match_text if rag_packaging else prompt,
                    matched_ids=matched_ids,
                    injected_ids=[],
                    dropped_ids=dropped_ids,
                    dropped_reasons=dropped_reasons,
                    injected_chars=injected_chars,
                    details=[],
                    hook_event_name="UserPromptSubmit",
                    delta=False,
                    used_summary_only=False,
                    matched_rules=matched_rules,
                    retrieval_query=match_text if rag_packaging else None,
                    retrieval_candidates=rag_retrieval_candidates,
                    delivery_status="skipped",
                    delivery_reason=_empty_render_delivery_reason(dropped_reasons),
                )
            return AugmentDecision(should_deliver=False, reason="empty_render")

        payload = _request_submit_payload(
            rendered_text=rendered_text,
            matched_ids=matched_ids,
            matched_rules=matched_rules,
        )
        if wrap_payload_fn is not None and _payload_rendered_chars(payload, wrap_payload_fn=wrap_payload_fn) > char_budget:
            reserved_budget = max(0, char_budget - _payload_wrapper_reserve(payload, wrap_payload_fn=wrap_payload_fn))
            if rag_packaging:
                rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, _ = _render_analysis_log_rag_matches(
                    matches=rag_selected_matches,
                    char_budget=reserved_budget,
                )
                dropped_ids, dropped_reasons = _merge_dropped_chunks(
                    rag_pre_dropped_ids,
                    rag_pre_dropped_reasons,
                    dropped_ids,
                    dropped_reasons,
                )
            else:
                rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, _ = _render_selected_knowledge(
                    router=router,
                    selected_note_matches=note_matches,
                    char_budget=reserved_budget,
                    state=state,
                    match_text=match_text,
                    current_app_name=current_app_name,
                )
            if not rendered_text.strip():
                if dropped_ids:
                    _append_record(
                        mode=mode,
                        query_excerpt=match_text if rag_packaging else prompt,
                        matched_ids=matched_ids,
                        injected_ids=[],
                        dropped_ids=dropped_ids,
                        dropped_reasons=dropped_reasons,
                        injected_chars=0,
                        details=[],
                        hook_event_name="UserPromptSubmit",
                        delta=False,
                        used_summary_only=False,
                        matched_rules=matched_rules,
                        retrieval_query=match_text if rag_packaging else None,
                        retrieval_candidates=rag_retrieval_candidates,
                        delivery_status="skipped",
                        delivery_reason=_empty_render_delivery_reason(dropped_reasons),
                    )
                return AugmentDecision(should_deliver=False, reason="empty_render")
            payload = _request_submit_payload(
                rendered_text=rendered_text,
                matched_ids=matched_ids,
                matched_rules=matched_rules,
            )

        injected_chars = _payload_rendered_chars(payload, wrap_payload_fn=wrap_payload_fn)
        _append_record(
            mode=mode,
            query_excerpt=match_text if rag_packaging else prompt,
            matched_ids=matched_ids,
            injected_ids=injected_ids,
            dropped_ids=dropped_ids,
            dropped_reasons=dropped_reasons,
            injected_chars=injected_chars,
            details=details,
            hook_event_name="UserPromptSubmit",
            delta=False,
            used_summary_only=used_summary_only,
            matched_rules=matched_rules,
            retrieval_query=match_text if rag_packaging else None,
            retrieval_candidates=rag_retrieval_candidates,
        )
        _mark_injected(
            state,
            injected_ids,
            injected_note_case_keys=injected_note_case_keys,
            rendered_skill_modes=_rendered_skill_modes(details),
            react_turn_index=(
                int(state.react_turn_index or 0)
                if state is not None and state.transcript_path
                else None
            ),
            touch_delta_timestamp=False,
        )
        return AugmentDecision(should_deliver=True, payload=payload)
    finally:
        _reset_runtime_context(token)


def compute_after_tool_augment(
    *,
    runtime_config: AugmentRuntimeConfig,
    ctx: AfterToolContext,
    wrap_payload_fn: WrapPayloadFn | None = None,
) -> AugmentDecision:
    token = _set_runtime_context(runtime_config)
    lock: threading.RLock | None = None
    try:
        session_id = str(ctx.session.session_id or "").strip() or None
        lock = _get_realtime_lock(session_id)
        if lock is not None:
            lock.acquire()

        mode = _knowledge_mode()
        if mode not in {"warm"}:
            return AugmentDecision(should_deliver=False, reason="mode_disabled")

        packaging_mode = _knowledge_packaging_mode()
        if (
            _runtime_injection_mode() == "start_only"
            or packaging_mode == KNOWLEDGE_PACKAGING_ANALYSIS_LOG_RAG_INITIAL
        ):
            append_injection_skip_record(
                runtime_config=runtime_config,
                hook_event_name="PostToolUse",
                delivery_reason="runtime_injection_mode_start_only",
                delta=True,
                query_excerpt=str(ctx.tool_name or ""),
            )
            return AugmentDecision(should_deliver=False, reason="runtime_injection_mode_start_only")

        transcript_path = str(ctx.transcript_path or "").strip() or None
        state = _get_realtime_state(session_id, transcript_path=transcript_path)
        _hydrate_no_repeat_state_from_log(state)
        _advance_hook_index(state)
        if state and _throttle_realtime_injection(state):
            return AugmentDecision(should_deliver=False, reason="throttled")

        tool_name = str(ctx.tool_name or "").strip()
        tool_response_text = stringify_tool_response(ctx.tool_output)

        delta_text = str(ctx.delta_context or "")
        delta_fingerprints: list[str] = []
        if state is not None and not delta_text:
            delta_text, next_offset, delta_fingerprints = read_transcript_delta(
                state.transcript_path or transcript_path,
                start_offset=state.last_transcript_offset,
            )
            state.last_transcript_offset = int(next_offset)
            for fp in delta_fingerprints:
                state.seen_message_fingerprints.add(fp)
        if state is not None and delta_text:
            _advance_react_turn_index(state, extract_react_tool_turn_ids(delta_text))

        router = _runtime_router()
        current_app_name = ctx.app_name or _analysis_app_name()
        source_desc = ctx.source or _analysis_source()
        rag_packaging = is_analysis_log_rag_packaging_mode(packaging_mode)
        if rag_packaging:
            query_tool_response_text = _strip_injected_context_for_rag_query(tool_response_text)
            query_delta_text = _strip_injected_context_for_rag_query(delta_text)
        else:
            query_tool_response_text = tool_response_text
            query_delta_text = delta_text
        match_text = _build_match_text(query_tool_response_text, query_delta_text, source_desc)
        if not match_text:
            return AugmentDecision(should_deliver=False, reason="empty_match_text")

        top_k = _knowledge_top_k()
        recall_top_m = _knowledge_recall_top_m(top_k)
        if rag_packaging:
            matches = router.recall(
                text=match_text,
                limit=top_k,
                current_app_name=current_app_name,
                current_run_id=_current_run_id_from_injection_log(),
            )
        elif packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
            matches = router.recall(
                text=match_text,
                limit=top_k,
                current_app_name=current_app_name,
            )
        else:
            min_score = _knowledge_min_score()
            matches = [
                m
                for m in router.recall(
                    text=match_text,
                    limit=recall_top_m,
                    current_app_name=current_app_name,
                )
                if float(getattr(m, "score", 0.0) or 0.0) >= min_score
            ]
        matched_ids = [
            str(getattr(m, "chunk_id", "") or getattr(m, "skill_id", ""))
            for m in matches
        ]
        matched_rules = _summarize_match_reasons(matches)
        if not matches:
            if rag_packaging:
                append_injection_skip_record(
                    runtime_config=runtime_config,
                    hook_event_name="PostToolUse",
                    delivery_reason="no_matches",
                    delta=True,
                    query_excerpt=match_text,
                )
            return AugmentDecision(should_deliver=False, reason="no_matches")

        char_budget = _delta_char_budget(_knowledge_injection_char_budget())
        rag_retrieval_candidates = (
            _analysis_log_rag_candidate_details(matches)
            if rag_packaging
            else None
        )
        rag_selected_matches = matches
        rag_pre_dropped_ids: list[str] = []
        rag_pre_dropped_reasons: dict[str, str] = {}
        if rag_packaging:
            rag_selected_matches, rag_pre_dropped_ids, rag_pre_dropped_reasons = _select_fresh_analysis_log_rag_matches(
                matches,
                state=state,
                limit=top_k,
            )
            rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_analysis_log_rag_matches(
                matches=rag_selected_matches,
                char_budget=char_budget,
            )
            dropped_ids, dropped_reasons = _merge_dropped_chunks(
                rag_pre_dropped_ids,
                rag_pre_dropped_reasons,
                dropped_ids,
                dropped_reasons,
            )
        else:
            note_matches = _pick_notes(matches, state=state, limit=top_k)
            rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_selected_knowledge(
                router=router,
                selected_note_matches=note_matches,
                char_budget=char_budget,
                state=state,
                match_text=match_text,
                current_app_name=current_app_name,
            )
        payload = _after_tool_payload(
            rendered_text=rendered_text,
            matched_ids=matched_ids,
            matched_rules=matched_rules,
            tool_name=tool_name,
        )
        if wrap_payload_fn is not None and payload.text and _payload_rendered_chars(payload, wrap_payload_fn=wrap_payload_fn) > char_budget:
            reserved_budget = max(0, char_budget - _payload_wrapper_reserve(payload, wrap_payload_fn=wrap_payload_fn))
            if rag_packaging:
                rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_analysis_log_rag_matches(
                    matches=rag_selected_matches,
                    char_budget=reserved_budget,
                )
                dropped_ids, dropped_reasons = _merge_dropped_chunks(
                    rag_pre_dropped_ids,
                    rag_pre_dropped_reasons,
                    dropped_ids,
                    dropped_reasons,
                )
            else:
                rendered_text, injected_ids, details, injected_note_case_keys, used_summary_only, dropped_ids, dropped_reasons, injected_chars = _render_selected_knowledge(
                    router=router,
                    selected_note_matches=note_matches,
                    char_budget=reserved_budget,
                    state=state,
                    match_text=match_text,
                    current_app_name=current_app_name,
                )
            payload = _after_tool_payload(
                rendered_text=rendered_text,
                matched_ids=matched_ids,
                matched_rules=matched_rules,
                tool_name=tool_name,
            )
        if not payload.text.strip():
            if dropped_ids:
                _append_record(
                    mode=mode,
                    query_excerpt=(
                        f"[PostToolUse:{tool_name}] {match_text[:160]}"
                        if rag_packaging
                        else f"[PostToolUse:{tool_name}] {tool_response_text[:160]}"
                    ),
                    matched_ids=matched_ids,
                    injected_ids=[],
                    dropped_ids=dropped_ids,
                    dropped_reasons=dropped_reasons,
                    injected_chars=injected_chars,
                    details=[],
                    hook_event_name="PostToolUse",
                    delta=True,
                    used_summary_only=False,
                    matched_rules=matched_rules[:24],
                    retrieval_query=match_text if rag_packaging else None,
                    retrieval_candidates=rag_retrieval_candidates,
                    delivery_status="skipped",
                    delivery_reason=_empty_render_delivery_reason(dropped_reasons),
                )
            return AugmentDecision(should_deliver=False, reason="empty_render")

        injected_chars = _payload_rendered_chars(payload, wrap_payload_fn=wrap_payload_fn)
        _mark_injected(
            state,
            injected_ids,
            injected_note_case_keys=injected_note_case_keys,
            rendered_skill_modes=_rendered_skill_modes(details),
            touch_delta_timestamp=True,
        )
        _append_record(
            mode=mode,
            query_excerpt=(
                f"[PostToolUse:{tool_name}] {match_text[:160]}"
                if rag_packaging
                else f"[PostToolUse:{tool_name}] {tool_response_text[:160]}"
            ),
            matched_ids=matched_ids,
            injected_ids=injected_ids,
            dropped_ids=dropped_ids,
            dropped_reasons=dropped_reasons,
            injected_chars=injected_chars,
            details=details,
            hook_event_name="PostToolUse",
            delta=True,
            used_summary_only=used_summary_only,
            matched_rules=matched_rules[:24],
            retrieval_query=match_text if rag_packaging else None,
            retrieval_candidates=rag_retrieval_candidates,
        )
        return AugmentDecision(should_deliver=True, payload=payload)
    finally:
        if lock is not None:
            lock.release()
        _reset_runtime_context(token)
