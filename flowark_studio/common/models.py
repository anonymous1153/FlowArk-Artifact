from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

TaskKind = Literal["run", "eval"]
DispatchMode = Literal["serial_queue", "force_parallel"]
TaskStatus = Literal[
    "queued",
    "starting",
    "running",
    "pausing",
    "finishing",
    "paused",
    "success",
    "warning",
    "error",
    "timeout",
    "cancelled",
]

TASK_LIST_METADATA_KEYS = {
    "historical",
    "draft",
    "tags",
    "group",
    "run_dir",
    "eval_dir",
    "out_dir",
    "dispatch_mode",
    "queue_waiting",
    "queue_position",
    "pause_requested",
    "pause_confirmed",
    "pause_mode",
    "pause_mode_requested",
    "pause_reason",
    "eval_status_counts",
    "eval_progress",
    "eval_open_code_cost",
}


def _copy_known_items(payload: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload}


@dataclass(slots=True)
class StudioTask:
    task_id: str
    kind: TaskKind
    status: TaskStatus
    created_at: str
    params: dict[str, Any]
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    return_code: int | None = None
    error: str | None = None
    last_seq: int = 0
    paths: dict[str, str] = field(default_factory=dict)
    artifact_roots: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_summary_dict(self) -> dict[str, Any]:
        metadata = _copy_known_items(dict(self.metadata or {}), TASK_LIST_METADATA_KEYS)
        metadata["_list_summary"] = True
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "return_code": self.return_code,
            "error": self.error,
            "last_seq": self.last_seq,
            "params": dict(self.params or {}),
            "paths": dict(self.paths or {}),
            "artifact_roots": [],
            "metadata": metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StudioTask":
        allowed = {item.name for item in fields(cls)}
        data = {key: value for key, value in dict(payload or {}).items() if key in allowed}
        return cls(**data)

    def register_root(self, path: Path | None) -> None:
        if not path:
            return
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            return
        if resolved not in self.artifact_roots:
            self.artifact_roots.append(resolved)


@dataclass(slots=True)
class StudioEvent:
    event_id: str
    task_id: str
    kind: TaskKind
    type: str
    ts: str
    seq: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TailCursor:
    path: str
    offset: int = 0


@dataclass(slots=True)
class WatchedFileState:
    path: str
    last_size: int | None = None
    last_mtime_ns: int | None = None
    cursor: TailCursor | None = None


@dataclass(slots=True)
class RealtimeWatchState:
    transcript_cursors: dict[str, TailCursor] = field(default_factory=dict)
    generic_cursors: dict[str, TailCursor] = field(default_factory=dict)
    artifact_stats: dict[str, WatchedFileState] = field(default_factory=dict)
    seen_eval_runs_parent_dirs: set[str] = field(default_factory=set)
    seen_eval_run_dirs: set[str] = field(default_factory=set)
    known_progress_events: int = 0
    last_eval_state_status: str = ""
    last_eval_pause_mode: str = ""
    last_eval_run_dir_discovery_monotonic: float = 0.0
    eval_run_tail_cursor_index: int = 0
    artifact_scan_cursor_index: int = 0
