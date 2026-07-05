"""Control/state/result helpers for the evaluation harness."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flowark.timeutil import from_timestamp_tz8_iso

from .common import (
    _RESULT_STATUS_COMPLETED,
    _RESULT_STATUS_RESUMABLE,
    _append_jsonl,
    _json_dump,
    _json_load,
    _now_utc_iso,
    _safe_int,
)
from .models import EvalTask

if TYPE_CHECKING:
    from .orchestrator import EvaluationHarness


def _control_path(eval_root: Path) -> Path:
    return eval_root / "control.json"


def _eval_state_path(eval_root: Path) -> Path:
    return eval_root / "eval_state.json"


def _default_control_payload() -> dict[str, Any]:
    now = _now_utc_iso()
    return {
        "created_at": now,
        "updated_at": now,
        "skip_task_indexes": [],
        "skip_repeat_dirs": [],
        "skip_run_dirs": [],
        "pause_after_active": False,
        "pause_mode": "none",
        "force_abort_task_indexes": [],
        "force_abort_repeat_dirs": [],
        "rerun_task_indexes": [],
        "rerun_repeat_dirs": [],
        "rerun_only": False,
    }


def _default_eval_state_payload(*, total_tasks: int, resume_count: int = 0) -> dict[str, Any]:
    now = _now_utc_iso()
    return {
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "pause_mode": "none",
        "resumable": False,
        "resume_count": int(resume_count),
        "total_task_count": int(total_tasks),
        "completed_task_count": 0,
        "pending_task_count": int(total_tasks),
        "running_task_count": 0,
        "active_task_indexes": [],
        "active_repeat_dirs": [],
    }


def _load_control(self: EvaluationHarness, eval_root: Path) -> dict[str, Any]:
    path = self._control_path(eval_root)
    if not path.exists():
        return {}
    try:
        data = _json_load(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_control(self: EvaluationHarness, eval_root: Path, payload: dict[str, Any]) -> Path:
    merged = dict(self._default_control_payload())
    merged.update(payload or {})
    for key in ("rerun_request_id", "rerun_source", "rerun_requested_at", "rerun_only"):
        if merged.get(key) in {None, ""}:
            merged.pop(key, None)
    merged["updated_at"] = _now_utc_iso()
    if not merged.get("created_at"):
        merged["created_at"] = _now_utc_iso()
    path = self._control_path(eval_root)
    _json_dump(path, merged)
    return path


def _load_eval_state(self: EvaluationHarness, eval_root: Path) -> dict[str, Any]:
    path = self._eval_state_path(eval_root)
    if not path.exists():
        return {}
    try:
        data = _json_load(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_eval_state(
    self: EvaluationHarness,
    eval_root: Path,
    *,
    total_tasks: int,
    completed_task_count: int,
    pending_task_count: int,
    running_task_count: int,
    active_task_indexes: list[int],
    active_repeat_dirs: list[str],
    status: str,
    pause_mode: str,
    resumable: bool,
    resume_count: int = 0,
) -> Path:
    existing = self._load_eval_state(eval_root)
    payload = {
        "created_at": existing.get("created_at") or _now_utc_iso(),
        "updated_at": _now_utc_iso(),
        "status": str(status or "running"),
        "pause_mode": str(pause_mode or "none"),
        "resumable": bool(resumable),
        "resume_count": int(resume_count),
        "total_task_count": int(total_tasks),
        "completed_task_count": int(max(0, completed_task_count)),
        "pending_task_count": int(max(0, pending_task_count)),
        "running_task_count": int(max(0, running_task_count)),
        "active_task_indexes": sorted({int(v) for v in active_task_indexes}),
        "active_repeat_dirs": sorted({str(v) for v in active_repeat_dirs if str(v).strip()}),
    }
    path = self._eval_state_path(eval_root)
    _json_dump(path, payload)
    return path


def _as_str_set(values: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(values, list):
        return out
    for item in values:
        text = str(item or "").strip()
        if text:
            out.add(text)
    return out


def _as_int_set(values: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(values, list):
        return out
    for item in values:
        ivalue = _safe_int(item)
        if ivalue is not None:
            out.add(int(ivalue))
    return out


def _is_task_skip_requested(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    task: EvalTask,
    run_dir: Path | None = None,
) -> bool:
    control = self._load_control(eval_root)
    if not control:
        return False
    if task.task_index in self._as_int_set(control.get("skip_task_indexes")):
        return True
    repeat_dir = str(task.repeat_dir)
    if repeat_dir in self._as_str_set(control.get("skip_repeat_dirs")):
        return True
    if run_dir is not None and str(run_dir) in self._as_str_set(control.get("skip_run_dirs")):
        return True
    return False


def _is_pause_requested(self: EvaluationHarness, eval_root: Path) -> tuple[bool, str]:
    control = self._load_control(eval_root)
    if not control:
        return False, "none"
    return bool(control.get("pause_after_active")), str(control.get("pause_mode") or "none")


def _is_task_force_abort_requested(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    task: EvalTask,
    run_dir: Path | None = None,
) -> bool:
    control = self._load_control(eval_root)
    if not control:
        return False
    if task.task_index in self._as_int_set(control.get("force_abort_task_indexes")):
        return True
    repeat_dir = str(task.repeat_dir)
    if repeat_dir in self._as_str_set(control.get("force_abort_repeat_dirs")):
        return True
    return False


def _result_path_for_task(task: EvalTask) -> Path:
    return task.repeat_dir / "result.json"


def _load_task_result(self: EvaluationHarness, task: EvalTask) -> dict[str, Any] | None:
    path = self._result_path_for_task(task)
    if not path.exists():
        return None
    try:
        data = _json_load(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _result_is_final(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("status") or "") in _RESULT_STATUS_COMPLETED


def _result_requires_resume(result: dict[str, Any] | None) -> bool:
    if result is None:
        return True
    return str(result.get("status") or "") in _RESULT_STATUS_RESUMABLE


def _completed_task_count_from_results(results: list[dict[str, Any]]) -> int:
    return sum(1 for item in results if str(item.get("status") or "") in _RESULT_STATUS_COMPLETED)


def _resumable_task_count_from_results(results: list[dict[str, Any]]) -> int:
    return sum(1 for item in results if str(item.get("status") or "") in _RESULT_STATUS_RESUMABLE)


def _task_matches_control_set(
    self: EvaluationHarness,
    task: EvalTask,
    *,
    task_indexes: set[int],
    repeat_dirs: set[str],
) -> bool:
    if task.task_index in task_indexes:
        return True
    return str(task.repeat_dir) in repeat_dirs


_RERUN_REPEAT_ARTIFACT_NAMES = [
    "runs",
    "result.json",
    "stdout.txt",
    "stderr.txt",
    "subprocess_command.json",
    "llm_judge_input.json",
    "llm_judge_raw_response.json",
    "llm_judge_result.json",
]


def _resolve_path_text(path: Path | str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:
        return str(path)


def _rerun_requests_path(eval_root: Path) -> Path:
    return eval_root / "rerun_requests.jsonl"


def _append_rerun_request_event(
    self: EvaluationHarness,
    eval_root: Path,
    payload: dict[str, Any],
) -> Path:
    path = self._rerun_requests_path(eval_root)
    event = dict(payload)
    event.setdefault("schema_version", 1)
    event.setdefault("created_at", _now_utc_iso())
    _append_jsonl(path, event)
    return path


def _load_planned_runs_by_index(self: EvaluationHarness, eval_root: Path) -> dict[int, dict[str, Any]]:
    path = eval_root / "planned_runs.json"
    if not path.exists():
        raise ValueError(f"缺少 planned_runs.json: {path}")
    payload = _json_load(path)
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise ValueError(f"无效 planned_runs.json: {path}")
    out: dict[int, dict[str, Any]] = {}
    for item in runs:
        if not isinstance(item, dict):
            continue
        task_index = _safe_int(item.get("task_index"))
        if task_index is not None:
            out[int(task_index)] = item
    return out


def _validate_task_matches_planned(task: EvalTask, planned: dict[str, Any]) -> str | None:
    expected = {
        "task_total": task.task_total,
        "flow_id": task.case.flow_id,
        "source_id": task.case.source_id,
        "mode": task.mode,
        "repeat_idx": task.repeat_idx,
        "repeat_dir": _resolve_path_text(task.repeat_dir),
    }
    actual = {
        "task_total": _safe_int(planned.get("task_total")),
        "flow_id": planned.get("flow_id"),
        "source_id": planned.get("source_id"),
        "mode": planned.get("mode"),
        "repeat_idx": _safe_int(planned.get("repeat_idx")),
        "repeat_dir": _resolve_path_text(str(planned.get("repeat_dir") or "")),
    }
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            return f"task_index={task.task_index} planned {key} mismatch: expected={expected_value!r} actual={actual.get(key)!r}"
    return None


def _validate_all_tasks_match_planned(
    self: EvaluationHarness,
    *,
    tasks: list[EvalTask],
    planned_by_index: dict[int, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    task_indexes = {int(task.task_index) for task in tasks}
    planned_indexes = set(planned_by_index.keys())
    if len(planned_indexes) != len(tasks):
        errors.append(f"planned task count mismatch: expected={len(tasks)} actual={len(planned_indexes)}")
    missing_indexes = sorted(task_indexes - planned_indexes)
    extra_indexes = sorted(planned_indexes - task_indexes)
    if missing_indexes:
        errors.append(f"planned_runs.json missing task indexes: {missing_indexes}")
    if extra_indexes:
        errors.append(f"planned_runs.json has extra task indexes: {extra_indexes}")
    for task in tasks:
        planned = planned_by_index.get(int(task.task_index))
        if not isinstance(planned, dict):
            continue
        mismatch = _validate_task_matches_planned(task, planned)
        if mismatch:
            errors.append(mismatch)
    return errors


def _archive_repeat_dir_for_explicit_rerun(
    self: EvaluationHarness,
    *,
    task: EvalTask,
    request_id: str,
) -> dict[str, Any]:
    repeat_dir = task.repeat_dir
    archive_root = repeat_dir / ".rerun_archive" / request_id
    if archive_root.exists():
        suffix = 1
        while (repeat_dir / ".rerun_archive" / f"{request_id}-{suffix}").exists():
            suffix += 1
        archive_root = repeat_dir / ".rerun_archive" / f"{request_id}-{suffix}"
    archive_root.mkdir(parents=True, exist_ok=True)
    archived: list[dict[str, str]] = []
    for name in _RERUN_REPEAT_ARTIFACT_NAMES:
        src = repeat_dir / name
        if not src.exists():
            continue
        dst = archive_root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        archived.append({"name": name, "from": str(src), "to": str(dst)})
    manifest = {
        "schema_version": 1,
        "created_at": _now_utc_iso(),
        "request_id": request_id,
        "task_index": int(task.task_index),
        "repeat_dir": str(repeat_dir),
        "archive_dir": str(archive_root),
        "archived": archived,
        "archive_existing": True,
        "rollback_knowledge": False,
        "knowledge_policy": "keep_current_scope_no_rollback",
        "clean_rerun": False,
    }
    _json_dump(archive_root / "archive_manifest.json", manifest)
    return {
        "task_index": int(task.task_index),
        "repeat_dir": str(repeat_dir),
        "archive_dir": str(archive_root),
        "archived": archived,
    }


def _cleanup_repeat_dir_for_rerun(self: EvaluationHarness, task: EvalTask) -> None:
    repeat_dir = task.repeat_dir
    runs_parent = repeat_dir / "runs"
    if runs_parent.exists():
        for run_dir in sorted([p for p in runs_parent.iterdir() if p.is_dir()]):
            self._rollback_run_dir_knowledge_side_effects(run_dir)
        shutil.rmtree(runs_parent, ignore_errors=True)
    for name in [
        "result.json",
        "stdout.txt",
        "stderr.txt",
        "subprocess_command.json",
        "llm_judge_input.json",
        "llm_judge_raw_response.json",
        "llm_judge_result.json",
    ]:
        path = repeat_dir / name
        try:
            if path.exists():
                path.unlink()
        except Exception:
            continue


def _prepare_explicit_rerun_tasks(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    tasks: list[EvalTask],
    control: dict[str, Any],
    rerun_task_indexes: set[int],
    rerun_repeat_dirs: set[str],
) -> list[EvalTask]:
    if not rerun_task_indexes and not rerun_repeat_dirs:
        return []

    request_id = str(control.get("rerun_request_id") or f"rerun-{uuid.uuid4().hex[:12]}")
    source = str(control.get("rerun_source") or "eval_resume_control")
    normalized_repeat_dirs = {_resolve_path_text(value) for value in rerun_repeat_dirs}
    has_explicit_rerun_metadata = bool(control.get("rerun_request_id") or control.get("rerun_source"))
    matched: list[EvalTask] = []
    unmatched_task_indexes = sorted(rerun_task_indexes)
    unmatched_repeat_dirs = sorted(normalized_repeat_dirs)
    skipped_resumable_task_indexes: list[int] = []

    for task in tasks:
        task_repeat_dir = _resolve_path_text(task.repeat_dir)
        if task.task_index not in rerun_task_indexes and task_repeat_dir not in normalized_repeat_dirs:
            continue
        result = self._load_task_result(task)
        if isinstance(result, dict) and str(result.get("status") or "") in _RESULT_STATUS_RESUMABLE:
            skipped_resumable_task_indexes.append(int(task.task_index))
            if task.task_index in unmatched_task_indexes:
                unmatched_task_indexes.remove(task.task_index)
            if task_repeat_dir in unmatched_repeat_dirs:
                unmatched_repeat_dirs.remove(task_repeat_dir)
            continue
        matched.append(task)
        if task.task_index in unmatched_task_indexes:
            unmatched_task_indexes.remove(task.task_index)
        if task_repeat_dir in unmatched_repeat_dirs:
            unmatched_repeat_dirs.remove(task_repeat_dir)

    if not matched and skipped_resumable_task_indexes and not unmatched_task_indexes and not unmatched_repeat_dirs:
        if has_explicit_rerun_metadata:
            self._append_rerun_request_event(
                eval_root,
                {
                    "event": "explicit_rerun_delegated_to_resumable",
                    "request_id": request_id,
                    "source": source,
                    "skipped_resumable_task_indexes": skipped_resumable_task_indexes,
                    "rollback_knowledge": "existing_resumable_cleanup_policy",
                },
            )
        return []

    self._append_rerun_request_event(
        eval_root,
        {
            "event": "explicit_rerun_requested",
            "request_id": request_id,
            "source": source,
            "requested_task_indexes": sorted(rerun_task_indexes),
            "requested_repeat_dirs": sorted(normalized_repeat_dirs),
            "matched_task_indexes": [int(task.task_index) for task in matched],
            "matched_repeat_dirs": [str(task.repeat_dir) for task in matched],
            "unmatched_task_indexes": unmatched_task_indexes,
            "unmatched_repeat_dirs": unmatched_repeat_dirs,
            "skipped_resumable_task_indexes": skipped_resumable_task_indexes,
            "archive_existing": True,
            "rollback_knowledge": False,
            "knowledge_policy": "keep_current_scope_no_rollback",
            "clean_rerun": False,
        },
    )

    errors: list[str] = []
    if not matched:
        errors.append("rerun target matched zero tasks")
    if unmatched_task_indexes:
        errors.append(f"unmatched task indexes: {unmatched_task_indexes}")
    if unmatched_repeat_dirs:
        errors.append(f"unmatched repeat dirs: {unmatched_repeat_dirs}")

    planned_by_index: dict[int, dict[str, Any]] = {}
    if not errors:
        try:
            planned_by_index = self._load_planned_runs_by_index(eval_root)
        except Exception as exc:
            errors.append(str(exc))
    if not errors:
        errors.extend(self._validate_all_tasks_match_planned(tasks=tasks, planned_by_index=planned_by_index))
    if not errors:
        skip_task_indexes = self._as_int_set(control.get("skip_task_indexes"))
        skip_repeat_dirs = {_resolve_path_text(value) for value in self._as_str_set(control.get("skip_repeat_dirs"))}
        skip_run_dirs = {_resolve_path_text(value) for value in self._as_str_set(control.get("skip_run_dirs"))}
        for task in matched:
            task_repeat_dir = _resolve_path_text(task.repeat_dir)
            if task.task_index in skip_task_indexes:
                errors.append(f"task_index={task.task_index} still present in skip_task_indexes")
            if task_repeat_dir in skip_repeat_dirs:
                errors.append(f"repeat_dir still present in skip_repeat_dirs: {task_repeat_dir}")
            for run_dir in skip_run_dirs:
                try:
                    Path(run_dir).relative_to(Path(task_repeat_dir))
                except Exception:
                    continue
                errors.append(f"skip_run_dirs contains path under rerun repeat_dir: {run_dir}")

    if errors:
        self._append_rerun_request_event(
            eval_root,
            {
                "event": "explicit_rerun_rejected",
                "request_id": request_id,
                "source": source,
                "errors": errors,
            },
        )
        raise ValueError("invalid explicit rerun request: " + "; ".join(errors))

    archives: list[dict[str, Any]] = []
    for task in matched:
        archives.append(
            self._archive_repeat_dir_for_explicit_rerun(
                task=task,
                request_id=request_id,
            )
        )
    self._append_rerun_request_event(
        eval_root,
        {
            "event": "explicit_rerun_prepared",
            "request_id": request_id,
            "source": source,
            "matched_task_indexes": [int(task.task_index) for task in matched],
            "archives": archives,
            "rollback_knowledge": False,
            "knowledge_policy": "keep_current_scope_no_rollback",
            "clean_rerun": False,
        },
    )
    return matched


def _collect_results_from_disk(self: EvaluationHarness, tasks: list[EvalTask]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: item.task_index):
        result = self._load_task_result(task)
        if isinstance(result, dict):
            collected.append(result)
    return collected


def _rewrite_result_indexes(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    tasks: list[EvalTask],
) -> tuple[Path, Path, list[dict[str, Any]]]:
    results_path = eval_root / "results.jsonl"
    errors_path = eval_root / "errors.jsonl"
    results = self._collect_results_from_disk(tasks)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", encoding="utf-8") as rf, errors_path.open("w", encoding="utf-8") as ef:
        for item in results:
            line = json.dumps(item, ensure_ascii=False)
            rf.write(line + "\n")
            if str(item.get("status") or "") not in {"success"}:
                ef.write(line + "\n")
    return results_path, errors_path, results


def _select_tasks_for_execution(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    tasks: list[EvalTask],
    resume: bool,
) -> list[EvalTask]:
    control = self._load_control(eval_root)
    rerun_task_indexes = self._as_int_set(control.get("rerun_task_indexes"))
    rerun_repeat_dirs = {_resolve_path_text(value) for value in self._as_str_set(control.get("rerun_repeat_dirs"))}
    explicit_rerun_tasks = (
        self._prepare_explicit_rerun_tasks(
            eval_root=eval_root,
            tasks=tasks,
            control=control,
            rerun_task_indexes=rerun_task_indexes,
            rerun_repeat_dirs=rerun_repeat_dirs,
        )
        if resume
        else []
    )
    explicit_rerun_indexes = {task.task_index for task in explicit_rerun_tasks}
    if resume and explicit_rerun_tasks and bool(control.get("rerun_only")):
        return list(explicit_rerun_tasks)
    selected: list[EvalTask] = []
    for task in tasks:
        result = self._load_task_result(task)
        if resume:
            if task.task_index in explicit_rerun_indexes:
                selected.append(task)
                continue
            if self._result_requires_resume(result):
                if result is not None and str(result.get("status") or "") in _RESULT_STATUS_RESUMABLE:
                    self._cleanup_repeat_dir_for_rerun(task)
                selected.append(task)
            continue
        if result is not None:
            continue
        selected.append(task)
    return selected


def _build_skipped_result(
    self: EvaluationHarness,
    *,
    task: EvalTask,
    repeat_dir: Path,
    started_at: float,
    reason: str,
) -> dict[str, Any]:
    case = task.case
    completed_at = time.time()
    return {
        "flow_id": case.flow_id,
        "source_id": case.source_id,
        "task_index": task.task_index,
        "task_total": task.task_total,
        "dataset": case.dataset,
        "app_name": case.app_name,
        "apk_name": case.apk_name,
        "classification": case.classification,
        "mode": task.mode,
        "repeat_idx": task.repeat_idx,
        "status": "skipped",
        "return_code": None,
        "timed_out": False,
        "cancelled_by_user": True,
        "skip_reason": reason,
        "started_at": from_timestamp_tz8_iso(started_at),
        "finished_at": from_timestamp_tz8_iso(completed_at),
        "wall_time_seconds": round(completed_at - started_at, 3),
        "repeat_dir": str(repeat_dir),
        "run_dir": None,
        "dummy_run": bool(self.config.dummy_run),
        "ground_truth_sink_categories": sorted({c for c in (case.ground_truth_sink_categories or []) if c}),
        "ground_truth_sink_count": len(case.sink_entries or []),
        "llm_judge": {
            "enabled": bool(self.config.llm_judge_enabled),
            "status": "skipped_task",
            "verdict": "unknown",
            "is_correct": None,
            "confidence": None,
            "score": None,
            "usage": {},
        },
        "metrics": {
            "analysis": {},
            "end_to_end": {},
        },
    }
