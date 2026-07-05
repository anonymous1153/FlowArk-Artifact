"""Batch evaluation harness orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flowark.anthropic_env import (
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)
from flowark.git_snapshot import (
    FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV,
    FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_ENV,
    build_git_launch_history,
    capture_workspace_git_snapshot,
    load_git_snapshot_from_env,
    normalize_git_snapshot,
)
from flowark.timeutil import from_timestamp_tz8_iso

from . import control, judge, knowledge, reporting, task_runner
from .backend_sidecar import (
    apply_backend_sidecars_to_config,
    load_backend_sidecars,
    write_backend_sidecars,
)
from .cases import (
    _count_apps,
    effective_case_sink_categories,
    load_cases,
    resolve_eval_input_paths,
    select_cases,
)
from .common import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    _RESULT_STATUS_COMPLETED,
    _RESULT_STATUS_RESUMABLE,
    _append_jsonl,
    _json_dump,
    _json_load,
    _normalize_optional_positive_int,
    _now_utc_iso,
    _safe_int,
)
from .models import (
    EvalCase,
    EvalTask,
    EvaluationConfig,
    validate_embedding_packaging_backend_config,
)
from .session_pool import GlobalEvalSessionPoolController

_SECRET_CONFIG_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "auth_token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "authorization",
)


def _is_secret_config_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SECRET_CONFIG_KEY_FRAGMENTS)


def _redact_config_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_secret_config_key(key):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_config_secrets(item)
        return redacted
    if isinstance(value, list):
        return [_redact_config_secrets(item) for item in value]
    return value


def _env_text(key: str) -> str:
    return str(os.environ.get(key) or "").strip()


def _apply_runtime_backend_env_fallback(config: EvaluationConfig) -> EvaluationConfig:
    if str(config.runtime_backend_mode or "single").strip().lower() != "single":
        return config
    runtime_backend_marker = _env_text(STUDIO_BACKEND_PROFILE_ENV)
    if runtime_backend_marker and not config.runtime_backend_profile:
        config.runtime_backend_profile = runtime_backend_marker
    if not config.runtime_backend_base_url:
        config.runtime_backend_base_url = (
            _env_text(STUDIO_RUNTIME_BASE_URL_ENV)
            or _env_text("ANTHROPIC_BASE_URL")
            or _env_text("OPENAI_BASE_URL")
            or None
        )
    if not config.runtime_backend_auth_token:
        config.runtime_backend_auth_token = (
            _env_text(STUDIO_RUNTIME_AUTH_TOKEN_ENV)
            or _env_text("ANTHROPIC_AUTH_TOKEN")
            or _env_text("OPENAI_API_KEY")
            or None
        )
    if not config.runtime_backend_model:
        config.runtime_backend_model = (
            _env_text(STUDIO_RUNTIME_MODEL_ENV)
            or _env_text("ANTHROPIC_MODEL")
            or _env_text("OPENAI_MODEL")
            or None
        )
    return config


class EvaluationHarness:
    _control_path = staticmethod(control._control_path)
    _eval_state_path = staticmethod(control._eval_state_path)
    _default_control_payload = staticmethod(control._default_control_payload)
    _default_eval_state_payload = staticmethod(control._default_eval_state_payload)
    _load_control = control._load_control
    _write_control = control._write_control
    _load_eval_state = control._load_eval_state
    _write_eval_state = control._write_eval_state
    _as_str_set = staticmethod(control._as_str_set)
    _as_int_set = staticmethod(control._as_int_set)
    _is_task_skip_requested = control._is_task_skip_requested
    _is_pause_requested = control._is_pause_requested
    _is_task_force_abort_requested = control._is_task_force_abort_requested
    _result_path_for_task = staticmethod(control._result_path_for_task)
    _load_task_result = control._load_task_result
    _result_is_final = staticmethod(control._result_is_final)
    _result_requires_resume = staticmethod(control._result_requires_resume)
    _completed_task_count_from_results = staticmethod(control._completed_task_count_from_results)
    _resumable_task_count_from_results = staticmethod(control._resumable_task_count_from_results)
    _task_matches_control_set = control._task_matches_control_set
    _rerun_requests_path = staticmethod(control._rerun_requests_path)
    _append_rerun_request_event = control._append_rerun_request_event
    _load_planned_runs_by_index = control._load_planned_runs_by_index
    _validate_all_tasks_match_planned = control._validate_all_tasks_match_planned
    _archive_repeat_dir_for_explicit_rerun = control._archive_repeat_dir_for_explicit_rerun
    _prepare_explicit_rerun_tasks = control._prepare_explicit_rerun_tasks
    _cleanup_repeat_dir_for_rerun = control._cleanup_repeat_dir_for_rerun
    _collect_results_from_disk = control._collect_results_from_disk
    _rewrite_result_indexes = control._rewrite_result_indexes
    _select_tasks_for_execution = control._select_tasks_for_execution
    _build_skipped_result = control._build_skipped_result

    _knowledge_dirs = knowledge._knowledge_dirs
    _extract_candidate_artifacts_from_run_dir = staticmethod(knowledge._extract_candidate_artifacts_from_run_dir)
    _rollback_run_dir_knowledge_side_effects = knowledge._rollback_run_dir_knowledge_side_effects
    _prepare_knowledge_scope = knowledge._prepare_knowledge_scope

    _rebuild_summary_payload = reporting._rebuild_summary_payload
    _write_eval_artifact_audit = reporting._write_eval_artifact_audit
    _shorten = staticmethod(reporting._shorten)
    _format_task_source_meta = reporting._format_task_source_meta
    _emit_progress_start = reporting._emit_progress_start
    _emit_progress_finish = reporting._emit_progress_finish
    _dataset_slug = reporting._dataset_slug
    _modes_slug = staticmethod(reporting._modes_slug)
    _agent_adapter_slug = reporting._agent_adapter_slug
    _knowledge_mode_slug = reporting._knowledge_mode_slug
    _auto_knowledge_cycle_slug = reporting._auto_knowledge_cycle_slug
    _distillation_mode_slug = reporting._distillation_mode_slug
    _packaging_mode_slug = reporting._packaging_mode_slug
    _runtime_injection_mode_slug = reporting._runtime_injection_mode_slug
    _validation_mode_slug = reporting._validation_mode_slug
    _reuse_digest_mode_slug = reporting._reuse_digest_mode_slug
    _prepare_eval_root = reporting._prepare_eval_root
    _build_tasks = reporting._build_tasks
    _task_entry_id = staticmethod(reporting._task_entry_id)
    _write_planned_runs = reporting._write_planned_runs
    _extract_numeric = staticmethod(reporting._extract_numeric)
    _build_mode_summary = reporting._build_mode_summary
    _metric_mean = staticmethod(reporting._metric_mean)
    _build_comparison = reporting._build_comparison

    _build_run_command = task_runner._build_run_command
    _collect_run_dir = staticmethod(task_runner._collect_run_dir)
    _collect_mem0_usage_after_child_exit = staticmethod(task_runner._collect_mem0_usage_after_child_exit)
    _extract_run_metrics = staticmethod(task_runner._extract_run_metrics)
    _dummy_sink_label = staticmethod(task_runner._dummy_sink_label)
    _write_dummy_run_artifacts = task_runner._write_dummy_run_artifacts
    _stream_subprocess_pipe = staticmethod(task_runner._stream_subprocess_pipe)
    _run_one_task = task_runner._run_one_task

    _normalize_openai_base_url = staticmethod(judge._normalize_openai_base_url)
    _load_final_report_for_judge = staticmethod(judge._load_final_report_for_judge)
    _build_ground_truth_for_judge = staticmethod(judge._build_ground_truth_for_judge)
    _openai_chat_completion = staticmethod(judge._openai_chat_completion)

    def __init__(self, config: EvaluationConfig, *, workspace_root: Path | None = None) -> None:
        validate_embedding_packaging_backend_config(config)
        self.config = config
        self.workspace_root = Path(workspace_root or Path(__file__).resolve().parents[3]).resolve()
        self._io_lock = asyncio.Lock()
        self._knowledge_scope_root: Path | None = None

    @staticmethod
    def _now_utc_iso_for_runner() -> str:
        return _now_utc_iso()

    @staticmethod
    def _from_timestamp_for_runner(value: float) -> str:
        return from_timestamp_tz8_iso(value)

    def _effective_task_timeout_seconds(self) -> int:
        value = _safe_int(self.config.timeout_seconds)
        if value is None or value <= 0:
            return DEFAULT_TASK_TIMEOUT_SECONDS
        return int(value)

    async def _run_llm_judge(
        self,
        *,
        case: EvalCase,
        task: EvalTask,
        run_dir: Path | None,
        repeat_dir: Path,
        query: str,
        source_desc: str,
        sink_types: list[str],
    ) -> dict[str, Any]:
        return await judge._run_llm_judge(
            self.config,
            case=case,
            task=task,
            run_dir=run_dir,
            repeat_dir=repeat_dir,
            query=query,
            source_desc=source_desc,
            sink_types=sink_types,
        )

    async def _run_existing_eval(
        self,
        *,
        eval_root: Path,
        cases: list[EvalCase],
        tasks: list[EvalTask],
        modes: list[str],
        selected_app_count: int,
        selection_info: dict[str, Any],
        knowledge_scope_info: dict[str, Any],
        resume: bool,
    ) -> dict[str, Any]:
        progress_path = eval_root / "progress.jsonl"
        total_tasks = len(tasks)
        started_count = 0
        finished_count = 0
        active_count = 0
        active_task_indexes: set[int] = set()
        active_repeat_dirs: set[str] = set()
        resume_count = int(self._load_eval_state(eval_root).get("resume_count") or 0)
        if resume:
            resume_count += 1
        tasks_to_run = self._select_tasks_for_execution(eval_root=eval_root, tasks=tasks, resume=resume)
        control_payload = self._load_control(eval_root)
        rerun_only_mode = bool(control_payload.get("rerun_only"))
        if resume:
            control_payload.update(
                {
                    "pause_after_active": False,
                    "pause_mode": "none",
                    "force_abort_task_indexes": [],
                    "force_abort_repeat_dirs": [],
                    "rerun_task_indexes": [],
                    "rerun_repeat_dirs": [],
                    "rerun_request_id": None,
                    "rerun_source": None,
                    "rerun_requested_at": None,
                    "rerun_only": None,
                }
            )
            self._write_control(eval_root, control_payload)
        existing_results = self._collect_results_from_disk(tasks)
        self._write_eval_state(
            eval_root,
            total_tasks=total_tasks,
            completed_task_count=self._completed_task_count_from_results(existing_results),
            pending_task_count=max(0, total_tasks - self._completed_task_count_from_results(existing_results)),
            running_task_count=0,
            active_task_indexes=[],
            active_repeat_dirs=[],
            status="running",
            pause_mode="none",
            resumable=False,
            resume_count=resume_count,
        )

        dispatch_lock = asyncio.Lock()
        pending_tasks = list(tasks_to_run)
        dispatch_event = asyncio.Event()
        dispatch_event.set()
        active_app_keys: set[str] = set()
        app_queues: dict[str, list[EvalTask]] = {}
        app_order: list[str] = []
        next_app_cursor = 0

        def _task_app_key(task: EvalTask) -> str:
            return (str(task.case.app_name or "").strip() or "unknown").casefold()

        if self.config.serialize_within_app:
            for task in pending_tasks:
                app_key = _task_app_key(task)
                if app_key not in app_queues:
                    app_queues[app_key] = []
                    app_order.append(app_key)
                app_queues[app_key].append(task)

        async def _write_runtime_state(*, status: str | None = None, pause_mode: str | None = None) -> None:
            completed_results = self._collect_results_from_disk(tasks)
            completed_task_count = self._completed_task_count_from_results(completed_results)
            running_task_count = len(active_task_indexes)
            pending_task_count = max(0, total_tasks - completed_task_count - running_task_count)
            self._write_eval_state(
                eval_root,
                total_tasks=total_tasks,
                completed_task_count=completed_task_count,
                pending_task_count=pending_task_count,
                running_task_count=running_task_count,
                active_task_indexes=list(active_task_indexes),
                active_repeat_dirs=list(active_repeat_dirs),
                status=status or "running",
                pause_mode=pause_mode or "none",
                resumable=bool(
                    pending_task_count > 0
                    or any(str(item.get("status") or "") in _RESULT_STATUS_RESUMABLE for item in completed_results)
                ),
                resume_count=resume_count,
            )

        def _remaining_pending_count_locked() -> int:
            if self.config.serialize_within_app:
                return sum(len(queue) for queue in app_queues.values())
            return len(pending_tasks)

        def _peek_task_locked() -> tuple[EvalTask | None, bool]:
            nonlocal next_app_cursor
            pause_requested, _pause_mode = self._is_pause_requested(eval_root)
            if pause_requested:
                return None, False
            if self.config.serialize_within_app:
                remaining_pending = _remaining_pending_count_locked()
                if remaining_pending <= 0:
                    return None, False
                app_count = len(app_order)
                blocked_by_app_serialization = False
                for offset in range(app_count):
                    if app_count <= 0:
                        break
                    idx = (next_app_cursor + offset) % app_count
                    app_key = app_order[idx]
                    queue = app_queues.get(app_key) or []
                    if not queue:
                        continue
                    if app_key in active_app_keys:
                        blocked_by_app_serialization = True
                        continue
                    return queue[0], False
                return None, blocked_by_app_serialization
            if not pending_tasks:
                return None, False
            return pending_tasks[0], False

        async def _peek_task() -> tuple[EvalTask | None, bool]:
            async with dispatch_lock:
                task, blocked_by_app_serialization = _peek_task_locked()
                if task is None and blocked_by_app_serialization:
                    dispatch_event.clear()
                return task, blocked_by_app_serialization

        async def _claim_task(expected: EvalTask) -> EvalTask | None:
            nonlocal next_app_cursor
            async with dispatch_lock:
                pause_requested, _pause_mode = self._is_pause_requested(eval_root)
                if pause_requested:
                    return None
                task: EvalTask | None = None
                if self.config.serialize_within_app:
                    app_key = _task_app_key(expected)
                    queue = app_queues.get(app_key) or []
                    if not queue or queue[0] is not expected or app_key in active_app_keys:
                        return None
                    task = queue.pop(0)
                    active_app_keys.add(app_key)
                    if app_order:
                        with contextlib.suppress(ValueError):
                            next_app_cursor = (app_order.index(app_key) + 1) % len(app_order)
                else:
                    if not pending_tasks or pending_tasks[0] is not expected:
                        return None
                    task = pending_tasks.pop(0)
                active_task_indexes.add(task.task_index)
                active_repeat_dirs.add(str(task.repeat_dir))
                await _write_runtime_state()
                return task

        async def worker(task: EvalTask) -> dict[str, Any]:
            mode_lower = str(task.mode or "").strip().lower()
            if mode_lower == "native":
                mode_lower = "naive"

            return await _run_worker_body(task)

        async def _run_worker_body(
            task: EvalTask,
        ) -> dict[str, Any]:
            nonlocal started_count, finished_count, active_count
            async with self._io_lock:
                started_count += 1
                active_count += 1
                _append_jsonl(
                    progress_path,
                    {
                        "event": "start",
                        "ts": _now_utc_iso(),
                        "entry_id": self._task_entry_id(task),
                        "task_index": task.task_index,
                        "task_total": task.task_total,
                        "flow_id": task.case.flow_id,
                        "source_id": task.case.source_id,
                        "mode": task.mode,
                        "repeat_idx": task.repeat_idx,
                        "repeat_dir": str(task.repeat_dir),
                        "status": "running",
                        "exec_state": "running",
                    },
                )
                self._emit_progress_start(
                    task=task,
                    started_count=started_count,
                    total=total_tasks,
                    active_count=active_count,
            )
            result: dict[str, Any] | None = None
            try:
                result = await self._run_one_task(task, eval_root=eval_root)
            except Exception as exc:  # pragma: no cover - defensive
                result = {
                    "entry_id": self._task_entry_id(task),
                    "flow_id": task.case.flow_id,
                    "source_id": task.case.source_id,
                    "task_index": task.task_index,
                    "task_total": task.task_total,
                    "dataset": task.case.dataset,
                    "app_name": task.case.app_name,
                    "mode": task.mode,
                    "repeat_idx": task.repeat_idx,
                    "status": "harness_error",
                    "repeat_dir": str(task.repeat_dir),
                    "error": str(exc),
                }
                _json_dump(task.repeat_dir / "result.json", result)
            finally:
                if result is not None:
                    async with self._io_lock:
                        finished_count += 1
                        active_count = max(0, active_count - 1)
                        status = str(result.get("status") or "unknown")
                        exec_state = "completed" if status in (_RESULT_STATUS_COMPLETED | _RESULT_STATUS_RESUMABLE) else "running"
                        _append_jsonl(progress_path, {"event": "finish", "ts": _now_utc_iso(), "exec_state": exec_state, **result})
                        self._emit_progress_finish(
                            task=task,
                            result=result,
                            finished_count=finished_count,
                            total=total_tasks,
                            active_count=active_count,
                        )
                else:
                    async with self._io_lock:
                        active_count = max(0, active_count - 1)
                async with dispatch_lock:
                    active_task_indexes.discard(task.task_index)
                    active_repeat_dirs.discard(str(task.repeat_dir))
                    active_app_keys.discard(_task_app_key(task))
                    await _write_runtime_state()
                    dispatch_event.set()
            if result is None:
                raise asyncio.CancelledError()
            return result

        worker_count = min(max(1, int(self.config.parallel)), max(1, len(tasks_to_run)))
        requested_parallel = min(max(1, int(self.config.parallel)), len(tasks_to_run)) if tasks_to_run else 0
        pool_controller = (
            GlobalEvalSessionPoolController.from_env(
                workspace_root=self.workspace_root,
                eval_root=eval_root,
                requested_parallel=requested_parallel,
                label=f"eval · {eval_root.name}",
            )
            if requested_parallel > 0
            else None
        )
        if pool_controller is not None:
            await pool_controller.open()

        async def worker_loop(worker_idx: int) -> None:
            worker_slot_id = f"worker-{worker_idx + 1}"
            while True:
                slot_acquired = False
                candidate_task, blocked_by_app_serialization = await _peek_task()
                if candidate_task is None:
                    pause_requested, _ = self._is_pause_requested(eval_root)
                    if pause_requested:
                        return
                    if self.config.serialize_within_app:
                        async with dispatch_lock:
                            remaining_pending = _remaining_pending_count_locked()
                        if remaining_pending > 0:
                            if blocked_by_app_serialization:
                                await dispatch_event.wait()
                            else:
                                await asyncio.sleep(0.05)
                            continue
                    return
                if pool_controller is not None:
                    await pool_controller.wait_for_slot(worker_slot_id)
                    slot_acquired = True
                try:
                    task = await _claim_task(candidate_task)
                    if task is None:
                        if pool_controller is not None and slot_acquired:
                            await pool_controller.release_slot(worker_slot_id)
                            slot_acquired = False
                        await asyncio.sleep(0.05)
                        continue
                    await worker(task)
                finally:
                    if pool_controller is not None and slot_acquired:
                        await pool_controller.release_slot(worker_slot_id)

        worker_tasks = [asyncio.create_task(worker_loop(idx)) for idx in range(worker_count)]
        try:
            worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)
        finally:
            if pool_controller is not None:
                await pool_controller.close()
        worker_failures = [
            item
            for item in worker_results
            if isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError)
        ]
        if worker_failures:
            first_failure = worker_failures[0]
            _, _, results = self._rewrite_result_indexes(eval_root=eval_root, tasks=tasks)
            summary, comparison = self._rebuild_summary_payload(
                eval_root=eval_root,
                tasks=tasks,
                results=results,
                modes=modes,
                selected_app_count=selected_app_count,
                selection_info=selection_info,
                knowledge_scope_info=knowledge_scope_info,
                paused=True,
                pause_mode="interrupted",
            )
            artifact_audit = self._write_eval_artifact_audit(eval_root=eval_root, modes=modes)
            if artifact_audit is not None:
                summary["artifact_audit"] = artifact_audit
            _json_dump(eval_root / "summary.json", summary)
            _json_dump(eval_root / "comparison.json", comparison)
            await _write_runtime_state(status="paused", pause_mode="interrupted")
            raise RuntimeError(f"eval worker failed: {first_failure}") from first_failure

        results_path, _, results = self._rewrite_result_indexes(eval_root=eval_root, tasks=tasks)
        pause_requested, pause_mode = self._is_pause_requested(eval_root)
        completed_task_count = self._completed_task_count_from_results(results)
        resumable_task_count = self._resumable_task_count_from_results(results)
        has_unfinished_tasks = completed_task_count < total_tasks or resumable_task_count > 0
        paused = bool(has_unfinished_tasks and pause_requested)
        if rerun_only_mode and has_unfinished_tasks:
            paused = True
            pause_mode = "rerun_only"
        elif has_unfinished_tasks and not paused:
            paused = True
            pause_mode = "interrupted"
        summary, comparison = self._rebuild_summary_payload(
            eval_root=eval_root,
            tasks=tasks,
            results=results,
            modes=modes,
            selected_app_count=selected_app_count,
            selection_info=selection_info,
            knowledge_scope_info=knowledge_scope_info,
            paused=paused,
            pause_mode=pause_mode,
        )
        artifact_audit = self._write_eval_artifact_audit(eval_root=eval_root, modes=modes)
        if artifact_audit is not None:
            summary["artifact_audit"] = artifact_audit
        _json_dump(eval_root / "summary.json", summary)
        _json_dump(eval_root / "comparison.json", comparison)
        await _write_runtime_state(status=("paused" if paused else "completed"), pause_mode=(pause_mode if paused else "none"))
        return {
            "eval_root": str(eval_root),
            "summary_path": str(eval_root / "summary.json"),
            "comparison_path": str(eval_root / "comparison.json"),
            "results_path": str(results_path),
            "config_path": str(eval_root / "config.json"),
            "manifest_path": str(eval_root / "manifest.json"),
            "task_count": len(tasks),
            "case_count": len(cases),
            "source_count": len(cases),
            "app_count": selected_app_count,
            "dummy_run": bool(self.config.dummy_run),
            "knowledge_scope": knowledge_scope_info,
            "modes": modes,
            "paused": paused,
            "pause_mode": pause_mode if paused else "none",
            "success_count": int(summary.get("success_count") or 0),
            "warning_count": int(summary.get("warning_count") or 0),
            "error_count": int(summary.get("error_count") or 0),
            "artifact_audit": artifact_audit,
        }

    async def run(self) -> dict[str, Any]:
        modes = self.config.normalized_modes()
        max_apps = self.config.normalized_max_apps()
        max_sources = self.config.effective_max_sources()
        app_names = self.config.normalized_app_names()
        classification_filter = self.config.normalized_classification_filter()
        resolved_input_path = Path(self.config.input_path).expanduser().resolve()
        input_files = resolve_eval_input_paths(resolved_input_path)

        all_cases = load_cases(resolved_input_path)
        cases = select_cases(
            all_cases,
            max_apps=max_apps,
            max_sources=max_sources,
            app_names=app_names,
            classification_filter=classification_filter,
        )
        if not cases:
            raise ValueError("未解析到任何有效样本")

        all_source_count = len(all_cases)
        selected_source_count = len(cases)
        all_app_count = _count_apps(all_cases)
        selected_app_count = _count_apps(cases)
        selection_info = {
            "classification_filter": classification_filter,
            "app_names": app_names,
            "max_apps": max_apps,
            "max_sources": max_sources,
            "dummy_run": bool(self.config.dummy_run),
            "raw_source_count": all_source_count,
            "raw_app_count": all_app_count,
            "selected_source_count": selected_source_count,
            "selected_app_count": selected_app_count,
        }
        workspace_git_submitted = load_git_snapshot_from_env(FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_ENV)
        workspace_git_started = (
            load_git_snapshot_from_env(FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV)
            or capture_workspace_git_snapshot(self.workspace_root)
        )

        eval_root = self._prepare_eval_root(cases, modes)
        knowledge_scope_info = self._prepare_knowledge_scope(
            eval_root=eval_root,
            cases=cases,
            modes=modes,
        )
        tasks = self._build_tasks(cases, modes, eval_root)
        planned_runs_path = self._write_planned_runs(eval_root=eval_root, tasks=tasks)
        control_path = self._control_path(eval_root)
        if not control_path.exists():
            self._write_control(eval_root, {})

        config_payload = asdict(self.config)
        config_payload["input_path"] = str(resolved_input_path)
        config_payload["input_is_directory"] = resolved_input_path.is_dir()
        config_payload["input_file_count"] = len(input_files)
        config_payload["input_files"] = [str(path) for path in input_files]
        config_payload["out_dir"] = str(Path(self.config.out_dir).expanduser().resolve())
        config_payload["knowledge_distillation_mode"] = str(
            getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
            or "with_selection_rules"
        )
        config_payload["knowledge_packaging_mode"] = str(
            getattr(self.config, "knowledge_packaging_mode", "dsl_rule") or "dsl_rule"
        )
        config_payload["knowledge_reuse_digest_mode"] = str(
            self.config.knowledge_reuse_digest_mode or "off"
        )
        config_payload["modes"] = modes
        config_payload["classification_filter"] = classification_filter
        config_payload["app_names"] = app_names
        config_payload["max_apps"] = max_apps
        config_payload["max_sources"] = max_sources
        config_payload["effective_max_sources"] = max_sources
        config_payload["knowledge_scope"] = knowledge_scope_info
        config_payload = _redact_config_secrets(config_payload)
        _json_dump(eval_root / "config.json", config_payload)
        write_backend_sidecars(eval_root, self.config)
        manifest_payload = {
            "created_at": _now_utc_iso(),
            "workspace_root": str(self.workspace_root),
            "input_path": str(resolved_input_path),
            "input_is_directory": resolved_input_path.is_dir(),
            "input_file_count": len(input_files),
            "input_files": [str(path) for path in input_files],
            "case_count": len(cases),
            "source_count": len(cases),
            "app_count": selected_app_count,
            "task_count": len(tasks),
            "modes": modes,
            "parallel": self.config.parallel,
            "repeats": self.config.repeats,
            "dummy_run": bool(self.config.dummy_run),
            "selection": selection_info,
            "knowledge_scope": knowledge_scope_info,
            "planned_runs_json": str(planned_runs_path),
            "control_json": str(control_path),
            "cases": [
                {
                    "flow_id": c.flow_id,
                    "source_id": c.source_id,
                    "benchmark_family": c.benchmark_family,
                    "dataset": c.dataset,
                    "app_name": c.app_name,
                    "apk_name": c.apk_name,
                    "source_dir": c.source_dir,
                    "target_sink_categories": effective_case_sink_categories(
                        c,
                        fallback=list(self.config.sink_categories),
                    ),
                    "sink_count": len(c.sink_entries or []),
                    "true_sink_count": sum(
                        1
                        for item in (c.sink_entries or [])
                        if isinstance(item, dict)
                        and str(item.get("classification") or "").strip().upper() == "TRUE"
                    ),
                    "false_sink_count": sum(
                        1
                        for item in (c.sink_entries or [])
                        if isinstance(item, dict)
                        and str(item.get("classification") or "").strip().upper() == "FALSE"
                    ),
                    "ground_truth_sink_categories": list(c.ground_truth_sink_categories or []),
                }
                for c in cases
            ],
        }
        if workspace_git_submitted is not None:
            manifest_payload["workspace_git_submitted"] = workspace_git_submitted
        if workspace_git_started is not None:
            manifest_payload["workspace_git_started"] = workspace_git_started
            manifest_payload["workspace_git_last_started"] = workspace_git_started
            manifest_payload["workspace_git_launch_history"] = build_git_launch_history(
                [],
                action="eval_run",
                snapshot=workspace_git_started,
            )
        _json_dump(eval_root / "manifest.json", manifest_payload)

        return await self._run_existing_eval(
            eval_root=eval_root,
            cases=cases,
            tasks=tasks,
            modes=modes,
            selected_app_count=selected_app_count,
            selection_info=selection_info,
            knowledge_scope_info=knowledge_scope_info,
            resume=False,
        )

    async def resume(self, *, eval_root: Path) -> dict[str, Any]:
        eval_root = Path(eval_root).expanduser().resolve()
        config_path = eval_root / "config.json"
        manifest_path = eval_root / "manifest.json"
        if not config_path.exists():
            raise ValueError(f"缺少 config.json: {config_path}")
        config_payload = _json_load(config_path)
        manifest_payload = _json_load(manifest_path) if manifest_path.exists() else {}
        if not isinstance(config_payload, dict):
            raise ValueError(f"无效 config.json: {config_path}")
        if not isinstance(manifest_payload, dict):
            manifest_payload = {}
        workspace_git_started = (
            load_git_snapshot_from_env(FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV)
            or capture_workspace_git_snapshot(self.workspace_root)
        )
        existing_workspace_git_started = normalize_git_snapshot(manifest_payload.get("workspace_git_started"))
        if existing_workspace_git_started is None and workspace_git_started is not None:
            manifest_payload["workspace_git_started"] = workspace_git_started
        if workspace_git_started is not None:
            manifest_payload["workspace_git_last_started"] = workspace_git_started
            manifest_payload["workspace_git_launch_history"] = build_git_launch_history(
                manifest_payload.get("workspace_git_launch_history"),
                action="eval_resume",
                snapshot=workspace_git_started,
            )
        _json_dump(manifest_path, manifest_payload)

        input_path = Path(str(config_payload.get("input_path") or "")).expanduser().resolve()
        if not str(input_path):
            raise ValueError(f"{config_path}: 缺少 input_path")
        modes = [str(v) for v in (config_payload.get("modes") or []) if str(v).strip()] or self.config.normalized_modes()
        classification_filter = str(config_payload.get("classification_filter") or "all")
        app_names = [str(v) for v in (config_payload.get("app_names") or []) if str(v).strip()]
        max_apps = _normalize_optional_positive_int(config_payload.get("max_apps"), field_name="max_apps") if config_payload.get("max_apps") not in {None, ""} else None
        max_sources = _normalize_optional_positive_int(config_payload.get("max_sources"), field_name="max_sources") if config_payload.get("max_sources") not in {None, ""} else None

        all_cases = load_cases(input_path)
        cases = select_cases(
            all_cases,
            max_apps=max_apps,
            max_sources=max_sources,
            app_names=app_names,
            classification_filter=classification_filter,
        )
        if not cases:
            raise ValueError("resume 未解析到任何有效样本")

        selection_info = manifest_payload.get("selection") if isinstance(manifest_payload.get("selection"), dict) else {
            "classification_filter": classification_filter,
            "app_names": app_names,
            "max_apps": max_apps,
            "max_sources": max_sources,
            "dummy_run": bool(self.config.dummy_run),
            "raw_source_count": len(all_cases),
            "raw_app_count": _count_apps(all_cases),
            "selected_source_count": len(cases),
            "selected_app_count": _count_apps(cases),
        }
        knowledge_scope_info = manifest_payload.get("knowledge_scope") if isinstance(manifest_payload.get("knowledge_scope"), dict) else {}
        self._knowledge_scope_root = (eval_root / "knowledge_scope").resolve()
        for directory in [
            self._knowledge_scope_root / "skills",
            self._knowledge_scope_root / "egress",
            self._knowledge_scope_root / "provenance",
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        tasks = self._build_tasks(cases, modes, eval_root)
        planned_runs_path = eval_root / "planned_runs.json"
        control_payload = self._load_control(eval_root)
        has_explicit_rerun_control = bool(
            self._as_int_set(control_payload.get("rerun_task_indexes"))
            or self._as_str_set(control_payload.get("rerun_repeat_dirs"))
        )
        if not planned_runs_path.exists() and has_explicit_rerun_control:
            raise ValueError(f"显式 rerun 需要已有 planned_runs.json: {planned_runs_path}")
        if not planned_runs_path.exists():
            self._write_planned_runs(eval_root=eval_root, tasks=tasks)
        if not self._control_path(eval_root).exists():
            self._write_control(eval_root, {})

        return await self._run_existing_eval(
            eval_root=eval_root,
            cases=cases,
            tasks=tasks,
            modes=modes,
            selected_app_count=int(manifest_payload.get("app_count") or _count_apps(cases)),
            selection_info=selection_info,
            knowledge_scope_info=knowledge_scope_info,
            resume=True,
        )


async def run_evaluation(config: EvaluationConfig, *, workspace_root: Path | None = None) -> dict[str, Any]:
    harness = EvaluationHarness(config=config, workspace_root=workspace_root)
    return await harness.run()


def load_evaluation_config_from_eval_root(eval_root: Path) -> EvaluationConfig:
    eval_root = Path(eval_root).expanduser().resolve()
    config_path = eval_root / "config.json"
    if not config_path.exists():
        raise ValueError(f"缺少 config.json: {config_path}")
    payload = _json_load(config_path)
    if not isinstance(payload, dict):
        raise ValueError(f"无效 config.json: {config_path}")
    legacy_enable_mcp = payload.get("enable_mcp")
    if isinstance(legacy_enable_mcp, str):
        legacy_enable_mcp_active = legacy_enable_mcp.strip().lower() in {"1", "true", "yes", "on"}
    else:
        legacy_enable_mcp_active = bool(legacy_enable_mcp)
    if legacy_enable_mcp_active:
        raise ValueError("当前版本不支持恢复依赖 enable_mcp=true 的旧评测目录，请迁移或放弃 resume")
    snapshot, secrets = load_backend_sidecars(eval_root)
    dummy_run = bool(payload.get("dummy_run"))
    llm_judge_enabled = bool(payload.get("llm_judge_enabled", True))

    def _nonnegative_int_payload(key: str, default: int) -> int:
        value = payload.get(key)
        if value in {None, ""}:
            return max(0, int(default))
        return max(0, int(value))

    def _positive_int_payload(key: str, legacy_key: str, default: int) -> int:
        value = payload.get(key)
        if value in {None, ""}:
            value = payload.get(legacy_key)
        if value in {None, ""}:
            return max(1, int(default))
        return max(1, int(value))

    def _bool_payload(key: str, default: bool = False) -> bool:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if value in {None, ""}:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _dict_payload(key: str) -> dict[str, Any]:
        value = payload.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def _list_payload(key: str) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    config = EvaluationConfig(
        input_path=Path(str(payload.get("input_path") or "")).expanduser().resolve(),
        out_dir=Path(str(payload.get("out_dir") or eval_root.parent)).expanduser().resolve(),
        agent_adapter=str(payload.get("agent_adapter") or "opencode"),
        opencode_binary=(
            str(payload.get("opencode_binary")).strip()
            if payload.get("opencode_binary") not in {None, ""}
            else None
        ),
        opencode_model=str(payload.get("opencode_model") or ""),
        opencode_provider=str(payload.get("opencode_provider") or "anthropic"),
        opencode_after_tool_delivery=str(payload.get("opencode_after_tool_delivery") or "no_reply_context"),
        opencode_bash_policy=str(payload.get("opencode_bash_policy") or "read_only_guarded"),
        opencode_post_phase_mode=str(payload.get("opencode_post_phase_mode") or "plain_json_same_surface"),
        opencode_structured_output=bool(payload.get("opencode_structured_output", True)),
        modes=[str(v) for v in (payload.get("modes") or ["naive", "flowark"]) if str(v).strip()],
        parallel=max(1, int(payload.get("parallel") or 1)),
        serialize_within_app=bool(payload.get("serialize_within_app", True)),
        repeats=max(1, int(payload.get("repeats") or 1)),
        max_cases=(int(payload["max_cases"]) if payload.get("max_cases") not in {None, ""} else None),
        max_apps=(int(payload["max_apps"]) if payload.get("max_apps") not in {None, ""} else None),
        max_sources=(int(payload["max_sources"]) if payload.get("max_sources") not in {None, ""} else None),
        app_names=[str(v) for v in (payload.get("app_names") or []) if str(v).strip()],
        classification_filter=str(payload.get("classification_filter") or "all"),
        dummy_run=dummy_run,
        knowledge_mode=str(payload.get("knowledge_mode") or "warm"),
        knowledge_allow_repeat_injection_within_session=bool(payload.get("knowledge_allow_repeat_injection_within_session", True)),
        auto_knowledge_cycle=bool(payload.get("auto_knowledge_cycle", True)),
        runtime_injection_mode=str(payload.get("runtime_injection_mode") or "context_aware"),
        knowledge_distillation_mode=str(
            payload.get("knowledge_distillation_mode") or "with_selection_rules"
        ),
        knowledge_packaging_mode=str(payload.get("knowledge_packaging_mode") or "dsl_rule"),
        auto_knowledge_validate_mode=str(payload.get("auto_knowledge_validate_mode") or "static"),
        knowledge_reuse_digest_mode=str(payload.get("knowledge_reuse_digest_mode") or "off"),
        knowledge_repeat_summary_react_gap=_nonnegative_int_payload(
            "knowledge_repeat_summary_react_gap",
            0,
        ),
        knowledge_repeat_full_react_gap=_nonnegative_int_payload(
            "knowledge_repeat_full_react_gap",
            1,
        ),
        code_recall_intensity=str(payload.get("code_recall_intensity") or "normal"),
        knowledge_top_k=_positive_int_payload("knowledge_top_k", "knowledge_router_final_top_n", 3),
        knowledge_recall_top_m=_positive_int_payload("knowledge_recall_top_m", "knowledge_router_recall_top_m", 8),
        timeout_seconds=(int(payload["timeout_seconds"]) if payload.get("timeout_seconds") not in {None, ""} else DEFAULT_TASK_TIMEOUT_SECONDS),
        runtime_backend_profile=(
            str(payload.get("runtime_backend_profile")).strip()
            if payload.get("runtime_backend_profile") not in {None, ""}
            else None
        ),
        runtime_backend_mode=str(payload.get("runtime_backend_mode") or "single"),
        runtime_backend_pool=(
            str(payload.get("runtime_backend_pool")).strip()
            if payload.get("runtime_backend_pool") not in {None, ""}
            else None
        ),
        runtime_backend_pool_candidates=[
            dict(item)
            for item in list(payload.get("runtime_backend_pool_candidates") or [])
            if isinstance(item, dict)
        ],
        runtime_backend_base_url=None,
        runtime_backend_auth_token=None,
        runtime_backend_model=None,
        llm_judge_enabled=llm_judge_enabled,
        llm_judge_base_url="",
        llm_judge_api_key="",
        llm_judge_model=str(payload.get("llm_judge_model") or ""),
        llm_judge_timeout_seconds=max(1, int(payload.get("llm_judge_timeout_seconds") or 120)),
        llm_judge_max_retries=max(0, int(payload.get("llm_judge_max_retries") or 2)),
        reuse_embed_base_url=None,
        reuse_embed_api_key=None,
        reuse_embed_model=None,
        reuse_embed_verify_ssl=False,
        reuse_rerank_base_url=None,
        reuse_rerank_api_key=None,
        reuse_rerank_model=None,
        reuse_rerank_timeout_seconds=60,
        requested_params=_dict_payload("requested_params"),
        effective_params=_dict_payload("effective_params"),
        normalization_warnings=_list_payload("normalization_warnings"),
    )
    config = apply_backend_sidecars_to_config(config, snapshot=snapshot, secrets=secrets)
    config = _apply_runtime_backend_env_fallback(config)
    validate_embedding_packaging_backend_config(config)
    if llm_judge_enabled and not dummy_run:
        missing: list[str] = []
        if not config.llm_judge_base_url:
            missing.append("llm_judge_backend.base_url")
        if not config.llm_judge_api_key:
            missing.append("llm_judge_backend.api_key")
        if missing:
            raise ValueError(
                "LLM judge 配置缺失，请检查 eval_root 内部 sidecar: " + ", ".join(missing)
            )
    return config


async def resume_evaluation(eval_root: Path, *, workspace_root: Path | None = None) -> dict[str, Any]:
    config = load_evaluation_config_from_eval_root(eval_root)
    harness = EvaluationHarness(config=config, workspace_root=workspace_root)
    return await harness.resume(eval_root=eval_root)
