from __future__ import annotations

import json
import math
from decimal import Decimal
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
                record["_eval_root"] = str(results_path.parent.resolve())
                record["_expects_mem0"] = "mem0" in results_path.parent.name.lower()
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
        for key in (EVAL_PROGRESS_KEY, EVAL_OPEN_CODE_COST_KEY, EVAL_PROGRESS_RECORDS_KEY):
            metadata.pop(key, None)
        return
    metric_records = [item for item in clean_records.values() if not item.get("paper_metric_excluded")]
    count = len(metric_records)

    def amounts(key: str) -> list[float]:
        return [value for item in metric_records if (value := _finite_float(item.get(key))) is not None]

    def total_amount(values: list[float]) -> float:
        return float(sum((Decimal(str(value)) for value in values), Decimal(0)))

    main_costs = amounts("opencode_cost")
    display_costs = amounts("cost")
    mem0_costs = amounts("mem0_backend_cost")
    combined_costs = amounts("total_with_mem0_cost")
    attempted = sum(item.get("mem0_metering_attempted") is True for item in metric_records)
    tracked = sum(item.get("mem0_cost_tracked") is True for item in metric_records)
    missing = count - len(display_costs)
    cost_payload: dict[str, Any] = {
        "completed_end_to_end_cost_usd": total_amount(main_costs) if len(main_costs) == count else None,
        "completed_display_cost_usd": total_amount(display_costs) if not missing else None,
        "tracked_end_to_end_cost_usd": total_amount(main_costs),
        "tracked_display_cost_usd": total_amount(display_costs),
        "cost_record_count": count,
        "cost_tracked_count": len(display_costs),
        "cost_missing_count": missing,
        "cost_complete": missing == 0,
        "mem0_backend_attempted_count": attempted,
        "mem0_backend_cost_tracked_count": tracked,
        "mem0_backend_cost_untracked_count": attempted - tracked,
        "mem0_backend_missing_count": max(0, count - attempted) if attempted else 0,
    }
    if mem0_costs:
        cost_payload["completed_tracked_mem0_backend_cost_usd"] = total_amount(mem0_costs)
    if attempted and tracked == count and len(combined_costs) == count:
        cost_payload["completed_mem0_backend_cost_usd"] = total_amount(mem0_costs)
        cost_payload["completed_total_with_mem0_cost_usd"] = total_amount(combined_costs)
    for field in ("input_tokens", "output_tokens"):
        values = [item[field] for item in metric_records if item.get(field) is not None]
        cost_payload["completed_" + field] = sum(values) if len(values) == count else None
    if completed_count > count:
        cost_payload["paper_metric_excluded_count"] = completed_count - count
    metadata[EVAL_PROGRESS_KEY] = {"completed_count": completed_count, "total_count": total}
    metadata[EVAL_OPEN_CODE_COST_KEY] = cost_payload
    if clean_records:
        metadata[EVAL_PROGRESS_RECORDS_KEY] = clean_records
    else:
        metadata.pop(EVAL_PROGRESS_RECORDS_KEY, None)


def _event_key(event: dict[str, Any]) -> str:
    task_index = _positive_int(event.get("task_index"))
    if task_index is not None:
        return f"task:{task_index}"
    entry_id = str(event.get("entry_id") or "").strip()
    if entry_id:
        return f"entry:{entry_id}"
    for field in ("repeat_dir", "run_dir"):
        value = str(event.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return ""


def _completed_record_for_event(event: dict[str, Any], *, status: str) -> dict[str, Any]:
    excluded = event.get("paper_included") is False
    costs = _eval_event_costs(event) if not excluded else {}
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    e2e = metrics.get("end_to_end") if isinstance(metrics.get("end_to_end"), dict) else {}
    input_tokens = _nonnegative_int(e2e.get("input_tokens"))
    if input_tokens is not None:
        input_tokens += sum(_nonnegative_int(e2e.get(key), default=0) or 0 for key in
                            ("cache_read_input_tokens", "cache_creation_input_tokens"))
    return {
        "status": status,
        "cost": costs.get("display_cost"),
        "opencode_cost": costs.get("opencode_cost"),
        "mem0_backend_cost": costs.get("mem0_backend_cost"),
        "total_with_mem0_cost": costs.get("total_with_mem0_cost"),
        "mem0_cost_tracked": costs.get("mem0_cost_tracked", False),
        "mem0_metering_attempted": costs.get("mem0_metering_attempted", False),
        "mem0_metering_status": costs.get("mem0_metering_status"),
        "paper_metric_excluded": excluded,
        "input_tokens": input_tokens,
        "output_tokens": _nonnegative_int(e2e.get("output_tokens")),
    }


def _eval_event_costs(event: dict[str, Any]) -> dict[str, Any]:
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    e2e = metrics.get("end_to_end") if isinstance(metrics.get("end_to_end"), dict) else {}
    backend = metrics.get("mem0_backend") if isinstance(metrics.get("mem0_backend"), dict) else {}
    main_cost = _finite_float(e2e.get("total_cost_usd_sum"))
    mem0_cost = _finite_float(e2e.get("mem0_total_cost_usd_sum"))
    if mem0_cost is None:
        mem0_cost = _finite_float(backend.get("total_cost_usd"))
    combined = _finite_float(e2e.get("total_with_mem0_cost_usd_sum"))
    attempted = bool(backend) or mem0_cost is not None or combined is not None or event.get("_expects_mem0") is True
    tracked = mem0_cost is not None and (backend.get("cost_tracked") is not False)
    artifact: dict[str, Any] = {}
    if main_cost is None or (attempted and mem0_cost is None):
        artifact = _eval_event_artifact_costs(event) or {}
        if main_cost is None:
            main_cost = _finite_float(artifact.get("opencode_cost"))
        if mem0_cost is None:
            mem0_cost = _finite_float(artifact.get("mem0_backend_cost"))
            tracked = artifact.get("mem0_cost_tracked") is True and mem0_cost is not None
        attempted = attempted or artifact.get("mem0_metering_attempted") is True
    if attempted:
        combined = main_cost + mem0_cost if main_cost is not None and tracked and mem0_cost is not None else None
        display = combined
    else:
        combined = None
        display = main_cost
    return {
        "display_cost": display,
        "opencode_cost": main_cost,
        "mem0_backend_cost": mem0_cost if tracked else None,
        "total_with_mem0_cost": combined,
        "mem0_cost_tracked": tracked,
        "mem0_metering_attempted": attempted,
        "mem0_metering_status": backend.get("status") or artifact.get("mem0_metering_status"),
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


def _event_path(event: dict[str, Any], value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    base_value = event.get("_eval_root")
    if base_value:
        base = Path(str(base_value)).resolve()
        path = (base / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(base):
            return None
    elif not path.is_absolute():
        return None
    return path


def _candidate_cost_summary_paths(event: dict[str, Any]) -> list[Path]:
    paths = event.get("paths") if isinstance(event.get("paths"), dict) else {}
    direct = _event_path(event, paths.get("cost_summary_json"))
    run = _event_path(event, event.get("run_dir"))
    candidates = [p for p in (direct, run / "cost_summary.json" if run else None) if p is not None]
    return list(dict.fromkeys(candidates))


def _candidate_mem0_usage_summary_paths(event: dict[str, Any]) -> list[Path]:
    paths = event.get("paths") if isinstance(event.get("paths"), dict) else {}
    direct = _event_path(event, paths.get("mem0_usage_summary_json"))
    run = _event_path(event, event.get("run_dir"))
    candidates = [p for p in (direct, run / "mem0_usage_summary.json" if run else None) if p is not None]
    return list(dict.fromkeys(candidates))


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
    for raw_key, raw in value.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw, dict):
            continue
        records[key] = {
            "status": str(raw.get("status") or "").strip().lower(),
            **{field: _finite_float(raw.get(field)) for field in
               ("cost", "opencode_cost", "mem0_backend_cost", "total_with_mem0_cost")},
            "mem0_cost_tracked": raw.get("mem0_cost_tracked") is True,
            "mem0_metering_attempted": raw.get("mem0_metering_attempted") is True,
            "mem0_metering_status": raw.get("mem0_metering_status"),
            "paper_metric_excluded": raw.get("paper_metric_excluded") is True,
            "input_tokens": _nonnegative_int(raw.get("input_tokens")),
            "output_tokens": _nonnegative_int(raw.get("output_tokens")),
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
    return parsed if math.isfinite(parsed) and parsed >= 0 else None
