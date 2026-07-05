from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowark_studio.common.models import StudioTask


@dataclass(slots=True)
class RuntimeRestoreResult:
    task_count: int
    restored_tasks: list[StudioTask]
    tasks_to_migrate_groups: list[StudioTask]


class StudioRuntimeState:
    def read_runtime_state_payload(self, path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if not path.exists() or not path.is_file():
            return None, {
                "loaded": False,
                "task_count": 0,
            }

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None, {
                "loaded": False,
                "task_count": 0,
                "error": "runtime_state_corrupt",
            }
        if not isinstance(payload, dict):
            return None, {
                "loaded": False,
                "task_count": 0,
                "error": "runtime_state_invalid",
            }
        return payload, {}

    def restore_tasks_from_runtime_payload(
        self,
        payload: dict[str, Any],
        *,
        normalize_restored_task: Any,
        pending_task_tags: Any,
        normalize_task_tags: Any,
        set_task_tags: Any,
        task_root_key: Any,
    ) -> RuntimeRestoreResult:
        restored_tasks: list[StudioTask] = []
        tasks_to_migrate_groups: list[StudioTask] = []

        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            return RuntimeRestoreResult(
                task_count=0,
                restored_tasks=[],
                tasks_to_migrate_groups=[],
            )

        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            try:
                task = StudioTask.from_dict(item)
            except Exception:
                continue
            if task.kind != "eval":
                continue
            if not normalize_restored_task(task):
                continue
            pending_tags = pending_task_tags(task.task_id)
            if pending_tags and not normalize_task_tags(task.metadata.get("tags")):
                set_task_tags(task, pending_tags)
            restored_tasks.append(task)
            if task_root_key(task) and pending_task_tags(task.task_id):
                tasks_to_migrate_groups.append(task)

        return RuntimeRestoreResult(
            task_count=len(restored_tasks),
            restored_tasks=restored_tasks,
            tasks_to_migrate_groups=tasks_to_migrate_groups,
        )

    def restore_serial_queue_from_runtime_payload(
        self,
        payload: dict[str, Any],
        *,
        tasks_by_id: dict[str, StudioTask],
        task_dispatch_mode: Any,
        is_live_task: Any,
    ) -> tuple[list[str], str | None]:
        serial_queue_ids = payload.get("serial_queue_task_ids")
        restored_queue: list[str] = []
        if isinstance(serial_queue_ids, list):
            for task_id in serial_queue_ids:
                task_id_text = str(task_id or "").strip()
                if not task_id_text:
                    continue
                task = tasks_by_id.get(task_id_text)
                if task is None or task_dispatch_mode(task) != "serial_queue" or not is_live_task(task):
                    continue
                if task_id_text not in restored_queue:
                    restored_queue.append(task_id_text)

        active_task_id = str(payload.get("serial_queue_active_task_id") or "").strip()
        restored_active_task_id: str | None = None
        if active_task_id and active_task_id in restored_queue:
            active_task = tasks_by_id.get(active_task_id)
            if active_task is not None and str(active_task.status or "").strip().lower() == "paused":
                restored_active_task_id = active_task_id
        return restored_queue, restored_active_task_id

    def load_historical_tasks(
        self,
        *,
        evals_dir: Path,
        iter_eval_root_dirs: Any,
        reconstruct_eval_task: Any,
    ) -> list[StudioTask]:
        loaded_tasks: list[StudioTask] = []
        if evals_dir.is_dir():
            for eval_dir in iter_eval_root_dirs(evals_dir):
                task = reconstruct_eval_task(eval_dir)
                if task:
                    loaded_tasks.append(task)
        return loaded_tasks

    def collect_known_task_roots(
        self,
        tasks: list[StudioTask],
        *,
        task_root_key: Any,
    ) -> set[str]:
        known_eval_dirs: set[str] = set()
        for task in tasks:
            root = task_root_key(task)
            if not root:
                continue
            if task.kind == "eval":
                known_eval_dirs.add(root)
        return known_eval_dirs

    def discover_new_eval_dirs(
        self,
        *,
        evals_dir: Path,
        known_eval_dirs: set[str],
        iter_eval_root_dirs: Any,
        norm_dir_path: Any,
    ) -> list[Path]:
        discovered_eval_dirs: list[Path] = []
        if not evals_dir.is_dir():
            return discovered_eval_dirs
        for eval_dir in iter_eval_root_dirs(evals_dir):
            eval_key = norm_dir_path(str(eval_dir))
            if not eval_key or eval_key in known_eval_dirs:
                continue
            discovered_eval_dirs.append(eval_dir)
        return discovered_eval_dirs
