from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

EVAL_COMPLETED_STATUSES = {
    "success",
    "warning",
    "error",
    "timeout",
    "cancelled",
    "skipped",
    "harness_error",
}

EVAL_PROGRESS_KEY = "eval_progress"
EVAL_OPEN_CODE_COST_KEY = "eval_open_code_cost"
EVAL_PROGRESS_RECORDS_KEY = "_eval_progress_completed_records"


def update_eval_progress_metadata_from_event(metadata: dict[str, Any], event: dict[str, Any]) -> bool:
    event_name = str(event.get("event") or "").strip().lower()
    records = _normalize_records(metadata.get(EVAL_PROGRESS_RECORDS_KEY))
    if event_name != "finish":
        if event_name == "start":
            key = _event_key(event)
            if key:
                records.pop(key, None)
        total_count = _max_total_count(metadata.get(EVAL_PROGRESS_KEY), event)
        if total_count <= 0:
            return False
        _write_eval_progress_metadata(metadata, records, total_count=total_count)
        return True

    key = _event_key(event)
    if not key:
        return False

    status = str(event.get("status") or "").strip().lower()
    if status in EVAL_COMPLETED_STATUSES:
        records[key] = _completed_record_for_event(event, status=status)
    else:
        records.pop(key, None)

    total_count = _max_total_count(metadata.get(EVAL_PROGRESS_KEY), event)
    _write_eval_progress_metadata(metadata, records, total_count=total_count)
    return True


def rebuild_eval_progress_metadata_from_progress_file(metadata: dict[str, Any], progress_path: Path) -> None:
    records: dict[str, dict[str, Any]] = {}
    total_count = 0
    if not progress_path.exists() or not progress_path.is_file():
        _write_eval_progress_metadata(metadata, records, total_count=total_count)
        return

    try:
        handle = progress_path.open("r", encoding="utf-8")
    except OSError:
        _write_eval_progress_metadata(metadata, records, total_count=total_count)
        return

    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            total_count = max(total_count, _positive_int(event.get("task_total")) or 0)
            event_name = str(event.get("event") or "").strip().lower()
            if event_name == "start":
                key = _event_key(event)
                if key:
                    records.pop(key, None)
                continue
            if event_name != "finish":
                continue
            key = _event_key(event)
            if not key:
                continue
            status = str(event.get("status") or "").strip().lower()
            if status in EVAL_COMPLETED_STATUSES:
                records[key] = _completed_record_for_event(event, status=status)
            else:
                records.pop(key, None)

    _write_eval_progress_metadata(metadata, records, total_count=total_count)


def rebuild_eval_progress_metadata_from_results_file(
    metadata: dict[str, Any],
    results_path: Path,
    *,
    total_count: int = 0,
) -> None:
    records: dict[str, dict[str, Any]] = {}
    inferred_total_count = max(0, int(total_count or 0))
    if not results_path.exists() or not results_path.is_file():
        _write_eval_progress_metadata(metadata, records, total_count=inferred_total_count)
        return

    try:
        handle = results_path.open("r", encoding="utf-8")
    except OSError:
        _write_eval_progress_metadata(metadata, records, total_count=inferred_total_count)
        return

    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            inferred_total_count = max(inferred_total_count, _positive_int(record.get("task_total")) or 0)
            key = _event_key(record)
            if not key:
                continue
            status = str(record.get("status") or "").strip().lower()
            if status in EVAL_COMPLETED_STATUSES:
                records[key] = _completed_record_for_event(record, status=status)
            else:
                records.pop(key, None)

    _write_eval_progress_metadata(metadata, records, total_count=inferred_total_count)


def _write_eval_progress_metadata(
    metadata: dict[str, Any],
    records: dict[str, dict[str, Any]],
    *,
    total_count: int,
) -> None:
    clean_records = _normalize_records(records)
    completed_count = len(clean_records)
    total = max(int(total_count or 0), completed_count)
    if total <= 0 and completed_count <= 0:
        metadata.pop(EVAL_PROGRESS_KEY, None)
        metadata.pop(EVAL_OPEN_CODE_COST_KEY, None)
        metadata.pop(EVAL_PROGRESS_RECORDS_KEY, None)
        return

    metric_records = {
        key: item
        for key, item in clean_records.items()
        if item.get("paper_metric_excluded") is not True
    }
    metric_record_count = len(metric_records)
    opencode_cost = round(sum(float(item.get("opencode_cost") or 0.0) for item in metric_records.values()), 12)
    tracked_mem0_costs = [
        float(item.get("mem0_backend_cost"))
        for item in metric_records.values()
        if item.get("mem0_cost_tracked") is True and _finite_float(item.get("mem0_backend_cost")) is not None
    ]
    mem0_cost = round(sum(tracked_mem0_costs), 12)
    comparable_total_with_mem0_costs = [
        float(item.get("total_with_mem0_cost"))
        for item in metric_records.values()
        if _finite_float(item.get("total_with_mem0_cost")) is not None
    ]
    total_with_mem0_cost = round(
        sum(comparable_total_with_mem0_costs),
        12,
    )
    mem0_tracked_count = sum(1 for item in metric_records.values() if item.get("mem0_cost_tracked") is True)
    mem0_untracked_count = sum(
        1
        for item in metric_records.values()
        if item.get("mem0_metering_attempted") is True and item.get("mem0_cost_tracked") is not True
    )
    mem0_attempted_count = mem0_tracked_count + mem0_untracked_count
    mem0_missing_count = max(0, metric_record_count - mem0_attempted_count)
    paper_metric_excluded_count = completed_count - metric_record_count
    metadata[EVAL_PROGRESS_KEY] = {
        "completed_count": completed_count,
        "total_count": total,
    }
    cost_payload: dict[str, Any] = {
        "completed_end_to_end_cost_usd": opencode_cost,
        "completed_display_cost_usd": opencode_cost,
        "mem0_backend_attempted_count": mem0_attempted_count,
        "mem0_backend_cost_tracked_count": mem0_tracked_count,
        "mem0_backend_cost_untracked_count": mem0_untracked_count,
        "mem0_backend_missing_count": mem0_missing_count,
    }
    if mem0_tracked_count > 0:
        cost_payload["completed_tracked_mem0_backend_cost_usd"] = mem0_cost
    if paper_metric_excluded_count > 0:
        cost_payload["paper_metric_excluded_count"] = paper_metric_excluded_count
    all_records_have_comparable_total = len(comparable_total_with_mem0_costs) == metric_record_count
    if (
        mem0_tracked_count > 0
        and mem0_untracked_count == 0
        and mem0_missing_count == 0
        and all_records_have_comparable_total
    ):
        cost_payload["completed_mem0_backend_cost_usd"] = mem0_cost
        cost_payload["completed_total_with_mem0_cost_usd"] = total_with_mem0_cost
        cost_payload["completed_display_cost_usd"] = total_with_mem0_cost
    metadata[EVAL_OPEN_CODE_COST_KEY] = cost_payload
    if clean_records:
        metadata[EVAL_PROGRESS_RECORDS_KEY] = clean_records
    else:
        metadata.pop(EVAL_PROGRESS_RECORDS_KEY, None)


def _event_key(event: dict[str, Any]) -> str:
    entry_id = str(event.get("entry_id") or "").strip()
    if entry_id:
        return f"entry:{entry_id}"
    task_index = _positive_int(event.get("task_index"))
    if task_index is not None:
        return f"task:{task_index}"
    for field in ("repeat_dir", "run_dir"):
        value = str(event.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return ""


def _completed_record_for_event(event: dict[str, Any], *, status: str) -> dict[str, Any]:
    if event.get("public_merge_excluded_from_paper_metrics") is True:
        return {
            "status": status,
            "cost": 0.0,
            "opencode_cost": 0.0,
            "mem0_backend_cost": None,
            "total_with_mem0_cost": None,
            "mem0_cost_tracked": False,
            "mem0_metering_attempted": False,
            "mem0_metering_status": None,
            "paper_metric_excluded": True,
        }
    cost = _eval_event_costs(event)
    return {
        "status": status,
        "cost": cost["display_cost"],
        "opencode_cost": cost["opencode_cost"],
        "mem0_backend_cost": cost["mem0_backend_cost"],
        "total_with_mem0_cost": cost["total_with_mem0_cost"],
        "mem0_cost_tracked": cost["mem0_cost_tracked"],
        "mem0_metering_attempted": cost["mem0_metering_attempted"],
        "mem0_metering_status": cost["mem0_metering_status"],
        "paper_metric_excluded": False,
    }


def _eval_event_costs(event: dict[str, Any]) -> dict[str, Any]:
    metrics = event.get("metrics")
    end_to_end = metrics.get("end_to_end") if isinstance(metrics, dict) else None
    metric_mem0_backend = metrics.get("mem0_backend") if isinstance(metrics, dict) else None
    metric_mem0_backend = metric_mem0_backend if isinstance(metric_mem0_backend, dict) else {}
    artifact_costs = _eval_event_artifact_costs(event)
    artifact_costs = artifact_costs or {}
    if not isinstance(end_to_end, dict):
        if artifact_costs:
            raw_opencode_cost = _finite_float(artifact_costs.get("opencode_cost"))
            opencode_cost = raw_opencode_cost if raw_opencode_cost is not None else 0.0
            mem0_cost_raw = artifact_costs.get("mem0_backend_cost")
            mem0_cost = _finite_float(mem0_cost_raw)
            mem0_cost_tracked = artifact_costs.get("mem0_cost_tracked") is True
            mem0_attempted = artifact_costs.get("mem0_metering_attempted") is True
            total_with_mem0 = artifact_costs.get("total_with_mem0_cost")
            if total_with_mem0 is None and mem0_cost_tracked and raw_opencode_cost is not None:
                mem0_cost_value = mem0_cost if mem0_cost is not None else 0.0
                total_with_mem0 = opencode_cost + mem0_cost_value if mem0_cost_value > 0 else opencode_cost
            display_cost = total_with_mem0 if mem0_cost_tracked and total_with_mem0 is not None else opencode_cost
            return {
                "display_cost": display_cost,
                "opencode_cost": opencode_cost,
                "mem0_backend_cost": mem0_cost,
                "total_with_mem0_cost": total_with_mem0,
                "mem0_cost_tracked": mem0_cost_tracked,
                "mem0_metering_attempted": mem0_attempted,
                "mem0_metering_status": artifact_costs.get("mem0_metering_status"),
            }
        return {
            "display_cost": 0.0,
            "opencode_cost": 0.0,
            "mem0_backend_cost": None,
            "total_with_mem0_cost": None,
            "mem0_cost_tracked": False,
            "mem0_metering_attempted": False,
            "mem0_metering_status": None,
        }
    metric_opencode = metrics.get("opencode") if isinstance(metrics, dict) else None
    metric_opencode = metric_opencode if isinstance(metric_opencode, dict) else {}
    raw_opencode_cost = _finite_float(end_to_end.get("total_cost_usd_sum"))
    if raw_opencode_cost is None:
        raw_opencode_cost = _finite_float(metric_opencode.get("total_cost_usd"))
    if raw_opencode_cost is None:
        raw_opencode_cost = _finite_float(artifact_costs.get("opencode_cost")) if artifact_costs else None
    opencode_cost = raw_opencode_cost if raw_opencode_cost is not None else 0.0
    mem0_cost = _finite_float(end_to_end.get("mem0_total_cost_usd_sum"))
    if mem0_cost is None:
        mem0_cost = artifact_costs.get("mem0_backend_cost") if artifact_costs else None
    total_with_mem0 = _finite_float(end_to_end.get("total_with_mem0_cost_usd_sum"))
    if total_with_mem0 is None:
        total_with_mem0 = artifact_costs.get("total_with_mem0_cost") if artifact_costs else None
    mem0_status = (
        str(metric_mem0_backend.get("status") or artifact_costs.get("mem0_metering_status") or "").strip()
        or None
    )
    metric_cost_tracked = metric_mem0_backend.get("cost_tracked") is True
    artifact_cost_tracked = artifact_costs.get("mem0_cost_tracked") is True
    has_mem0_total = _finite_float(end_to_end.get("mem0_total_cost_usd_sum")) is not None
    has_total_with_mem0 = _finite_float(end_to_end.get("total_with_mem0_cost_usd_sum")) is not None
    mem0_cost_tracked = (
        (metric_cost_tracked or artifact_cost_tracked or (has_mem0_total and has_total_with_mem0))
        and mem0_cost is not None
    )
    mem0_attempted = (
        bool(metric_mem0_backend)
        or artifact_costs.get("mem0_metering_attempted") is True
        or has_mem0_total
        or has_total_with_mem0
    )
    mem0_cost_value = mem0_cost if mem0_cost is not None else 0.0
    if total_with_mem0 is None and mem0_cost_tracked and raw_opencode_cost is not None:
        total_with_mem0 = opencode_cost + mem0_cost_value if mem0_cost_value > 0 else opencode_cost
    display_cost = total_with_mem0 if mem0_cost_tracked and total_with_mem0 is not None else opencode_cost
    return {
        "display_cost": display_cost,
        "opencode_cost": opencode_cost,
        "mem0_backend_cost": mem0_cost if mem0_cost_tracked else None,
        "total_with_mem0_cost": total_with_mem0,
        "mem0_cost_tracked": mem0_cost_tracked,
        "mem0_metering_attempted": mem0_attempted,
        "mem0_metering_status": mem0_status,
    }


def _eval_event_artifact_costs(event: dict[str, Any]) -> dict[str, Any] | None:
    for cost_summary_path in _candidate_cost_summary_paths(event):
        payload = _read_json_dict(cost_summary_path)
        aggregated = payload.get("aggregated_metrics") if isinstance(payload, dict) else None
        if not isinstance(aggregated, dict):
            continue
        main_agent = aggregated.get("main_agent") if isinstance(aggregated.get("main_agent"), dict) else {}
        mem0_backend = aggregated.get("mem0_backend") if isinstance(aggregated.get("mem0_backend"), dict) else {}
        total_with_mem0 = (
            aggregated.get("total_with_mem0")
            if isinstance(aggregated.get("total_with_mem0"), dict)
            else {}
        )
        opencode_cost = _finite_float(main_agent.get("total_cost_usd_sum"))
        mem0_cost = _finite_float(mem0_backend.get("total_cost_usd_sum"))
        total_with_cost = _finite_float(total_with_mem0.get("total_cost_usd_sum"))
        mem0_status = str(mem0_backend.get("status") or "").strip() or None
        mem0_attempted = bool(mem0_backend)
        request_only_zero_cost = _is_request_only_zero_cost_mem0_summary(mem0_backend)
        if not request_only_zero_cost:
            request_only_zero_cost = _is_request_only_zero_cost_mem0_summary(
                _read_json_dict(cost_summary_path.with_name("mem0_usage_summary.json"))
            )
        mem0_cost_tracked = (
            (mem0_backend.get("cost_tracked") is True or request_only_zero_cost)
            and mem0_cost is not None
        )
        if total_with_cost is None and mem0_cost_tracked and opencode_cost is not None:
            total_with_cost = opencode_cost + mem0_cost
        if opencode_cost is None and mem0_cost is None and total_with_cost is None and not mem0_attempted:
            continue
        return {
            "opencode_cost": opencode_cost,
            "mem0_backend_cost": mem0_cost,
            "total_with_mem0_cost": total_with_cost if total_with_cost is not None else None,
            "mem0_cost_tracked": mem0_cost_tracked,
            "mem0_metering_attempted": mem0_attempted,
            "mem0_metering_status": mem0_status,
        }
    for mem0_summary_path in _candidate_mem0_usage_summary_paths(event):
        payload = _read_json_dict(mem0_summary_path)
        if not payload:
            continue
        mem0_status = str(payload.get("status") or "").strip() or None
        mem0_cost = _finite_float(payload.get("total_cost_usd"))
        mem0_cost_tracked = (
            str(mem0_status or "").lower() == "ok"
            and (
                payload.get("cost_tracked") is not False
                or _is_request_only_zero_cost_mem0_summary(payload)
            )
            and mem0_cost is not None
        )
        return {
            "opencode_cost": None,
            "mem0_backend_cost": mem0_cost,
            "total_with_mem0_cost": None,
            "mem0_cost_tracked": mem0_cost_tracked,
            "mem0_metering_attempted": True,
            "mem0_metering_status": mem0_status,
        }
    return None


def _is_request_only_zero_cost_mem0_summary(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status and status != "ok":
        return False
    total_cost = _finite_float(payload.get("total_cost_usd"))
    if total_cost is None:
        total_cost = _finite_float(payload.get("total_cost_usd_sum"))
    if total_cost != 0.0:
        return False
    record_count = _positive_int(
        payload.get("record_count")
        or payload.get("request_count")
        or payload.get("result_count")
    )
    if record_count is None:
        return False
    if _nonnegative_int(payload.get("metered_record_count"), default=0) != 0:
        return False
    if _nonnegative_int(payload.get("estimated_token_records"), default=0) != 0:
        return False
    if _nonnegative_int(payload.get("missing_usage_records"), default=0) != 0:
        return False

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    token_fields = (
        payload.get("llm_prompt_tokens"),
        payload.get("llm_completion_tokens"),
        payload.get("llm_total_tokens"),
        payload.get("embedding_total_tokens"),
        payload.get("total_tokens"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("llm_total_tokens"),
        usage.get("embedding_tokens"),
        usage.get("total_tokens"),
    )
    if any(_nonnegative_int(value, default=0) > 0 for value in token_fields):
        return False
    if payload.get("request_only_zero_cost") is True:
        return True

    records_by_operation = payload.get("records_by_operation")
    if isinstance(records_by_operation, dict) and records_by_operation:
        request_record_count = 0
        for key, value in records_by_operation.items():
            if not str(key).startswith("request."):
                return False
            if isinstance(value, dict):
                request_record_count += _nonnegative_int(value.get("record_count"), default=0)
        return request_record_count == record_count

    by_kind = payload.get("by_kind") if isinstance(payload.get("by_kind"), dict) else {}
    request_kind = by_kind.get("request") if isinstance(by_kind.get("request"), dict) else {}
    return _nonnegative_int(request_kind.get("record_count"), default=0) == record_count


def _candidate_cost_summary_paths(event: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []

    def add_run_dir(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            candidates.append(Path(text).expanduser() / "cost_summary.json")

    add_run_dir(event.get("run_dir"))

    repeat_dir_text = str(event.get("repeat_dir") or "").strip()
    if repeat_dir_text:
        repeat_dir = Path(repeat_dir_text).expanduser()
        result_payload = _read_json_dict(repeat_dir / "result.json")
        add_run_dir(result_payload.get("run_dir"))
        runs_dir = repeat_dir / "runs"
        try:
            run_dirs = sorted(
                [path for path in runs_dir.iterdir() if path.is_dir()],
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
        except OSError:
            run_dirs = []
        for run_dir in run_dirs:
            candidates.append(run_dir / "cost_summary.json")

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _candidate_mem0_usage_summary_paths(event: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []

    def add_run_dir(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            candidates.append(Path(text).expanduser() / "mem0_usage_summary.json")

    add_run_dir(event.get("run_dir"))

    repeat_dir_text = str(event.get("repeat_dir") or "").strip()
    if repeat_dir_text:
        repeat_dir = Path(repeat_dir_text).expanduser()
        result_payload = _read_json_dict(repeat_dir / "result.json")
        add_run_dir(result_payload.get("run_dir"))
        runs_dir = repeat_dir / "runs"
        try:
            run_dirs = sorted(
                [path for path in runs_dir.iterdir() if path.is_dir()],
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
        except OSError:
            run_dirs = []
        for run_dir in run_dirs:
            candidates.append(run_dir / "mem0_usage_summary.json")

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _max_total_count(progress: Any, event: dict[str, Any]) -> int:
    values = [_positive_int(event.get("task_total")) or 0]
    if isinstance(progress, dict):
        values.append(_positive_int(progress.get("total_count")) or 0)
    return max(values)


def _normalize_records(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for raw_key, raw_record in value.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_record, dict):
            continue
        cost = _finite_float(raw_record.get("cost"))
        if cost is None:
            cost = 0.0
        opencode_cost = _finite_float(raw_record.get("opencode_cost"))
        if opencode_cost is None:
            opencode_cost = cost
        mem0_backend_cost = _finite_float(raw_record.get("mem0_backend_cost"))
        total_with_mem0_cost = _finite_float(raw_record.get("total_with_mem0_cost"))
        mem0_cost_tracked = raw_record.get("mem0_cost_tracked") is True or (
            mem0_backend_cost is not None and total_with_mem0_cost is not None
        )
        mem0_metering_status = str(raw_record.get("mem0_metering_status") or "").strip()
        mem0_metering_attempted = (
            raw_record.get("mem0_metering_attempted") is True
            or mem0_cost_tracked
            or bool(mem0_metering_status)
        )
        status = str(raw_record.get("status") or "").strip().lower()
        records[key] = {
            "status": status,
            "cost": cost,
            "opencode_cost": opencode_cost,
            "mem0_backend_cost": mem0_backend_cost,
            "total_with_mem0_cost": total_with_mem0_cost,
            "mem0_cost_tracked": mem0_cost_tracked,
            "mem0_metering_attempted": mem0_metering_attempted,
            "mem0_metering_status": mem0_metering_status,
            "paper_metric_excluded": raw_record.get("paper_metric_excluded") is True,
        }
    return records


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any, *, default: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None
