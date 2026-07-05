from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from flowark_studio.common.eval_progress import (
    EVAL_OPEN_CODE_COST_KEY,
    rebuild_eval_progress_metadata_from_progress_file,
    rebuild_eval_progress_metadata_from_results_file,
)
from flowark_studio.common.models import StudioTask, TaskStatus
from flowark_studio.process.backend_profiles import apply_backend_selection_param_defaults

_EVAL_PARAM_KEYS = [
    "experiment_preset",
    "model_backend_preset",
    "dataset_preset",
    "input_path",
    "modes",
    "agent_adapter",
    "opencode_binary",
    "opencode_model",
    "opencode_provider",
    "opencode_after_tool_delivery",
    "opencode_bash_policy",
    "opencode_post_phase_mode",
    "opencode_structured_output",
    "parallel",
    "repeats",
    "app_names",
    "out_dir",
    "max_cases",
    "max_apps",
    "max_sources",
    "timeout_seconds",
    "classification_filter",
    "knowledge_mode",
    "auto_knowledge_cycle",
    "runtime_injection_mode",
    "knowledge_allow_repeat_injection_within_session",
    "knowledge_repeat_summary_react_gap",
    "knowledge_repeat_full_react_gap",
    "dummy_run",
    "serialize_within_app",
    "knowledge_distillation_mode",
    "knowledge_packaging_mode",
    "auto_knowledge_validate_mode",
    "knowledge_reuse_digest_mode",
    "knowledge_top_k",
    "knowledge_recall_top_m",
    "runtime_backend_mode",
    "runtime_backend",
    "runtime_backend_base_url",
    "reuse_embed_backend",
    "reuse_rerank_backend",
    "backend_profile",
]


def _stable_task_id_from_name(value: str) -> str:
    text = str(value or "").strip()
    digest = hashlib.blake2s(text.encode("utf-8"), digest_size=6)
    return digest.hexdigest()


def _with_legacy_router_param_aliases(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    legacy_top_k = normalized.get("knowledge_router_final_top_n")
    if "knowledge_top_k" not in normalized and legacy_top_k not in {None, ""}:
        normalized["knowledge_top_k"] = legacy_top_k
    legacy_recall_top_m = normalized.get("knowledge_router_recall_top_m")
    if "knowledge_recall_top_m" not in normalized and legacy_recall_top_m not in {None, ""}:
        normalized["knowledge_recall_top_m"] = legacy_recall_top_m
    return normalized


_BACKEND_AUDIT_FALLBACK_KEYS = (
    "runtime_backend_mode",
    "runtime_backend",
    "reuse_embed_backend",
    "reuse_rerank_backend",
    "backend_profile",
)


def _has_audit_param_value(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _with_backend_param_audit_fallbacks(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    for audit_key in ("effective_params", "requested_params"):
        audit_payload = payload.get(audit_key)
        if not isinstance(audit_payload, dict):
            continue
        for key in _BACKEND_AUDIT_FALLBACK_KEYS:
            value = audit_payload.get(key)
            if (
                not _has_audit_param_value(normalized.get(key))
                and _has_audit_param_value(value)
            ):
                normalized[key] = value
    return normalized


class HistoricalTaskReconstructor:
    def __init__(
        self,
        *,
        workspace_root: Path,
        read_task_group_sidecar: Callable[[Path], list[str]],
        load_eval_state: Callable[[Path], dict[str, Any]],
        normalize_workspace_git_snapshot: Callable[[Any], dict[str, Any] | None],
        normalize_workspace_git_history: Callable[[Any], list[dict[str, Any]]],
    ) -> None:
        self.workspace_root = workspace_root
        self._read_task_group_sidecar = read_task_group_sidecar
        self._load_eval_state = load_eval_state
        self._normalize_workspace_git_snapshot = normalize_workspace_git_snapshot
        self._normalize_workspace_git_history = normalize_workspace_git_history

    def reconstruct_eval_task(self, eval_dir: Path) -> StudioTask | None:
        manifest_path = eval_dir / "manifest.json"
        config_path = eval_dir / "config.json"
        if not manifest_path.exists() and not config_path.exists():
            return None

        manifest = self._load_json_object(manifest_path) or {}
        config = self._load_json_object(config_path) or {}
        if not manifest and not config:
            return None

        dir_name = eval_dir.name
        task_id = _stable_task_id_from_name(dir_name)

        created_at = manifest.get("created_at")
        if not created_at:
            ts_part = dir_name.split("-")[0] if "-" in dir_name else dir_name[:15]
            created_at = ts_part

        param_source = _with_backend_param_audit_fallbacks(
            _with_legacy_router_param_aliases(config)
        )
        params = {key: param_source[key] for key in _EVAL_PARAM_KEYS if key in param_source}
        params = apply_backend_selection_param_defaults(
            workspace_root=self.workspace_root,
            params=params,
            strict_explicit=False,
        )

        status, error_msg = self._detect_eval_status(eval_dir)
        task = StudioTask(
            task_id=task_id,
            kind="eval",
            status=status,
            created_at=created_at,
            params=params,
            command=[],
            cwd=str(self.workspace_root),
            started_at=created_at,
            finished_at=created_at,
            return_code=(0 if status in {"success", "warning"} else None if status in {"running", "paused"} else 143 if status == "cancelled" else 1),
            error=error_msg,
            metadata={
                "historical": True,
                "eval_dir": str(eval_dir),
                "out_dir": str(eval_dir.parent),
                "case_count": manifest.get("case_count", 0),
                "source_count": manifest.get("source_count", 0),
            },
        )
        self._copy_param_audit_metadata(task.metadata, config, allowed_keys=_EVAL_PARAM_KEYS)

        workspace_git_submitted = self._normalize_workspace_git_snapshot(
            manifest.get("workspace_git_submitted") or config.get("workspace_git_submitted")
        )
        workspace_git_started = self._normalize_workspace_git_snapshot(
            manifest.get("workspace_git_started") or config.get("workspace_git_started")
        )
        workspace_git_last_started = self._normalize_workspace_git_snapshot(
            manifest.get("workspace_git_last_started")
            or config.get("workspace_git_last_started")
            or workspace_git_started
        )
        workspace_git_history = self._normalize_workspace_git_history(
            manifest.get("workspace_git_launch_history") or config.get("workspace_git_launch_history")
        )
        if workspace_git_submitted is not None:
            task.metadata["workspace_git_submitted"] = workspace_git_submitted
        if workspace_git_started is not None:
            task.metadata["workspace_git_started"] = workspace_git_started
        if workspace_git_last_started is not None:
            task.metadata["workspace_git_last_started"] = workspace_git_last_started
        if workspace_git_history:
            task.metadata["workspace_git_launch_history"] = workspace_git_history
        stored_tags = self._read_task_group_sidecar(eval_dir)
        if stored_tags:
            task.metadata["tags"] = stored_tags

        summary = self._load_json_object(eval_dir / "summary.json")
        if isinstance(summary, dict):
            self._attach_eval_status_counts(task.metadata, summary)
        total_count = 0
        if isinstance(summary, dict):
            for key in ("task_count", "completed_task_count", "success_count"):
                try:
                    total_count = max(total_count, int(summary.get(key) or 0))
                except Exception:
                    continue
        rebuild_eval_progress_metadata_from_progress_file(task.metadata, eval_dir / "progress.jsonl")
        if EVAL_OPEN_CODE_COST_KEY not in task.metadata:
            rebuild_eval_progress_metadata_from_results_file(
                task.metadata,
                eval_dir / "results.jsonl",
                total_count=total_count,
            )

        task.register_root(eval_dir)
        if config.get("input_path"):
            task.register_root(Path(config["input_path"]).parent)

        task.paths = {
            "eval_dir": str(eval_dir),
            "config": str(config_path),
            "eval_root": str(eval_dir),
        }
        if manifest_path.exists():
            task.paths["manifest"] = str(manifest_path)
        for fname in [
            "summary.json",
            "results.jsonl",
            "progress.jsonl",
            "planned_runs.json",
            "eval_state.json",
            "note_only_artifact_audit.json",
        ]:
            fpath = eval_dir / fname
            if fpath.exists():
                task.paths[fname.replace(".", "_")] = str(fpath)
        return task

    @staticmethod
    def _copy_param_audit_metadata(
        metadata: dict[str, Any],
        source: dict[str, Any],
        *,
        allowed_keys: list[str],
    ) -> None:
        def _filtered_params(payload: dict[str, Any]) -> dict[str, Any]:
            normalized = _with_legacy_router_param_aliases(payload)
            return {key: normalized[key] for key in allowed_keys if key in normalized}

        requested = source.get("requested_params")
        if isinstance(requested, dict):
            metadata["requested_params"] = _filtered_params(requested)
        effective = source.get("effective_params")
        if isinstance(effective, dict):
            metadata["effective_params"] = _filtered_params(effective)
        warnings = source.get("normalization_warnings")
        if isinstance(warnings, list):
            normalized_warnings = [str(item) for item in warnings if str(item).strip()]
            metadata["normalization_warnings"] = normalized_warnings
            if normalized_warnings:
                metadata["parameter_warnings"] = normalized_warnings

    @staticmethod
    def _load_json_object(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _attach_eval_status_counts(metadata: dict[str, Any], summary: dict[str, Any]) -> None:
        counts: dict[str, int] = {}
        for key in ["success_count", "warning_count", "error_count"]:
            try:
                value = int(summary.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                counts[key] = value
        if counts:
            metadata["eval_status_counts"] = counts

    def _detect_eval_status(self, eval_dir: Path) -> tuple[TaskStatus, str | None]:
        status: TaskStatus = "success"
        error_msg = None
        eval_state = self._load_eval_state(eval_dir / "eval_state.json")
        summary_path = eval_dir / "summary.json"
        summary = self._load_json_object(summary_path)
        progress_path = eval_dir / "progress.jsonl"
        planned_runs_path = eval_dir / "planned_runs.json"
        total_runs = 0
        finished_runs = 0

        def _count_from(payload: dict[str, Any] | None, key: str) -> int | None:
            if not isinstance(payload, dict):
                return None
            try:
                value = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        def _terminal_count_from_summary(payload: dict[str, Any] | None) -> int | None:
            if not isinstance(payload, dict):
                return None
            total = 0
            for key in ("success_count", "warning_count", "error_count"):
                try:
                    total += int(payload.get(key) or 0)
                except (TypeError, ValueError):
                    continue
            return total if total > 0 else None

        planned = self._load_json_object(planned_runs_path)
        runs = planned.get("runs") if isinstance(planned, dict) else None
        if isinstance(runs, list):
            total_runs = len(runs)

        if progress_path.exists():
            progress_metadata: dict[str, Any] = {}
            rebuild_eval_progress_metadata_from_progress_file(progress_metadata, progress_path)
            progress = progress_metadata.get("eval_progress")
            if isinstance(progress, dict):
                try:
                    finished_runs = int(progress.get("completed_count") or 0)
                except Exception:
                    finished_runs = 0
                try:
                    total_runs = max(total_runs, int(progress.get("total_count") or 0))
                except Exception:
                    pass

        state_total = _count_from(eval_state, "total_task_count")
        state_finished = _count_from(eval_state, "completed_task_count")
        state_pending = _count_from(eval_state, "pending_task_count")
        summary_total = _count_from(summary, "task_count")
        summary_finished = _count_from(summary, "completed_task_count")
        summary_pending = _count_from(summary, "pending_task_count")
        summary_terminal_count = _terminal_count_from_summary(summary)
        for fallback_total in (state_total, summary_total):
            if fallback_total is not None:
                total_runs = max(total_runs, fallback_total)
        for finished, pending in ((state_finished, state_pending), (summary_finished, summary_pending)):
            if finished is not None:
                finished_runs = max(finished_runs, finished)
            if pending is not None:
                total_runs = max(total_runs, (finished or 0) + pending)
        if summary_terminal_count is not None:
            finished_runs = max(finished_runs, summary_terminal_count)
            if summary_total is None:
                total_runs = max(total_runs, summary_terminal_count)

        interrupted_error = "Eval was interrupted; Studio reconstruction detected that the task did not finish normally"
        if isinstance(eval_state, dict) and eval_state:
            state_status = str(eval_state.get("status") or "").strip().lower()
            if state_status == "cancelled":
                status = "cancelled"
            elif state_status == "paused":
                status = "paused"
            elif state_status == "running":
                status = "error"
                error_msg = interrupted_error
            elif state_status == "completed":
                if total_runs > 0 and finished_runs < total_runs:
                    if bool(eval_state.get("resumable")):
                        status = "paused"
                    else:
                        status = "error"
                        error_msg = interrupted_error
                else:
                    status = "success"
        elif total_runs > 0 and finished_runs < total_runs:
            status = "error"
            error_msg = interrupted_error
        elif not progress_path.exists() and not (total_runs > 0 and finished_runs >= total_runs):
            status = "error"
            error_msg = "Missing progress file"

        if status in {"success", "warning"} and isinstance(summary, dict) and bool(summary.get("incomplete")):
            status = "paused"
        if status == "success" and isinstance(summary, dict):
            if int(summary.get("error_count") or 0) > 0:
                status = "error"
                error_msg = f"case_error_count={int(summary.get('error_count') or 0)}"
            elif int(summary.get("warning_count") or 0) > 0:
                status = "warning"
        if status in {"success", "warning"} and isinstance(summary, dict):
            audit = summary.get("artifact_audit")
            if isinstance(audit, dict) and audit.get("ok") is False:
                status = "error"
                error_msg = self._format_artifact_audit_error(audit)
        if status == "success" and not summary_path.exists():
            if total_runs > 0 and finished_runs < total_runs:
                status = "error"
                error_msg = interrupted_error
            elif isinstance(eval_state, dict) and str(eval_state.get("status") or "").strip().lower() == "running":
                status = "error"
                error_msg = interrupted_error
        return status, error_msg

    @staticmethod
    def _format_artifact_audit_error(audit: dict[str, Any]) -> str:
        errors = int(audit.get("error_count") or 0)
        warnings = int(audit.get("warning_count") or 0)
        message = f"artifact_audit_failed errors={errors} warnings={warnings}"
        top_issues = audit.get("top_issues") if isinstance(audit.get("top_issues"), list) else audit.get("issues")
        if not isinstance(top_issues, list):
            top_issues = []
        for item in top_issues:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            file = str(item.get("file") or "").strip()
            if kind or file:
                suffix = f": {kind}" if kind else ""
                if file:
                    suffix += f" ({file})"
                return (message + suffix)[:500]
        return message[:500]
