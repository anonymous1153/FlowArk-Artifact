"""Single-task execution helpers for the evaluation harness."""

from __future__ import annotations

import os
import sys
import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flowark.anthropic_env import (
    ANTHROPIC_AUTH_ENV_KEYS,
    ANTHROPIC_BASE_URL_ENV_KEYS,
    ANTHROPIC_MODEL_ENV_KEYS,
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)
from flowark.backend_transport import (
    INTERNAL_BACKEND_TRANSPORT_ENV_KEYS,
    build_internal_backend_transport_env,
)
from flowark.mem0_metering import build_mem0_backend_aggregate, collect_mem0_usage_for_run
from flowark.process_tree import terminate_asyncio_process_tree

from .cases import build_eval_query, build_source_description, effective_case_sink_categories
from .common import DEFAULT_SINK_CATEGORIES, _json_dump, _json_load, _safe_float, _safe_int, _slugify
from .health import apply_health_status, collect_case_health_issues
from .models import EvalTask

if TYPE_CHECKING:
    from .orchestrator import EvaluationHarness


def _build_eval_child_backend_env(harness: "EvaluationHarness") -> dict[str, str]:
    overrides = build_internal_backend_transport_env(
        llm_judge_base_url=harness.config.llm_judge_base_url,
        llm_judge_api_key=harness.config.llm_judge_api_key,
        llm_judge_model=harness.config.llm_judge_model,
        llm_judge_timeout_seconds=harness.config.llm_judge_timeout_seconds,
        llm_judge_max_retries=harness.config.llm_judge_max_retries,
        reuse_embed_base_url=harness.config.reuse_embed_base_url,
        reuse_embed_api_key=harness.config.reuse_embed_api_key,
        reuse_embed_model=harness.config.reuse_embed_model,
        reuse_embed_verify_ssl=harness.config.reuse_embed_verify_ssl,
        reuse_rerank_base_url=harness.config.reuse_rerank_base_url,
        reuse_rerank_api_key=harness.config.reuse_rerank_api_key,
        reuse_rerank_model=harness.config.reuse_rerank_model,
        reuse_rerank_timeout_seconds=harness.config.reuse_rerank_timeout_seconds,
    )
    runtime_name = str(getattr(harness, "_runtime_backend_selection_name", "") or "").strip()
    runtime_base_url = str(harness.config.runtime_backend_base_url or "").strip()
    runtime_auth_token = str(harness.config.runtime_backend_auth_token or "").strip()
    runtime_model = str(harness.config.runtime_backend_model or "").strip()
    if runtime_name or runtime_base_url or runtime_auth_token or runtime_model:
        overrides[STUDIO_BACKEND_PROFILE_ENV] = runtime_name or "eval_snapshot"
    if runtime_base_url:
        overrides[STUDIO_RUNTIME_BASE_URL_ENV] = str(harness.config.runtime_backend_base_url)
    if runtime_auth_token:
        overrides[STUDIO_RUNTIME_AUTH_TOKEN_ENV] = str(harness.config.runtime_backend_auth_token)
    if runtime_model:
        overrides[STUDIO_RUNTIME_MODEL_ENV] = str(harness.config.runtime_backend_model)
    return overrides


def _build_run_command(harness: EvaluationHarness, task: EvalTask) -> list[str]:
    case = task.case
    sink_types = effective_case_sink_categories(
        case,
        fallback=list(harness.config.sink_categories or DEFAULT_SINK_CATEGORIES),
    )
    sink_csv = ",".join(sink_types)
    mode_lower = str(task.mode or "").strip().lower()
    if mode_lower == "native":
        mode_lower = "naive"
    skills_dir, _, _ = harness._knowledge_dirs()
    cmd = [
        sys.executable,
        str(harness.workspace_root / "main.py"),
        "run",
        "--agent-mode",
        task.mode,
        "--query",
        build_eval_query(case),
        "--source",
        build_source_description(case),
        "--app-name",
        case.app_name,
        "--sink-types",
        sink_csv,
        "--cwd",
        case.source_dir,
        "--out-dir",
        str(task.repeat_dir / "runs"),
        "--agent-adapter",
        str(harness.config.agent_adapter or "opencode"),
        "--knowledge-mode",
        harness.config.knowledge_mode,
        "--knowledge-allow-repeat-injection-within-session",
        "on" if harness.config.knowledge_allow_repeat_injection_within_session else "off",
        "--knowledge-repeat-summary-react-gap",
        str(max(0, int(harness.config.knowledge_repeat_summary_react_gap))),
        "--knowledge-repeat-full-react-gap",
        str(max(0, int(harness.config.knowledge_repeat_full_react_gap))),
        "--auto-knowledge-cycle",
        "on" if harness.config.auto_knowledge_cycle else "off",
        "--runtime-injection-mode",
        str(harness.config.runtime_injection_mode or "context_aware"),
        "--knowledge-distillation-mode",
        str(harness.config.knowledge_distillation_mode or "with_selection_rules"),
        "--knowledge-packaging-mode",
        str(harness.config.knowledge_packaging_mode or "dsl_rule"),
        "--auto-knowledge-validate-mode",
        str(harness.config.auto_knowledge_validate_mode or "static"),
        "--knowledge-top-k",
        str(max(1, int(harness.config.knowledge_top_k or 3))),
        "--knowledge-recall-top-m",
        str(max(1, int(harness.config.knowledge_recall_top_m or 8))),
    ]
    if harness.config.opencode_binary:
        cmd += ["--opencode-binary", str(harness.config.opencode_binary)]
    if harness.config.opencode_model:
        cmd += ["--opencode-model", str(harness.config.opencode_model)]
    cmd += [
        "--opencode-provider",
        str(harness.config.opencode_provider or "anthropic"),
        "--opencode-after-tool-delivery",
        str(harness.config.opencode_after_tool_delivery or "no_reply_context"),
        "--opencode-bash-policy",
        str(harness.config.opencode_bash_policy or "read_only_guarded"),
        "--opencode-post-phase-mode",
        str(harness.config.opencode_post_phase_mode or "plain_json_same_surface"),
        "--opencode-structured-output",
        "on" if harness.config.opencode_structured_output else "off",
    ]
    cmd += [
        "--knowledge-reuse-digest-mode",
        str(harness.config.knowledge_reuse_digest_mode or "off"),
        "--runtime-backend-mode",
        "single",
    ]
    if mode_lower == "flowark":
        cmd += ["--skills-dir", str(skills_dir)]
    return cmd


def _collect_run_dir(runs_parent: Path, before: set[str]) -> Path | None:
    if not runs_parent.exists():
        return None
    current = {p.name for p in runs_parent.iterdir() if p.is_dir()}
    new_names = sorted(current - before)
    if new_names:
        return runs_parent / new_names[-1]
    return None


def _phase_family_from_name(name: Any) -> str | None:
    phase_name = str(name or "").strip()
    if not phase_name:
        return None
    if phase_name.startswith("analysis"):
        return "analysis"
    if phase_name.startswith("final_report"):
        return "final_report"
    if phase_name.startswith("eval_summary"):
        return "eval_summary"
    if phase_name.startswith("knowledge_synth"):
        return "knowledge_synth"
    if phase_name.startswith("knowledge_rule_repair"):
        return "knowledge_rule_repair"
    return None


def _empty_usage_metric() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _add_usage_metric(target: dict[str, int], usage: dict[str, Any]) -> None:
    for key in tuple(target.keys()):
        target[key] += int(_safe_int(usage.get(key)) or 0)


def _aggregate_phase_family(phases: list[dict[str, Any]], family: str) -> dict[str, Any]:
    usage = _empty_usage_metric()
    phase_names: list[str] = []
    total_turns = 0
    total_duration_ms = 0
    total_duration_api_ms = 0
    total_cost_usd = 0.0
    total_tool_use_blocks = 0
    total_subagent_runs = 0
    terminal_phase_name: str | None = None
    terminal_is_error = False

    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("name") or phase.get("phase_name") or "").strip()
        if _phase_family_from_name(phase_name) != family:
            continue
        phase_result = phase.get("result") if isinstance(phase.get("result"), dict) else {}
        phase_usage = phase_result.get("usage") if isinstance(phase_result.get("usage"), dict) else {}
        phase_names.append(phase_name)
        total_turns += int(_safe_int(phase_result.get("num_turns")) or 0)
        total_duration_ms += int(_safe_int(phase_result.get("duration_ms")) or 0)
        total_duration_api_ms += int(_safe_int(phase_result.get("duration_api_ms")) or 0)
        total_cost_usd += float(_safe_float(phase_result.get("total_cost_usd")) or 0.0)
        total_tool_use_blocks += int(_safe_int(phase.get("tool_use_block_count")) or 0)
        total_subagent_runs += int(_safe_int(phase.get("subagent_run_count")) or 0)
        _add_usage_metric(usage, phase_usage)
        terminal_phase_name = phase_name
        terminal_is_error = phase_result.get("is_error") is True

    if not phase_names:
        return {}
    return {
        "phase_name": terminal_phase_name,
        "phase_names": phase_names,
        "phase_count": len(phase_names),
        "is_error": terminal_is_error,
        "react_turns": total_turns,
        "num_turns": total_turns,
        "duration_ms": total_duration_ms,
        "duration_api_ms": total_duration_api_ms,
        "total_cost_usd": round(total_cost_usd, 10),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_input_tokens": usage["cache_read_input_tokens"],
        "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
        "visible_non_cache_tokens": (
            usage["input_tokens"] + usage["output_tokens"] + usage["cache_creation_input_tokens"]
        ),
        "tool_use_block_count": total_tool_use_blocks,
        "subagent_run_count": total_subagent_runs,
    }


def _terminal_phase_errors(phases: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    latest_by_family: dict[str, tuple[str, bool]] = {}
    all_error_phase_names: list[str] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("name") or phase.get("phase_name") or "").strip()
        phase_result = phase.get("result") if isinstance(phase.get("result"), dict) else {}
        if phase_result.get("is_error") is True and phase_name:
            all_error_phase_names.append(phase_name)
        family = _phase_family_from_name(phase_name)
        if family:
            latest_by_family[family] = (phase_name, phase_result.get("is_error") is True)
    terminal_error_phase_names = [
        phase_name
        for phase_name, is_error in latest_by_family.values()
        if is_error and str(phase_name or "").strip()
    ]
    return all_error_phase_names, terminal_error_phase_names


def _extract_run_metrics(run_dir: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "analysis": {},
        "end_to_end": {},
        "paths": {},
    }
    if not run_dir:
        return result
    paths = {
        "run_dir": str(run_dir),
        "cost_summary_json": str(run_dir / "cost_summary.json"),
        "final_report_md": str(run_dir / "final_report.md"),
        "final_report_json": str(run_dir / "final_report.json"),
        "raw_transcript": str(run_dir / "raw_transcript.txt"),
        "auto_knowledge_cycle_json": str(run_dir / "auto_knowledge_cycle.json"),
    }
    opencode_metrics = _extract_opencode_run_metrics(run_dir)
    if opencode_metrics:
        paths.update(
            {
                "opencode_usage_rollup_json": str(run_dir / "opencode_usage_rollup.json"),
                "opencode_messages_json": str(run_dir / "opencode_messages.json"),
                "opencode_hook_trace_jsonl": str(run_dir / "opencode_hook_trace.jsonl"),
                "opencode_tool_summary_json": str(run_dir / "opencode_tool_summary.json"),
            }
        )
    result["paths"] = paths
    result["opencode"] = opencode_metrics
    mem0_snapshot_path = run_dir / "mem0_runtime_snapshot.json"
    mem0_summary_path = run_dir / "mem0_usage_summary.json"
    if mem0_snapshot_path.exists():
        paths["mem0_runtime_snapshot_json"] = str(mem0_snapshot_path)
    if mem0_summary_path.exists():
        paths["mem0_usage_summary_json"] = str(mem0_summary_path)
        try:
            mem0_summary = _json_load(mem0_summary_path)
        except Exception:
            mem0_summary = {}
        if isinstance(mem0_summary, dict):
            _attach_mem0_backend_metrics(
                result,
                mem0_backend=build_mem0_backend_aggregate(mem0_summary),
            )
    cost_path = run_dir / "cost_summary.json"
    if not cost_path.exists():
        return result
    try:
        cost = _json_load(cost_path)
    except Exception:
        return result
    phases = cost.get("phases") if isinstance(cost, dict) else []
    all_error_phase_names: list[str] = []
    terminal_error_phase_names: list[str] = []
    if isinstance(phases, list):
        all_error_phase_names, terminal_error_phase_names = _terminal_phase_errors(phases)
        result["analysis"] = _aggregate_phase_family(phases, "analysis")
    agg = cost.get("aggregated_metrics") if isinstance(cost, dict) else {}
    if isinstance(agg, dict):
        main_agent = agg.get("main_agent") if isinstance(agg.get("main_agent"), dict) else {}
        subagents = agg.get("subagents") if isinstance(agg.get("subagents"), dict) else {}
        mem0_backend = agg.get("mem0_backend") if isinstance(agg.get("mem0_backend"), dict) else {}
        total_with_mem0 = agg.get("total_with_mem0") if isinstance(agg.get("total_with_mem0"), dict) else {}
        main_usage = main_agent.get("usage") if isinstance(main_agent.get("usage"), dict) else {}
        error_result_count = _safe_int(main_agent.get("error_result_count"))
        if error_result_count is None:
            error_result_count = len(all_error_phase_names)
        raw_error_phase_names = main_agent.get("error_phase_names")
        if isinstance(raw_error_phase_names, list):
            all_error_phase_names = [str(item).strip() for item in raw_error_phase_names if str(item).strip()]
        terminal_error_result_count = _safe_int(main_agent.get("terminal_error_result_count"))
        if terminal_error_result_count is None:
            terminal_error_result_count = len(terminal_error_phase_names)
        raw_terminal_error_phase_names = main_agent.get("terminal_error_phase_names")
        if isinstance(raw_terminal_error_phase_names, list):
            terminal_error_phase_names = [
                str(item).strip() for item in raw_terminal_error_phase_names if str(item).strip()
            ]
        result["end_to_end"] = {
            "react_turns": _safe_int(main_agent.get("num_turns_sum")),
            "num_turns_sum": _safe_int(main_agent.get("num_turns_sum")),
            "duration_ms_sum": _safe_int(main_agent.get("duration_ms_sum")),
            "duration_api_ms_sum": _safe_int(main_agent.get("duration_api_ms_sum")),
            "total_cost_usd_sum": _safe_float(main_agent.get("total_cost_usd_sum")),
            "error_result_count": error_result_count,
            "error_phase_names": all_error_phase_names,
            "has_error_result": bool(main_agent.get("has_error_result")) or bool((error_result_count or 0) > 0),
            "terminal_error_result_count": terminal_error_result_count,
            "terminal_error_phase_names": terminal_error_phase_names,
            "has_terminal_error_result": bool(main_agent.get("has_terminal_error_result"))
            or bool((terminal_error_result_count or 0) > 0),
            "input_tokens": _safe_int(main_usage.get("input_tokens")),
            "output_tokens": _safe_int(main_usage.get("output_tokens")),
            "cache_read_input_tokens": _safe_int(main_usage.get("cache_read_input_tokens")),
            "cache_creation_input_tokens": _safe_int(main_usage.get("cache_creation_input_tokens")),
            "visible_non_cache_tokens": _safe_int(main_usage.get("visible_non_cache_tokens")),
            "tool_use_block_count_total": _safe_int(cost.get("tool_use_block_count_total")),
            "subagent_run_count": _safe_int(subagents.get("run_count")),
        }
        if mem0_backend:
            _attach_mem0_backend_metrics(
                result,
                mem0_backend=mem0_backend,
                total_with_mem0=total_with_mem0,
                main_agent=main_agent,
            )
    return result


def _extract_opencode_run_metrics(run_dir: Path) -> dict[str, Any]:
    rollup_path = run_dir / "opencode_usage_rollup.json"
    if not rollup_path.exists():
        return {}
    try:
        rollup = _json_load(rollup_path)
    except Exception:
        return {}
    if not isinstance(rollup, dict):
        return {}
    usage = rollup.get("usage") if isinstance(rollup.get("usage"), dict) else {}
    bash = rollup.get("bash") if isinstance(rollup.get("bash"), dict) else {}
    turns = rollup.get("turns") if isinstance(rollup.get("turns"), list) else []
    deliveries = []
    structured_requested = False
    structured_delivered = False
    structured_errors: list[str] = []
    json_parse_modes: list[str] = []
    native_structured_output = False
    for item in turns:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        json_mode = result.get("json_parse_mode") or item.get("json_parse_mode")
        if isinstance(json_mode, str) and json_mode.strip() and json_mode not in json_parse_modes:
            json_parse_modes.append(json_mode)
        if bool(result.get("native_structured_output") or item.get("native_structured_output")):
            native_structured_output = True
        if result.get("structured_output_requested") is True or item.get("structured_output_requested") is True:
            structured_requested = True
        if result.get("structured_output_delivered") is True or item.get("structured_output_delivered") is True:
            structured_delivered = True
        error_text = str(result.get("structured_output_error") or item.get("structured_output_error") or "").strip()
        if error_text:
            structured_errors.append(error_text)
        turn_delivery = result.get("after_tool_delivery") or item.get("after_tool_delivery")
        if isinstance(turn_delivery, str) and turn_delivery.strip() and turn_delivery not in deliveries:
            deliveries.append(turn_delivery)
    latest_usage_path = run_dir / "opencode_usage.json"
    latest_usage = {}
    if latest_usage_path.exists():
        try:
            payload = _json_load(latest_usage_path)
        except Exception:
            payload = {}
        latest_usage = payload if isinstance(payload, dict) else {}
        delivery = latest_usage.get("after_tool_delivery")
        if isinstance(delivery, str) and delivery.strip() and delivery not in deliveries:
            deliveries.append(delivery)
    return {
        "adapter": "opencode",
        "usage_rollup_json": str(rollup_path),
        "turn_count": _safe_int(rollup.get("turn_count")),
        "unique_assistant_usage_record_count": _safe_int(rollup.get("unique_assistant_usage_record_count")),
        "request_usage_record_count": _safe_int(rollup.get("request_usage_record_count")),
        "after_tool_deliveries": deliveries,
        "structured_output_requested": structured_requested,
        "structured_output_delivered": structured_delivered,
        "structured_output_error": structured_errors[-1] if structured_errors else None,
        "json_parse_modes": json_parse_modes,
        "native_structured_output": native_structured_output,
        "input_tokens": _safe_int(usage.get("input_tokens")),
        "output_tokens": _safe_int(usage.get("output_tokens")),
        "reasoning_tokens": _safe_int(usage.get("reasoning_tokens")),
        "total_tokens": _safe_int(usage.get("total_tokens")),
        "cache_read_input_tokens": _safe_int(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _safe_int(usage.get("cache_creation_input_tokens")),
        "total_cost_usd": _safe_float(rollup.get("total_cost_usd")),
        "knowledge_injection_count": _sum_turn_field(turns, "knowledge_injection_count"),
        "knowledge_injected_chars": _sum_turn_field(turns, "knowledge_injected_chars"),
        "synthetic_flowark_user_message_count": _sum_turn_field(turns, "synthetic_flowark_user_message_count"),
        "bash_count": _safe_int(bash.get("bash_count")) or 0,
        "bash_code_context_count": _safe_int(bash.get("bash_code_context_count")) or 0,
        "bash_trace_only_count": _safe_int(bash.get("bash_trace_only_count")) or 0,
        "bash_blocked_count": _safe_int(bash.get("bash_blocked_count")) or 0,
    }


def _sum_turn_field(turns: list[Any], field: str) -> int:
    total = 0
    for item in turns:
        if isinstance(item, dict):
            total += int(_safe_int(item.get(field)) or 0)
    return total


def _attach_mem0_backend_metrics(
    result: dict[str, Any],
    *,
    mem0_backend: dict[str, Any],
    total_with_mem0: dict[str, Any] | None = None,
    main_agent: dict[str, Any] | None = None,
) -> None:
    if not mem0_backend:
        return

    mem0_usage = mem0_backend.get("usage") if isinstance(mem0_backend.get("usage"), dict) else {}
    mem0_status = str(mem0_backend.get("status") or "").strip().lower()
    mem0_cost_tracked = mem0_backend.get("cost_tracked") is True
    mem0_usage_tracked = mem0_status == "ok"
    mem0_cost = _safe_float(mem0_backend.get("total_cost_usd_sum")) if mem0_cost_tracked else None
    total_with = total_with_mem0 if isinstance(total_with_mem0, dict) else {}
    main = main_agent if isinstance(main_agent, dict) else {}
    total_with_cost = _safe_float(total_with.get("total_cost_usd_sum"))
    main_cost = _safe_float(main.get("total_cost_usd_sum"))
    if total_with_cost is None and mem0_cost_tracked and mem0_cost is not None and main_cost is not None:
        total_with_cost = main_cost + mem0_cost

    result["mem0_backend"] = {
        "status": mem0_backend.get("status"),
        "cost_tracked": mem0_cost_tracked,
        "record_count": (
            _safe_int(mem0_backend.get("result_count")) or _safe_int(mem0_backend.get("request_count"))
            if mem0_usage_tracked
            else None
        ),
        "total_cost_usd_sum": mem0_cost,
        "input_tokens": _safe_int(mem0_usage.get("input_tokens")) if mem0_usage_tracked else None,
        "output_tokens": _safe_int(mem0_usage.get("output_tokens")) if mem0_usage_tracked else None,
        "llm_total_tokens": _safe_int(mem0_usage.get("llm_total_tokens")) if mem0_usage_tracked else None,
        "embedding_total_tokens": (
            _safe_int(mem0_usage.get("embedding_tokens")) if mem0_usage_tracked else None
        ),
        "total_tokens": _safe_int(mem0_usage.get("total_tokens")) if mem0_usage_tracked else None,
        "estimated_token_records": (
            _safe_int(mem0_backend.get("estimated_token_records")) if mem0_usage_tracked else None
        ),
        "missing_usage_records": (
            _safe_int(mem0_backend.get("missing_usage_records")) if mem0_usage_tracked else None
        ),
    }
    end_to_end = result.setdefault("end_to_end", {})
    end_to_end["mem0_total_cost_usd_sum"] = mem0_cost
    end_to_end["total_with_mem0_cost_usd_sum"] = total_with_cost if mem0_cost_tracked else None
    end_to_end["mem0_llm_total_tokens"] = (
        _safe_int(mem0_usage.get("llm_total_tokens")) if mem0_usage_tracked else None
    )
    end_to_end["mem0_embedding_total_tokens"] = (
        _safe_int(mem0_usage.get("embedding_tokens")) if mem0_usage_tracked else None
    )
    end_to_end["mem0_total_tokens"] = (
        _safe_int(mem0_usage.get("total_tokens")) if mem0_usage_tracked else None
    )


def _mem0_usage_summary_is_reusable(summary_path: Path, snapshot_path: Path) -> bool:
    try:
        summary = _json_load(summary_path)
        snapshot = _json_load(snapshot_path)
    except Exception:
        return False
    if not isinstance(summary, dict) or not isinstance(snapshot, dict):
        return False
    if str(summary.get("status") or "").strip().lower() != "ok":
        return False
    expected_id = str(snapshot.get("metering_run_id") or "").strip()
    actual_id = str(summary.get("metering_run_id") or "").strip()
    return not expected_id or actual_id == expected_id


def _collect_mem0_usage_after_child_exit(run_dir: Path | None) -> None:
    if run_dir is None:
        return
    snapshot_path = run_dir / "mem0_runtime_snapshot.json"
    if not snapshot_path.is_file():
        return
    summary_path = run_dir / "mem0_usage_summary.json"
    if summary_path.is_file() and _mem0_usage_summary_is_reusable(summary_path, snapshot_path):
        return

    cost_path = run_dir / "cost_summary.json"
    rewrite_cost_summary = False
    try:
        run_summary = _json_load(cost_path) if cost_path.is_file() else {}
        rewrite_cost_summary = cost_path.is_file() and isinstance(run_summary, dict)
    except Exception:
        run_summary = {}
    if not isinstance(run_summary, dict):
        run_summary = {}
        rewrite_cost_summary = False
    try:
        summary = collect_mem0_usage_for_run(run_dir=run_dir, run_summary=run_summary)
    except Exception:
        return
    if summary is not None and rewrite_cost_summary:
        _json_dump(cost_path, run_summary)


def _build_interrupted_llm_judge_result(
    harness: EvaluationHarness,
    *,
    repeat_dir: Path,
    reason: str,
) -> dict[str, Any]:
    judge_input_path = repeat_dir / "llm_judge_input.json"
    judge_raw_response_path = repeat_dir / "llm_judge_raw_response.json"
    judge_result_path = repeat_dir / "llm_judge_result.json"
    payload = {
        "enabled": bool(harness.config.llm_judge_enabled),
        "eligible": False,
        "skipped": True,
        "status": "skipped_interrupted",
        "model": harness.config.llm_judge_model,
        "input_path": str(judge_input_path),
        "raw_response_path": str(judge_raw_response_path),
        "result_path": str(judge_result_path),
        "verdict": "unknown",
        "is_correct": None,
        "confidence": None,
        "score": None,
        "summary": "",
        "reasons": [str(reason or "interrupted")],
        "matched_true_flow_ids": [],
        "missed_true_flow_ids": [],
        "false_positive_flow_ids": [],
        "usage": {},
        "error": None,
        "skip_reason": str(reason or "interrupted"),
    }
    _json_dump(judge_result_path, payload)
    return payload


def _dummy_sink_label(category: str) -> str:
    mapping = {
        "log": "log",
        "network": "network",
        "icc": "component communication",
        "file": "file write",
        "database": "database",
        "storage": "sharedpreferences storage",
        "others": "others",
    }
    key = str(category or "").strip().lower()
    return mapping.get(key, key or "unknown")


def _write_dummy_run_artifacts(
    harness: EvaluationHarness,
    task: EvalTask,
    *,
    runs_parent: Path,
    gt_sink_categories: list[str],
) -> tuple[Path, str, str]:
    run_dir = runs_parent / f"dummy-{_slugify(task.mode, 16)}-r{task.repeat_idx:02d}-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Dummy mode is for validating the batch harness; keep predictions deterministic
    # and close to the GT categories so summary/comparison logic can be exercised.
    predicted_categories = sorted({c for c in (gt_sink_categories or []) if c}) or ["log"]
    sinks_found = [
        {
            "sink_type": _dummy_sink_label(cat),
            "description": f"dummy_run simulated sink category={cat}",
        }
        for cat in predicted_categories
    ]
    final_report_md = (
        f"# Dummy Report ({task.mode})\n\n"
        f"- simulated: true\n"
        f"- predicted sink categories: {', '.join(predicted_categories)}\n"
        f"- repeat: {task.repeat_idx}\n"
    )
    _json_dump(
        run_dir / "final_report.json",
        {
            "schema_version": "flowark-final-report-v2",
            "dummy_run": True,
            "mode": task.mode,
            "repeat_idx": task.repeat_idx,
            "query": build_eval_query(task.case),
            "source": {
                "description": build_source_description(task.case),
                "method": None,
                "location": None,
            },
            "dataflows": [
                {
                    "explain": str(item.get("description") or "dummy sink"),
                    "confidence": "high",
                    "sink": {
                        "sink_type": str(item.get("sink_type") or "unknown"),
                        "statement": str(item.get("description") or "") or None,
                        "method": None,
                        "location": None,
                    },
                    "path": [],
                }
                for item in sinks_found
            ],
            "uncertainties": [],
            "skipped_branches": [],
            "knowledge_used": {"notes": [], "flow_facts": []},
        },
    )
    (run_dir / "final_report.md").write_text(final_report_md, encoding="utf-8")
    (run_dir / "raw_transcript.txt").write_text(
        f"[dummy_run] simulated transcript for mode={task.mode} repeat={task.repeat_idx}\n",
        encoding="utf-8",
    )

    mode_factor = 2 if str(task.mode).strip().lower() == "flowark" else 1
    sink_factor = max(1, len(predicted_categories))
    analysis_turns = 4 + mode_factor + (task.repeat_idx - 1)
    analysis_duration_ms = 700 * mode_factor + 120 * sink_factor + 15 * task.repeat_idx
    analysis_api_ms = max(100, analysis_duration_ms - 80)
    analysis_input_tokens = 800 * mode_factor + 90 * sink_factor + 10 * task.repeat_idx
    analysis_output_tokens = 260 * mode_factor + 30 * sink_factor + 5 * task.repeat_idx
    cache_read_tokens = 120 * (mode_factor - 1)
    total_cost_usd = round(0.0025 * mode_factor + 0.0002 * sink_factor + 0.0001 * task.repeat_idx, 6)

    cost_summary = {
        "dummy_run": True,
        "phases": [
            {
                "name": "analysis",
                "tool_use_block_count": 2 + mode_factor,
                "subagent_run_count": 1 if mode_factor > 1 else 0,
                "result": {
                    "num_turns": analysis_turns,
                    "duration_ms": analysis_duration_ms,
                    "duration_api_ms": analysis_api_ms,
                    "total_cost_usd": total_cost_usd,
                    "usage": {
                        "input_tokens": analysis_input_tokens,
                        "output_tokens": analysis_output_tokens,
                        "cache_read_input_tokens": cache_read_tokens,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        ],
        "tool_use_block_count_total": 3 + mode_factor,
        "aggregated_metrics": {
            "main_agent": {
                "num_turns_sum": analysis_turns + mode_factor,
                "duration_ms_sum": analysis_duration_ms + 120 * mode_factor,
                "duration_api_ms_sum": analysis_api_ms + 80 * mode_factor,
                "total_cost_usd_sum": round(total_cost_usd + 0.0008 * mode_factor, 6),
                "usage": {
                    "input_tokens": analysis_input_tokens + 200 * mode_factor,
                    "output_tokens": analysis_output_tokens + 70 * mode_factor,
                    "cache_read_input_tokens": cache_read_tokens,
                    "cache_creation_input_tokens": 0,
                    "visible_non_cache_tokens": (
                        analysis_input_tokens + 200 * mode_factor + analysis_output_tokens + 70 * mode_factor
                    ),
                },
            },
            "subagents": {
                "run_count": 1 if mode_factor > 1 else 0,
            },
        },
    }
    _json_dump(run_dir / "cost_summary.json", cost_summary)
    _json_dump(
        run_dir / "auto_knowledge_cycle.json",
        {
            "dummy_run": True,
            "background": False,
            "background_status": "disabled",
        },
    )

    stdout_text = (
        f"[dummy_run] mode={task.mode} repeat={task.repeat_idx} "
        f"source_id={task.case.source_id or task.case.flow_id}\n"
    )
    stderr_text = ""
    return run_dir, stdout_text, stderr_text


async def _stream_subprocess_pipe(
    reader: asyncio.StreamReader | None,
    path: Path,
) -> str:
    if reader is None:
        path.write_text("", encoding="utf-8")
        return ""
    chunks: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            chunks.append(text)
            fp.write(text)
            fp.flush()
    return "".join(chunks)


async def _run_one_task(
    harness: EvaluationHarness,
    task: EvalTask,
    *,
    eval_root: Path,
) -> dict[str, Any]:
    started_at = time.time()
    case = task.case
    repeat_dir = task.repeat_dir
    runs_parent = repeat_dir / "runs"
    runs_parent.mkdir(parents=True, exist_ok=True)
    entry_id = harness._task_entry_id(task)

    case_input_path = repeat_dir / "case_input.json"
    normalized_case_path = repeat_dir / "normalized_case.json"
    query = build_eval_query(case)
    source_desc = build_source_description(case)
    sink_types = effective_case_sink_categories(
        case,
        fallback=list(harness.config.sink_categories or DEFAULT_SINK_CATEGORIES),
    )
    gt_sink_categories = sorted({c for c in (case.ground_truth_sink_categories or []) if c})
    gt_sink_category = gt_sink_categories[0] if len(gt_sink_categories) == 1 else None
    normalized_case = {
        "flow_id": case.flow_id,
        "source_id": case.source_id,
        "benchmark_family": case.benchmark_family,
        "dataset": case.dataset,
        "app_name": case.app_name,
        "apk_name": case.apk_name,
        "source_dir": case.source_dir,
        "classification": case.classification,
        "source": {
            "method": case.source_method,
            "classname": case.source_classname,
            "statement": case.source_statement,
        },
        "sink": {
            "method": case.sink_method,
            "classname": case.sink_classname,
            "statement": case.sink_statement,
        },
        "sink_count": len(case.sink_entries or []),
        "sink_flow_ids": [
            str(item.get("flow_id") or "").strip()
            for item in (case.sink_entries or [])
            if isinstance(item, dict) and str(item.get("flow_id") or "").strip()
        ],
        "sink_types": sink_types,
        "generated_query": query,
        "generated_source": source_desc,
        "ground_truth_sink_category": gt_sink_category,
        "ground_truth_sink_categories": gt_sink_categories,
    }
    _json_dump(case_input_path, case.raw)
    _json_dump(normalized_case_path, normalized_case)

    if harness._is_task_skip_requested(eval_root=eval_root, task=task):
        result = harness._build_skipped_result(
            task=task,
            repeat_dir=repeat_dir,
            started_at=started_at,
            reason="cancel_requested_before_launch",
        )
        result["entry_id"] = entry_id
        _json_dump(repeat_dir / "result.json", result)
        return result

    cmd = harness._build_run_command(task)
    cmd_file = repeat_dir / "subprocess_command.json"
    _json_dump(
        cmd_file,
        {
            "cmd": cmd,
            "cwd": str(harness.workspace_root),
            "started_at": harness._now_utc_iso_for_runner(),
        },
    )
    timed_out = False
    cancelled_by_user = False
    force_paused = False
    interruption_reason: str | None = None
    return_code: int
    run_dir: Path | None = None
    pipe_drain_timed_out = False
    stdout_path = repeat_dir / "stdout.txt"
    stderr_path = repeat_dir / "stderr.txt"
    if harness.config.dummy_run:
        run_dir, stdout_text, stderr_text = harness._write_dummy_run_artifacts(
            task,
            runs_parent=runs_parent,
            gt_sink_categories=gt_sink_categories,
        )
        return_code = 0
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
    else:
        before = {p.name for p in runs_parent.iterdir() if p.is_dir()}
        child_env = dict(os.environ)
        for key in INTERNAL_BACKEND_TRANSPORT_ENV_KEYS:
            child_env.pop(key, None)
        for key in (*ANTHROPIC_AUTH_ENV_KEYS, *ANTHROPIC_BASE_URL_ENV_KEYS, *ANTHROPIC_MODEL_ENV_KEYS):
            child_env.pop(key, None)
        child_env.pop(STUDIO_BACKEND_PROFILE_ENV, None)
        child_env.pop(STUDIO_RUNTIME_BASE_URL_ENV, None)
        child_env.pop(STUDIO_RUNTIME_AUTH_TOKEN_ENV, None)
        child_env.pop(STUDIO_RUNTIME_MODEL_ENV, None)
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "API_TIMEOUT_MS",
            "FLOWARK_BASE_URL",
            "FLOWARK_API_KEY",
            "FLOWARK_LLM_JUDGE_BASE_URL",
            "FLOWARK_LLM_JUDGE_API_KEY",
            "FLOWARK_KNOWLEDGE_ROUTER_BASE_URL",
            "FLOWARK_KNOWLEDGE_ROUTER_API_KEY",
            "FLOWARK_REUSE_EMBED_BASE_URL",
            "FLOWARK_REUSE_EMBED_API_KEY",
            "FLOWARK_REUSE_EMBED_MODEL",
            "FLOWARK_REUSE_EMBED_VERIFY_SSL",
            "FLOWARK_REUSE_RERANK_BASE_URL",
            "FLOWARK_REUSE_RERANK_API_KEY",
            "FLOWARK_REUSE_RERANK_MODEL",
            "FLOWARK_REUSE_RERANK_TIMEOUT_SECONDS",
        ):
            child_env.pop(key, None)
        child_env.update(_build_eval_child_backend_env(harness))
        # 确保子进程 stdout/stderr 在被 pipe 时仍尽量实时刷新，便于 eval 目录观察。
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(harness.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(harness._stream_subprocess_pipe(proc.stdout, stdout_path))
        stderr_task = asyncio.create_task(harness._stream_subprocess_pipe(proc.stderr, stderr_path))
        timeout_seconds = harness._effective_task_timeout_seconds()
        deadline_ts = time.time() + float(timeout_seconds)
        try:
            while True:
                if proc.returncode is not None:
                    break

                if run_dir is None:
                    run_dir = harness._collect_run_dir(runs_parent, before)

                if harness._is_task_force_abort_requested(eval_root=eval_root, task=task, run_dir=run_dir):
                    force_paused = True
                    cancelled_by_user = True
                    interruption_reason = "force_pause_requested"
                    await terminate_asyncio_process_tree(proc)
                    break

                if harness._is_task_skip_requested(eval_root=eval_root, task=task, run_dir=run_dir):
                    cancelled_by_user = True
                    interruption_reason = "cancel_requested"
                    await terminate_asyncio_process_tree(proc)
                    break

                now_ts = time.time()
                remaining = deadline_ts - now_ts
                if remaining <= 0:
                    timed_out = True
                    await terminate_asyncio_process_tree(proc)
                    break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
        finally:
            if run_dir is None:
                run_dir = harness._collect_run_dir(runs_parent, before)

        try:
            stdout_text, stderr_text = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pipe_drain_timed_out = True
            await terminate_asyncio_process_tree(proc)
            for pipe_task in (stdout_task, stderr_task):
                pipe_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            stdout_text = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
            stderr_text = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
        return_code = proc.returncode

        if harness._is_task_force_abort_requested(eval_root=eval_root, task=task, run_dir=run_dir):
            force_paused = True
            cancelled_by_user = True
            interruption_reason = interruption_reason or "force_pause_requested_after_process_exit"
        elif harness._is_task_skip_requested(eval_root=eval_root, task=task, run_dir=run_dir):
            cancelled_by_user = True
            interruption_reason = interruption_reason or "cancel_requested_after_process_exit"

    await asyncio.to_thread(harness._collect_mem0_usage_after_child_exit, run_dir)
    metrics = harness._extract_run_metrics(run_dir)
    pipe_drain_health_issue = (
        {
            "severity": "error",
            "code": "subprocess_pipe_drain_timeout",
            "message": "subprocess stdout/stderr did not close after process termination",
            "artifact_path": str(repeat_dir),
        }
        if pipe_drain_timed_out
        else None
    )
    pre_judge_health_issues = collect_case_health_issues(
        run_dir=run_dir,
        repeat_dir=repeat_dir,
        digest_mode=str(harness.config.knowledge_reuse_digest_mode or "off"),
        llm_judge=None,
    )
    if pipe_drain_health_issue is not None:
        pre_judge_health_issues.append(pipe_drain_health_issue)
    pre_judge_health_error = any(
        str(item.get("severity") or "").strip().lower() == "error"
        for item in pre_judge_health_issues
    )
    if cancelled_by_user:
        llm_judge = _build_interrupted_llm_judge_result(
            harness,
            repeat_dir=repeat_dir,
            reason=interruption_reason or ("force_pause_requested" if force_paused else "cancel_requested"),
        )
    elif pre_judge_health_error:
        llm_judge = _build_interrupted_llm_judge_result(
            harness,
            repeat_dir=repeat_dir,
            reason="health_error_before_llm_judge",
        )
    else:
        llm_judge = await harness._run_llm_judge(
            case=case,
            task=task,
            run_dir=run_dir,
            repeat_dir=repeat_dir,
            query=query,
            source_desc=source_desc,
            sink_types=sink_types,
        )

    completed_at = time.time()
    end_to_end_metrics = metrics.get("end_to_end") or {}
    phase_error_detected = bool(
        end_to_end_metrics.get("has_terminal_error_result")
        if "has_terminal_error_result" in end_to_end_metrics
        else end_to_end_metrics.get("has_error_result")
    )
    if force_paused:
        status = "force_paused"
    elif cancelled_by_user:
        status = "cancelled"
    elif timed_out:
        status = "timeout"
    else:
        status = "success" if return_code == 0 and not phase_error_detected else "error"
    health_issues = collect_case_health_issues(
        run_dir=run_dir,
        repeat_dir=repeat_dir,
        digest_mode=str(harness.config.knowledge_reuse_digest_mode or "off"),
        llm_judge=llm_judge,
    )
    if pipe_drain_health_issue is not None:
        health_issues.append(pipe_drain_health_issue)
    status = apply_health_status(status, health_issues)
    result = {
        "entry_id": entry_id,
        "flow_id": case.flow_id,
        "source_id": case.source_id,
        "task_index": task.task_index,
        "task_total": task.task_total,
        "dataset": case.dataset,
        "app_name": case.app_name,
        "apk_name": case.apk_name,
        "benchmark_family": case.benchmark_family,
        "classification": case.classification,
        "mode": task.mode,
        "repeat_idx": task.repeat_idx,
        "status": status,
        "health_issues": health_issues,
        "return_code": return_code,
        "timed_out": timed_out,
        "cancelled_by_user": cancelled_by_user,
        "force_paused": force_paused,
        "pipe_drain_timed_out": pipe_drain_timed_out,
        "phase_error_detected": phase_error_detected,
        "query": query,
        "source": source_desc,
        "sink_types": sink_types,
        "started_at": harness._from_timestamp_for_runner(started_at),
        "finished_at": harness._from_timestamp_for_runner(completed_at),
        "wall_time_seconds": round(completed_at - started_at, 3),
        "repeat_dir": str(repeat_dir),
        "run_dir": str(run_dir) if run_dir else None,
        "paths": {
            "case_input": str(case_input_path),
            "normalized_case": str(normalized_case_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "command": str(cmd_file),
            "llm_judge_input_json": str(repeat_dir / "llm_judge_input.json"),
            "llm_judge_raw_response_json": str(repeat_dir / "llm_judge_raw_response.json"),
            "llm_judge_result_json": str(repeat_dir / "llm_judge_result.json"),
            **(metrics.get("paths") or {}),
        },
        "dummy_run": bool(harness.config.dummy_run),
        "ground_truth_sink_categories": gt_sink_categories,
        "ground_truth_sink_count": len(case.sink_entries or []),
        "llm_judge": llm_judge,
        "metrics": {
            "analysis": metrics.get("analysis") or {},
            "end_to_end": metrics.get("end_to_end") or {},
            "opencode": metrics.get("opencode") or {},
            "mem0_backend": metrics.get("mem0_backend") or {},
        },
    }
    health_error_detected = any(str(item.get("severity") or "").strip().lower() == "error" for item in health_issues)
    if return_code != 0:
        result["error_excerpt"] = (stderr_text or stdout_text)[-2000:]
    elif health_error_detected:
        result["error_excerpt"] = "; ".join(
            str(item.get("message") or item.get("code") or "").strip()
            for item in health_issues
            if str(item.get("severity") or "").strip().lower() == "error"
        )[:2000]
    elif phase_error_detected:
        transcript_tail = ""
        raw_transcript_path = Path(str((metrics.get("paths") or {}).get("raw_transcript") or "")).expanduser()
        if raw_transcript_path.exists():
            try:
                transcript_tail = raw_transcript_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
            except Exception:
                transcript_tail = ""
        if transcript_tail:
            result["error_excerpt"] = transcript_tail
        elif (stderr_text or stdout_text).strip():
            result["error_excerpt"] = (stderr_text or stdout_text)[-2000:]
    _json_dump(repeat_dir / "result.json", result)
    return result
