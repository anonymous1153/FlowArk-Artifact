"""Reporting/progress/summary helpers for the evaluation harness."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from flowark.knowledge.artifact_audit import (
    compact_artifact_audit_result,
    write_note_only_artifact_audit,
)

from .cases import build_eval_query, build_source_description, effective_case_sink_categories
from .common import (
    DEFAULT_SINK_CATEGORIES,
    _json_dump,
    _mean,
    _median,
    _now_utc_iso,
    _safe_float,
    _safe_int,
    _slugify,
    _timestamp_slug,
)
from .models import EvalCase, EvalTask

if TYPE_CHECKING:
    from .orchestrator import EvaluationHarness


def _build_health_issue_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_code: dict[str, dict[str, Any]] = {}
    total = 0
    for result in results:
        issues = result.get("health_issues") if isinstance(result, dict) else None
        if not isinstance(issues, list):
            continue
        app_name = str(result.get("app_name") or "").strip() or "unknown"
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            total += 1
            code = str(issue.get("code") or "unknown").strip() or "unknown"
            severity = str(issue.get("severity") or "warning").strip().lower() or "warning"
            entry = by_code.setdefault(
                code,
                {
                    "code": code,
                    "severity": severity,
                    "count": 0,
                    "app_counts": {},
                },
            )
            entry["count"] = int(entry.get("count") or 0) + 1
            if severity == "error":
                entry["severity"] = "error"
            app_counts = entry.get("app_counts") if isinstance(entry.get("app_counts"), dict) else {}
            app_counts[app_name] = int(app_counts.get(app_name) or 0) + 1
            entry["app_counts"] = app_counts
    return {
        "total_issue_count": total,
        "by_code": by_code,
    }


def _rebuild_summary_payload(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    tasks: list[EvalTask],
    results: list[dict[str, Any]],
    modes: list[str],
    selected_app_count: int,
    selection_info: dict[str, Any],
    knowledge_scope_info: dict[str, Any],
    paused: bool,
    pause_mode: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        by_mode.setdefault(str(item.get("mode") or "unknown"), []).append(item)
    summaries = {mode: self._build_mode_summary(mode, by_mode.get(mode, [])) for mode in modes}
    success_count = sum(1 for item in results if str(item.get("status") or "") == "success")
    warning_count = sum(1 for item in results if str(item.get("status") or "") == "warning")
    error_count = sum(1 for item in results if str(item.get("status") or "") in {"error", "harness_error"})
    completed_task_count = self._completed_task_count_from_results(results)
    resumable_task_count = self._resumable_task_count_from_results(results)
    pending_task_count = max(0, len(tasks) - completed_task_count)
    benchmark_families = sorted(
        {
            str(task.case.benchmark_family or "").strip()
            for task in tasks
            if str(task.case.benchmark_family or "").strip()
        }
    )
    summary = {
        "created_at": _now_utc_iso(),
        "eval_root": str(eval_root),
        "case_count": len({task.case.flow_id for task in tasks}),
        "source_count": len({task.case.flow_id for task in tasks}),
        "app_count": selected_app_count,
        "benchmark_families": benchmark_families,
        "task_count": len(tasks),
        "modes": modes,
        "dummy_run": bool(self.config.dummy_run),
        "selection": selection_info,
        "knowledge_scope": knowledge_scope_info,
        "llm_judge": {
            "enabled": bool(self.config.llm_judge_enabled),
            "base_url": self.config.llm_judge_base_url,
            "model": self.config.llm_judge_model,
            "timeout_seconds": int(self.config.llm_judge_timeout_seconds),
            "max_retries": int(self.config.llm_judge_max_retries),
        },
        "incomplete": bool(paused),
        "pause_mode": str(pause_mode or "none"),
        "completed_task_count": completed_task_count,
        "pending_task_count": pending_task_count,
        "resumable_task_count": resumable_task_count,
        "success_count": success_count,
        "warning_count": warning_count,
        "error_count": error_count,
        "health_issue_summary": _build_health_issue_summary(results),
        "mode_summaries": summaries,
    }
    comparison = self._build_comparison(summaries)
    return summary, comparison


def _write_eval_artifact_audit(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    modes: list[str],
) -> dict[str, Any] | None:
    if not any(str(mode or "").strip().lower() == "flowark" for mode in modes):
        return None
    audit_path, audit_result = write_note_only_artifact_audit(eval_root)
    return compact_artifact_audit_result(audit_result, path=audit_path)


def _shorten(text: str | None, *, max_len: int = 96) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _format_task_source_meta(self: EvaluationHarness, task: EvalTask) -> str:
    case = task.case
    source_text = case.source_method or case.source_statement or case.source_classname or ""
    flow_label = case.source_id or case.flow_id
    return (
        f"case={flow_label} mode={task.mode} repeat={task.repeat_idx} "
        f"app={self._shorten(case.app_name, max_len=36)} "
        f"source={self._shorten(source_text, max_len=88)}"
    )


def _emit_progress_start(
    self: EvaluationHarness,
    *,
    task: EvalTask,
    started_count: int,
    total: int,
    active_count: int,
) -> None:
    print(
        f"[eval][START {started_count}/{total}][active={active_count}] "
        f"{self._format_task_source_meta(task)}",
        flush=True,
    )


def _emit_progress_finish(
    self: EvaluationHarness,
    *,
    task: EvalTask,
    result: dict[str, Any],
    finished_count: int,
    total: int,
    active_count: int,
) -> None:
    status = str(result.get("status") or "unknown")
    wall = _safe_float(result.get("wall_time_seconds"))
    metrics = (result.get("metrics") or {}) if isinstance(result, dict) else {}
    analysis = metrics.get("analysis") if isinstance(metrics, dict) and isinstance(metrics.get("analysis"), dict) else {}
    end_to_end = metrics.get("end_to_end") if isinstance(metrics, dict) and isinstance(metrics.get("end_to_end"), dict) else {}
    turns = _safe_int(analysis.get("react_turns")) or _safe_int(end_to_end.get("react_turns"))
    input_tokens = _safe_int(analysis.get("input_tokens")) or _safe_int(end_to_end.get("input_tokens"))
    duration_ms = _safe_int(analysis.get("duration_ms")) or _safe_int(end_to_end.get("duration_ms_sum"))
    cost = _safe_float(analysis.get("total_cost_usd")) or _safe_float(end_to_end.get("total_cost_usd_sum"))
    judge = result.get("llm_judge") if isinstance(result.get("llm_judge"), dict) else {}
    judge_status = str(judge.get("status") or "-")
    judge_verdict = str(judge.get("verdict") or "-")
    judge_correct = judge.get("is_correct")
    judge_text = f"{judge_status}/{judge_verdict}/{judge_correct}"
    msg = (
        f"[eval][DONE  {finished_count}/{total}][active={active_count}] "
        f"{self._format_task_source_meta(task)} "
        f"status={status} wall={wall if wall is not None else '-'}s "
        f"turns={turns if turns is not None else '-'} "
        f"in={input_tokens if input_tokens is not None else '-'} "
        f"dur_ms={duration_ms if duration_ms is not None else '-'} "
    )
    if cost is not None:
        msg += f"cost=${cost:.6f} "
    msg += f"judge={judge_text}"
    print(msg, flush=True)


def _dataset_slug(self: EvaluationHarness, cases: list[EvalCase]) -> str:
    datasets = sorted({(c.dataset or "unknown") for c in cases})
    if not datasets:
        return "unknown"
    if len(datasets) == 1:
        return _slugify(datasets[0], 32)
    return "mixed"


def _modes_slug(modes: list[str]) -> str:
    normalized: list[str] = []
    for mode in modes:
        text = str(mode or "").strip().lower()
        if text == "native":
            text = "naive"
        if text:
            normalized.append(text)
    return "+".join([_slugify(m, 16) for m in normalized])


def _normalized_mode_set(modes: list[str]) -> set[str]:
    normalized_modes = {str(item or "").strip().lower() for item in modes}
    if "native" in normalized_modes:
        normalized_modes.discard("native")
        normalized_modes.add("naive")
    return normalized_modes


def _modes_include_flowark(modes: list[str]) -> bool:
    return "flowark" in _normalized_mode_set(modes)


def _normalize_auto_knowledge_validate_mode(value: Any) -> str:
    mode = str(value or "static").strip().lower()
    if mode == "full":
        raise ValueError("auto_knowledge_validate_mode=full 已删除，请改用 static 或 off")
    if mode not in {"off", "static"}:
        raise ValueError("auto_knowledge_validate_mode 必须是 off 或 static")
    return mode


def _validation_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    validate_mode = _normalize_auto_knowledge_validate_mode(self.config.auto_knowledge_validate_mode)
    return f"v-{_slugify(validate_mode, 16)}"


def _distillation_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    distillation_mode = str(
        getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
        or "with_selection_rules"
    ).strip().lower()
    if distillation_mode == "generic":
        return "m1-gen"
    return "m1-sel"


def _packaging_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    packaging_mode = str(
        getattr(self.config, "knowledge_packaging_mode", "dsl_rule")
        or "dsl_rule"
    ).strip().lower()
    if packaging_mode == "embedding":
        return "m2-emb"
    if packaging_mode == "analysis_log_rag":
        return "rag-log"
    if packaging_mode == "analysis_log_rag_initial":
        return "rag-init"
    return "m2-dsl"


def _agent_adapter_slug(self: EvaluationHarness) -> str:
    return "a-oc"


def _knowledge_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    mode = str(getattr(self.config, "knowledge_mode", "warm") or "warm").strip().lower()
    if mode not in {"warm", "cold", "off"}:
        mode = "warm"
    return f"k-{mode}"


def _auto_knowledge_cycle_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    return "cy-on" if bool(getattr(self.config, "auto_knowledge_cycle", True)) else "cy-off"


def _runtime_injection_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    mode = str(
        getattr(self.config, "runtime_injection_mode", "context_aware")
        or "context_aware"
    ).strip().lower()
    if mode == "start_only":
        return "m3-start"
    return "m3-ctx"


def _reuse_digest_mode_slug(self: EvaluationHarness, modes: list[str]) -> str:
    if not _modes_include_flowark(modes):
        return ""
    digest_mode = str(self.config.knowledge_reuse_digest_mode or "off").strip().lower()
    if digest_mode == "live_corridor":
        return "d-v1"
    if digest_mode == "live_corridor_v2":
        return "d-v2"
    return "d-off"


def _prepare_eval_root(self: EvaluationHarness, cases: list[EvalCase], modes: list[str]) -> Path:
    root = self.config.out_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    mode_slugs = [
        self._agent_adapter_slug(),
        self._knowledge_mode_slug(modes),
        self._auto_knowledge_cycle_slug(modes),
        self._distillation_mode_slug(modes),
        self._packaging_mode_slug(modes),
        self._runtime_injection_mode_slug(modes),
        self._validation_mode_slug(modes),
        self._reuse_digest_mode_slug(modes),
    ]
    mode_part = "".join(f"-{slug}" for slug in mode_slugs if slug)
    name = (
        f"{_timestamp_slug()}-"
        f"{self._dataset_slug(cases)}-"
        f"{self._modes_slug(modes)}"
        f"{mode_part}-"
        f"p{int(self.config.parallel)}-r{int(self.config.repeats)}"
    )
    eval_root = root / name
    eval_root.mkdir(parents=True, exist_ok=True)
    return eval_root


def _build_tasks(
    self: EvaluationHarness,
    cases: list[EvalCase],
    modes: list[str],
    eval_root: Path,
) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    repeats = max(1, int(self.config.repeats))
    for case in cases:
        for i in range(1, repeats + 1):
            for mode in modes:
                case_dir = eval_root / mode / _slugify(case.flow_id, 80)
                repeat_dir = case_dir / f"repeat-{i:02d}"
                repeat_dir.mkdir(parents=True, exist_ok=True)
                tasks.append(EvalTask(case=case, mode=mode, repeat_idx=i, repeat_dir=repeat_dir))
    total = len(tasks)
    for idx, task in enumerate(tasks, start=1):
        task.task_index = idx
        task.task_total = total
    return tasks


def _task_entry_id(task: EvalTask) -> str:
    return f"task-{int(task.task_index)}"


def _write_planned_runs(self: EvaluationHarness, *, eval_root: Path, tasks: list[EvalTask]) -> Path:
    payload = {
        "created_at": _now_utc_iso(),
        "task_total": len(tasks),
        "runs": [
            {
                "entry_id": self._task_entry_id(task),
                "task_index": task.task_index,
                "task_total": task.task_total,
                "mode": task.mode,
                "repeat_idx": task.repeat_idx,
                "flow_id": task.case.flow_id,
                "source_id": task.case.source_id,
                "benchmark_family": task.case.benchmark_family,
                "app_name": task.case.app_name,
                "dataset": task.case.dataset,
                "query": build_eval_query(task.case),
                "source": build_source_description(task.case),
                "sink_types": effective_case_sink_categories(
                    task.case,
                    fallback=list(self.config.sink_categories or DEFAULT_SINK_CATEGORIES),
                ),
                "repeat_dir": str(task.repeat_dir),
                "status": "pending",
                "exec_state": "pending",
            }
            for task in tasks
        ],
    }
    path = eval_root / "planned_runs.json"
    _json_dump(path, payload)
    return path
def _extract_numeric(results: list[dict[str, Any]], path: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for item in results:
        cur: Any = item
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if not ok:
            continue
        fv = _safe_float(cur)
        if fv is not None:
            values.append(fv)
    return values


def _build_mode_summary(self: EvaluationHarness, mode: str, mode_results: list[dict[str, Any]]) -> dict[str, Any]:
    success_results = [r for r in mode_results if r.get("status") == "success"]
    warning_results = [r for r in mode_results if r.get("status") == "warning"]
    error_results = [r for r in mode_results if str(r.get("status") or "") in {"error", "harness_error"}]

    def metric_stats(path: tuple[str, ...]) -> dict[str, Any]:
        vals = self._extract_numeric(success_results, path)
        return {
            "count": len(vals),
            "mean": _mean(vals),
            "median": _median(vals),
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        }

    def mem0_metric_stats(path: tuple[str, ...], *, require_cost_tracked: bool = False) -> dict[str, Any]:
        filtered: list[dict[str, Any]] = []
        for result in success_results:
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            backend = metrics.get("mem0_backend") if isinstance(metrics.get("mem0_backend"), dict) else {}
            if require_cost_tracked:
                if backend.get("cost_tracked") is not True:
                    continue
            elif str(backend.get("status") or "").strip().lower() != "ok":
                continue
            filtered.append(result)
        vals = self._extract_numeric(filtered, path)
        return {
            "count": len(vals),
            "mean": _mean(vals),
            "median": _median(vals),
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        }

    def mem0_backend_coverage() -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        attempted_count = 0
        tracked_count = 0
        untracked_count = 0
        for result in success_results:
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            backend = metrics.get("mem0_backend") if isinstance(metrics.get("mem0_backend"), dict) else {}
            attempted = bool(backend)
            if attempted:
                attempted_count += 1
                status = str(backend.get("status") or "unknown").strip().lower() or "unknown"
                status_counts[status] = int(status_counts.get(status) or 0) + 1
            cost_tracked = backend.get("cost_tracked") is True and _safe_float(
                backend.get("total_cost_usd_sum")
            ) is not None
            if cost_tracked:
                tracked_count += 1
            elif attempted:
                untracked_count += 1
        success_count = len(success_results)
        return {
            "success_count": success_count,
            "attempted_count": attempted_count,
            "tracked_count": tracked_count,
            "untracked_count": untracked_count,
            "missing_count": max(0, success_count - attempted_count),
            "attempted_rate": (attempted_count / success_count) if success_count else None,
            "tracked_rate": (tracked_count / success_count) if success_count else None,
            "status_counts": status_counts,
        }

    judged = [
        r
        for r in success_results
        if isinstance(r.get("llm_judge"), dict)
        and str(r["llm_judge"].get("status") or "") == "ok"
    ]
    eligible_count = sum(
        1
        for r in mode_results
        if isinstance(r.get("llm_judge"), dict) and r["llm_judge"].get("eligible") is True
    )
    judge_failed_count = sum(
        1
        for r in mode_results
        if isinstance(r.get("llm_judge"), dict)
        and r["llm_judge"].get("eligible") is True
        and not r["llm_judge"].get("skipped")
        and str(r["llm_judge"].get("status") or "") != "ok"
    )
    judge_skipped_eligible_count = sum(
        1
        for r in mode_results
        if isinstance(r.get("llm_judge"), dict)
        and r["llm_judge"].get("eligible") is True
        and r["llm_judge"].get("skipped") is True
    )
    skipped_count = sum(
        1
        for r in mode_results
        if isinstance(r.get("llm_judge"), dict) and r["llm_judge"].get("skipped") is True
    )
    correct_count = sum(1 for r in judged if r["llm_judge"].get("is_correct") is True)
    incorrect_count = sum(1 for r in judged if r["llm_judge"].get("is_correct") is False)
    partial_count = sum(
        1
        for r in judged
        if str(r["llm_judge"].get("verdict") or "") == "partially_correct"
    )
    unknown_count = len(judged) - correct_count - incorrect_count

    return {
        "mode": mode,
        "task_count": len(mode_results),
        "success_count": len(success_results),
        "warning_count": len(warning_results),
        "error_count": len(error_results),
        "failure_count": len(mode_results) - len(success_results),
        "llm_judge_stats": {
            "enabled": bool(self.config.llm_judge_enabled),
            "eligible_count": eligible_count,
            "skipped_count": skipped_count,
            "failed_count": judge_failed_count,
            "skipped_eligible_count": judge_skipped_eligible_count,
            "evaluated_count": len(judged),
            "correct_count": correct_count,
            "correct_rate": (correct_count / len(judged)) if judged else None,
            "incorrect_count": incorrect_count,
            "partial_count": partial_count,
            "unknown_count": unknown_count,
        },
        "llm_judge_metrics": {
            "score": metric_stats(("llm_judge", "score")),
            "confidence": metric_stats(("llm_judge", "confidence")),
            "input_tokens": metric_stats(("llm_judge", "usage", "input_tokens")),
            "output_tokens": metric_stats(("llm_judge", "usage", "output_tokens")),
            "total_tokens": metric_stats(("llm_judge", "usage", "total_tokens")),
            "latency_ms": metric_stats(("llm_judge", "latency_ms")),
        },
        "analysis_metrics": {
            "react_turns": metric_stats(("metrics", "analysis", "react_turns")),
            "duration_ms": metric_stats(("metrics", "analysis", "duration_ms")),
            "total_cost_usd": metric_stats(("metrics", "analysis", "total_cost_usd")),
            "input_tokens": metric_stats(("metrics", "analysis", "input_tokens")),
            "output_tokens": metric_stats(("metrics", "analysis", "output_tokens")),
            "cache_read_input_tokens": metric_stats(("metrics", "analysis", "cache_read_input_tokens")),
            "tool_use_block_count": metric_stats(("metrics", "analysis", "tool_use_block_count")),
        },
        "end_to_end_metrics": {
            "react_turns": metric_stats(("metrics", "end_to_end", "react_turns")),
            "duration_ms_sum": metric_stats(("metrics", "end_to_end", "duration_ms_sum")),
            "total_cost_usd_sum": metric_stats(("metrics", "end_to_end", "total_cost_usd_sum")),
            "mem0_total_cost_usd_sum": metric_stats(("metrics", "end_to_end", "mem0_total_cost_usd_sum")),
            "total_with_mem0_cost_usd_sum": metric_stats(
                ("metrics", "end_to_end", "total_with_mem0_cost_usd_sum")
            ),
            "input_tokens": metric_stats(("metrics", "end_to_end", "input_tokens")),
            "output_tokens": metric_stats(("metrics", "end_to_end", "output_tokens")),
            "cache_read_input_tokens": metric_stats(("metrics", "end_to_end", "cache_read_input_tokens")),
            "mem0_llm_total_tokens": metric_stats(("metrics", "end_to_end", "mem0_llm_total_tokens")),
            "mem0_embedding_total_tokens": metric_stats(
                ("metrics", "end_to_end", "mem0_embedding_total_tokens")
            ),
            "mem0_total_tokens": metric_stats(("metrics", "end_to_end", "mem0_total_tokens")),
            "tool_use_block_count_total": metric_stats(("metrics", "end_to_end", "tool_use_block_count_total")),
        },
        "mem0_backend_metrics": {
            "total_cost_usd_sum": mem0_metric_stats(
                ("metrics", "mem0_backend", "total_cost_usd_sum"),
                require_cost_tracked=True,
            ),
            "record_count": mem0_metric_stats(("metrics", "mem0_backend", "record_count")),
            "input_tokens": mem0_metric_stats(("metrics", "mem0_backend", "input_tokens")),
            "output_tokens": mem0_metric_stats(("metrics", "mem0_backend", "output_tokens")),
            "llm_total_tokens": mem0_metric_stats(("metrics", "mem0_backend", "llm_total_tokens")),
            "embedding_total_tokens": mem0_metric_stats(("metrics", "mem0_backend", "embedding_total_tokens")),
            "total_tokens": mem0_metric_stats(("metrics", "mem0_backend", "total_tokens")),
            "estimated_token_records": mem0_metric_stats(("metrics", "mem0_backend", "estimated_token_records")),
            "missing_usage_records": mem0_metric_stats(("metrics", "mem0_backend", "missing_usage_records")),
        },
        "mem0_backend_coverage": mem0_backend_coverage(),
    }


def _metric_mean(summary: dict[str, Any], section: str, metric: str) -> float | None:
    try:
        return _safe_float(summary[section][metric]["mean"])
    except Exception:
        return None


def _mem0_coverage_complete(summary: dict[str, Any]) -> bool:
    coverage = summary.get("mem0_backend_coverage") if isinstance(summary.get("mem0_backend_coverage"), dict) else {}
    success_count = _safe_int(coverage.get("success_count")) or 0
    tracked_count = _safe_int(coverage.get("tracked_count")) or 0
    untracked_count = _safe_int(coverage.get("untracked_count")) or 0
    missing_count = _safe_int(coverage.get("missing_count")) or 0
    return success_count > 0 and tracked_count == success_count and untracked_count == 0 and missing_count == 0


def _is_mem0_comparison_metric(section: str, metric: str) -> bool:
    if section == "mem0_backend_metrics":
        return True
    if section != "end_to_end_metrics":
        return False
    return metric.startswith("mem0_") or metric.startswith("total_with_mem0")


def _build_comparison(self: EvaluationHarness, summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    modes = sorted(summaries.keys())
    comparisons: list[dict[str, Any]] = []
    if len(modes) < 2:
        return {"comparisons": comparisons}

    candidate_metrics = [
        ("analysis_metrics", "react_turns"),
        ("analysis_metrics", "duration_ms"),
        ("analysis_metrics", "total_cost_usd"),
        ("analysis_metrics", "input_tokens"),
        ("analysis_metrics", "cache_read_input_tokens"),
        ("end_to_end_metrics", "total_cost_usd_sum"),
        ("end_to_end_metrics", "mem0_total_cost_usd_sum"),
        ("end_to_end_metrics", "total_with_mem0_cost_usd_sum"),
        ("end_to_end_metrics", "input_tokens"),
        ("end_to_end_metrics", "cache_read_input_tokens"),
        ("end_to_end_metrics", "mem0_total_tokens"),
        ("mem0_backend_metrics", "total_cost_usd_sum"),
        ("mem0_backend_metrics", "total_tokens"),
        ("mem0_backend_metrics", "embedding_total_tokens"),
        ("llm_judge_metrics", "score"),
        ("llm_judge_metrics", "confidence"),
        ("llm_judge_metrics", "total_tokens"),
        ("llm_judge_metrics", "latency_ms"),
    ]
    for base in modes:
        for target in modes:
            if base == target:
                continue
            base_summary = summaries[base]
            target_summary = summaries[target]
            metrics: dict[str, Any] = {}
            for section, metric in candidate_metrics:
                if _is_mem0_comparison_metric(section, metric) and not (
                    _mem0_coverage_complete(base_summary) and _mem0_coverage_complete(target_summary)
                ):
                    continue
                b = self._metric_mean(base_summary, section, metric)
                t = self._metric_mean(target_summary, section, metric)
                if b is None or t is None:
                    continue
                delta = t - b
                ratio = (delta / b) if b not in {0.0, -0.0} else None
                metrics[f"{section}.{metric}"] = {
                    "base_mean": b,
                    "target_mean": t,
                    "delta": delta,
                    "delta_ratio": ratio,
                }
            comparisons.append(
                {
                    "base_mode": base,
                    "target_mode": target,
                    "metrics": metrics,
                }
            )
    return {"comparisons": comparisons}
