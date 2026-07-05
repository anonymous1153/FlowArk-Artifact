from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from flowark.anthropic_env import (
    ANTHROPIC_AUTH_ENV_KEYS,
    ANTHROPIC_BASE_URL_ENV_KEYS,
    ANTHROPIC_MODEL_ENV_KEYS,
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
    STUDIO_RUNTIME_OVERRIDE_ENV_KEYS,
)
from flowark.backend_transport import INTERNAL_BACKEND_TRANSPORT_ENV_KEYS
from flowark.config import build_workspace_child_env_overrides
from flowark.eval.harness.session_pool import (
    GLOBAL_EVAL_PARALLEL_DEFAULT,
    GLOBAL_EVAL_POOL_WAIT_POLL_SEC,
    FLOWARK_STUDIO_ENABLE_GLOBAL_EVAL_POOL,
    FLOWARK_STUDIO_EVAL_PRIORITY,
    FLOWARK_STUDIO_TASK_ID,
    EvalSessionPoolStore,
)
from flowark.eval.harness.models import (
    FLOWARK_STUDIO_EFFECTIVE_PARAMS_ENV,
    FLOWARK_STUDIO_NORMALIZATION_WARNINGS_ENV,
    FLOWARK_STUDIO_REQUESTED_PARAMS_ENV,
)
from flowark.eval.harness.backend_sidecar import EPHEMERAL_BACKEND_SECRETS_ENV
from flowark.git_snapshot import (
    FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV,
    FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_ENV,
    capture_workspace_git_snapshot,
    normalize_git_snapshot,
)
from flowark.process_tree import ProcessTreeSnapshot, signal_process_tree, snapshot_process_tree
from flowark.state_paths import get_workspace_state_paths
from flowark.timeutil import from_timestamp_tz8_iso, now_tz8_iso
from flowark_studio.common.config_presets import EXPERIMENT_PRESET_VALUES, get_eval_schema
from flowark_studio.common.eval_progress import (
    EVAL_OPEN_CODE_COST_KEY,
    rebuild_eval_progress_metadata_from_progress_file,
    rebuild_eval_progress_metadata_from_results_file,
    update_eval_progress_metadata_from_event,
)
from flowark_studio.common.event_bus import EventBus
from flowark_studio.common.file_watch import find_new_subdirs, safe_read_text, tail_jsonl_objects, tail_text_delta
from flowark_studio.common.models import DispatchMode, RealtimeWatchState, StudioEvent, StudioTask, TailCursor, TaskKind, TaskStatus, WatchedFileState
from flowark_studio.process.backend_profiles import (
    build_backend_selection_env_overrides,
    resolve_backend_selections,
)
from flowark_studio.process.command_codec import StudioCommandCodec
from flowark_studio.process.inspection import StudioTaskInspection
from flowark_studio.process.monitoring import StudioProcessMonitoring
from flowark_studio.process.runtime_state import StudioRuntimeState
from flowark_studio.process.task_reconstruction import HistoricalTaskReconstructor

logger = logging.getLogger(__name__)

RUNTIME_STATE_SCHEMA_VERSION = 1
TASK_TAG_METADATA_SCHEMA_VERSION = 2
PENDING_TASK_TAGS_SCHEMA_VERSION = 2
TASK_GROUP_SIDECAR_NAME = ".flowark_studio_task.json"
ARCHIVED_DIR_NAME = "archived"
EVALS_ARCHIVED_DIR_NAME = "evals_archived"
ARCHIVED_DIR_NAMES = {ARCHIVED_DIR_NAME, EVALS_ARCHIVED_DIR_NAME}
PAUSE_REASON_MANUAL = "manual"
ACTIVE_TASK_STATUSES = {"starting", "running", "pausing", "finishing"}
SERIAL_QUEUE_REFILL_POLL_SEC = GLOBAL_EVAL_POOL_WAIT_POLL_SEC
DEFAULT_DISPATCH_LAUNCH_DELAY_SEC = 10.0


class ProcessManager:
    def __init__(
        self,
        *,
        workspace_root: Path,
        queue_idle_sleep_sec: float = 5.0,
        dispatch_launch_delay_sec: float = DEFAULT_DISPATCH_LAUNCH_DELAY_SEC,
        time_fn: Any | None = None,
        monotonic_fn: Any | None = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self._state_paths = get_workspace_state_paths(self.workspace_root)
        self._tasks: dict[str, StudioTask] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._runner_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_runtime_auth_tokens: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._event_bus = EventBus()
        self._historical_refresh_lock = asyncio.Lock()
        self._historical_refresh_interval_sec = 5.0
        self._last_historical_refresh_monotonic = 0.0
        self._historical_refresh_task: asyncio.Task[int] | None = None
        self._historical_refresh_last_error: str | None = None
        self._serial_queue_task_ids: list[str] = []
        self._serial_queue_active_task_id: str | None = None
        self._dispatch_paused = False
        self._dispatch_block_reasons: dict[str, dict[str, Any]] = {}
        self._dispatch_resume_event = asyncio.Event()
        self._dispatch_resume_event.set()
        self._runtime_state_persist_lock = asyncio.Lock()
        self._task_group_persist_lock = asyncio.Lock()
        self._serial_queue_refill_task: asyncio.Task[None] | None = None
        self._queue_idle_sleep_sec = max(0.01, float(queue_idle_sleep_sec))
        self._dispatch_launch_delay_sec = max(0.0, float(dispatch_launch_delay_sec))
        self._last_dispatch_launch_monotonic: float | None = None
        self._time_fn = time_fn or time.time
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._runtime_state_dir = self._state_paths.studio_state_dir
        self._runtime_state_path = self._runtime_state_dir / "runtime_state.json"
        self._pending_task_groups_path = self._runtime_state_dir / "pending_task_groups.json"
        self._pending_task_groups: dict[str, dict[str, Any]] = {}
        self._next_global_eval_priority = 1
        self._eval_session_pool_store = EvalSessionPoolStore(self.workspace_root)
        self._command_codec = StudioCommandCodec(
            workspace_root=self.workspace_root,
            state_paths=self._state_paths,
        )
        self._process_inspection = StudioTaskInspection(workspace_root=self.workspace_root)
        self._process_monitoring = StudioProcessMonitoring()
        self._process_runtime_state = StudioRuntimeState()
        self._task_reconstructor = HistoricalTaskReconstructor(
            workspace_root=self.workspace_root,
            read_task_group_sidecar=self._read_task_group_sidecar,
            load_eval_state=self._load_eval_state,
            normalize_workspace_git_snapshot=self._normalize_workspace_git_snapshot,
            normalize_workspace_git_history=self._normalize_workspace_git_history,
        )

    async def ensure_studio_state_initialized(self) -> dict[str, Any]:
        raw_settings = self._read_json_dict_file(self._eval_session_pool_store.settings_path)
        settings = await asyncio.to_thread(self._eval_session_pool_store.load_settings)
        recommended_limit = self._recommended_global_eval_parallel_limit()
        raw_limit = self._safe_positive_int(
            (raw_settings or {}).get("global_eval_parallel_limit"),
            default=GLOBAL_EVAL_PARALLEL_DEFAULT,
        )
        current_limit = self._safe_positive_int(
            settings.get("global_eval_parallel_limit"),
            default=GLOBAL_EVAL_PARALLEL_DEFAULT,
        )
        should_apply_recommended_limit = (
            recommended_limit != current_limit
            and (raw_settings is None or raw_limit == GLOBAL_EVAL_PARALLEL_DEFAULT)
        )
        if should_apply_recommended_limit:
            await asyncio.to_thread(self._eval_session_pool_store.set_limit, recommended_limit)
            settings = await asyncio.to_thread(self._eval_session_pool_store.load_settings)
        pending_groups = await asyncio.to_thread(self._load_pending_task_groups_file)
        async with self._lock:
            self._pending_task_groups = pending_groups
        return dict(settings)

    def _recommended_global_eval_parallel_limit(self) -> int:
        return GLOBAL_EVAL_PARALLEL_DEFAULT

    @staticmethod
    def _norm_dir_path(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return str(Path(text).expanduser().resolve())
        except Exception:
            return text

    def _task_root_key(self, task: StudioTask) -> str | None:
        return (
            self._norm_dir_path(task.metadata.get("eval_dir"))
            or self._norm_dir_path(task.paths.get("eval_dir"))
            or self._norm_dir_path(task.paths.get("eval_root"))
        )

    def _task_group_sidecar_path(self, task: StudioTask | None = None, *, root: str | Path | None = None) -> Path | None:
        root_text = self._norm_dir_path(root) if root is not None else self._task_root_key(task) if task is not None else None
        if not root_text:
            return None
        return Path(root_text) / TASK_GROUP_SIDECAR_NAME

    @staticmethod
    def _is_archived_task_root(path: Path) -> bool:
        return any(str(part).strip().lower() in ARCHIVED_DIR_NAMES for part in path.parts)

    @staticmethod
    def _normalize_group_name(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    def _normalize_task_tags(self, values: Any) -> list[str]:
        if values is None:
            return []
        raw_values = values if isinstance(values, list) else [values]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_values:
            tag = self._normalize_group_name(item)
            if not tag or tag in seen:
                continue
            normalized.append(tag)
            seen.add(tag)
        return normalized

    @staticmethod
    def _normalize_workspace_git_snapshot(value: Any) -> dict[str, Any] | None:
        return normalize_git_snapshot(value)

    def _normalize_workspace_git_history(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            snapshot = self._normalize_workspace_git_snapshot(item)
            if snapshot is not None:
                normalized.append(snapshot)
        return normalized

    def _capture_workspace_git_snapshot(self) -> dict[str, Any]:
        return capture_workspace_git_snapshot(self.workspace_root)

    @staticmethod
    def _read_json_dict_file(path: Path) -> dict[str, Any] | None:
        if not path.exists() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _attach_eval_status_counts(task: StudioTask, summary: dict[str, Any] | None) -> None:
        if not isinstance(summary, dict):
            task.metadata.pop("eval_status_counts", None)
            return
        counts: dict[str, int] = {}
        for key in ["success_count", "warning_count", "error_count"]:
            try:
                value = int(summary.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                counts[key] = value
        if counts:
            task.metadata["eval_status_counts"] = counts
        else:
            task.metadata.pop("eval_status_counts", None)

    @staticmethod
    def _eval_summary_total_count(summary: dict[str, Any] | None) -> int:
        if not isinstance(summary, dict):
            return 0
        total_count = 0
        for key in ("task_count", "completed_task_count", "success_count"):
            try:
                total_count = max(total_count, int(summary.get(key) or 0))
            except Exception:
                continue
        return total_count

    @staticmethod
    def _write_json_dict_file(path: Path, payload: dict[str, Any] | None) -> None:
        if payload is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def _load_pending_task_groups_file(self) -> dict[str, dict[str, Any]]:
        payload = self._read_json_dict_file(self._pending_task_groups_path)
        if not isinstance(payload, dict):
            return {}
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for task_id, item in raw_tasks.items():
            task_id_text = str(task_id or "").strip()
            if not task_id_text or not isinstance(item, dict):
                continue
            tags = self._normalize_task_tags(item.get("tags"))
            if not tags:
                legacy_group = self._normalize_group_name(item.get("group"))
                tags = [legacy_group] if legacy_group else []
            if not tags:
                continue
            normalized[task_id_text] = {
                "tags": tags,
                "updated_at": str(item.get("updated_at") or ""),
            }
        return normalized

    def _build_pending_task_groups_locked(self) -> dict[str, Any] | None:
        if not self._pending_task_groups:
            return None
        tasks = {
            task_id: {
                "tags": self._normalize_task_tags(item.get("tags")),
                "updated_at": str(item.get("updated_at") or self._now_iso()),
            }
            for task_id, item in sorted(self._pending_task_groups.items())
            if self._normalize_task_tags(item.get("tags"))
        }
        if not tasks:
            return None
        return {
            "schema_version": PENDING_TASK_TAGS_SCHEMA_VERSION,
            "updated_at": self._now_iso(),
            "tasks": tasks,
        }

    async def _persist_pending_task_groups(self) -> None:
        async with self._task_group_persist_lock:
            async with self._lock:
                payload = self._build_pending_task_groups_locked()
            await asyncio.to_thread(self._write_json_dict_file, self._pending_task_groups_path, payload)

    def _set_task_tags_locked(self, task: StudioTask, tags: Any) -> list[str]:
        normalized = self._normalize_task_tags(tags)
        task.metadata.pop("group", None)
        if normalized:
            task.metadata["tags"] = normalized
        else:
            task.metadata.pop("tags", None)
        return normalized

    def _pending_task_tags_locked(self, task_id: str) -> list[str]:
        item = self._pending_task_groups.get(str(task_id or "").strip()) or {}
        tags = self._normalize_task_tags(item.get("tags"))
        if tags:
            return tags
        legacy_group = self._normalize_group_name(item.get("group"))
        return [legacy_group] if legacy_group else []

    def _read_task_group_sidecar(self, root: Path | str) -> list[str]:
        sidecar_path = self._task_group_sidecar_path(root=root)
        if sidecar_path is None:
            return []
        payload = self._read_json_dict_file(sidecar_path)
        if not isinstance(payload, dict):
            return []
        tags = self._normalize_task_tags(payload.get("tags"))
        if tags:
            return tags
        legacy_group = self._normalize_group_name(payload.get("group"))
        return [legacy_group] if legacy_group else []

    def _write_task_group_sidecar(self, root: Path | str, tags: Any) -> None:
        sidecar_path = self._task_group_sidecar_path(root=root)
        if sidecar_path is None:
            return
        normalized = self._normalize_task_tags(tags)
        if not normalized:
            self._write_json_dict_file(sidecar_path, None)
            return
        self._write_json_dict_file(
            sidecar_path,
            {
                "schema_version": TASK_TAG_METADATA_SCHEMA_VERSION,
                "tags": normalized,
                "updated_at": self._now_iso(),
            },
        )

    @staticmethod
    def _is_terminal_eval_run_status(status: Any) -> bool:
        normalized = str(status or "").strip().lower()
        return normalized in {
            "success",
            "warning",
            "error",
            "timeout",
            "cancelled",
            "skipped",
            "harness_error",
            "force_paused",
        }

    def _assign_unique_task_id_locked(self, task: StudioTask) -> None:
        base = str(task.task_id or "").strip() or uuid.uuid4().hex[:12]
        existing = self._tasks.get(base)
        if existing is None:
            task.task_id = base
            return
        incoming_root = self._task_root_key(task)
        existing_root = self._task_root_key(existing)
        if incoming_root and existing_root and incoming_root == existing_root:
            task.task_id = base
            return
        idx = 1
        while True:
            candidate = f"{base}-{idx}"
            if candidate not in self._tasks:
                task.task_id = candidate
                return
            idx += 1

    @staticmethod
    def _iter_eval_root_dirs(evals_dir: Path) -> list[Path]:
        if not evals_dir.is_dir():
            return []
        found: dict[str, Path] = {}
        for root, dirnames, files in os.walk(evals_dir):
            dirnames[:] = [
                item
                for item in dirnames
                if not str(item or "").strip().startswith(".")
                and str(item or "").strip().lower() not in ARCHIVED_DIR_NAMES
            ]
            if "config.json" not in files and "manifest.json" not in files:
                continue
            try:
                resolved = Path(root).expanduser().resolve()
            except Exception:
                continue
            if ProcessManager._is_archived_task_root(resolved):
                continue
            found[str(resolved)] = resolved
        return sorted(found.values(), key=lambda p: str(p))

    @staticmethod
    def _first_nonempty(mapping: dict[str, str], *keys: str) -> str | None:
        for key in keys:
            value = str(mapping.get(key) or "").strip()
            if value:
                return value
        return None

    def _load_workspace_env_overrides(self) -> dict[str, str]:
        overrides = build_workspace_child_env_overrides(self.workspace_root)
        for key in INTERNAL_BACKEND_TRANSPORT_ENV_KEYS:
            overrides.pop(key, None)
        return overrides

    @staticmethod
    def _json_env_value(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None

    def _ensure_eval_task_runtime_metadata_locked(self, task: StudioTask) -> None:
        if task.kind != "eval":
            return
        requested_parallel = self._safe_positive_int(task.params.get("parallel"), default=2)
        task.metadata["requested_parallel"] = int(requested_parallel)
        raw_priority = task.metadata.get("global_eval_priority")
        try:
            priority = int(raw_priority)
        except Exception:
            priority = 0
        if priority <= 0:
            priority = self._next_global_eval_priority
        task.metadata["global_eval_priority"] = int(priority)
        self._next_global_eval_priority = max(self._next_global_eval_priority, int(priority) + 1)

    def _build_child_env(self, task: StudioTask | None = None) -> dict[str, str]:
        child_env = dict(os.environ)
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        for key in STUDIO_RUNTIME_OVERRIDE_ENV_KEYS:
            child_env.pop(key, None)
        for key in INTERNAL_BACKEND_TRANSPORT_ENV_KEYS:
            child_env.pop(key, None)
        for key in (*ANTHROPIC_AUTH_ENV_KEYS, *ANTHROPIC_BASE_URL_ENV_KEYS, *ANTHROPIC_MODEL_ENV_KEYS):
            child_env.pop(key, None)
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

        overrides = self._load_workspace_env_overrides()
        if overrides:
            child_env.update(overrides)
        if task is not None:
            selected_names, resolved_options = resolve_backend_selections(
                workspace_root=self.workspace_root,
                params=task.params or {},
            )
            runtime_backend = resolved_options.get("runtime_backend")
            selection_overrides = build_backend_selection_env_overrides(
                resolved_options,
                selected_names=selected_names,
                include_runtime_backend=True,
            )
        else:
            selection_overrides = {}
        if selection_overrides:
            for key in selection_overrides:
                child_env.pop(key, None)
            child_env.update(selection_overrides)
        if task is not None and task.kind == "eval":
            child_env[EPHEMERAL_BACKEND_SECRETS_ENV] = "1"
            form_runtime_base_url = str(task.params.get("runtime_backend_base_url") or "").strip()
            form_runtime_model = str(task.params.get("opencode_model") or "").strip()
            form_runtime_auth_token = str(self._task_runtime_auth_tokens.get(task.task_id) or "").strip()
            if form_runtime_base_url or form_runtime_model or form_runtime_auth_token:
                child_env[STUDIO_BACKEND_PROFILE_ENV] = "studio_form"
            if form_runtime_base_url:
                child_env[STUDIO_RUNTIME_BASE_URL_ENV] = form_runtime_base_url
            if form_runtime_auth_token:
                child_env[STUDIO_RUNTIME_AUTH_TOKEN_ENV] = form_runtime_auth_token
            if form_runtime_model:
                child_env[STUDIO_RUNTIME_MODEL_ENV] = form_runtime_model
            priority = self._safe_positive_int(task.metadata.get("global_eval_priority"), default=self._next_global_eval_priority)
            child_env[FLOWARK_STUDIO_ENABLE_GLOBAL_EVAL_POOL] = "1"
            child_env[FLOWARK_STUDIO_TASK_ID] = str(task.task_id)
            child_env[FLOWARK_STUDIO_EVAL_PRIORITY] = str(priority)
            for env_key, metadata_key in (
                (FLOWARK_STUDIO_REQUESTED_PARAMS_ENV, "requested_params"),
                (FLOWARK_STUDIO_EFFECTIVE_PARAMS_ENV, "effective_params"),
                (FLOWARK_STUDIO_NORMALIZATION_WARNINGS_ENV, "normalization_warnings"),
            ):
                payload = self._json_env_value(task.metadata.get(metadata_key))
                if payload is not None:
                    child_env[env_key] = payload
        return child_env

    def _now_iso(self) -> str:
        return from_timestamp_tz8_iso(self._time_fn())

    def _task_pause_reason(self, task: StudioTask | None) -> str | None:
        if task is None:
            return None
        raw = str(task.metadata.get("pause_reason") or "").strip().lower()
        if raw == PAUSE_REASON_MANUAL:
            return raw
        return None

    def _set_task_pause_reason_locked(
        self,
        task: StudioTask,
        reason: str | None,
        *,
        pause_requested: bool | None = None,
        pause_confirmed: bool | None = None,
        pause_mode: str | None = None,
        requested_at_key: str | None = None,
    ) -> None:
        if reason:
            task.metadata["pause_reason"] = reason
        else:
            task.metadata.pop("pause_reason", None)
        if pause_requested is not None:
            task.metadata["pause_requested"] = bool(pause_requested)
            if pause_requested:
                task.metadata["pause_requested_at"] = self._now_iso()
        if pause_confirmed is not None:
            task.metadata["pause_confirmed"] = bool(pause_confirmed)
            if pause_confirmed:
                task.metadata["pause_confirmed_at"] = self._now_iso()
        if pause_mode:
            task.metadata["pause_mode_requested"] = pause_mode
        if requested_at_key:
            task.metadata[requested_at_key] = self._now_iso()

    def _clear_task_pause_metadata_locked(self, task: StudioTask) -> None:
        for key in (
            "pause_reason",
            "pause_requested",
            "pause_requested_at",
            "pause_confirmed",
            "pause_confirmed_at",
            "pause_mode_requested",
            "usage_auto_pause_requested",
            "usage_auto_pause_requested_at",
            "usage_auto_resume_pending",
            "usage_auto_resume_pending_at",
            "force_pause_recovery_pending",
            "force_pause_recovery_requested_at",
            "startup_recovery_pending",
            "recovery_restart_required",
            "persisted_orphan_pid",
            "persisted_orphan_started_at",
            "persisted_orphan_started_at_epoch_sec",
        ):
            task.metadata.pop(key, None)

    def _set_dispatch_block_reason_locked(
        self,
        reason: str,
        *,
        active: bool,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if active:
            record = dict(payload or {})
            record.setdefault("reason", reason)
            record["updated_at"] = self._now_iso()
            self._dispatch_block_reasons[reason] = record
        else:
            self._dispatch_block_reasons.pop(reason, None)
        was_paused = self._dispatch_paused
        self._dispatch_paused = bool(self._dispatch_block_reasons)
        if self._dispatch_paused:
            self._dispatch_resume_event.clear()
        else:
            if was_paused and self._has_prelaunch_runner_locked():
                self._last_dispatch_launch_monotonic = self._monotonic_fn()
            self._dispatch_resume_event.set()

    def _is_live_task_locked(self, task: StudioTask) -> bool:
        return not bool(task.metadata.get("historical")) and str(task.status or "").strip().lower() not in {
            "success",
            "error",
            "timeout",
            "cancelled",
        }

    def _prune_missing_inactive_tasks_locked(self) -> int:
        removed = 0
        for task_id, task in list(self._tasks.items()):
            if self._is_live_task_locked(task):
                continue
            root = self._task_root_key(task)
            if not root:
                continue
            try:
                root_exists = Path(root).exists()
            except Exception:
                root_exists = False
            if root_exists:
                continue
            self._tasks.pop(task_id, None)
            self._remove_task_from_serial_queue_locked(task_id)
            if self._serial_queue_active_task_id == task_id:
                self._serial_queue_active_task_id = None
            removed += 1
        return removed

    def _prune_stale_pending_task_groups_locked(self) -> bool:
        keep_ids = {
            task.task_id
            for task in self._tasks.values()
            if self._is_live_task_locked(task) and not self._task_root_key(task)
        }
        stale_ids = [task_id for task_id in self._pending_task_groups if task_id not in keep_ids]
        if not stale_ids:
            return False
        for task_id in stale_ids:
            self._pending_task_groups.pop(task_id, None)
        return True

    async def _migrate_pending_tags_to_task_root(self, task: StudioTask | None, tags: Any = None) -> None:
        if task is None:
            return
        async with self._lock:
            root = self._task_root_key(task)
            pending_tags = self._normalize_task_tags(tags) or self._pending_task_tags_locked(task.task_id)
            if not root or not pending_tags:
                return
            normalized_tags = self._set_task_tags_locked(task, pending_tags)
            self._pending_task_groups.pop(task.task_id, None)
        await asyncio.to_thread(self._write_task_group_sidecar, root, normalized_tags)
        await self._persist_pending_task_groups()
        await self._persist_runtime_state()
        compat_group = normalized_tags[0] if len(normalized_tags) == 1 else None
        await self._publish(
            task,
            "task_status",
            {
                "tags": normalized_tags,
                "tags_persisted_to": "task_sidecar",
                "group": compat_group,
                "group_persisted_to": "task_sidecar",
                "message": "task_tags_migrated",
            },
        )

    def _should_keep_runtime_state_locked(self) -> bool:
        if self._dispatch_block_reasons:
            return True
        return any(task.kind == "eval" and self._is_live_task_locked(task) for task in self._tasks.values())

    def _build_runtime_state_locked(self) -> dict[str, Any] | None:
        if not self._should_keep_runtime_state_locked():
            return None
        live_eval_task_ids = {
            task.task_id
            for task in self._tasks.values()
            if task.kind == "eval" and self._is_live_task_locked(task)
        }
        live_tasks = [
            task.to_dict()
            for task in self._tasks.values()
            if task.task_id in live_eval_task_ids
        ]
        serial_queue_task_ids = [
            task_id for task_id in self._serial_queue_task_ids if task_id in live_eval_task_ids
        ]
        serial_queue_active_task_id = (
            self._serial_queue_active_task_id
            if self._serial_queue_active_task_id in live_eval_task_ids
            else None
        )
        return {
            "schema_version": RUNTIME_STATE_SCHEMA_VERSION,
            "updated_at": self._now_iso(),
            "dispatch_block_reasons": dict(self._dispatch_block_reasons),
            "serial_queue_task_ids": serial_queue_task_ids,
            "serial_queue_active_task_id": serial_queue_active_task_id,
            "tasks": live_tasks,
        }

    def _write_runtime_state_file(self, payload: dict[str, Any] | None) -> None:
        self._write_json_dict_file(self._runtime_state_path, payload)

    async def _persist_runtime_state(self) -> None:
        async with self._runtime_state_persist_lock:
            async with self._lock:
                payload = self._build_runtime_state_locked()
            await asyncio.to_thread(self._write_runtime_state_file, payload)

    def _remove_conflicting_historical_tasks_locked(self, task: StudioTask) -> None:
        incoming_root = self._task_root_key(task)
        if not incoming_root:
            return
        for task_id, existing in list(self._tasks.items()):
            if not existing.metadata.get("historical"):
                continue
            if self._task_root_key(existing) != incoming_root:
                continue
            self._tasks.pop(task_id, None)

    def _normalize_restored_task_locked(self, task: StudioTask) -> bool:
        if task.kind != "eval":
            return False
        task.metadata["historical"] = False
        task.metadata.pop("backend_selection_details", None)
        task.pid = int(task.pid) if isinstance(task.pid, int) else None
        self._set_task_tags_locked(task, task.metadata.get("tags") or task.metadata.get("group"))
        self._ensure_eval_task_runtime_metadata_locked(task)
        status = str(task.status or "").strip().lower()
        if status in {"success", "warning", "error", "timeout", "cancelled"}:
            return False

        dispatch_mode = self._normalize_dispatch_mode(task.metadata.get("dispatch_mode"))
        task.metadata["dispatch_mode"] = dispatch_mode

        if status in ACTIVE_TASK_STATUSES:
            return False
        elif status == "queued":
            task.pid = None
            task.return_code = None
            task.finished_at = None
            task.error = None
        elif status == "paused":
            if self._task_pause_reason(task) is None:
                self._set_task_pause_reason_locked(task, PAUSE_REASON_MANUAL)
        else:
            return False
        for key in (
            "force_pause_recovery_pending",
            "force_pause_recovery_requested_at",
            "startup_recovery_pending",
            "recovery_restart_required",
            "persisted_orphan_pid",
            "persisted_orphan_started_at",
            "persisted_orphan_started_at_epoch_sec",
        ):
            task.metadata.pop(key, None)

        eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir") or task.metadata.get("eval_dir")
        if isinstance(eval_root_raw, str) and eval_root_raw.strip():
            eval_root = str(Path(eval_root_raw).expanduser().resolve())
            task.paths["eval_root"] = eval_root
            task.paths["eval_dir"] = eval_root
            task.metadata["eval_dir"] = eval_root
            task.paths.setdefault("progress_jsonl", str(Path(eval_root) / "progress.jsonl"))
            task.paths.setdefault("planned_runs_json", str(Path(eval_root) / "planned_runs.json"))
            eval_root_path = Path(eval_root)
            rebuild_eval_progress_metadata_from_progress_file(task.metadata, eval_root_path / "progress.jsonl")
            if EVAL_OPEN_CODE_COST_KEY not in task.metadata:
                summary = self._read_json_dict_file(eval_root_path / "summary.json")
                total_count = self._eval_summary_total_count(summary)
                rebuild_eval_progress_metadata_from_results_file(
                    task.metadata,
                    eval_root_path / "results.jsonl",
                    total_count=total_count,
                )
            task.register_root(eval_root_path)
        return True

    def _read_runtime_state_payload(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return self._process_runtime_state.read_runtime_state_payload(self._runtime_state_path)

    def _restore_tasks_from_runtime_payload_locked(
        self,
        payload: dict[str, Any],
    ) -> tuple[int, list[StudioTask]]:
        result = self._process_runtime_state.restore_tasks_from_runtime_payload(
            payload,
            normalize_restored_task=self._normalize_restored_task_locked,
            pending_task_tags=self._pending_task_tags_locked,
            normalize_task_tags=self._normalize_task_tags,
            set_task_tags=self._set_task_tags_locked,
            task_root_key=self._task_root_key,
        )
        for task in result.restored_tasks:
            self._remove_conflicting_historical_tasks_locked(task)
            self._tasks[task.task_id] = task
        return (
            result.task_count,
            result.tasks_to_migrate_groups,
        )

    def _restore_serial_queue_from_runtime_payload_locked(self, payload: dict[str, Any]) -> None:
        restored_queue, restored_active_task_id = self._process_runtime_state.restore_serial_queue_from_runtime_payload(
            payload,
            tasks_by_id=self._tasks,
            task_dispatch_mode=self._task_dispatch_mode,
            is_live_task=self._is_live_task_locked,
        )
        self._serial_queue_task_ids = restored_queue
        self._serial_queue_active_task_id = restored_active_task_id

    def _restore_dispatch_state_from_runtime_payload_locked(self, _payload: dict[str, Any]) -> None:
        self._dispatch_block_reasons = {}
        self._dispatch_paused = False
        self._dispatch_resume_event.set()

    async def load_runtime_state(self) -> dict[str, Any]:
        payload, empty_result = self._read_runtime_state_payload()
        if payload is None:
            return empty_result

        async with self._lock:
            restored_tasks, tasks_to_migrate_groups = self._restore_tasks_from_runtime_payload_locked(payload)
            pending_tags_to_migrate = {
                task.task_id: self._pending_task_tags_locked(task.task_id)
                for task in tasks_to_migrate_groups
            }
            self._restore_serial_queue_from_runtime_payload_locked(payload)
            self._restore_dispatch_state_from_runtime_payload_locked(payload)
            pending_changed = self._prune_stale_pending_task_groups_locked()
            queue_updates = self._refresh_dispatch_locked()
        await self._publish_queue_state_updates(queue_updates)
        if pending_changed:
            await self._persist_pending_task_groups()
        for task in tasks_to_migrate_groups:
            await self._migrate_pending_tags_to_task_root(
                task,
                tags=pending_tags_to_migrate.get(task.task_id) or task.metadata.get("tags"),
            )
        await self._persist_runtime_state()
        return {
            "loaded": restored_tasks > 0,
            "task_count": restored_tasks,
        }

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    async def start_background_tasks(self) -> None:
        async with self._lock:
            if self._serial_queue_refill_task is None or self._serial_queue_refill_task.done():
                self._serial_queue_refill_task = asyncio.create_task(
                    self._serial_queue_refill_loop(),
                    name="flowark-studio-serial-queue-refill",
                )

    async def shutdown(self) -> None:
        async with self._lock:
            refill = self._serial_queue_refill_task
            self._serial_queue_refill_task = None
        for task in [refill]:
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _serial_queue_refill_loop(self) -> None:
        while True:
            try:
                async with self._lock:
                    has_work = self._has_serial_queue_refill_work_locked()
                    queue_updates = self._refresh_dispatch_locked() if has_work else []
                    sleep_for = self._queue_idle_sleep_sec
                    if has_work:
                        delay_remaining = self._dispatch_launch_delay_remaining_locked()
                        sleep_for = delay_remaining if delay_remaining > 0.0 else SERIAL_QUEUE_REFILL_POLL_SEC
                if queue_updates:
                    await self._persist_runtime_state()
                    await self._publish_queue_state_updates(queue_updates)
                await asyncio.sleep(max(0.01, sleep_for))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self._queue_idle_sleep_sec)

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _safe_positive_int(value: Any, *, default: int) -> int:
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except Exception:
            pass
        return default

    def _dispatch_launch_delay_remaining_locked(self) -> float:
        if self._dispatch_launch_delay_sec <= 0.0 or self._last_dispatch_launch_monotonic is None:
            return 0.0
        next_launch = self._last_dispatch_launch_monotonic + self._dispatch_launch_delay_sec
        return max(0.0, next_launch - self._monotonic_fn())

    def _has_prelaunch_runner_locked(self) -> bool:
        for task_id, runner in self._runner_tasks.items():
            if runner.done():
                continue
            task = self._tasks.get(task_id)
            if task is None or task_id in self._procs:
                continue
            if self._is_terminal_task_status(task.status):
                continue
            return True
        return False

    def _try_queue_task_runner_locked(self, task_id: str) -> bool:
        if self._dispatch_paused:
            return False
        if self._dispatch_launch_delay_remaining_locked() > 0.0:
            return False
        self._runner_tasks[task_id] = asyncio.create_task(self._run_task(task_id))
        self._last_dispatch_launch_monotonic = self._monotonic_fn()
        return True

    def _is_dispatch_launch_candidate_locked(self, task_id: str, task: StudioTask) -> bool:
        if task.metadata.get("historical"):
            return False
        if task.status != "queued":
            return False
        if not task.command:
            return False
        return task_id not in self._runner_tasks and task_id not in self._procs

    def _refresh_dispatch_locked(self) -> list[StudioTask]:
        changed_tasks = self._refresh_serial_queue_state_locked()
        if self._dispatch_paused:
            return changed_tasks
        for task_id, task in self._tasks.items():
            if self._task_dispatch_mode(task) != "force_parallel":
                continue
            if not self._is_dispatch_launch_candidate_locked(task_id, task):
                continue
            if not self._try_queue_task_runner_locked(task_id):
                break
        return changed_tasks

    def _task_has_serial_queue_runtime_locked(self, task: StudioTask) -> bool:
        status = str(task.status or "").strip().lower()
        return (
            task.task_id in self._runner_tasks
            or task.task_id in self._procs
            or status in ACTIVE_TASK_STATUSES
        )

    def _serial_queue_has_global_eval_headroom_locked(self, active_serial_eval_task_ids: list[str]) -> bool:
        if not active_serial_eval_task_ids:
            return False
        snapshot = self._eval_session_pool_store.snapshot()
        participants = list(snapshot.get("participants") or [])
        if not participants:
            return False
        participant_ids = {
            str(item.get("task_id") or "").strip()
            for item in participants
            if str(item.get("task_id") or "").strip()
        }
        if not participant_ids.intersection(active_serial_eval_task_ids):
            return False
        waiting_sessions = max(0, int(snapshot.get("waiting_sessions") or 0))
        if waiting_sessions > 0:
            return False
        active_sessions = max(0, int(snapshot.get("active_sessions") or 0))
        limit = max(1, int(snapshot.get("limit") or GLOBAL_EVAL_PARALLEL_DEFAULT))
        return active_sessions < limit

    def _can_opportunistically_start_serial_eval_locked(self, active_task_ids: list[str]) -> bool:
        active_serial_eval_task_ids: list[str] = []
        for task_id in active_task_ids:
            task = self._tasks.get(task_id)
            if task is None or task.kind != "eval":
                return False
            status = str(task.status or "").strip().lower()
            if status in {"paused", "pausing"}:
                return False
            if task_id in self._procs or status in {"running", "finishing"}:
                active_serial_eval_task_ids.append(task_id)
                continue
            return False
        return self._serial_queue_has_global_eval_headroom_locked(active_serial_eval_task_ids)

    def _has_force_parallel_dispatch_candidate_locked(self) -> bool:
        if self._dispatch_paused:
            return False
        for task_id, task in self._tasks.items():
            if self._task_dispatch_mode(task) != "force_parallel":
                continue
            if self._is_dispatch_launch_candidate_locked(task_id, task):
                return True
        return False

    def _has_serial_queue_dispatch_candidate_locked(self) -> bool:
        if self._dispatch_paused:
            return False
        activated_task_ids: list[str] = []
        queue_blocked = False
        for task_id in self._serial_queue_task_ids:
            task = self._tasks.get(task_id)
            if task is None or self._task_dispatch_mode(task) != "serial_queue":
                continue
            if self._is_terminal_task_status(task.status):
                continue
            status = str(task.status or "").strip().lower()
            if self._task_has_serial_queue_runtime_locked(task):
                activated_task_ids.append(task_id)
                if task.kind != "eval" or status == "pausing":
                    queue_blocked = True
                continue
            if status == "paused":
                continue
            if queue_blocked:
                continue
            if not self._is_dispatch_launch_candidate_locked(task_id, task):
                queue_blocked = True
                continue
            if not activated_task_ids:
                return True
            if (
                task.kind == "eval"
                and self._can_opportunistically_start_serial_eval_locked(activated_task_ids)
            ):
                return True
            queue_blocked = True
        return False

    def _has_serial_queue_refill_work_locked(self) -> bool:
        if self._dispatch_paused:
            return False
        if self._has_force_parallel_dispatch_candidate_locked():
            return True
        if self._has_serial_queue_dispatch_candidate_locked():
            return True
        if len(self._serial_queue_task_ids) < 2:
            return False
        has_queued_tail = False
        has_active_eval_prefix = False
        for task_id in self._serial_queue_task_ids:
            task = self._tasks.get(task_id)
            if task is None or self._is_terminal_task_status(task.status):
                continue
            status = str(task.status or "").strip().lower()
            if status == "paused":
                continue
            if self._task_has_serial_queue_runtime_locked(task):
                if task.kind == "eval" and status != "pausing":
                    has_active_eval_prefix = True
                continue
            has_queued_tail = True
            break
        return has_active_eval_prefix and has_queued_tail

    async def _wait_for_dispatch_resume(self) -> None:
        while self._dispatch_paused:
            await self._dispatch_resume_event.wait()

    @staticmethod
    def _normalize_dispatch_mode(value: Any) -> DispatchMode:
        raw = str(value or "").strip().lower()
        aliases: dict[str, DispatchMode] = {
            "serial_queue": "serial_queue",
            "serial-queue": "serial_queue",
            "serialqueue": "serial_queue",
            "queue": "serial_queue",
            "queued": "serial_queue",
            "serial": "serial_queue",
            "force_parallel": "force_parallel",
            "force-parallel": "force_parallel",
            "forceparallel": "force_parallel",
            "parallel": "force_parallel",
            "concurrent": "force_parallel",
            "immediate": "force_parallel",
        }
        return aliases.get(raw, "force_parallel")

    def _task_dispatch_mode(self, task: StudioTask | None) -> DispatchMode:
        if task is None:
            return "force_parallel"
        return self._normalize_dispatch_mode(task.metadata.get("dispatch_mode"))

    @staticmethod
    def _is_terminal_task_status(status: Any) -> bool:
        return str(status or "").strip().lower() in {"success", "warning", "error", "timeout", "cancelled"}

    def _remove_task_from_serial_queue_locked(self, task_id: str) -> None:
        self._serial_queue_task_ids = [item for item in self._serial_queue_task_ids if item != task_id]
        if self._serial_queue_active_task_id == task_id:
            self._serial_queue_active_task_id = None

    def _set_task_queue_metadata_locked(
        self,
        task: StudioTask,
        *,
        queue_waiting: bool,
        queue_position: int | None,
    ) -> bool:
        changed = False
        old_waiting = bool(task.metadata.get("queue_waiting"))
        old_position = task.metadata.get("queue_position")
        if old_waiting != queue_waiting:
            task.metadata["queue_waiting"] = queue_waiting
            changed = True
            if queue_waiting:
                task.metadata.setdefault("queued_at", self._now_iso())
        if queue_position is None:
            if "queue_position" in task.metadata:
                task.metadata.pop("queue_position", None)
                changed = True
        elif old_position != queue_position:
            task.metadata["queue_position"] = queue_position
            changed = True
        is_head = not queue_waiting
        old_head_at = task.metadata.get("queue_head_at")
        if is_head:
            if old_head_at in {None, ""}:
                task.metadata["queue_head_at"] = self._now_iso()
                changed = True
        elif old_head_at not in {None, ""}:
            task.metadata.pop("queue_head_at", None)
            changed = True
        return changed

    def _refresh_serial_queue_state_locked(self) -> list[StudioTask]:
        known_tasks: list[str] = []
        seen: set[str] = set()
        changed_tasks: list[StudioTask] = []
        for task_id in self._serial_queue_task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            task = self._tasks.get(task_id)
            if task is None or self._task_dispatch_mode(task) != "serial_queue":
                continue
            if self._is_terminal_task_status(task.status):
                if self._set_task_queue_metadata_locked(task, queue_waiting=False, queue_position=None):
                    changed_tasks.append(task)
                continue
            known_tasks.append(task_id)
        self._serial_queue_task_ids = known_tasks
        activated_task_ids: list[str] = []
        soft_overlap_started = False
        queue_blocked = False
        for task_id in self._serial_queue_task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                continue
            status = str(task.status or "").strip().lower()
            if self._task_has_serial_queue_runtime_locked(task):
                activated_task_ids.append(task_id)
                if task.kind != "eval" or status == "pausing":
                    queue_blocked = True
                continue
            if status == "paused":
                continue
            if queue_blocked:
                continue
            if not task.command:
                queue_blocked = True
                continue
            if not activated_task_ids:
                if not self._dispatch_paused and task_id not in self._runner_tasks and task_id not in self._procs:
                    if self._try_queue_task_runner_locked(task_id):
                        activated_task_ids.append(task_id)
                queue_blocked = True
                continue
            if (
                not soft_overlap_started
                and task.kind == "eval"
                and self._can_opportunistically_start_serial_eval_locked(activated_task_ids)
                and task_id not in self._runner_tasks
                and task_id not in self._procs
            ):
                if self._try_queue_task_runner_locked(task_id):
                    activated_task_ids.append(task_id)
                    soft_overlap_started = True
            queue_blocked = True

        visible_active_task_ids = activated_task_ids or (self._serial_queue_task_ids[:1] if self._serial_queue_task_ids else [])
        active_task_id_set = set(visible_active_task_ids)
        self._serial_queue_active_task_id = visible_active_task_ids[0] if visible_active_task_ids else None
        wait_index = 1
        for task_id in self._serial_queue_task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                continue
            queue_waiting = task_id not in active_task_id_set
            queue_position = wait_index if queue_waiting else None
            if self._set_task_queue_metadata_locked(task, queue_waiting=queue_waiting, queue_position=queue_position):
                changed_tasks.append(task)
            if queue_waiting:
                wait_index += 1
        return changed_tasks

    async def _publish_queue_state_updates(self, tasks: Iterable[StudioTask]) -> None:
        seen: set[str] = set()
        for task in tasks:
            if task.task_id in seen:
                continue
            seen.add(task.task_id)
            payload: dict[str, Any] = {
                "status": task.status,
                "dispatch_mode": self._task_dispatch_mode(task),
                "queue_waiting": bool(task.metadata.get("queue_waiting")),
            }
            if task.kind == "eval":
                payload["eval_progress"] = task.metadata.get("eval_progress")
                payload["eval_open_code_cost"] = task.metadata.get("eval_open_code_cost")
                payload["pause_reason"] = self._task_pause_reason(task)
            queue_position = task.metadata.get("queue_position")
            if queue_position not in {None, ""}:
                payload["queue_position"] = queue_position
            await self._publish(task, "task_status", payload)

    async def start_eval(self, params: dict[str, Any], *, dispatch_mode: Any = None) -> str:
        return await self._start_task(kind="eval", params=params, dispatch_mode=dispatch_mode)

    async def _start_task(self, *, kind: TaskKind, params: dict[str, Any], dispatch_mode: Any = None) -> str:
        if kind != "eval":
            raise ValueError("The public Studio only supports eval tasks")
        task_id = uuid.uuid4().hex[:12]
        normalized_dispatch_mode = self._normalize_dispatch_mode(dispatch_mode)
        if "auto_knowledge_validate_repair_mode" in dict(params or {}):
            raise ValueError("auto_knowledge_validate_repair_mode was removed with full validation")
        sanitized_params = self._sanitize_task_params(kind, params)
        runtime_auth_token = str(sanitized_params.pop("runtime_backend_auth_token", "") or "").strip()
        effective_params, parameter_warnings = self._command_codec.normalize_effective_params(
            kind,
            sanitized_params,
        )
        task = StudioTask(
            task_id=task_id,
            kind=kind,
            status="queued",
            created_at=now_tz8_iso(),
            params=effective_params,
        )
        cmd, cwd, meta = self._build_command(kind, task.params)
        task.command = cmd
        task.cwd = str(cwd)
        task.metadata.update(meta)
        normalization_warnings = list(parameter_warnings)
        task.metadata["requested_params"] = dict(sanitized_params)
        task.metadata["effective_params"] = dict(effective_params)
        task.metadata["normalization_warnings"] = normalization_warnings
        if normalization_warnings:
            task.metadata["parameter_warnings"] = normalization_warnings
        for key in (
            "model_backend_preset",
            "runtime_backend_mode",
            "runtime_backend",
            "reuse_embed_backend",
            "reuse_rerank_backend",
        ):
            task.metadata[key] = str(task.params.get(key) or "")
        if kind == "eval":
            task.metadata["workspace_git_submitted"] = await asyncio.to_thread(self._capture_workspace_git_snapshot)
        task.metadata["dispatch_mode"] = normalized_dispatch_mode
        task.metadata["queued_at"] = self._now_iso()
        # Allow reading from configured output root immediately.
        if isinstance(meta.get("out_dir"), str):
            task.register_root(Path(meta["out_dir"]))

        queue_updates: list[StudioTask] = []
        async with self._lock:
            self._ensure_eval_task_runtime_metadata_locked(task)
            self._tasks[task_id] = task
            if runtime_auth_token:
                self._task_runtime_auth_tokens[task_id] = runtime_auth_token
            if normalized_dispatch_mode == "serial_queue":
                self._serial_queue_task_ids.append(task_id)
            queue_updates = self._refresh_dispatch_locked()
        await self._persist_runtime_state()
        await self._publish_queue_state_updates(queue_updates)
        return task_id

    def _sanitize_task_params(self, kind: TaskKind, params: dict[str, Any] | None) -> dict[str, Any]:
        if kind != "eval":
            raise ValueError("The public Studio only supports eval tasks")
        raw = dict(params or {})
        raw_preset = str(raw.get("experiment_preset") or "").strip().lower()
        if raw_preset and raw_preset not in set(EXPERIMENT_PRESET_VALUES):
            raise ValueError("The public Studio only supports the 7 public eval presets")
        schema = get_eval_schema(workspace_root=self.workspace_root)
        allowed = {
            str(field.get("name") or "").strip()
            for field in schema.get("fields", [])
            if str(field.get("name") or "").strip()
        }
        sanitized: dict[str, Any] = {}
        for key, value in raw.items():
            if key in allowed:
                sanitized[key] = value
        return sanitized

    async def _prepare_stop_task(self, task_id: str) -> dict[str, Any]:
        eval_state_path: str | None = None
        async with self._lock:
            task = self._tasks.get(task_id)
            proc = self._procs.get(task_id)
            runner = self._runner_tasks.get(task_id)
            cancel_before_start = bool(task) and proc is None and task.status == "queued"
            cancel_paused_eval = (
                bool(task)
                and task is not None
                and proc is None
                and task.kind == "eval"
                and not bool(task.metadata.get("historical"))
                and task.status == "paused"
            )
            queue_updates: list[StudioTask] = []
            if (cancel_before_start or cancel_paused_eval) and task is not None:
                if cancel_before_start:
                    self._task_runtime_auth_tokens.pop(task.task_id, None)
                task.metadata["cancel_requested"] = True
                task.error = None
                task.pid = None
                task.return_code = None if cancel_before_start else 143
                task.finished_at = self._now_iso()
                if self._task_dispatch_mode(task) == "serial_queue":
                    self._set_task_queue_metadata_locked(task, queue_waiting=False, queue_position=None)
                    self._remove_task_from_serial_queue_locked(task.task_id)
                else:
                    self._runner_tasks.pop(task.task_id, None)
                self._clear_task_pause_metadata_locked(task)
                if cancel_paused_eval:
                    eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir") or task.metadata.get("eval_dir")
                    if isinstance(eval_root_raw, str) and eval_root_raw.strip():
                        eval_state_path = str(Path(eval_root_raw).expanduser().resolve() / "eval_state.json")
                task.status = "cancelled"  # type: ignore[assignment]
                queue_updates = self._refresh_dispatch_locked()
            else:
                runner = None
        return {
            "task": task,
            "proc": proc,
            "runner": runner,
            "cancel_before_start": cancel_before_start,
            "cancel_paused_eval": cancel_paused_eval,
            "eval_state_path": eval_state_path,
            "queue_updates": queue_updates,
        }

    async def _finalize_pre_launch_task_stop(
        self,
        *,
        task: StudioTask,
        cancel_before_start: bool,
        eval_state_path: str | None,
        queue_updates: list[StudioTask],
    ) -> None:
        if eval_state_path:
            await asyncio.to_thread(
                self._write_eval_state,
                Path(eval_state_path).parent,
                {
                    "status": "cancelled",
                    "pause_mode": "none",
                    "resumable": False,
                    "cancelled_at": now_tz8_iso(),
                },
            )
        await self._persist_runtime_state()
        await self._publish_queue_state_updates(queue_updates)
        await self._set_status(
            task,
            "cancelled",
            extra={
                "message": "cancelled_before_start" if cancel_before_start else "cancelled_while_paused",
                "dispatch_mode": self._task_dispatch_mode(task),
                **({"eval_state_path": eval_state_path} if eval_state_path else {}),
            },
        )

    async def _terminate_running_task(self, task: StudioTask, proc: asyncio.subprocess.Process) -> bool:
        if proc.returncode is not None:
            return False
        task.metadata["cancel_requested"] = True
        control_path: str | None = None
        if task.kind == "eval" and not task.metadata.get("historical"):
            control_path = await self._request_eval_stop_control(task)
        process_snapshot = snapshot_process_tree(getattr(proc, "pid", None))
        if not self._signal_task_process(proc, force=False, snapshot=process_snapshot):
            return False
        payload: dict[str, Any] = {"message": "terminate sent"}
        if control_path:
            payload["control_path"] = control_path
        await self._publish(task, "task_status", payload)

        async def _force_kill() -> None:
            await asyncio.sleep(5)
            if self._signal_task_process(proc, force=True, snapshot=process_snapshot):
                await self._publish(task, "task_status", {"message": "kill sent"})

        asyncio.create_task(_force_kill())
        return True

    async def stop_task(self, task_id: str) -> bool:
        stop_state = await self._prepare_stop_task(task_id)
        task = stop_state["task"]
        proc = stop_state["proc"]
        runner = stop_state["runner"]
        cancel_before_start = bool(stop_state["cancel_before_start"])
        cancel_paused_eval = bool(stop_state["cancel_paused_eval"])
        eval_state_path = stop_state["eval_state_path"]
        queue_updates = stop_state["queue_updates"]
        if not task:
            return False
        if runner is not None and proc is None:
            runner.cancel()
        if cancel_before_start or cancel_paused_eval:
            await self._finalize_pre_launch_task_stop(
                task=task,
                cancel_before_start=cancel_before_start,
                eval_state_path=eval_state_path,
                queue_updates=queue_updates,
            )
            return True
        if not proc:
            return False
        return await self._terminate_running_task(task, proc)

    @staticmethod
    def _signal_task_process(
        proc: asyncio.subprocess.Process,
        *,
        force: bool,
        snapshot: ProcessTreeSnapshot | None = None,
    ) -> bool:
        if proc.returncode is not None and snapshot is None:
            return False
        sig = signal.SIGKILL if force else signal.SIGTERM
        pid = getattr(proc, "pid", None)
        if signal_process_tree(pid, sig, snapshot=snapshot):
            return True
        if proc.returncode is not None:
            return False
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            return True
        except ProcessLookupError:
            return False

    async def _request_eval_stop_control(self, task: StudioTask) -> str | None:
        eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir")
        if not isinstance(eval_root_raw, str) or not eval_root_raw.strip():
            return None
        eval_root = Path(eval_root_raw).expanduser().resolve()
        runs_payload = await self.list_task_eval_runs(task.task_id)
        runs = [item for item in (runs_payload.get("runs") or []) if isinstance(item, dict)]
        control = self._load_eval_control(self._eval_control_path(eval_root))
        skip_task_indexes = self._norm_int_set(control.get("skip_task_indexes"))
        skip_repeat_dirs = self._norm_str_set(control.get("skip_repeat_dirs"))
        skip_run_dirs = self._norm_str_set(control.get("skip_run_dirs"))
        force_abort_task_indexes = self._norm_int_set(control.get("force_abort_task_indexes"))
        force_abort_repeat_dirs = self._norm_str_set(control.get("force_abort_repeat_dirs"))

        for item in runs:
            task_index = item.get("task_index")
            if task_index not in {None, ""}:
                try:
                    task_index_int = int(task_index)
                except Exception:
                    task_index_int = None
                if task_index_int is not None:
                    skip_task_indexes.add(task_index_int)
                    exec_state = str(item.get("exec_state") or "").strip().lower()
                    if exec_state == "running":
                        force_abort_task_indexes.add(task_index_int)

            repeat_dir = str(item.get("repeat_dir") or "").strip()
            if repeat_dir:
                repeat_dir_resolved = str(Path(repeat_dir).expanduser().resolve())
                skip_repeat_dirs.add(repeat_dir_resolved)
                exec_state = str(item.get("exec_state") or "").strip().lower()
                if exec_state == "running":
                    force_abort_repeat_dirs.add(repeat_dir_resolved)

            run_dir = str(item.get("run_dir") or "").strip()
            if run_dir:
                skip_run_dirs.add(str(Path(run_dir).expanduser().resolve()))

        control_path = self._write_eval_control(
            eval_root,
            {
                "pause_after_active": True,
                "pause_mode": "force",
                "skip_task_indexes": sorted(skip_task_indexes),
                "skip_repeat_dirs": sorted(skip_repeat_dirs),
                "skip_run_dirs": sorted(skip_run_dirs),
                "force_abort_task_indexes": sorted(force_abort_task_indexes),
                "force_abort_repeat_dirs": sorted(force_abort_repeat_dirs),
            },
        )
        await self._publish(
            task,
            "task_status",
            {
                "message": "eval_stop_requested",
                "control_path": str(control_path),
                "run_count": len(runs),
                "force_abort_count": len(force_abort_task_indexes),
            },
        )
        return str(control_path)

    @staticmethod
    def _eval_control_path(eval_root: Path) -> Path:
        return eval_root / "control.json"

    @staticmethod
    def _eval_state_path(eval_root: Path) -> Path:
        return eval_root / "eval_state.json"

    @staticmethod
    def _load_eval_control(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _load_eval_state(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write_eval_control(self, eval_root: Path, payload: dict[str, Any]) -> Path:
        control_path = self._eval_control_path(eval_root)
        control = self._load_eval_control(control_path)
        merged = {
            "created_at": control.get("created_at") or now_tz8_iso(),
            "updated_at": now_tz8_iso(),
            "skip_task_indexes": sorted(self._norm_int_set((payload.get("skip_task_indexes") if "skip_task_indexes" in payload else control.get("skip_task_indexes")) or [])),
            "skip_repeat_dirs": sorted(self._norm_str_set((payload.get("skip_repeat_dirs") if "skip_repeat_dirs" in payload else control.get("skip_repeat_dirs")) or [])),
            "skip_run_dirs": sorted(self._norm_str_set((payload.get("skip_run_dirs") if "skip_run_dirs" in payload else control.get("skip_run_dirs")) or [])),
            "pause_after_active": bool(payload.get("pause_after_active", control.get("pause_after_active", False))),
            "pause_mode": str(payload.get("pause_mode") or control.get("pause_mode") or "none"),
            "force_abort_task_indexes": sorted(self._norm_int_set((payload.get("force_abort_task_indexes") if "force_abort_task_indexes" in payload else control.get("force_abort_task_indexes")) or [])),
            "force_abort_repeat_dirs": sorted(self._norm_str_set((payload.get("force_abort_repeat_dirs") if "force_abort_repeat_dirs" in payload else control.get("force_abort_repeat_dirs")) or [])),
        }
        control_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return control_path

    def _write_eval_state(self, eval_root: Path, payload: dict[str, Any]) -> Path:
        state_path = self._eval_state_path(eval_root)
        existing = self._load_eval_state(state_path)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(payload or {})
        merged["updated_at"] = now_tz8_iso()
        if not merged.get("created_at"):
            merged["created_at"] = now_tz8_iso()
        state_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return state_path

    def _reconcile_eval_state_after_exit(self, eval_root: Path, *, final_status: str) -> None:
        if str(final_status or "").strip().lower() != "cancelled":
            return
        existing = self._load_eval_state(self._eval_state_path(eval_root))
        existing_status = str(existing.get("status") or "").strip().lower()
        if existing_status in {"cancelled", "completed", "paused"}:
            return
        self._write_eval_state(
            eval_root,
            {
                "status": "cancelled",
                "pause_mode": "none",
                "resumable": False,
                "cancelled_at": now_tz8_iso(),
                "running_task_count": 0,
                "active_task_indexes": [],
                "active_repeat_dirs": [],
            },
        )

    @staticmethod
    def _norm_str_set(values: Any) -> set[str]:
        out: set[str] = set()
        if not isinstance(values, list):
            return out
        for item in values:
            text = str(item or "").strip()
            if text:
                out.add(text)
        return out

    @staticmethod
    def _norm_int_set(values: Any) -> set[int]:
        out: set[int] = set()
        if not isinstance(values, list):
            return out
        for item in values:
            try:
                out.add(int(item))
            except Exception:
                continue
        return out

    async def pause_eval(self, task_id: str, *, pause_reason: str = PAUSE_REASON_MANUAL) -> bool:
        task = await self._get_task_obj(task_id)
        if task is None or task.kind != "eval" or task.metadata.get("historical"):
            return False
        if task.status not in {"queued", "starting", "running", "finishing"}:
            return False
        eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir")
        if not isinstance(eval_root_raw, str) or not eval_root_raw.strip():
            return False
        eval_root = Path(eval_root_raw).expanduser().resolve()
        control_path = self._write_eval_control(eval_root, {"pause_after_active": True, "pause_mode": "graceful"})
        async with self._lock:
            self._set_task_pause_reason_locked(
                task,
                pause_reason,
                pause_requested=True,
                pause_confirmed=False,
                pause_mode="graceful",
            )
        await self._persist_runtime_state()
        await self._set_status(
            task,
            "pausing",
            extra={
                "message": "eval_pause_requested",
                "control_path": str(control_path),
                "pause_requested": True,
                "pause_confirmed": False,
                "pause_mode": "graceful",
                "pause_reason": pause_reason,
            },
        )
        return True

    async def resume_eval(
        self,
        task_id: str,
        *,
        preserve_queue_slot: bool = False,
        defer_dispatch_refresh: bool = False,
        allow_terminal: bool = False,
    ) -> bool:
        task = await self._get_task_obj(task_id)
        if task is None or task.kind != "eval":
            return False
        status = str(task.status or "").strip().lower()
        if status != "paused" and not (allow_terminal and self._is_terminal_task_status(status)):
            return False
        eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir") or task.metadata.get("eval_dir")
        if not isinstance(eval_root_raw, str) or not eval_root_raw.strip():
            return False
        eval_root = Path(eval_root_raw).expanduser().resolve()
        cmd = ["uv", "run", "python", "main.py", "eval", "resume", "--eval-root", str(eval_root)]
        task.command = cmd
        task.cwd = str(self.workspace_root)
        task.pid = None
        task.return_code = None
        task.finished_at = None
        task.error = None
        task.metadata["cancel_requested"] = False
        task.metadata["historical"] = False
        task.metadata["out_dir"] = str(eval_root.parent)
        task.paths["eval_root"] = str(eval_root)
        task.paths["eval_dir"] = str(eval_root)
        task.paths.setdefault("progress_jsonl", str(eval_root / "progress.jsonl"))
        task.paths.setdefault("planned_runs_json", str(eval_root / "planned_runs.json"))
        task.register_root(eval_root)
        queue_updates: list[StudioTask] = []
        async with self._lock:
            self._ensure_eval_task_runtime_metadata_locked(task)
            task.status = "queued"  # type: ignore[assignment]
            self._clear_task_pause_metadata_locked(task)
            task.metadata["queued_at"] = self._now_iso()
            if self._task_dispatch_mode(task) == "serial_queue":
                if task.task_id not in self._serial_queue_task_ids:
                    self._serial_queue_task_ids.append(task.task_id)
                elif not preserve_queue_slot:
                    self._serial_queue_task_ids = [item for item in self._serial_queue_task_ids if item != task.task_id]
                    self._serial_queue_task_ids.append(task.task_id)
            if not defer_dispatch_refresh:
                queue_updates = self._refresh_dispatch_locked()
                if not any(item.task_id == task.task_id for item in queue_updates):
                    queue_updates.append(task)
        await self._persist_runtime_state()
        if defer_dispatch_refresh:
            return True
        await self._publish_queue_state_updates(queue_updates)
        return True

    async def load_historical_tasks(self) -> int:
        """Scan runs/ and evals/ directories to load historical tasks.

        Returns the number of historical tasks loaded.
        """
        discovered_tasks = await asyncio.to_thread(
            self._process_runtime_state.load_historical_tasks,
            evals_dir=self._state_paths.evals_dir,
            iter_eval_root_dirs=self._iter_eval_root_dirs,
            reconstruct_eval_task=self._reconstruct_eval_task,
        )
        async with self._lock:
            for task in discovered_tasks:
                self._assign_unique_task_id_locked(task)
                self._tasks[task.task_id] = task
        self._last_historical_refresh_monotonic = self._monotonic_fn()
        return len(discovered_tasks)

    def _schedule_historical_refresh(self, *, force: bool = False) -> None:
        task = self._historical_refresh_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self.refresh_historical_tasks(force=force))
        self._historical_refresh_task = task

        def _consume_result(done: asyncio.Task[int]) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._historical_refresh_last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Studio historical task refresh failed", exc_info=exc)
                return
            self._historical_refresh_last_error = None

        task.add_done_callback(_consume_result)

    def _discover_historical_task_candidates(
        self,
        *,
        evals_dir: Path,
        known_eval_dirs: set[str],
    ) -> list[tuple[Path, StudioTask | None]]:
        discovered_eval_dirs = self._process_runtime_state.discover_new_eval_dirs(
            evals_dir=evals_dir,
            known_eval_dirs=set(known_eval_dirs),
            iter_eval_root_dirs=self._iter_eval_root_dirs,
            norm_dir_path=self._norm_dir_path,
        )
        discovered_eval_tasks = [(eval_dir, self._reconstruct_eval_task(eval_dir)) for eval_dir in discovered_eval_dirs]
        return discovered_eval_tasks

    async def refresh_historical_tasks(self, *, force: bool = False) -> int:
        """Incrementally discover run/eval directories created after Studio startup."""
        pending_changed = False
        async with self._historical_refresh_lock:
            async with self._lock:
                self._prune_missing_inactive_tasks_locked()

                if not force:
                    now = self._monotonic_fn()
                    if (
                        self._last_historical_refresh_monotonic > 0
                        and (now - self._last_historical_refresh_monotonic) < self._historical_refresh_interval_sec
                    ):
                        return 0

                evals_dir = self._state_paths.evals_dir
                known_eval_dirs = self._process_runtime_state.collect_known_task_roots(
                    list(self._tasks.values()),
                    task_root_key=self._task_root_key,
                )

            discovered_eval_tasks = await asyncio.to_thread(
                self._discover_historical_task_candidates,
                evals_dir=evals_dir,
                known_eval_dirs=known_eval_dirs,
            )

            added = 0
            async with self._lock:
                known_eval_dirs = self._process_runtime_state.collect_known_task_roots(
                    list(self._tasks.values()),
                    task_root_key=self._task_root_key,
                )
                for eval_dir, task in discovered_eval_tasks:
                    eval_key = self._norm_dir_path(str(eval_dir))
                    if not eval_key or eval_key in known_eval_dirs:
                        continue
                    if self._maybe_attach_eval_root_to_live_task_locked(eval_dir):
                        known_eval_dirs.add(eval_key)
                        continue
                    if not task:
                        continue
                    self._assign_unique_task_id_locked(task)
                    self._tasks[task.task_id] = task
                    known_eval_dirs.add(eval_key)
                    added += 1

                pending_changed = self._prune_stale_pending_task_groups_locked()
            self._last_historical_refresh_monotonic = self._monotonic_fn()
            if pending_changed:
                await self._persist_pending_task_groups()
            return added

    def _maybe_attach_eval_root_to_live_task_locked(self, eval_dir: Path) -> bool:
        eval_key = self._norm_dir_path(str(eval_dir))
        if not eval_key:
            return False
        parent_out_dir = self._norm_dir_path(str(eval_dir.parent))
        if not parent_out_dir:
            return False
        for task in self._tasks.values():
            if task.kind != "eval" or task.metadata.get("historical"):
                continue
            existing_root = self._task_root_key(task)
            if existing_root == eval_key:
                return True
            out_dir = self._norm_dir_path(task.metadata.get("out_dir"))
            if out_dir != parent_out_dir:
                continue
            if task.paths.get("eval_root") or task.paths.get("eval_dir"):
                continue
            if not self._eval_root_matches_live_task(eval_dir, task):
                continue
            task.paths["eval_root"] = str(eval_dir)
            task.paths["eval_dir"] = str(eval_dir)
            task.metadata["eval_dir"] = str(eval_dir)
            task.paths.setdefault("planned_runs_json", str(eval_dir / "planned_runs.json"))
            task.paths.setdefault("progress_jsonl", str(eval_dir / "progress.jsonl"))
            for fname in ["summary.json", "results.jsonl", "progress.jsonl", "eval_state.json"]:
                fpath = eval_dir / fname
                if fpath.exists():
                    task.paths[fname.replace(".", "_")] = str(fpath)
            task.register_root(eval_dir)
            if self._pending_task_tags_locked(task.task_id):
                asyncio.create_task(self._migrate_pending_tags_to_task_root(task))
            return True
        return False

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        raw_items: list[Any]
        if isinstance(value, list):
            raw_items = value
        elif isinstance(value, tuple):
            raw_items = list(value)
        else:
            raw_items = str(value).split(",")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            text = str(item or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered == "native":
                lowered = "naive"
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(lowered)
        return normalized

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_optional_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return None

    @staticmethod
    def _normalize_optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text.lower() if text else None

    def _eval_root_matches_live_task(self, eval_dir: Path, task: StudioTask) -> bool:
        if task.kind != "eval":
            return False
        config = self._read_json_dict_file(eval_dir / "config.json")
        if not config:
            return False
        params = task.params or {}

        task_modes = self._normalize_string_list(params.get("modes"))
        config_modes = self._normalize_string_list(config.get("modes"))
        if task_modes and config_modes and task_modes != config_modes:
            return False

        task_apps = self._normalize_string_list(params.get("app_names"))
        config_apps = self._normalize_string_list(config.get("app_names"))
        if task_apps and config_apps and task_apps != config_apps:
            return False

        for key in (
            "knowledge_distillation_mode",
            "knowledge_packaging_mode",
            "runtime_injection_mode",
            "auto_knowledge_validate_mode",
            "knowledge_reuse_digest_mode",
        ):
            task_value = self._normalize_optional_text(params.get(key))
            config_value = self._normalize_optional_text(config.get(key))
            if key == "auto_knowledge_validate_mode" and (
                task_value == "full" or config_value == "full"
            ):
                return False
            task_distillation = self._normalize_optional_text(
                params.get("knowledge_distillation_mode")
            )
            config_distillation = self._normalize_optional_text(
                config.get("knowledge_distillation_mode")
            )
            task_packaging = self._normalize_optional_text(
                params.get("knowledge_packaging_mode")
            )
            config_packaging = self._normalize_optional_text(
                config.get("knowledge_packaging_mode")
            )
            if key in {"auto_knowledge_validate_mode", "knowledge_reuse_digest_mode"} and (
                task_distillation == "generic" or config_distillation == "generic"
            ):
                task_value = "off" if task_value is not None else None
                config_value = "off" if config_value is not None else None
            if key == "auto_knowledge_validate_mode" and (
                task_packaging == "embedding" or config_packaging == "embedding"
            ):
                task_value = "off" if task_value is not None else None
                config_value = "off" if config_value is not None else None
            if key in {"auto_knowledge_validate_mode", "knowledge_reuse_digest_mode"} and (
                task_packaging in {"analysis_log_rag", "analysis_log_rag_initial"}
                or config_packaging in {"analysis_log_rag", "analysis_log_rag_initial"}
            ):
                task_value = "off" if task_value is not None else None
                config_value = "off" if config_value is not None else None
            if task_value is not None and config_value is not None and task_value != config_value:
                return False

        for key in ("max_cases", "max_apps", "max_sources", "parallel", "repeats"):
            task_value = self._normalize_optional_int(params.get(key))
            config_value = self._normalize_optional_int(config.get(key))
            if task_value is not None and config_value is not None and task_value != config_value:
                return False

        task_serialize = self._normalize_optional_bool(params.get("serialize_within_app"))
        config_serialize = self._normalize_optional_bool(config.get("serialize_within_app"))
        if task_serialize is not None and config_serialize is not None and task_serialize != config_serialize:
            return False

        return True

    def _reconstruct_eval_task(self, eval_dir: Path) -> StudioTask | None:
        return self._task_reconstructor.reconstruct_eval_task(eval_dir)

    async def list_tasks(self, *, summary: bool = False) -> list[dict[str, Any]]:
        self._schedule_historical_refresh()
        async with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: (t.created_at, t.task_id), reverse=True)
        if summary:
            return [t.to_summary_dict() for t in tasks]
        return [t.to_dict() for t in tasks]

    async def list_public_tasks(self, *, summary: bool = False) -> list[dict[str, Any]]:
        self._schedule_historical_refresh()
        async with self._lock:
            tasks = [task for task in self._tasks.values() if task.kind == "eval"]
        tasks.sort(key=lambda t: (t.created_at, t.task_id), reverse=True)
        return [self._process_inspection.public_task_payload(t, summary=summary) for t in tasks]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            task = self._tasks.get(task_id)
        return task.to_dict() if task else None

    async def get_public_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            task = self._tasks.get(task_id)
        if task is None or task.kind != "eval":
            return None
        return self._process_inspection.public_task_payload(task, summary=False) if task else None

    async def list_tags(self, query: Any = None) -> dict[str, Any]:
        await self.refresh_historical_tasks()
        async with self._lock:
            tasks = list(self._tasks.values())
        return self._process_inspection.list_tags(
            tasks,
            query=query,
            normalize_group_name=self._normalize_group_name,
            normalize_task_tags=self._normalize_task_tags,
        )

    async def lookup_tag(self, tag: Any) -> dict[str, Any]:
        await self.refresh_historical_tasks()
        async with self._lock:
            tasks = list(self._tasks.values())
        return self._process_inspection.lookup_tag(
            tasks,
            tag=tag,
            normalize_group_name=self._normalize_group_name,
            normalize_task_tags=self._normalize_task_tags,
            norm_dir_path=self._norm_dir_path,
        )

    async def lookup_public_tag(self, tag: Any) -> dict[str, Any]:
        payload = await self.lookup_tag(tag)
        return self._process_inspection.public_lookup_tag_payload(payload)

    async def set_task_tags(self, task_id: str, tags: Any) -> dict[str, Any]:
        normalized_tags = self._normalize_task_tags(tags)
        pending_changed = False
        persisted_to = "pending_store"
        root: str | None = None
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            normalized_tags = self._set_task_tags_locked(task, normalized_tags)
            root = self._task_root_key(task)
            if root:
                persisted_to = "task_sidecar"
                if task.task_id in self._pending_task_groups:
                    self._pending_task_groups.pop(task.task_id, None)
                    pending_changed = True
            else:
                existing = self._pending_task_groups.get(task.task_id)
                if normalized_tags:
                    next_record = {"tags": normalized_tags, "updated_at": self._now_iso()}
                    if existing != next_record:
                        self._pending_task_groups[task.task_id] = next_record
                        pending_changed = True
                elif task.task_id in self._pending_task_groups:
                    self._pending_task_groups.pop(task.task_id, None)
                    pending_changed = True

        if root:
            await asyncio.to_thread(self._write_task_group_sidecar, root, normalized_tags)
        if pending_changed:
            await self._persist_pending_task_groups()
        await self._persist_runtime_state()
        compat_group = normalized_tags[0] if len(normalized_tags) == 1 else None
        await self._publish(
            task,
            "task_status",
            {
                "tags": normalized_tags,
                "tags_persisted_to": persisted_to,
                "group": compat_group,
                "group_persisted_to": persisted_to,
                "message": "task_tags_updated",
            },
        )
        return {"ok": True, "tags": normalized_tags, "group": compat_group, "persisted_to": persisted_to}

    async def subscribe_task_events(self, task_id: str, *, replay_last: int = 200):
        return self._event_bus.subscribe_task(task_id, replay_last=replay_last)

    async def subscribe_all_events(self, *, replay_last: int = 100):
        return self._event_bus.subscribe_all(replay_last=replay_last)

    async def public_event_payload(self, event: StudioEvent) -> dict[str, Any]:
        async with self._lock:
            task = self._tasks.get(event.task_id)
        return self._process_inspection.public_event_payload(event, task=task)

    async def list_task_artifacts(
        self,
        task_id: str,
        *,
        selected_run_dir: str | None = None,
        include_all_eval_runs: bool = False,
    ) -> dict[str, Any]:
        task = await self._get_task_obj(task_id)
        if task is None:
            raise KeyError(task_id)
        return self._process_inspection.list_task_artifacts(
            task,
            selected_run_dir=selected_run_dir,
            include_all_eval_runs=include_all_eval_runs,
        )

    async def list_task_eval_runs(self, task_id: str, *, detail: str = "summary") -> dict[str, Any]:
        task = await self._get_task_obj(task_id)
        if task is None:
            raise KeyError(task_id)
        return self._process_inspection.list_task_eval_runs(
            task,
            is_terminal_eval_run_status=self._is_terminal_eval_run_status,
            detail=detail,
        )

    async def list_public_task_eval_runs(self, task_id: str, *, detail: str = "summary") -> dict[str, Any]:
        task = await self._get_task_obj(task_id)
        if task is None:
            raise KeyError(task_id)
        payload = self._process_inspection.list_task_eval_runs(
            task,
            is_terminal_eval_run_status=self._is_terminal_eval_run_status,
            detail=detail,
        )
        return self._process_inspection.public_eval_runs_payload(task, payload)

    async def read_task_artifact(self, task_id: str, path: str, *, max_bytes: int = 2_000_000) -> dict[str, Any]:
        task = await self._get_task_obj(task_id)
        if task is None:
            raise KeyError(task_id)
        return self._process_inspection.read_task_artifact(task, path, max_bytes=max_bytes)

    async def tail_task_artifact(
        self,
        task_id: str,
        path: str,
        offset: int = 0,
        *,
        max_bytes: int = 512_000,
        from_end: bool = False,
    ) -> dict[str, Any]:
        task = await self._get_task_obj(task_id)
        if task is None:
            raise KeyError(task_id)
        return self._process_inspection.tail_task_artifact(
            task,
            path,
            offset,
            max_bytes=max_bytes,
            from_end=from_end,
        )

    async def _get_task_obj(self, task_id: str) -> StudioTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def _prepare_task_run(self, task: StudioTask) -> tuple[Path, set[str], dict[str, str]]:
        await self._wait_for_dispatch_resume()
        if task.status != "pausing":
            await self._set_status(task, "starting")
        default_out_dir = self._state_paths.evals_dir
        out_dir = Path(str(task.metadata.get("out_dir") or default_out_dir))
        out_dir.mkdir(parents=True, exist_ok=True)
        before_dirs = {p.name for p in out_dir.iterdir() if p.is_dir()} if out_dir.exists() else set()

        child_env = self._build_child_env(task)
        await self._apply_eval_start_git_snapshot(task, child_env)
        return out_dir, before_dirs, child_env

    async def _apply_eval_start_git_snapshot(self, task: StudioTask, child_env: dict[str, str]) -> None:
        if task.kind != "eval":
            return
        submitted_git_snapshot = self._normalize_workspace_git_snapshot(task.metadata.get("workspace_git_submitted"))
        started_git_snapshot = await asyncio.to_thread(self._capture_workspace_git_snapshot)
        async with self._lock:
            if submitted_git_snapshot is not None:
                task.metadata["workspace_git_submitted"] = submitted_git_snapshot
            task.metadata["workspace_git_started"] = started_git_snapshot
            task.metadata["workspace_git_last_started"] = started_git_snapshot
        child_env[FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV] = json.dumps(started_git_snapshot, ensure_ascii=False)
        if submitted_git_snapshot is not None:
            child_env[FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_ENV] = json.dumps(
                submitted_git_snapshot,
                ensure_ascii=False,
            )
        await self._persist_runtime_state()
        await self._publish(
            task,
            "task_status",
            {
                "workspace_git_submitted": submitted_git_snapshot,
                "workspace_git_started": started_git_snapshot,
                "workspace_git_last_started": started_git_snapshot,
            },
        )

    async def _spawn_task_process(self, task: StudioTask, child_env: dict[str, str]) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *task.command,
            cwd=task.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            start_new_session=True,
        )
        async with self._lock:
            self._procs[task.task_id] = proc
            task.pid = proc.pid
            task.started_at = self._now_iso()
            task.metadata["process_started_at_epoch_sec"] = int(self._time_fn())
            task.metadata.pop("startup_recovery_pending", None)
            task.metadata.pop("persisted_orphan_pid", None)
            task.metadata.pop("persisted_orphan_started_at", None)
            task.metadata.pop("persisted_orphan_started_at_epoch_sec", None)
        await self._persist_runtime_state()
        await self._publish(task, "task_started", {"pid": proc.pid, "command": task.command, "cwd": task.cwd})
        if task.status != "pausing":
            await self._set_status(task, "running")
        return proc

    async def _monitor_task_process(
        self,
        task: StudioTask,
        proc: asyncio.subprocess.Process,
        *,
        out_dir: Path,
        before_dirs: set[str],
        watch_state: RealtimeWatchState,
    ) -> int:
        stdout_task = asyncio.create_task(self._stream_pipe(task, proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(self._stream_pipe(task, proc.stderr, "stderr"))
        wait_task = asyncio.create_task(proc.wait())

        while True:
            await self._poll_task_files(task, out_dir=out_dir, before_dirs=before_dirs, watch_state=watch_state)
            if wait_task.done():
                break
            await asyncio.sleep(0.35)

        await self._poll_task_files(task, out_dir=out_dir, before_dirs=before_dirs, watch_state=watch_state)
        if task.status != "pausing":
            await self._set_status(task, "finishing")
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return proc.returncode if proc.returncode is not None else await proc.wait()

    def _determine_task_final_status(self, task: StudioTask, return_code: int) -> str:
        if task.kind == "eval":
            eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir")
            eval_state = {}
            eval_root: Path | None = None
            if isinstance(eval_root_raw, str) and eval_root_raw.strip():
                eval_root = Path(eval_root_raw).expanduser().resolve()
                eval_state = self._load_eval_state(self._eval_state_path(eval_root))
            state_status = str(eval_state.get("status") or "").strip().lower()
            if state_status == "paused":
                return "paused"
            if bool(task.metadata.get("cancel_requested")) and int(return_code) != 0:
                return "cancelled"
            if int(return_code) == 0 and eval_root is not None:
                summary = self._read_json_dict_file(eval_root / "summary.json")
                if isinstance(summary, dict):
                    if int(summary.get("error_count") or 0) > 0:
                        return "error"
                    if int(summary.get("warning_count") or 0) > 0:
                        return "warning"
            return "success" if int(return_code) == 0 else "error"
        if bool(task.metadata.get("cancel_requested")) and int(return_code) != 0:
            return "cancelled"
        return "success" if int(return_code) == 0 else "error"

    async def _complete_task_after_exit(self, task: StudioTask, return_code: int) -> None:
        task.return_code = int(return_code)
        task.finished_at = self._now_iso()
        if task.status not in {"cancelled", "timeout"}:
            final_status = self._determine_task_final_status(task, return_code)
            if task.kind == "eval":
                eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir")
                if isinstance(eval_root_raw, str) and eval_root_raw.strip():
                    eval_root = Path(eval_root_raw).expanduser().resolve()
                    summary = self._read_json_dict_file(eval_root / "summary.json")
                    self._attach_eval_status_counts(task, summary)
                    rebuild_eval_progress_metadata_from_progress_file(task.metadata, eval_root / "progress.jsonl")
                    if EVAL_OPEN_CODE_COST_KEY not in task.metadata:
                        rebuild_eval_progress_metadata_from_results_file(
                            task.metadata,
                            eval_root / "results.jsonl",
                            total_count=self._eval_summary_total_count(summary),
                        )
            await self._set_status(task, final_status)
            if task.kind == "eval":
                eval_root_raw = task.paths.get("eval_root") or task.paths.get("eval_dir")
                if isinstance(eval_root_raw, str) and eval_root_raw.strip():
                    await asyncio.to_thread(
                        self._reconcile_eval_state_after_exit,
                        Path(eval_root_raw).expanduser().resolve(),
                        final_status=final_status,
                    )
        await self._publish(
            task,
            "task_finished",
            {
                "return_code": task.return_code,
                "status": task.status,
                "paths": dict(task.paths),
            },
        )

    async def _cleanup_task_after_run(self, task: StudioTask) -> None:
        queue_updates: list[StudioTask] = []
        async with self._lock:
            self._procs.pop(task.task_id, None)
            self._runner_tasks.pop(task.task_id, None)
            self._task_runtime_auth_tokens.pop(task.task_id, None)
            task.pid = None
            task.metadata.pop("process_started_at_epoch_sec", None)
            if self._task_dispatch_mode(task) == "serial_queue" and self._is_terminal_task_status(task.status):
                self._remove_task_from_serial_queue_locked(task.task_id)
                self._set_task_queue_metadata_locked(task, queue_waiting=False, queue_position=None)
            if task.status != "paused":
                self._clear_task_pause_metadata_locked(task)
            queue_updates = self._refresh_dispatch_locked()
        await self._persist_runtime_state()
        await self._publish_queue_state_updates(queue_updates)
        await self._event_bus.close_task_streams(task.task_id)

    async def _run_task(self, task_id: str) -> None:
        task = await self._get_task_obj(task_id)
        if task is None:
            return
        watch_state = RealtimeWatchState()
        try:
            out_dir, before_dirs, child_env = await self._prepare_task_run(task)
            self._prime_eval_progress_cursor(task, watch_state)
            proc = await self._spawn_task_process(task, child_env)
            return_code = await self._monitor_task_process(
                task,
                proc,
                out_dir=out_dir,
                before_dirs=before_dirs,
                watch_state=watch_state,
            )
            await self._complete_task_after_exit(task, return_code)
        except Exception as exc:
            task.error = str(exc)
            task.finished_at = self._now_iso()
            await self._set_status(task, "error")
            await self._publish(task, "task_error", {"error": str(exc)})
        finally:
            await self._cleanup_task_after_run(task)

    async def _stream_pipe(
        self,
        task: StudioTask,
        pipe: asyncio.StreamReader | None,
        stream_name: str,
    ) -> str:
        if pipe is None:
            return ""
        collected: list[str] = []
        while True:
            chunk = await pipe.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            collected.append(text)
            await self._publish(task, f"subprocess_{stream_name}", {"text": text})
            self._try_extract_paths_from_stdout(task, text)
        return "".join(collected)

    def _try_extract_paths_from_stdout(self, task: StudioTask, line: str) -> None:
        # Best-effort extraction from CLI summary output at task end.
        mapping = {
            "Eval directory:": "eval_root",
            "Summary file:": "summary_json",
            "Results JSONL:": "results_jsonl",
            "评估目录:": "eval_root",
            "汇总文件:": "summary_json",
            "结果明细:": "results_jsonl",
        }
        stripped = line.strip()
        for prefix, key in mapping.items():
            if stripped.startswith(prefix):
                value = stripped[len(prefix) :].strip()
                if value:
                    p = Path(value)
                    task.paths[key] = value
                    if p.exists():
                        task.register_root(p if p.is_dir() else p.parent)
                    if key == "eval_root" and self._pending_task_tags_locked(task.task_id):
                        asyncio.create_task(self._migrate_pending_tags_to_task_root(task))
                break

    @staticmethod
    def _prime_eval_progress_cursor(task: StudioTask, watch_state: RealtimeWatchState) -> None:
        if task.kind != "eval":
            return
        eval_root_str = task.paths.get("eval_root")
        if not isinstance(eval_root_str, str) or not eval_root_str.strip():
            return
        progress_path = Path(eval_root_str) / "progress.jsonl"
        try:
            size = progress_path.stat().st_size
        except OSError:
            return
        key = str(progress_path)
        if key not in watch_state.generic_cursors:
            watch_state.generic_cursors[key] = TailCursor(path=key, offset=int(size))

    async def _poll_task_files(
        self,
        task: StudioTask,
        *,
        out_dir: Path,
        before_dirs: set[str],
        watch_state: RealtimeWatchState,
    ) -> None:
        self._detect_primary_output_dir(task, out_dir=out_dir, before_dirs=before_dirs)
        self._poll_eval_files(task, watch_state)
        self._poll_artifact_changes(task, watch_state)

    def _detect_primary_output_dir(self, task: StudioTask, *, out_dir: Path, before_dirs: set[str]) -> None:
        key = "eval_root"
        if task.paths.get(key):
            return
        new_dirs = find_new_subdirs(out_dir, before_dirs)
        if not new_dirs:
            return
        matching_dirs = [path for path in new_dirs if self._eval_root_matches_live_task(path, task)]
        if not matching_dirs:
            return
        chosen = matching_dirs[-1]
        task.paths[key] = str(chosen)
        task.register_root(chosen)
        task.paths.setdefault("eval_root", str(chosen))
        task.paths.setdefault("eval_dir", str(chosen))
        task.metadata["eval_dir"] = str(chosen)
        if self._pending_task_tags_locked(task.task_id):
            asyncio.create_task(self._migrate_pending_tags_to_task_root(task))
        asyncio.create_task(
            self._publish(
                task,
                "task_status",
                {
                    key: str(chosen),
                    "message": f"detected_{key}",
                },
            )
        )

    def _poll_eval_files(self, task: StudioTask, watch_state: RealtimeWatchState) -> None:
        if task.kind != "eval":
            return
        eval_root_str = task.paths.get("eval_root")
        if not eval_root_str:
            return
        eval_root = Path(eval_root_str)
        if not eval_root.exists():
            return

        self._poll_eval_pause_state(task, watch_state, eval_root)
        audit_path = eval_root / "note_only_artifact_audit.json"
        if audit_path.exists():
            task.paths.setdefault("note_only_artifact_audit_json", str(audit_path))

        progress_path = eval_root / "progress.jsonl"
        if progress_path.exists():
            cursor = watch_state.generic_cursors.setdefault(str(progress_path), TailCursor(path=str(progress_path)))
            items = tail_jsonl_objects(progress_path, cursor)
            for obj in items:
                event_name = str(obj.get("event") or "")
                repeat_dir = obj.get("repeat_dir")
                if isinstance(repeat_dir, str) and repeat_dir.strip():
                    repeat_dir_path = Path(repeat_dir).expanduser().resolve()
                    watch_state.seen_eval_runs_parent_dirs.add(str(repeat_dir_path / "runs"))
                run_dir = obj.get("run_dir")
                if isinstance(run_dir, str) and run_dir.strip():
                    self._register_eval_run_dir(task, watch_state, Path(run_dir).expanduser().resolve())
                progress_changed = update_eval_progress_metadata_from_event(task.metadata, obj)
                if progress_changed:
                    asyncio.create_task(
                        self._publish(
                            task,
                            "task_status",
                            {
                                "eval_progress": task.metadata.get("eval_progress"),
                                "eval_open_code_cost": task.metadata.get("eval_open_code_cost"),
                            },
                        )
                    )
                if event_name == "start":
                    asyncio.create_task(self._publish(task, "eval_progress_start", obj))
                elif event_name == "finish":
                    asyncio.create_task(self._publish(task, "eval_progress_finish", obj))
                else:
                    asyncio.create_task(self._publish(task, "task_status", {"progress": obj}))

        now_monotonic = self._monotonic_fn()
        if (
            watch_state.last_eval_run_dir_discovery_monotonic <= 0
            or (now_monotonic - watch_state.last_eval_run_dir_discovery_monotonic) >= 2.0
        ):
            self._discover_eval_run_dirs(task, watch_state)
            watch_state.last_eval_run_dir_discovery_monotonic = now_monotonic

        # Tail transcripts for discovered nested runs.
        for run_dir_str in self._next_eval_run_dir_batch(
            watch_state,
            cursor_attr="eval_run_tail_cursor_index",
        ):
            transcript = Path(run_dir_str) / "raw_transcript.txt"
            if transcript.exists():
                cursor = watch_state.transcript_cursors.setdefault(str(transcript), TailCursor(path=str(transcript)))
                delta = tail_text_delta(transcript, cursor)
                if delta:
                    asyncio.create_task(
                        self._publish(
                            task,
                            "run_transcript_append",
                            {"path": str(transcript), "text": delta, "scope": "eval_case_run"},
                        )
                    )

            kb_log = Path(run_dir_str) / "knowledge_injection.jsonl"
            if kb_log.exists():
                cursor = watch_state.generic_cursors.setdefault(str(kb_log), TailCursor(path=str(kb_log)))
                items = tail_jsonl_objects(kb_log, cursor)
                for obj in items:
                    asyncio.create_task(
                        self._publish(
                            task,
                            "knowledge_injection_append",
                            {"path": str(kb_log), "item": obj, "scope": "eval_case_run"},
                        )
                    )

    def _register_eval_run_dir(self, task: StudioTask, watch_state: RealtimeWatchState, run_dir_path: Path) -> None:
        run_dir_str = str(run_dir_path)
        watch_state.seen_eval_run_dirs.add(run_dir_str)
        task.register_root(run_dir_path)
        eval_run_dirs = task.metadata.setdefault("eval_run_dirs", [])
        if isinstance(eval_run_dirs, list) and run_dir_str not in eval_run_dirs:
            eval_run_dirs.append(run_dir_str)

    @staticmethod
    def _next_eval_run_dir_batch(
        watch_state: RealtimeWatchState,
        *,
        cursor_attr: str,
        limit: int = 32,
    ) -> list[str]:
        run_dirs = sorted(str(item) for item in watch_state.seen_eval_run_dirs if str(item or "").strip())
        if not run_dirs:
            setattr(watch_state, cursor_attr, 0)
            return []
        if len(run_dirs) <= limit:
            setattr(watch_state, cursor_attr, 0)
            return run_dirs
        try:
            start = int(getattr(watch_state, cursor_attr) or 0) % len(run_dirs)
        except Exception:
            start = 0
        batch = [run_dirs[(start + offset) % len(run_dirs)] for offset in range(limit)]
        setattr(watch_state, cursor_attr, (start + limit) % len(run_dirs))
        return batch

    def _poll_eval_pause_state(self, task: StudioTask, watch_state: RealtimeWatchState, eval_root: Path) -> None:
        eval_state_path = eval_root / "eval_state.json"
        if not eval_state_path.exists() or not eval_state_path.is_file():
            return
        try:
            payload = json.loads(safe_read_text(eval_state_path, max_bytes=1_000_000))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        state_status = str(payload.get("status") or "").strip().lower()
        pause_mode = str(payload.get("pause_mode") or "").strip().lower()
        if (
            state_status == watch_state.last_eval_state_status
            and pause_mode == watch_state.last_eval_pause_mode
        ):
            return
        watch_state.last_eval_state_status = state_status
        watch_state.last_eval_pause_mode = pause_mode
        task.paths.setdefault("eval_state_json", str(eval_state_path.resolve()))
        if task.status == "pausing" and state_status == "paused" and not bool(task.metadata.get("pause_confirmed")):
            task.metadata["pause_confirmed"] = True
            task.metadata["pause_confirmed_at"] = self._now_iso()
            if pause_mode:
                task.metadata["pause_mode_requested"] = pause_mode
            asyncio.create_task(
                self._publish_eval_pause_confirmed(task, pause_mode)
            )

    async def _publish_eval_pause_confirmed(self, task: StudioTask, pause_mode: str) -> None:
        await self._publish(
            task,
            "task_status",
            {
                "status": "pausing",
                "message": "eval_pause_confirmed",
                "pause_requested": True,
                "pause_confirmed": True,
                "pause_mode": pause_mode or str(task.metadata.get("pause_mode_requested") or ""),
                "pause_reason": self._task_pause_reason(task),
            },
        )
        await self._persist_runtime_state()

    def _discover_eval_run_dirs(self, task: StudioTask, watch_state: RealtimeWatchState) -> None:
        for runs_parent_str in list(watch_state.seen_eval_runs_parent_dirs):
            runs_parent = Path(runs_parent_str)
            if not runs_parent.exists() or not runs_parent.is_dir():
                continue
            try:
                run_dirs = sorted(
                    [p.expanduser().resolve() for p in runs_parent.iterdir() if p.is_dir()],
                    key=lambda p: str(p),
                )
            except Exception:
                continue
            for run_dir_path in run_dirs:
                if str(run_dir_path) in watch_state.seen_eval_run_dirs:
                    continue
                self._register_eval_run_dir(task, watch_state, run_dir_path)

    def _poll_artifact_changes(self, task: StudioTask, watch_state: RealtimeWatchState) -> None:
        candidates: list[Path] = []
        if task.kind != "eval":
            return
        eval_root_str = task.paths.get("eval_root")
        if eval_root_str:
            eval_root = Path(eval_root_str)
            eval_artifacts = [
                eval_root / "progress.jsonl",
                eval_root / "summary.json",
                eval_root / "results.jsonl",
                eval_root / "manifest.json",
                eval_root / "config.json",
            ]
            candidates.extend(eval_artifacts)
            for artifact_path in eval_artifacts:
                if artifact_path.exists():
                    task.paths.setdefault(artifact_path.name.replace(".", "_"), str(artifact_path.resolve()))
                    if artifact_path.name == "config.json":
                        task.paths.setdefault("config", str(artifact_path.resolve()))
            for run_dir_str in self._next_eval_run_dir_batch(
                watch_state,
                cursor_attr="artifact_scan_cursor_index",
            ):
                run_dir = Path(run_dir_str)
                candidates.extend(
                    [
                        run_dir / "raw_transcript.txt",
                        run_dir / "knowledge_injection.jsonl",
                        run_dir / "analysis_log_rag" / "trimmed_transcript.txt",
                        run_dir / "analysis_log_rag" / "chunks.jsonl",
                        run_dir / "analysis_log_rag" / "index_update.json",
                        run_dir / "analysis_log_rag" / "rag_retrieval.jsonl",
                        run_dir / "analysis_log_rag" / "rag_injection.jsonl",
                    ]
                )
        changed_paths: list[dict[str, Any]] = []
        for p in candidates:
            key = str(p)
            state = watch_state.artifact_stats.get(key)
            exists = p.exists()
            if not exists:
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            size = int(st.st_size)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            if state is None:
                watch_state.artifact_stats[key] = WatchedFileState(path=key, last_size=size, last_mtime_ns=mtime_ns)
                if p.is_file():
                    task.register_root(p.parent)
                changed_paths.append({"path": key, "size": size, "change": "created"})
                continue
            if state.last_size != size or state.last_mtime_ns != mtime_ns:
                state.last_size = size
                state.last_mtime_ns = mtime_ns
                changed_paths.append({"path": key, "size": size, "change": "updated"})
        if changed_paths:
            asyncio.create_task(
                self._publish(
                    task,
                    "artifacts_changed",
                    {
                        "count": len(changed_paths),
                        "paths": changed_paths,
                    },
                )
            )

    async def _set_status(self, task: StudioTask, status: str, *, extra: dict[str, Any] | None = None) -> None:
        task.status = status  # type: ignore[assignment]
        payload: dict[str, Any] = {
            "status": status,
            "dispatch_mode": self._task_dispatch_mode(task),
            "queue_waiting": bool(task.metadata.get("queue_waiting")),
        }
        if task.kind == "eval":
            payload["eval_progress"] = task.metadata.get("eval_progress")
            payload["eval_open_code_cost"] = task.metadata.get("eval_open_code_cost")
            payload["pause_reason"] = self._task_pause_reason(task)
        await self._persist_runtime_state()
        queue_position = task.metadata.get("queue_position")
        if queue_position not in {None, ""}:
            payload["queue_position"] = queue_position
        if extra:
            payload.update(extra)
        await self._publish(task, "task_status", payload)

    async def _publish(self, task: StudioTask, event_type: str, data: dict[str, Any]) -> None:
        task.last_seq += 1
        event = StudioEvent(
            event_id=f"{task.task_id}-{task.last_seq}",
            task_id=task.task_id,
            kind=task.kind,
            type=event_type,
            ts=now_tz8_iso(),
            seq=task.last_seq,
            data=data,
        )
        await self._event_bus.publish(event)

    def _build_command(self, kind: TaskKind, params: dict[str, Any]) -> tuple[list[str], Path, dict[str, Any]]:
        return self._command_codec.build_command(kind, params)

    def _build_eval_command(self, params: dict[str, Any]) -> tuple[list[str], Path, dict[str, Any]]:
        return self._command_codec.build_eval_command(params)
