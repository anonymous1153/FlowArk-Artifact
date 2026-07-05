"""Health issue detection for completed eval case artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import _json_load

HealthIssue = dict[str, Any]


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = _json_load(path)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_error_context(exc: Exception) -> dict[str, Any]:
    return {"error": f"{type(exc).__name__}: {exc}"[:500]}


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    artifact_path: Path | str | None = None,
    context: dict[str, Any] | None = None,
) -> HealthIssue:
    payload: HealthIssue = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if artifact_path is not None:
        payload["artifact_path"] = str(artifact_path)
    if context:
        payload["context"] = dict(context)
    return payload


def _read_jsonl_objects_with_issues(
    path: Path | None,
    *,
    code: str,
    message: str,
) -> tuple[list[dict[str, Any]], list[HealthIssue]]:
    if path is None or not path.exists():
        return [], []
    out: list[dict[str, Any]] = []
    malformed_count = 0
    first_malformed_line: int | None = None
    first_malformed_error = ""
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line_no, line in enumerate(fp, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except Exception as exc:
                    malformed_count += 1
                    if first_malformed_line is None:
                        first_malformed_line = line_no
                        first_malformed_error = f"{type(exc).__name__}: {exc}"[:500]
                    continue
                if isinstance(payload, dict):
                    out.append(payload)
    except Exception as exc:
        return out, [
            _issue(
                "warning",
                code,
                message,
                artifact_path=path,
                context=_json_error_context(exc),
            )
        ]
    if malformed_count <= 0:
        return out, []
    return out, [
        _issue(
            "warning",
            code,
            message,
            artifact_path=path,
            context={
                "malformed_line_count": malformed_count,
                "first_malformed_line": first_malformed_line,
                "first_error": first_malformed_error,
            },
        )
    ]


def _collect_health_artifact_parse_issues(run_dir: Path) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    for name in (
        "cost_summary.json",
        "auto_knowledge_cycle.json",
        "run_meta.json",
        "knowledge_apply.json",
        "final_report.json",
    ):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            payload = _json_load(path)
        except Exception as exc:
            issues.append(
                _issue(
                    "warning",
                    "health_artifact_parse_failed",
                    "health artifact exists but could not be parsed",
                    artifact_path=path,
                    context=_json_error_context(exc),
                )
            )
            continue
        if not isinstance(payload, dict):
            issues.append(
                _issue(
                    "warning",
                    "health_artifact_parse_failed",
                    "health artifact is not a JSON object",
                    artifact_path=path,
                    context={"type": type(payload).__name__},
                )
            )
    return issues


def _append_issue_once(issues: list[HealthIssue], issue: HealthIssue) -> None:
    key = (str(issue.get("severity")), str(issue.get("code")), str(issue.get("artifact_path", "")))
    for existing in issues:
        existing_key = (
            str(existing.get("severity")),
            str(existing.get("code")),
            str(existing.get("artifact_path", "")),
        )
        if existing_key == key:
            return
    issues.append(issue)


def _issue_status(issues: list[HealthIssue]) -> str | None:
    severities = {str(item.get("severity") or "").strip().lower() for item in issues}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return None


def _collect_recall_embedding_issues(run_dir: Path, *, digest_mode: str) -> list[HealthIssue]:
    if str(digest_mode or "").strip().lower() != "live_corridor_v2":
        return []
    issues: list[HealthIssue] = []
    for name in ("historical_recall_candidates.json", "knowledge_recall_candidates.json"):
        path = run_dir / name
        payload = _read_json_object(path)
        reason = str(payload.get("reason") or "").strip()
        if reason not in {"embed_error", "embedding_config_missing"}:
            continue
        _append_issue_once(
            issues,
            _issue(
                "error",
                "reuse_embedding_failed",
                f"live_corridor_v2 recall failed: {reason}",
                artifact_path=path,
                context={
                    "reason": reason,
                    "candidate_count": payload.get("candidate_count"),
                    "selected_count": len(payload.get("selected") or []) if isinstance(payload.get("selected"), list) else 0,
                },
            ),
        )
    return issues


def _collect_final_report_issues(run_dir: Path) -> list[HealthIssue]:
    run_meta_path = run_dir / "run_meta.json"
    run_meta = _read_json_object(run_meta_path)
    final_report_path = run_dir / "final_report.json"
    final_report = _read_json_object(final_report_path)
    parse_error = str(
        run_meta.get("final_report_parse_error")
        or final_report.get("parse_error")
        or ""
    ).strip()
    generation_mode = str(run_meta.get("final_report_generation_mode") or "").strip()
    if not parse_error and generation_mode != "legacy_fallback":
        return []
    context: dict[str, Any] = {"generation_mode": generation_mode}
    if parse_error:
        context["parse_error"] = parse_error[:500]
    return [
        _issue(
            "warning",
            "final_report_parse_fallback",
            "final_report structured parse failed or used legacy fallback",
            artifact_path=final_report_path if final_report_path.exists() else run_meta_path,
            context=context,
        )
    ]


def _collect_auto_knowledge_issues(run_dir: Path) -> list[HealthIssue]:
    cycle_path = run_dir / "auto_knowledge_cycle.json"
    cycle = _read_json_object(cycle_path)
    if not cycle:
        return []
    issues: list[HealthIssue] = []
    synth_meta = cycle.get("synth_meta") if isinstance(cycle.get("synth_meta"), dict) else {}
    synth_parse_error = str(synth_meta.get("parse_error") or synth_meta.get("error") or "").strip()
    if synth_parse_error:
        issues.append(
            _issue(
                "error",
                "knowledge_synth_failed",
                "knowledge_synth failed",
                artifact_path=cycle_path,
                context={
                    "parse_error": synth_parse_error[:500],
                    "reason": cycle.get("reason"),
                    "synth_source": cycle.get("synth_source"),
                },
            )
        )

    return issues


def _collect_knowledge_apply_issues(run_dir: Path) -> list[HealthIssue]:
    apply_path = run_dir / "knowledge_apply.json"
    payload = _read_json_object(apply_path)
    if not payload:
        return []
    errors = payload.get("apply_errors") if isinstance(payload.get("apply_errors"), list) else []
    failed_ids = payload.get("apply_failed_candidate_ids")
    failed_count = int(payload.get("apply_error_count") or len(errors) or 0)
    if failed_count <= 0 and not errors:
        return []
    context: dict[str, Any] = {
        "apply_error_count": failed_count,
        "apply_failed_candidate_ids": failed_ids if isinstance(failed_ids, list) else [],
    }
    if errors and isinstance(errors[0], dict):
        context["first_error"] = {
            "candidate_id": errors[0].get("candidate_id"),
            "stage": errors[0].get("stage"),
            "error": str(errors[0].get("error") or "")[:500],
        }
    return [
        _issue(
            "error",
            "knowledge_apply_failed",
            "knowledge apply failed to write one or more candidates",
            artifact_path=apply_path,
            context=context,
        )
    ]


def _bridge_event_failed(record: dict[str, Any]) -> bool:
    bridge = record.get("bridge") if isinstance(record.get("bridge"), dict) else {}
    if str(bridge.get("action") or record.get("action") or "").strip() == "error":
        return True
    if str(bridge.get("delivery_status") or "").strip() == "failed":
        return True
    if str(record.get("event") or "").endswith(".no_reply_error"):
        return True
    return False


def _collect_opencode_plugin_issues(run_dir: Path) -> list[HealthIssue]:
    path = run_dir / "opencode_plugin_events.jsonl"
    records, issues = _read_jsonl_objects_with_issues(
        path,
        code="opencode_plugin_events_malformed",
        message="OpenCode plugin events JSONL contains malformed records",
    )
    for record in records:
        event = str(record.get("event") or "").strip()
        if not event or not _bridge_event_failed(record):
            continue
        bridge = record.get("bridge") if isinstance(record.get("bridge"), dict) else {}
        severity = "error" if event == "chat.message.bridge" else "warning"
        _append_issue_once(
            issues,
            _issue(
                severity,
                "opencode_bridge_failed",
                "OpenCode runtime bridge failed",
                artifact_path=path,
                context={
                    "event": event,
                    "delivery_status": bridge.get("delivery_status"),
                    "delivery_reason": bridge.get("delivery_reason"),
                    "error": str(record.get("error") or "")[:500],
                },
            ),
        )
    return issues


def _collect_opencode_hook_trace_issues(run_dir: Path) -> list[HealthIssue]:
    path = run_dir / "opencode_hook_trace.jsonl"
    records, issues = _read_jsonl_objects_with_issues(
        path,
        code="opencode_hook_trace_malformed",
        message="OpenCode hook trace JSONL contains malformed records",
    )
    for record in records:
        event = str(record.get("event") or "").strip()
        delivery_status = str(record.get("delivery_status") or "").strip()
        delivery_trace = record.get("delivery_trace") if isinstance(record.get("delivery_trace"), dict) else {}
        skip_type = str(delivery_trace.get("skip_type") or "").strip()
        if delivery_status != "failed" and skip_type != "bridge_error":
            continue
        severity = "error" if event == "chat.message" else "warning"
        _append_issue_once(
            issues,
            _issue(
                severity,
                "opencode_bridge_failed",
                "OpenCode runtime hook bridge failed",
                artifact_path=path,
                context={
                    "event": event,
                    "delivery_status": delivery_status,
                    "delivery_reason": record.get("delivery_reason"),
                    "skip_type": skip_type,
                },
            ),
        )
    return issues


def _is_opencode_cost_summary(cost: dict[str, Any], run_dir: Path) -> bool:
    if (run_dir / "opencode_usage_rollup.json").exists() or (run_dir / "opencode_usage.json").exists():
        return True
    agg = cost.get("aggregated_metrics") if isinstance(cost, dict) else {}
    if isinstance(agg, dict) and isinstance(agg.get("opencode"), dict):
        return True
    phases = cost.get("phases") if isinstance(cost.get("phases"), list) else []
    return any(isinstance(phase, dict) and phase.get("adapter") == "opencode" for phase in phases)


def _collect_opencode_usage_fallback_issues(run_dir: Path) -> list[HealthIssue]:
    cost_path = run_dir / "cost_summary.json"
    cost = _read_json_object(cost_path)
    if not cost or not _is_opencode_cost_summary(cost, run_dir):
        return []
    fallback_count = 0
    phases = cost.get("phases") if isinstance(cost.get("phases"), list) else []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        records = phase.get("request_usage_records") if isinstance(phase.get("request_usage_records"), list) else []
        for record in records:
            if isinstance(record, dict) and record.get("source") == "fallback_assistant_usage_record":
                fallback_count += 1
    if fallback_count <= 0:
        return []
    return [
        _issue(
            "warning",
            "opencode_usage_fallback",
            "OpenCode request usage used assistant-level fallback records",
            artifact_path=cost_path,
            context={"fallback_request_usage_record_count": fallback_count},
        )
    ]


def _collect_llm_judge_issues(llm_judge: dict[str, Any] | None, *, artifact_path: Path) -> list[HealthIssue]:
    judge = llm_judge if isinstance(llm_judge, dict) else {}
    if not judge.get("enabled") or judge.get("eligible") is not True:
        return []
    status = str(judge.get("status") or "").strip()
    if status == "ok":
        return []
    if judge.get("skipped") is True and status in {
        "skipped_dummy_run",
        "skipped_no_ground_truth",
        "skipped_interrupted",
    }:
        return []
    return [
        _issue(
            "warning",
            "llm_judge_failed",
            "LLM judge was enabled and eligible but did not complete successfully",
            artifact_path=artifact_path,
            context={
                "status": status,
                "error": str(judge.get("error") or "").strip()[:500],
            },
        )
    ]


def collect_case_health_issues(
    *,
    run_dir: Path | None,
    repeat_dir: Path,
    digest_mode: str,
    llm_judge: dict[str, Any] | None,
) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    if run_dir is not None:
        issues.extend(_collect_health_artifact_parse_issues(run_dir))
        issues.extend(_collect_recall_embedding_issues(run_dir, digest_mode=digest_mode))
        issues.extend(_collect_final_report_issues(run_dir))
        issues.extend(_collect_auto_knowledge_issues(run_dir))
        issues.extend(_collect_knowledge_apply_issues(run_dir))
        issues.extend(_collect_opencode_plugin_issues(run_dir))
        issues.extend(_collect_opencode_hook_trace_issues(run_dir))
        issues.extend(_collect_opencode_usage_fallback_issues(run_dir))
    issues.extend(_collect_llm_judge_issues(llm_judge, artifact_path=repeat_dir / "llm_judge_result.json"))
    return issues


def apply_health_status(base_status: str, issues: list[HealthIssue]) -> str:
    normalized = str(base_status or "").strip().lower()
    if normalized != "success":
        return normalized
    issue_status = _issue_status(issues)
    return issue_status or normalized
