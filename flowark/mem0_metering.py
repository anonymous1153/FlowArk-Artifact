"""Mem0 backend usage collection for the OpenCode + Mem0 baseline."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MEM0_RUNTIME_SNAPSHOT = "mem0_runtime_snapshot.json"
MEM0_USAGE_SUMMARY = "mem0_usage_summary.json"
DEFAULT_USAGE_RETRY_ATTEMPTS = 5
DEFAULT_USAGE_RETRY_BACKOFF_SECONDS = 2.0
MAX_USAGE_RETRY_ATTEMPTS = 10
MAX_USAGE_RETRY_BACKOFF_SECONDS = 30.0
RETRYABLE_USAGE_HTTP_STATUS = {429, 500, 502, 503, 504}


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return None
            if not math.isfinite(parsed):
                return None
            return int(parsed)
    return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _first_int(*values: Any) -> int:
    for value in values:
        parsed = _safe_int(value)
        if parsed is not None:
            return parsed
    return 0


def _first_float(*values: Any) -> float:
    parsed = _first_float_or_none(*values)
    return parsed if parsed is not None else 0.0


def _first_float_or_none(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _host_from_snapshot(snapshot: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    return str(
        snapshot.get("self_host_url")
        or environ.get("MEM0_SELF_HOST_URL")
        or environ.get("MEM0_HOST")
        or ""
    ).strip().rstrip("/")


def _metering_run_id(snapshot: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    return str(
        snapshot.get("metering_run_id")
        or environ.get("FLOWARK_MEM0_METERING_RUN_ID")
        or ""
    ).strip()


def _env_int(
    environ: Mapping[str, str],
    *keys: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    for key in keys:
        raw = str(environ.get(key) or "").strip()
        if not raw:
            continue
        parsed = _safe_int(raw)
        if parsed is not None:
            return max(minimum, min(maximum, parsed))
    return default


def _env_float(
    environ: Mapping[str, str],
    *keys: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    for key in keys:
        raw = str(environ.get(key) or "").strip()
        if not raw:
            continue
        parsed = _safe_float(raw)
        if parsed is not None:
            return max(minimum, min(maximum, parsed))
    return default


def _usage_retry_attempts(environ: Mapping[str, str]) -> int:
    return _env_int(
        environ,
        "MEM0_USAGE_RETRY_ATTEMPTS",
        "MEM0_SELF_HOST_RETRY_ATTEMPTS",
        "MEM0_HTTP_RETRY_ATTEMPTS",
        default=DEFAULT_USAGE_RETRY_ATTEMPTS,
        minimum=1,
        maximum=MAX_USAGE_RETRY_ATTEMPTS,
    )


def _usage_retry_backoff_seconds(environ: Mapping[str, str]) -> float:
    seconds = _env_float(
        environ,
        "MEM0_USAGE_RETRY_BACKOFF_SECONDS",
        "MEM0_SELF_HOST_RETRY_BACKOFF_SECONDS",
        "MEM0_HTTP_RETRY_BACKOFF_SECONDS",
        default=-1.0,
        minimum=0.0,
        maximum=MAX_USAGE_RETRY_BACKOFF_SECONDS,
    )
    if seconds >= 0:
        return seconds
    milliseconds = _env_float(
        environ,
        "MEM0_USAGE_RETRY_BACKOFF_MS",
        "MEM0_SELF_HOST_RETRY_BACKOFF_MS",
        "MEM0_HTTP_RETRY_BACKOFF_MS",
        default=DEFAULT_USAGE_RETRY_BACKOFF_SECONDS * 1000.0,
        minimum=0.0,
        maximum=MAX_USAGE_RETRY_BACKOFF_SECONDS * 1000.0,
    )
    return milliseconds / 1000.0


def _retry_detail(exc: BaseException, attempt: int, attempts: int) -> str:
    return f"{exc} (attempt {attempt}/{attempts})"


def _status_summary(
    *,
    snapshot: Mapping[str, Any],
    status: str,
    reason: str,
    detail: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "metering_run_id": snapshot.get("metering_run_id"),
        "eval_id": snapshot.get("eval_id"),
        "case_id": snapshot.get("case_id"),
        "app_id": snapshot.get("flowark_app_id") or snapshot.get("app_id") or snapshot.get("mem0_app_id"),
        "record_count": 0,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "embedding_total_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": None,
        "cost_tracked": False,
        "estimated_token_records": 0,
        "missing_usage_records": 0,
        "records_by_operation": {},
    }
    if detail:
        payload["detail"] = detail[:1000]
    return payload


def _response_metering_run_ids(payload: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in (payload.get("metering_run_id"), _dict(payload.get("summary")).get("metering_run_id")):
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def normalize_mem0_usage_summary(
    payload: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any] | None = None,
    metering_run_id: str | None = None,
) -> dict[str, Any]:
    """Normalize the self-host Mem0 usage summary into FlowArk's artifact schema."""

    raw = dict(payload)
    summary = _dict(raw.get("summary")) or raw
    usage = _dict(summary.get("usage"))
    by_kind = _dict(summary.get("by_kind"))
    llm_kind = _dict(by_kind.get("llm_chat")) or _dict(by_kind.get("llm"))
    embedding_kind = _dict(by_kind.get("embedding")) or _dict(by_kind.get("embedder"))

    llm_prompt = _first_int(
        summary.get("llm_prompt_tokens"),
        summary.get("llm_input_tokens"),
        llm_kind.get("prompt_tokens"),
        llm_kind.get("input_tokens"),
        usage.get("llm_prompt_tokens"),
        usage.get("input_tokens"),
    )
    llm_completion = _first_int(
        summary.get("llm_completion_tokens"),
        summary.get("llm_output_tokens"),
        llm_kind.get("completion_tokens"),
        llm_kind.get("output_tokens"),
        usage.get("llm_completion_tokens"),
        usage.get("output_tokens"),
    )
    llm_total = _first_int(
        summary.get("llm_total_tokens"),
        llm_kind.get("total_tokens"),
        usage.get("llm_total_tokens"),
        llm_prompt + llm_completion,
    )
    embedding_total = _first_int(
        summary.get("embedding_total_tokens"),
        summary.get("embedder_total_tokens"),
        embedding_kind.get("total_tokens"),
        embedding_kind.get("input_tokens"),
        usage.get("embedding_total_tokens"),
        usage.get("embedding_tokens"),
    )
    total_tokens = _first_int(
        summary.get("total_tokens"),
        usage.get("total_tokens"),
        llm_total + embedding_total,
    )
    record_count = _first_int(
        summary.get("record_count"),
        summary.get("request_count"),
        summary.get("call_count"),
    )
    if record_count <= 0:
        records = summary.get("records")
        if isinstance(records, list):
            record_count = len(records)
    if record_count <= 0:
        record_count = _first_int(llm_kind.get("record_count")) + _first_int(
            embedding_kind.get("record_count")
        )

    total_cost = _first_float_or_none(
        summary.get("total_cost_usd"),
        summary.get("total_cost_usd_sum"),
        usage.get("total_cost_usd"),
    )
    metered_record_count = _first_int(llm_kind.get("record_count")) + _first_int(embedding_kind.get("record_count"))
    has_metered_usage = metered_record_count > 0 or llm_total > 0 or embedding_total > 0
    estimated_token_records = _first_int(
        summary.get("estimated_token_records"),
        summary.get("estimated_record_count"),
    )
    missing_usage_records = _first_int(
        summary.get("missing_usage_records"),
        summary.get("missing_usage_record_count"),
    )
    records_by_operation = _dict(
        summary.get("records_by_operation")
        or summary.get("by_operation")
        or {}
    )
    request_kind = _dict(by_kind.get("request"))
    request_record_count = _first_int(request_kind.get("record_count"))
    request_operation_count = sum(
        _first_int(value.get("record_count") if isinstance(value, Mapping) else None)
        for key, value in records_by_operation.items()
        if str(key).startswith("request.")
    )
    has_only_request_operations = bool(records_by_operation) and all(
        str(key).startswith("request.") for key in records_by_operation
    )
    request_only_zero_cost = (
        total_cost == 0.0
        and record_count > 0
        and not has_metered_usage
        and total_tokens == 0
        and estimated_token_records == 0
        and missing_usage_records == 0
        and (
            request_record_count == record_count
            or (has_only_request_operations and request_operation_count == record_count)
        )
    )
    normalized: dict[str, Any] = {
        "schema_version": 1,
        "status": "ok",
        "metering_run_id": summary.get("metering_run_id") or metering_run_id,
        "eval_id": summary.get("eval_id"),
        "case_id": summary.get("case_id"),
        "app_id": summary.get("app_id"),
        "record_count": record_count,
        "metered_record_count": metered_record_count,
        "llm_prompt_tokens": llm_prompt,
        "llm_completion_tokens": llm_completion,
        "llm_total_tokens": llm_total,
        "embedding_total_tokens": embedding_total,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 12) if total_cost is not None else None,
        "cost_tracked": total_cost is not None and (has_metered_usage or request_only_zero_cost),
        "request_only_zero_cost": request_only_zero_cost,
        "estimated_token_records": estimated_token_records,
        "missing_usage_records": missing_usage_records,
        "records_by_operation": records_by_operation,
    }
    if snapshot:
        for key in ("metering_run_id", "eval_id", "case_id", "app_id"):
            if key == "app_id":
                normalized[key] = (
                    normalized.get(key)
                    or snapshot.get("flowark_app_id")
                    or snapshot.get("app_id")
                    or snapshot.get("mem0_app_id")
                )
            else:
                normalized[key] = normalized.get(key) or snapshot.get(key)
    provider = summary.get("provider") or summary.get("providers")
    model = summary.get("model") or summary.get("models")
    if provider:
        normalized["provider"] = provider
    if model:
        normalized["model"] = model
    pricing_profile = summary.get("pricing_profile")
    if pricing_profile:
        normalized["pricing_profile"] = pricing_profile
    return normalized


def build_mem0_backend_aggregate(summary: Mapping[str, Any]) -> dict[str, Any]:
    status = str(summary.get("status") or "unknown")
    llm_prompt = _first_int(summary.get("llm_prompt_tokens"))
    llm_completion = _first_int(summary.get("llm_completion_tokens"))
    llm_total = _first_int(summary.get("llm_total_tokens"), llm_prompt + llm_completion)
    embedding_total = _first_int(summary.get("embedding_total_tokens"))
    total_tokens = _first_int(summary.get("total_tokens"), llm_total + embedding_total)
    total_cost = _safe_float(summary.get("total_cost_usd"))
    cost_tracked = status == "ok" and total_cost is not None and summary.get("cost_tracked") is not False
    return {
        "status": status,
        "cost_tracked": cost_tracked,
        "result_count": _first_int(summary.get("record_count")),
        "request_count": _first_int(summary.get("record_count")),
        "total_cost_usd_sum": round(total_cost, 12) if total_cost is not None else None,
        "request_only_zero_cost": bool(summary.get("request_only_zero_cost")),
        "estimated_token_records": _first_int(summary.get("estimated_token_records")),
        "missing_usage_records": _first_int(summary.get("missing_usage_records")),
        "usage": {
            "input_tokens": llm_prompt,
            "output_tokens": llm_completion,
            "llm_total_tokens": llm_total,
            "embedding_tokens": embedding_total,
            "total_tokens": total_tokens,
        },
    }


def merge_mem0_usage_into_cost_summary(
    run_summary: dict[str, Any],
    mem0_summary: Mapping[str, Any],
) -> None:
    aggregated = run_summary.setdefault("aggregated_metrics", {})
    if not isinstance(aggregated, dict):
        return

    mem0_backend = build_mem0_backend_aggregate(mem0_summary)
    aggregated["mem0_backend"] = mem0_backend
    run_summary["mem0_metering"] = dict(mem0_summary)
    if mem0_backend.get("cost_tracked") is not True:
        aggregated.pop("total_with_mem0", None)
        return

    main_agent = aggregated.get("main_agent") if isinstance(aggregated.get("main_agent"), dict) else {}
    main_usage = _dict(main_agent.get("usage"))
    main_total_tokens = _first_int(main_usage.get("total_tokens"))
    if main_total_tokens <= 0:
        main_total_tokens = (
            _first_int(main_usage.get("input_tokens"))
            + _first_int(main_usage.get("output_tokens"))
            + _first_int(main_usage.get("cache_read_input_tokens"))
            + _first_int(main_usage.get("cache_creation_input_tokens"))
        )
    total_usage = {
        "input_tokens": _first_int(main_usage.get("input_tokens"))
        + _first_int(mem0_backend["usage"].get("input_tokens")),
        "output_tokens": _first_int(main_usage.get("output_tokens"))
        + _first_int(mem0_backend["usage"].get("output_tokens")),
        "cache_read_input_tokens": _first_int(main_usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _first_int(main_usage.get("cache_creation_input_tokens")),
        "visible_non_cache_tokens": _first_int(main_usage.get("visible_non_cache_tokens"))
        + _first_int(mem0_backend["usage"].get("input_tokens"))
        + _first_int(mem0_backend["usage"].get("output_tokens")),
        "mem0_embedding_tokens": _first_int(mem0_backend["usage"].get("embedding_tokens")),
        "total_tokens": main_total_tokens + _first_int(mem0_backend["usage"].get("total_tokens")),
    }
    opencode_cost = _first_float(main_agent.get("total_cost_usd_sum"))
    mem0_cost = _first_float(mem0_backend.get("total_cost_usd_sum"))
    aggregated["total_with_mem0"] = {
        "status": mem0_backend.get("status"),
        "cost_tracked": bool(mem0_backend.get("cost_tracked")),
        "opencode_total_cost_usd_sum": round(opencode_cost, 12),
        "mem0_backend_total_cost_usd_sum": round(mem0_cost, 12),
        "total_cost_usd_sum": round(opencode_cost + mem0_cost, 12),
        "usage": total_usage,
    }


def collect_mem0_usage_for_run(
    *,
    run_dir: Path | None,
    run_summary: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    snapshot_path = run_dir / MEM0_RUNTIME_SNAPSHOT
    if not snapshot_path.is_file():
        return None

    env = dict(os.environ if environ is None else environ)
    snapshot = _read_json_object(snapshot_path)
    if not snapshot.get("enabled"):
        return None

    output_path = run_dir / MEM0_USAGE_SUMMARY
    host = _host_from_snapshot(snapshot, env)
    api_key = str(env.get("MEM0_API_KEY") or "").strip()
    metering_run_id = _metering_run_id(snapshot, env)
    if not host:
        summary = _status_summary(snapshot=snapshot, status="unavailable", reason="missing_mem0_host")
        _write_json(output_path, summary)
        merge_mem0_usage_into_cost_summary(run_summary, summary)
        return summary
    if not api_key:
        summary = _status_summary(snapshot=snapshot, status="unavailable", reason="missing_mem0_api_key")
        _write_json(output_path, summary)
        merge_mem0_usage_into_cost_summary(run_summary, summary)
        return summary
    if not metering_run_id:
        summary = _status_summary(snapshot=snapshot, status="unavailable", reason="missing_metering_run_id")
        _write_json(output_path, summary)
        merge_mem0_usage_into_cost_summary(run_summary, summary)
        return summary

    query = urlencode({"metering_run_id": metering_run_id})
    request = Request(
        f"{host}/usage/summary?{query}",
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        method="GET",
    )
    attempts = _usage_retry_attempts(env)
    backoff_seconds = _usage_retry_backoff_seconds(env)
    summary: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                data = response.read()
            payload = json.loads(data.decode("utf-8")) if data else {}
            if not isinstance(payload, dict):
                raise ValueError("Mem0 usage summary response is not a JSON object")
            response_metering_run_ids = _response_metering_run_ids(payload)
            mismatched_ids = [item for item in response_metering_run_ids if item != metering_run_id]
            if not response_metering_run_ids:
                summary = _status_summary(
                    snapshot=snapshot,
                    status="unavailable",
                    reason="missing_response_metering_run_id",
                    detail=f"expected {metering_run_id}, got no metering_run_id in response",
                )
            elif mismatched_ids:
                summary = _status_summary(
                    snapshot=snapshot,
                    status="unavailable",
                    reason="metering_run_id_mismatch",
                    detail=f"expected {metering_run_id}, got {', '.join(mismatched_ids)}",
                )
            else:
                summary = normalize_mem0_usage_summary(
                    payload,
                    snapshot=snapshot,
                    metering_run_id=metering_run_id,
                )
            break
        except HTTPError as exc:
            if exc.code not in RETRYABLE_USAGE_HTTP_STATUS or attempt >= attempts:
                summary = _status_summary(
                    snapshot=snapshot,
                    status="unavailable",
                    reason=f"http_{exc.code}",
                    detail=_retry_detail(exc, attempt, attempts),
                )
                break
        except (OSError, URLError, TimeoutError) as exc:
            if attempt >= attempts:
                summary = _status_summary(
                    snapshot=snapshot,
                    status="unavailable",
                    reason=type(exc).__name__,
                    detail=_retry_detail(exc, attempt, attempts),
                )
                break
        except (ValueError, json.JSONDecodeError) as exc:
            summary = _status_summary(
                snapshot=snapshot,
                status="unavailable",
                reason=type(exc).__name__,
                detail=str(exc),
            )
            break
        if backoff_seconds > 0:
            delay_seconds = min(
                MAX_USAGE_RETRY_BACKOFF_SECONDS,
                backoff_seconds * (2 ** (attempt - 1)),
            )
            time.sleep(delay_seconds)

    if summary is None:
        summary = _status_summary(snapshot=snapshot, status="unavailable", reason="unknown_retry_failure")

    _write_json(output_path, summary)
    merge_mem0_usage_into_cost_summary(run_summary, summary)
    return summary
