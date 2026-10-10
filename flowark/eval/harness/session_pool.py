from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flowark.state_paths import get_workspace_state_paths
from flowark.timeutil import from_timestamp_tz8_iso

SETTINGS_SCHEMA_VERSION = 2
POOL_SCHEMA_VERSION = 1
GLOBAL_EVAL_PARALLEL_DEFAULT = 2
GLOBAL_EVAL_PARALLEL_MAX = 128
GLOBAL_USAGE_PAUSE_THRESHOLD_DEFAULT = 85.0
GLOBAL_USAGE_PAUSE_THRESHOLD_MIN = 1.0
GLOBAL_USAGE_PAUSE_THRESHOLD_MAX = 100.0
GLOBAL_EVAL_POOL_WAIT_POLL_SEC = 0.5
GLOBAL_EVAL_POOL_HEARTBEAT_SEC = 10.0
GLOBAL_EVAL_POOL_STALE_TTL_SEC = 35.0

FLOWARK_STUDIO_ENABLE_GLOBAL_EVAL_POOL = "FLOWARK_STUDIO_ENABLE_GLOBAL_EVAL_POOL"
FLOWARK_STUDIO_TASK_ID = "FLOWARK_STUDIO_TASK_ID"
FLOWARK_STUDIO_EVAL_PRIORITY = "FLOWARK_STUDIO_EVAL_PRIORITY"


def _state_dir(workspace_root: Path) -> Path:
    return get_workspace_state_paths(workspace_root).studio_state_dir


def get_studio_settings_path(workspace_root: Path) -> Path:
    return _state_dir(workspace_root) / "settings.json"


def get_eval_session_pool_path(workspace_root: Path) -> Path:
    return _state_dir(workspace_root) / "eval_session_pool.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _now_iso_from_ts(ts: float) -> str:
    return from_timestamp_tz8_iso(float(ts))


def _safe_positive_int(value: Any, *, default: int) -> int:
    try:
        num = int(value)
    except Exception:
        return int(default)
    if num <= 0:
        return int(default)
    return int(num)


def _safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if not (num == num):
        return None
    return float(num)


def _safe_percentage_float(value: Any, *, default: float) -> float:
    num = _safe_float(value)
    if num is None:
        return float(default)
    if num < GLOBAL_USAGE_PAUSE_THRESHOLD_MIN:
        return float(default)
    return float(min(GLOBAL_USAGE_PAUSE_THRESHOLD_MAX, num))


def _pid_is_live(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


@contextlib.contextmanager
def _locked_path(path: Path):
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any] | None) -> None:
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _normalize_settings_payload(payload: dict[str, Any] | None, *, now_ts: float) -> dict[str, Any]:
    data = dict(payload or {})
    limit = _safe_positive_int(
        data.get("global_eval_parallel_limit"),
        default=GLOBAL_EVAL_PARALLEL_DEFAULT,
    )
    limit = max(1, min(GLOBAL_EVAL_PARALLEL_MAX, limit))
    usage_pause_threshold_percent = _safe_percentage_float(
        data.get("usage_pause_threshold_percent"),
        default=GLOBAL_USAGE_PAUSE_THRESHOLD_DEFAULT,
    )
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "updated_at": str(data.get("updated_at") or _now_iso_from_ts(now_ts)),
        "global_eval_parallel_limit": limit,
        "usage_pause_threshold_percent": usage_pause_threshold_percent,
    }


def _canonicalize_participant(
    payload: dict[str, Any],
    *,
    now_ts: float,
) -> dict[str, Any] | None:
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return None
    active_worker_ids = sorted({str(item).strip() for item in (payload.get("active_worker_ids") or []) if str(item).strip()})
    waiting_worker_ids = sorted({str(item).strip() for item in (payload.get("waiting_worker_ids") or []) if str(item).strip() and str(item).strip() not in active_worker_ids})
    registered_at_ts = _safe_float(payload.get("registered_at_ts")) or now_ts
    last_heartbeat_at_ts = _safe_float(payload.get("last_heartbeat_at_ts")) or now_ts
    priority = _safe_positive_int(payload.get("priority"), default=10**9)
    requested_parallel = _safe_positive_int(payload.get("requested_parallel"), default=1)
    pid = payload.get("pid")
    try:
        pid_int = int(pid)
    except Exception:
        pid_int = None
    return {
        "task_id": task_id,
        "label": str(payload.get("label") or task_id).strip() or task_id,
        "eval_root": str(payload.get("eval_root") or "").strip(),
        "priority": priority,
        "requested_parallel": requested_parallel,
        "pid": pid_int,
        "registered_at_ts": registered_at_ts,
        "registered_at": str(payload.get("registered_at") or _now_iso_from_ts(registered_at_ts)),
        "last_heartbeat_at_ts": last_heartbeat_at_ts,
        "last_heartbeat_at": str(payload.get("last_heartbeat_at") or _now_iso_from_ts(last_heartbeat_at_ts)),
        "active_worker_ids": active_worker_ids,
        "waiting_worker_ids": waiting_worker_ids,
        "active_sessions": len(active_worker_ids),
        "waiting_sessions": len(waiting_worker_ids),
    }


def _cleanup_stale_participants(
    participants: list[dict[str, Any]],
    *,
    now_ts: float,
    stale_ttl_sec: float,
    pid_is_live_fn: Callable[[int | None], bool],
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in participants:
        participant = _canonicalize_participant(raw, now_ts=now_ts)
        if participant is None:
            continue
        pid = participant.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if pid_is_live_fn(pid):
            cleaned.append(participant)
            continue
        last_heartbeat_at_ts = _safe_float(participant.get("last_heartbeat_at_ts")) or 0.0
        if now_ts - last_heartbeat_at_ts >= stale_ttl_sec:
            continue
        continue
    return cleaned


def _normalize_pool_payload(
    payload: dict[str, Any] | None,
    *,
    limit: int,
    now_ts: float,
    stale_ttl_sec: float,
    pid_is_live_fn: Callable[[int | None], bool],
) -> dict[str, Any]:
    participants = _cleanup_stale_participants(
        list(payload.get("participants") or []) if isinstance(payload, dict) else [],
        now_ts=now_ts,
        stale_ttl_sec=stale_ttl_sec,
        pid_is_live_fn=pid_is_live_fn,
    )
    return {
        "schema_version": POOL_SCHEMA_VERSION,
        "updated_at": _now_iso_from_ts(now_ts),
        "updated_at_ts": now_ts,
        "limit": max(1, min(GLOBAL_EVAL_PARALLEL_MAX, int(limit))),
        "participants": participants,
    }


def _participants_sorted(participants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        participants,
        key=lambda item: (
            _safe_positive_int(item.get("priority"), default=10**9),
            _safe_float(item.get("registered_at_ts")) or 0.0,
            str(item.get("task_id") or ""),
        ),
    )


def _compute_allowed_sessions_by_task(payload: dict[str, Any]) -> dict[str, int]:
    limit = _safe_positive_int(payload.get("limit"), default=GLOBAL_EVAL_PARALLEL_DEFAULT)
    participants = _participants_sorted(list(payload.get("participants") or []))
    allowed = {
        str(participant.get("task_id") or ""): _safe_positive_int(participant.get("active_sessions"), default=0)
        for participant in participants
    }
    remaining = max(0, limit - sum(allowed.values()))
    if remaining <= 0:
        return allowed
    for participant in participants:
        task_id = str(participant.get("task_id") or "")
        active_sessions = _safe_positive_int(participant.get("active_sessions"), default=0)
        waiting_sessions = _safe_positive_int(participant.get("waiting_sessions"), default=0)
        requested_parallel = _safe_positive_int(participant.get("requested_parallel"), default=1)
        desired_sessions = min(requested_parallel, active_sessions + waiting_sessions)
        extra_needed = max(0, desired_sessions - active_sessions)
        if extra_needed <= 0:
            continue
        extra_granted = min(extra_needed, remaining)
        allowed[task_id] = allowed.get(task_id, active_sessions) + extra_granted
        remaining = max(0, remaining - extra_granted)
        if remaining <= 0:
            break
    return allowed


def _build_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    participants = _participants_sorted(list(payload.get("participants") or []))
    active_sessions = sum(_safe_positive_int(item.get("active_sessions"), default=0) for item in participants)
    waiting_sessions = sum(_safe_positive_int(item.get("waiting_sessions"), default=0) for item in participants)
    requested_sessions = sum(_safe_positive_int(item.get("requested_parallel"), default=1) for item in participants)
    limit = _safe_positive_int(payload.get("limit"), default=GLOBAL_EVAL_PARALLEL_DEFAULT)
    return {
        "limit": limit,
        "active_sessions": active_sessions,
        "waiting_sessions": waiting_sessions,
        "requested_sessions": requested_sessions,
        "running_eval_task_count": len(participants),
        "at_limit": active_sessions >= limit and limit > 0,
        "updated_at": str(payload.get("updated_at") or ""),
        "participants": [
            {
                "task_id": str(item.get("task_id") or ""),
                "label": str(item.get("label") or item.get("task_id") or ""),
                "eval_root": str(item.get("eval_root") or ""),
                "priority": _safe_positive_int(item.get("priority"), default=10**9),
                "requested_parallel": _safe_positive_int(item.get("requested_parallel"), default=1),
                "active_sessions": _safe_positive_int(item.get("active_sessions"), default=0),
                "waiting_sessions": _safe_positive_int(item.get("waiting_sessions"), default=0),
                "pid": item.get("pid"),
                "registered_at": str(item.get("registered_at") or ""),
                "last_heartbeat_at": str(item.get("last_heartbeat_at") or ""),
            }
            for item in participants
        ],
    }


class EvalSessionPoolStore:
    def __init__(
        self,
        workspace_root: Path,
        *,
        time_fn: Callable[[], float] | None = None,
        pid_is_live_fn: Callable[[int | None], bool] | None = None,
        stale_ttl_sec: float = GLOBAL_EVAL_POOL_STALE_TTL_SEC,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.time_fn = time_fn or time.time
        self.pid_is_live_fn = pid_is_live_fn or _pid_is_live
        self.stale_ttl_sec = max(1.0, float(stale_ttl_sec))
        self.settings_path = get_studio_settings_path(self.workspace_root)
        self.pool_path = get_eval_session_pool_path(self.workspace_root)

    def load_settings(self) -> dict[str, Any]:
        now_ts = self.time_fn()
        with _locked_path(self.settings_path):
            payload = _normalize_settings_payload(_read_json_file(self.settings_path), now_ts=now_ts)
            _write_json_file(self.settings_path, payload)
            return dict(payload)

    def get_limit(self) -> int:
        settings = self.load_settings()
        return _safe_positive_int(settings.get("global_eval_parallel_limit"), default=GLOBAL_EVAL_PARALLEL_DEFAULT)

    def get_usage_pause_threshold_percent(self) -> float:
        settings = self.load_settings()
        return _safe_percentage_float(
            settings.get("usage_pause_threshold_percent"),
            default=GLOBAL_USAGE_PAUSE_THRESHOLD_DEFAULT,
        )

    def set_limit(self, limit: int) -> dict[str, Any]:
        now_ts = self.time_fn()
        normalized_limit = max(1, min(GLOBAL_EVAL_PARALLEL_MAX, int(limit)))
        with _locked_path(self.settings_path):
            settings = _normalize_settings_payload(_read_json_file(self.settings_path), now_ts=now_ts)
            settings["global_eval_parallel_limit"] = normalized_limit
            settings["updated_at"] = _now_iso_from_ts(now_ts)
            _write_json_file(self.settings_path, settings)
        with _locked_path(self.pool_path):
            pool_payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=normalized_limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            if list(pool_payload.get("participants") or []):
                _write_json_file(self.pool_path, pool_payload)
            else:
                _write_json_file(self.pool_path, None)
            return _build_snapshot(pool_payload)

    def set_usage_pause_threshold_percent(self, threshold_percent: float) -> dict[str, Any]:
        now_ts = self.time_fn()
        normalized_threshold = _safe_percentage_float(
            threshold_percent,
            default=GLOBAL_USAGE_PAUSE_THRESHOLD_DEFAULT,
        )
        with _locked_path(self.settings_path):
            settings = _normalize_settings_payload(_read_json_file(self.settings_path), now_ts=now_ts)
            settings["usage_pause_threshold_percent"] = normalized_threshold
            settings["updated_at"] = _now_iso_from_ts(now_ts)
            _write_json_file(self.settings_path, settings)
            return dict(settings)

    def snapshot(self) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            if list(payload.get("participants") or []):
                _write_json_file(self.pool_path, payload)
            else:
                _write_json_file(self.pool_path, None)
            return _build_snapshot(payload)

    def register_participant(
        self,
        *,
        task_id: str,
        label: str,
        eval_root: str,
        priority: int,
        requested_parallel: int,
        pid: int,
    ) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            participants = list(payload.get("participants") or [])
            current = None
            for item in participants:
                if str(item.get("task_id") or "") == task_id:
                    current = item
                    break
            if current is None:
                current = {
                    "task_id": task_id,
                    "registered_at_ts": now_ts,
                    "registered_at": _now_iso_from_ts(now_ts),
                    "active_worker_ids": [],
                    "waiting_worker_ids": [],
                }
                participants.append(current)
            current["label"] = str(label or task_id).strip() or task_id
            current["eval_root"] = str(eval_root or "").strip()
            current["priority"] = max(1, int(priority))
            current["requested_parallel"] = max(1, int(requested_parallel))
            current["pid"] = int(pid)
            current["last_heartbeat_at_ts"] = now_ts
            current["last_heartbeat_at"] = _now_iso_from_ts(now_ts)
            payload["participants"] = [
                item for item in (_canonicalize_participant(item, now_ts=now_ts) for item in participants) if item is not None
            ]
            payload["updated_at"] = _now_iso_from_ts(now_ts)
            _write_json_file(self.pool_path, payload)
            return _build_snapshot(payload)

    def heartbeat(self, *, task_id: str) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            for item in list(payload.get("participants") or []):
                if str(item.get("task_id") or "") != task_id:
                    continue
                item["last_heartbeat_at_ts"] = now_ts
                item["last_heartbeat_at"] = _now_iso_from_ts(now_ts)
                break
            if list(payload.get("participants") or []):
                payload["updated_at"] = _now_iso_from_ts(now_ts)
                _write_json_file(self.pool_path, payload)
            else:
                _write_json_file(self.pool_path, None)
            return _build_snapshot(payload)

    def unregister_participant(self, *, task_id: str) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            payload["participants"] = [
                item for item in list(payload.get("participants") or [])
                if str(item.get("task_id") or "") != task_id
            ]
            payload["updated_at"] = _now_iso_from_ts(now_ts)
            if list(payload.get("participants") or []):
                _write_json_file(self.pool_path, payload)
            else:
                _write_json_file(self.pool_path, None)
            return _build_snapshot(payload)

    def try_acquire_slot(self, *, task_id: str, worker_id: str) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            participant = None
            for item in list(payload.get("participants") or []):
                if str(item.get("task_id") or "") == task_id:
                    participant = item
                    break
            if participant is None:
                snapshot = _build_snapshot(payload)
                snapshot["granted"] = True
                snapshot["retry_after_sec"] = GLOBAL_EVAL_POOL_WAIT_POLL_SEC
                return snapshot
            active_worker_ids = sorted({str(item).strip() for item in (participant.get("active_worker_ids") or []) if str(item).strip()})
            waiting_worker_ids = sorted({str(item).strip() for item in (participant.get("waiting_worker_ids") or []) if str(item).strip() and str(item).strip() not in active_worker_ids})
            if worker_id not in active_worker_ids and worker_id not in waiting_worker_ids:
                waiting_worker_ids.append(worker_id)
            participant["waiting_worker_ids"] = sorted(waiting_worker_ids)
            participant["waiting_sessions"] = len(participant["waiting_worker_ids"])
            allowed = _compute_allowed_sessions_by_task(payload).get(task_id, 0)
            granted = False
            if worker_id in active_worker_ids:
                granted = True
            elif len(active_worker_ids) < allowed:
                active_worker_ids.append(worker_id)
                granted = True
            if granted:
                waiting_worker_ids = [item for item in waiting_worker_ids if item != worker_id]
            elif worker_id not in waiting_worker_ids:
                waiting_worker_ids.append(worker_id)
            participant["active_worker_ids"] = sorted(active_worker_ids)
            participant["waiting_worker_ids"] = sorted(waiting_worker_ids)
            participant["active_sessions"] = len(participant["active_worker_ids"])
            participant["waiting_sessions"] = len(participant["waiting_worker_ids"])
            participant["last_heartbeat_at_ts"] = now_ts
            participant["last_heartbeat_at"] = _now_iso_from_ts(now_ts)
            payload["updated_at"] = _now_iso_from_ts(now_ts)
            _write_json_file(self.pool_path, payload)
            snapshot = _build_snapshot(payload)
            snapshot["granted"] = granted
            snapshot["retry_after_sec"] = GLOBAL_EVAL_POOL_WAIT_POLL_SEC
            return snapshot

    def release_slot(self, *, task_id: str, worker_id: str) -> dict[str, Any]:
        now_ts = self.time_fn()
        limit = self.get_limit()
        with _locked_path(self.pool_path):
            payload = _normalize_pool_payload(
                _read_json_file(self.pool_path),
                limit=limit,
                now_ts=now_ts,
                stale_ttl_sec=self.stale_ttl_sec,
                pid_is_live_fn=self.pid_is_live_fn,
            )
            for item in list(payload.get("participants") or []):
                if str(item.get("task_id") or "") != task_id:
                    continue
                item["active_worker_ids"] = [
                    value for value in (item.get("active_worker_ids") or [])
                    if str(value).strip() and str(value).strip() != worker_id
                ]
                item["waiting_worker_ids"] = [
                    value for value in (item.get("waiting_worker_ids") or [])
                    if str(value).strip() and str(value).strip() != worker_id
                ]
                item["active_sessions"] = len(item["active_worker_ids"])
                item["waiting_sessions"] = len(item["waiting_worker_ids"])
                item["last_heartbeat_at_ts"] = now_ts
                item["last_heartbeat_at"] = _now_iso_from_ts(now_ts)
                break
            payload["updated_at"] = _now_iso_from_ts(now_ts)
            if list(payload.get("participants") or []):
                _write_json_file(self.pool_path, payload)
            else:
                _write_json_file(self.pool_path, None)
            return _build_snapshot(payload)


class GlobalEvalSessionPoolController:
    def __init__(
        self,
        *,
        workspace_root: Path,
        task_id: str,
        priority: int,
        requested_parallel: int,
        eval_root: Path,
        label: str,
        heartbeat_interval_sec: float = GLOBAL_EVAL_POOL_HEARTBEAT_SEC,
        wait_poll_sec: float = GLOBAL_EVAL_POOL_WAIT_POLL_SEC,
        store: EvalSessionPoolStore | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.task_id = str(task_id).strip()
        self.priority = max(1, int(priority))
        self.requested_parallel = max(1, int(requested_parallel))
        self.eval_root = Path(eval_root).expanduser().resolve()
        self.label = str(label or self.task_id).strip() or self.task_id
        self.heartbeat_interval_sec = max(1.0, float(heartbeat_interval_sec))
        self.wait_poll_sec = max(0.05, float(wait_poll_sec))
        self.store = store or EvalSessionPoolStore(self.workspace_root)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    def from_env(
        cls,
        *,
        workspace_root: Path,
        eval_root: Path,
        requested_parallel: int,
        label: str | None = None,
    ) -> GlobalEvalSessionPoolController | None:
        enabled = str(os.getenv(FLOWARK_STUDIO_ENABLE_GLOBAL_EVAL_POOL) or "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None
        task_id = str(os.getenv(FLOWARK_STUDIO_TASK_ID) or "").strip()
        if not task_id:
            return None
        priority = _safe_positive_int(os.getenv(FLOWARK_STUDIO_EVAL_PRIORITY), default=10**9)
        final_label = str(label or eval_root.name or task_id).strip() or task_id
        return cls(
            workspace_root=workspace_root,
            task_id=task_id,
            priority=priority,
            requested_parallel=max(1, int(requested_parallel)),
            eval_root=eval_root,
            label=final_label,
        )

    async def open(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(
            self.store.register_participant,
            task_id=self.task_id,
            label=self.label,
            eval_root=str(self.eval_root),
            priority=self.priority,
            requested_parallel=self.requested_parallel,
            pid=os.getpid(),
        )
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"eval-session-pool-{self.task_id}")

    async def wait_for_slot(self, worker_id: str) -> None:
        if self._closed:
            return
        try:
            while True:
                result = await asyncio.to_thread(
                    self.store.try_acquire_slot,
                    task_id=self.task_id,
                    worker_id=worker_id,
                )
                if bool(result.get("granted")):
                    return
                await asyncio.sleep(max(self.wait_poll_sec, float(result.get("retry_after_sec") or self.wait_poll_sec)))
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.release_slot,
                task_id=self.task_id,
                worker_id=worker_id,
            )
            raise

    async def release_slot(self, worker_id: str) -> None:
        if self._closed:
            return
        await asyncio.to_thread(
            self.store.release_slot,
            task_id=self.task_id,
            worker_id=worker_id,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        heartbeat_task = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        await asyncio.to_thread(self.store.unregister_participant, task_id=self.task_id)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval_sec)
                await asyncio.to_thread(self.store.heartbeat, task_id=self.task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self.heartbeat_interval_sec)
