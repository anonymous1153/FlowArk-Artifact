from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from flowark_studio.common.file_watch import safe_read_text, tail_text_delta
from flowark_studio.common.models import StudioEvent, StudioTask, TailCursor


PUBLIC_EVAL_ROOT_ARTIFACT_NAMES = {
    "manifest.json",
    "note_only_artifact_audit.json",
    "summary.json",
}

PUBLIC_EVAL_RUN_ARTIFACT_NAMES = {
    "auto_knowledge_cycle.json",
    "cost_summary.json",
    "final_report.json",
    "final_report.md",
    "knowledge_injection.jsonl",
    "mem0_usage_summary.json",
    "note_only_artifact_audit.json",
    "raw_transcript.txt",
    "result.json",
}

PUBLIC_RAG_ARTIFACT_NAMES = {
    "chunks.jsonl",
    "index_update.json",
    "rag_injection.jsonl",
    "rag_retrieval.jsonl",
    "trimmed_transcript.txt",
}

PUBLIC_TEXT_ARTIFACT_SUFFIXES = {".json", ".jsonl", ".md", ".txt"}
PUBLIC_DENY_ARTIFACT_PARTS = {
    ".rerun_archive",
    "opencode_app_logs",
    "opencode_runtime",
    "opencode_turns",
}

PUBLIC_SENSITIVE_PATH_RE = re.compile(
    r"/(?:Users|Volumes|home|root|opt|private|var|tmp|mnt|data|workspace|Applications|Library|System|usr|etc)"
    r"(?:/[^\s\"'<>:,;)}\]]*)*"
)
PUBLIC_SECRET_KEY_RE = (
    r"OPENAI_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN"
    r"|api[_-]?key"
    r"|auth[_-]?token"
    r"|authorization"
    r"|access[_-]?token"
    r"|refresh[_-]?token"
    r"|session[_-]?token"
    r"|private[_-]?key"
    r"|password"
    r"|secret"
    r"|cookie"
)
PUBLIC_SECRET_FIELD_RE = re.compile(
    r"(?i)(?:"
    r"api[_-]?key"
    r"|auth[_-]?token"
    r"|authorization"
    r"|access[_-]?token"
    r"|refresh[_-]?token"
    r"|session[_-]?token"
    r"|private[_-]?key"
    r"|password"
    r"|secret"
    r"|cookie"
    r")"
)
PUBLIC_SECRET_JSON_RE = re.compile(
    rf"(?i)([\"']?(?:{PUBLIC_SECRET_KEY_RE})[\"']?\s*:\s*[\"'])([^\"']*)([\"'])"
)
PUBLIC_SECRET_TEXT_RE = re.compile(
    rf"(?i)\b(?:{PUBLIC_SECRET_KEY_RE})\s*[:=]\s*(?:bearer\s+)?(?:[\"'][^\"']*[\"']|[^\s,;}}\]]+)"
)
PUBLIC_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?(?:[\"'][^\"']*[\"']|[^\s,;}]+)"
)
PUBLIC_INTERNAL_JSON_FIELD_RE = re.compile(
    r"(?i)([\"'])(?:"
    r"knowledge_forbid_covered_code_reread"
    r"|force[_-]?pause[A-Za-z0-9_-]*"
    r"|force[_-]?paused"
    r"|hybrid[_-]?stable[_-]?prefix[A-Za-z0-9_-]*"
    r"|stable[_-]?prefix[A-Za-z0-9_-]*"
    r"|stableprefix[A-Za-z0-9_-]*"
    r"|counterfactual[A-Za-z0-9_-]*"
    r"|rerun[A-Za-z0-9_-]*"
    r"|usage[_-]?auto[_-]?pause[A-Za-z0-9_-]*"
    r")\1\s*:\s*(?:[\"'][^\"']*[\"']|[^,}\]\n]+)"
)
PUBLIC_INTERNAL_MARKER_RE = re.compile(
    r"(?i)(?:"
    r"knowledge_forbid_covered_code_reread"
    r"|force[_-]?pause[A-Za-z0-9_-]*"
    r"|force[_-]?paused"
    r"|hybrid[_-]?stable[_-]?prefix"
    r"|stable[_-]?prefix"
    r"|stableprefix"
    r"|counterfactual[A-Za-z0-9_-]*"
    r"|rerun[A-Za-z0-9_-]*"
    r"|usage[_-]?auto[_-]?pause[A-Za-z0-9_-]*"
    r")(?:(?:[A-Za-z0-9_-]+)*)(?:\s*[:=]\s*[^\s,;}]+)?"
)

PUBLIC_TASK_PARAM_KEYS = {
    "agent_adapter",
    "app_names",
    "experiment_preset",
    "input_path",
    "modes",
    "opencode_model",
    "opencode_provider",
}

PUBLIC_EXPERIMENT_PRESET_VALUES = {
    "naive",
    "flowark_full",
    "m1_generic",
    "m2_embedding",
    "m3_start_only",
    "mem0_enabled_opencode",
    "analysis_log_rag",
}

PUBLIC_TASK_METADATA_KEYS = {
    "draft",
    "eval_open_code_cost",
    "eval_progress",
    "eval_status_counts",
    "group",
    "historical",
    "normalization_warnings",
    "parameter_warnings",
    "pause_confirmed",
    "pause_mode",
    "pause_reason",
    "pause_requested",
    "queue_position",
    "queue_waiting",
    "tags",
}
PUBLIC_PAUSE_REASONS = {"manual"}

PUBLIC_TASK_PATH_FILES = {
    "manifest_json": "manifest.json",
    "note_only_artifact_audit_json": "note_only_artifact_audit.json",
    "summary_json": "summary.json",
}
PUBLIC_TAIL_REDACTION_CONTEXT_BYTES = 4096


class StudioTaskInspection:
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self._eval_runs_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def list_tags(
        self,
        tasks: Iterable[StudioTask],
        *,
        query: Any = None,
        normalize_group_name: Callable[[Any], str | None],
        normalize_task_tags: Callable[[Any], list[str]],
    ) -> dict[str, Any]:
        normalized_query = normalize_group_name(query)
        query_folded = normalized_query.casefold() if normalized_query else None

        summaries: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if task.kind != "eval":
                continue
            task_tags = normalize_task_tags(task.metadata.get("tags") or task.metadata.get("group"))
            if not task_tags:
                continue
            for tag in task_tags:
                if query_folded and query_folded not in tag.casefold():
                    continue
                summary = summaries.setdefault(
                    tag,
                    {
                        "tag": tag,
                        "task_count": 0,
                        "eval_task_count": 0,
                        "historical_task_count": 0,
                        "latest_created_at": None,
                    },
                )
                summary["task_count"] += 1
                if task.kind == "eval":
                    summary["eval_task_count"] += 1
                if task.metadata.get("historical"):
                    summary["historical_task_count"] += 1
                created_at = str(task.created_at or "")
                if created_at and (not summary["latest_created_at"] or created_at > str(summary["latest_created_at"])):
                    summary["latest_created_at"] = created_at

        tags = sorted(
            summaries.values(),
            key=lambda item: str(item["tag"]).casefold(),
        )
        return {
            "ok": True,
            "query": normalized_query,
            "tag_count": len(tags),
            "tags": tags,
        }

    def lookup_tag(
        self,
        tasks: Iterable[StudioTask],
        *,
        tag: Any,
        normalize_group_name: Callable[[Any], str | None],
        normalize_task_tags: Callable[[Any], list[str]],
        norm_dir_path: Callable[[Any], str | None],
    ) -> dict[str, Any]:
        normalized_tag = normalize_group_name(tag)
        if not normalized_tag:
            raise ValueError("tag cannot be empty")

        task_list = list(tasks)
        task_list.sort(key=lambda t: (t.created_at, t.task_id), reverse=True)

        related_counter: Counter[str] = Counter()
        matched_tasks: list[dict[str, Any]] = []
        eval_roots: list[str] = []
        seen_eval_roots: set[str] = set()

        for task in task_list:
            if task.kind != "eval":
                continue
            task_tags = normalize_task_tags(task.metadata.get("tags") or task.metadata.get("group"))
            if normalized_tag not in task_tags:
                continue

            for item in task_tags:
                if item != normalized_tag:
                    related_counter[item] += 1

            eval_root = (
                norm_dir_path(task.paths.get("eval_root"))
                or norm_dir_path(task.paths.get("eval_dir"))
                or norm_dir_path(task.metadata.get("eval_dir"))
            )
            if task.kind == "eval" and eval_root and eval_root not in seen_eval_roots:
                eval_roots.append(eval_root)
                seen_eval_roots.add(eval_root)

            matched_tasks.append(
                {
                    "task_id": task.task_id,
                    "kind": task.kind,
                    "status": self._public_status_value(task.status),
                    "created_at": task.created_at,
                    "historical": bool(task.metadata.get("historical")),
                    "tags": task_tags,
                    "eval_root": eval_root,
                    "eval_dir": eval_root,
                    "root_dir": eval_root,
                    "out_dir": norm_dir_path(task.metadata.get("out_dir")),
                }
            )

        related_tags = [
            {"tag": item, "task_count": count}
            for item, count in sorted(
                related_counter.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
        ]
        eval_task_count = sum(1 for item in matched_tasks if item["kind"] == "eval")
        return {
            "ok": True,
            "tag": normalized_tag,
            "task_count": len(matched_tasks),
            "eval_task_count": eval_task_count,
            "eval_roots": eval_roots,
            "eval_dirs": eval_roots,
            "related_tags": related_tags,
            "tasks": matched_tasks,
        }

    def public_task_payload(self, task: StudioTask, *, summary: bool = False) -> dict[str, Any]:
        metadata = {
            key: self._public_value(value)
            for key, value in dict(task.metadata or {}).items()
            if key in PUBLIC_TASK_METADATA_KEYS
        }
        if str(metadata.get("pause_reason") or "").strip().lower() not in PUBLIC_PAUSE_REASONS:
            metadata.pop("pause_reason", None)
        if summary:
            metadata["_list_summary"] = True
        return {
            "task_id": task.task_id,
            "kind": task.kind,
            "status": self._public_status_value(task.status),
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "pid": None,
            "return_code": task.return_code,
            "error": self._public_value(task.error),
            "last_seq": task.last_seq,
            "params": self._public_task_params(task),
            "paths": self._public_task_paths(task),
            "artifact_roots": [],
            "metadata": metadata,
            "command": [],
            "cwd": "",
        }

    def public_lookup_tag_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        updated = copy.deepcopy(payload)
        roots = [self._public_root_label(item) for item in updated.get("eval_roots") or []]
        roots = [item for item in roots if item]
        updated["eval_roots"] = roots
        updated["eval_dirs"] = list(roots)
        for item in updated.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            for key in ("eval_root", "eval_dir", "root_dir", "out_dir"):
                if key in item:
                    item[key] = self._public_root_label(item.get(key))
        return self._public_value(updated)

    def public_event_payload(self, event: StudioEvent, *, task: StudioTask | None = None) -> dict[str, Any]:
        payload = event.to_dict()
        data = payload.get("data")
        if isinstance(data, dict):
            public_data = {
                str(key): self._public_event_value(str(key), value, task=task)
                for key, value in data.items()
                if str(key) not in {"command", "cwd", "pid"}
            }
            payload["data"] = public_data
        else:
            payload["data"] = self._public_value(data)
        return self._public_json_value(payload)

    def public_eval_runs_payload(self, task: StudioTask, payload: dict[str, Any]) -> dict[str, Any]:
        eval_roots = self._public_eval_roots(task)

        def _public_run_value(key: str, value: Any) -> Any:
            if key in {"status", "exec_state"}:
                return self._public_status_value(value)
            if isinstance(value, str) and key in {"path", "run_dir", "repeat_dir", "eval_root", "eval_dir", "out_dir"}:
                path = self._path_or_none(value)
                if path is not None:
                    for root in eval_roots:
                        try:
                            path.relative_to(root)
                            return self._display_artifact_path(path, eval_roots)
                        except Exception:
                            continue
                return self._public_root_label(value)
            if isinstance(value, dict):
                return {str(child_key): _public_run_value(str(child_key), child_value) for child_key, child_value in value.items()}
            if isinstance(value, list):
                return [_public_run_value("", item) for item in value]
            if isinstance(value, tuple):
                return [_public_run_value("", item) for item in value]
            return self._public_value(value)

        updated = copy.deepcopy(payload)
        runs = updated.get("runs")
        if isinstance(runs, list):
            public_runs: list[dict[str, Any]] = []
            for item in runs:
                if not isinstance(item, dict):
                    continue
                public_item = _public_run_value("", item)
                if isinstance(public_item, dict):
                    public_runs.append(public_item)
            updated["runs"] = public_runs
        public_payload = self._public_json_value(updated)
        public_runs = public_payload.get("runs") if isinstance(public_payload, dict) else None
        if isinstance(public_runs, list):
            for item in public_runs:
                if isinstance(item, dict) and item.get("status") == "paused":
                    item["exec_state"] = "paused"
        return public_payload

    def list_task_artifacts(
        self,
        task: StudioTask,
        *,
        selected_run_dir: str | None = None,
        include_all_eval_runs: bool = False,
    ) -> dict[str, Any]:
        if task.kind != "eval":
            raise ValueError("Only eval tasks support artifact browsing")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidate_paths: list[Path] = []
        eval_roots: list[Path] = []
        if task.kind == "eval":
            for value in (
                task.paths.get("eval_root"),
                task.paths.get("eval_dir"),
                task.metadata.get("eval_dir"),
            ):
                if isinstance(value, str) and value.strip():
                    try:
                        eval_roots.append(Path(value).expanduser().resolve())
                    except Exception:
                        pass
            eval_roots = self._dedupe_paths(eval_roots)
        eval_root_base = eval_roots[0] if eval_roots else None

        def _resolve_eval_path(value: str | None) -> Path | None:
            resolved = self._resolve_path_or_none(value, base_dir=eval_root_base)
            return Path(resolved) if resolved else None

        selected_run_key = ""
        if selected_run_dir:
            try:
                selected_path = _resolve_eval_path(selected_run_dir)
                selected_run_key = str(selected_path.expanduser().resolve()) if selected_path else ""
            except Exception:
                selected_run_key = ""

        def _path_is_under_roots(path: Path, roots: Iterable[Path]) -> bool:
            try:
                path_str = str(path.expanduser().resolve())
            except Exception:
                return False
            for root in roots:
                try:
                    root_str = str(root.expanduser().resolve())
                    if os.path.commonpath([root_str, path_str]) == root_str:
                        return True
                except Exception:
                    continue
            return False

        def _add_eval_run_artifacts(run_dir: Path) -> None:
            candidate_paths.extend(
                [
                    run_dir / "raw_transcript.txt",
                    run_dir / "knowledge_injection.jsonl",
                    run_dir / "final_report.md",
                    run_dir / "final_report.json",
                    run_dir / "result.json",
                    run_dir / "cost_summary.json",
                    run_dir / "mem0_usage_summary.json",
                    run_dir / "auto_knowledge_cycle.json",
                    run_dir / "note_only_artifact_audit.json",
                    run_dir / "analysis_log_rag" / "trimmed_transcript.txt",
                    run_dir / "analysis_log_rag" / "chunks.jsonl",
                    run_dir / "analysis_log_rag" / "index_update.json",
                    run_dir / "analysis_log_rag" / "rag_retrieval.jsonl",
                    run_dir / "analysis_log_rag" / "rag_injection.jsonl",
                ]
            )

        for key, value in task.paths.items():
            if not isinstance(value, str):
                continue
            p = Path(value)
            candidate_paths.append(p)
            if p.is_dir():
                if key == "eval_root":
                    candidate_paths.extend(
                        [
                            p / "summary.json",
                            p / "manifest.json",
                            p / "note_only_artifact_audit.json",
                        ]
                    )
        eval_run_dirs: list[Path] = []
        for value in task.metadata.get("eval_run_dirs") or []:
            if isinstance(value, str) and value.strip():
                resolved = _resolve_eval_path(value)
                if resolved is not None:
                    eval_run_dirs.append(resolved)
        if task.kind == "eval" and selected_run_key:
            selected_path = Path(selected_run_key)
            if _path_is_under_roots(selected_path, eval_roots):
                eval_run_dirs.append(selected_path)
        if task.kind == "eval" and include_all_eval_runs:
            for root in eval_roots:
                if not root.is_dir():
                    continue
                for marker_name in (
                    "raw_transcript.txt",
                    "knowledge_injection.jsonl",
                    "final_report.md",
                    "final_report.json",
                    "result.json",
                    "cost_summary.json",
                    "mem0_usage_summary.json",
                    "auto_knowledge_cycle.json",
                    "note_only_artifact_audit.json",
                ):
                    for marker_path in root.rglob(marker_name):
                        eval_run_dirs.append(marker_path.parent)
        eval_run_dirs = self._dedupe_paths(eval_run_dirs)
        for p in eval_run_dirs:
            try:
                run_key = str(p.expanduser().resolve())
            except Exception:
                run_key = ""
            if task.kind == "eval" and not include_all_eval_runs and selected_run_key and run_key != selected_run_key:
                continue
            if task.kind == "eval" and not include_all_eval_runs and not selected_run_key:
                continue
            _add_eval_run_artifacts(p)

        for p in candidate_paths:
            try:
                resolved = p.expanduser().resolve()
            except Exception:
                continue
            sp = str(resolved)
            if sp in seen:
                continue
            seen.add(sp)
            if not self._is_public_artifact_path(resolved, eval_roots):
                continue
            exists = resolved.exists()
            if not exists:
                continue
            if exists and resolved.is_dir():
                continue
            info: dict[str, Any] = {
                "path": self._display_artifact_path(resolved, eval_roots),
                "name": resolved.name,
                "exists": exists,
                "is_dir": False,
            }
            if exists and resolved.is_file():
                try:
                    st = resolved.stat()
                    info["size"] = int(st.st_size)
                    info["mtime"] = int(st.st_mtime)
                except Exception:
                    pass
            items.append(info)

        items.sort(key=lambda x: (0 if x.get("exists") else 1, x.get("path") or ""))
        return {"task_id": task.task_id, "artifacts": items}

    def list_task_eval_runs(
        self,
        task: StudioTask,
        *,
        is_terminal_eval_run_status: Callable[[Any], bool],
        detail: str = "summary",
    ) -> dict[str, Any]:
        if task.kind != "eval":
            raise ValueError("Only eval tasks support sub-run browsing")
        detail_norm = str(detail or "summary").strip().lower()
        if detail_norm not in {"summary", "full"}:
            raise ValueError("detail must be summary or full")

        planned_candidates: list[Path] = []
        eval_root = task.paths.get("eval_root") or task.paths.get("eval_dir")
        if isinstance(eval_root, str) and eval_root.strip():
            root = Path(eval_root).expanduser().resolve()
            planned_candidates.append(root / "planned_runs.json")
        if isinstance(task.paths.get("planned_runs_json"), str):
            planned_candidates.append(Path(str(task.paths.get("planned_runs_json"))))
        planned_candidates = self._dedupe_paths(planned_candidates)

        progress_candidates: list[Path] = []
        progress_path = task.paths.get("progress_jsonl")
        if isinstance(progress_path, str) and progress_path.strip():
            progress_candidates.append(Path(progress_path))
        if isinstance(eval_root, str) and eval_root.strip():
            progress_candidates.append(Path(eval_root) / "progress.jsonl")
        progress_candidates = self._dedupe_paths(progress_candidates)

        results_candidates: list[Path] = []
        results_path = task.paths.get("results_jsonl")
        if isinstance(results_path, str) and results_path.strip():
            results_candidates.append(Path(results_path))
        if isinstance(eval_root, str) and eval_root.strip():
            results_candidates.append(Path(eval_root) / "results.jsonl")
        results_candidates = self._dedupe_paths(results_candidates)

        configured_sink_types = task.params.get("sink_categories")
        if not isinstance(configured_sink_types, list):
            configured_sink_types = None

        eval_root_base = self._path_or_none(eval_root)

        eval_run_dirs = [
            self._resolve_path_or_none(value, base_dir=eval_root_base)
            for value in (task.metadata.get("eval_run_dirs") or [])
            if isinstance(value, str) and value.strip()
        ]
        eval_run_dirs = [value for value in eval_run_dirs if value]
        base_signature = (
            self._resolve_path_or_none(eval_root),
            tuple(self._file_signature(path) for path in planned_candidates),
            tuple(self._file_signature(path) for path in progress_candidates),
            tuple(self._file_signature(path) for path in results_candidates),
            tuple(eval_run_dirs),
            tuple(configured_sink_types or []),
            bool(task.metadata.get("historical")),
            str(task.status or ""),
        )

        summary_cache_key = (str(task.task_id), "summary")
        cached_summary = self._eval_runs_cache.get(summary_cache_key)
        if detail_norm == "summary" and cached_summary and cached_summary.get("signature") == base_signature:
            return copy.deepcopy(cached_summary["payload"])

        entries: dict[str, dict[str, Any]] = {}

        def _entry_key(obj: dict[str, Any]) -> str:
            task_index = obj.get("task_index")
            if task_index is not None:
                try:
                    return f"task-{int(task_index)}"
                except Exception:
                    pass
            repeat_dir_val = obj.get("repeat_dir")
            if isinstance(repeat_dir_val, str) and repeat_dir_val.strip():
                try:
                    resolved = self._resolve_path_or_none(repeat_dir_val, base_dir=eval_root_base)
                    return f"repeat:{resolved or repeat_dir_val.strip()}"
                except Exception:
                    return f"repeat:{repeat_dir_val.strip()}"
            run_dir_val = obj.get("run_dir")
            if isinstance(run_dir_val, str) and run_dir_val.strip():
                try:
                    resolved = self._resolve_path_or_none(run_dir_val, base_dir=eval_root_base)
                    return f"run:{resolved or run_dir_val.strip()}"
                except Exception:
                    return f"run:{run_dir_val.strip()}"
            return f"unknown:{len(entries)}"

        def _upsert(raw_obj: dict[str, Any]) -> dict[str, Any]:
            key = _entry_key(raw_obj)
            rec = entries.setdefault(
                key,
                {
                    "entry_id": key,
                },
            )
            for field in (
                "task_index",
                "task_total",
                "mode",
                "source_id",
                "flow_id",
                "repeat_idx",
                "status",
                "exec_state",
                "started_at",
                "finished_at",
                "wall_time_seconds",
                "timed_out",
                "cancelled_by_user",
            ):
                if field in raw_obj and raw_obj.get(field) not in {None, ""}:
                    rec[field] = raw_obj.get(field)
            self._merge_eval_run_metadata(rec, raw_obj)

            repeat_dir = self._resolve_path_or_none(raw_obj.get("repeat_dir"), base_dir=eval_root_base)
            if repeat_dir:
                rec["repeat_dir"] = repeat_dir
            run_dir = self._resolve_path_or_none(raw_obj.get("run_dir"), base_dir=eval_root_base)
            if run_dir:
                rec["run_dir"] = run_dir
            app_name = raw_obj.get("app_name")
            if app_name not in {None, ""}:
                rec["app_name"] = app_name
            dataset = raw_obj.get("dataset")
            if dataset not in {None, ""}:
                rec["dataset"] = dataset
            return rec

        for rp in planned_candidates:
            if not rp.exists() or not rp.is_file():
                continue
            try:
                payload = json.loads(safe_read_text(rp, max_bytes=20_000_000))
            except Exception:
                continue
            runs = payload.get("runs") if isinstance(payload, dict) else None
            if not isinstance(runs, list):
                continue
            for item in runs:
                if not isinstance(item, dict):
                    continue
                rec = _upsert(item)
                rec.setdefault("status", "pending")
                rec.setdefault("exec_state", "pending")

        for rp in progress_candidates:
            if not rp.exists() or not rp.is_file():
                continue
            try:
                raw = safe_read_text(rp, max_bytes=20_000_000)
            except Exception:
                continue
            for line in raw.splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                event_name = str(obj.get("event") or "")
                if event_name not in {"start", "finish"}:
                    continue
                rec = _upsert(obj)
                if event_name == "start":
                    rec["status"] = "running"
                    rec["exec_state"] = "running"
                else:
                    final_status = str(obj.get("status") or rec.get("status") or "unknown")
                    rec["status"] = final_status
                    rec["exec_state"] = "pending" if final_status == "force_paused" else "completed"

        for rp in results_candidates:
            if not rp.exists() or not rp.is_file():
                continue
            try:
                raw = safe_read_text(rp, max_bytes=20_000_000)
            except Exception:
                continue
            for line in raw.splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    key = _entry_key(obj)
                    existing = entries.get(key)
                    result_run_dir = self._resolve_path_or_none(obj.get("run_dir"), base_dir=eval_root_base)
                    existing_run_dir = self._resolve_path_or_none(existing.get("run_dir")) if isinstance(existing, dict) else None
                    preserve_in_flight = (
                        isinstance(existing, dict)
                        and str(existing.get("exec_state") or "").strip().lower() == "running"
                        and bool(result_run_dir)
                        and bool(existing_run_dir)
                        and result_run_dir != existing_run_dir
                    )
                    preserved = dict(existing) if preserve_in_flight else None
                    rec = _upsert(obj)
                    if preserved is not None:
                        for field in ("status", "exec_state", "started_at", "run_dir"):
                            if preserved.get(field) not in {None, ""}:
                                rec[field] = preserved.get(field)
                    else:
                        status = str(rec.get("status") or "").strip().lower()
                        if is_terminal_eval_run_status(status):
                            rec["exec_state"] = "completed"

        for item in list(entries.values()):
            repeat_dir_text = self._resolve_path_or_none(item.get("repeat_dir"))
            if not repeat_dir_text:
                continue
            result_path = Path(repeat_dir_text) / "result.json"
            if not result_path.exists() or not result_path.is_file():
                continue
            try:
                obj = json.loads(safe_read_text(result_path, max_bytes=20_000_000))
            except Exception:
                continue
            if isinstance(obj, dict):
                rec = _upsert(obj)
                status = str(rec.get("status") or "").strip().lower()
                if is_terminal_eval_run_status(status):
                    rec["exec_state"] = "completed"

        run_dir_index: dict[str, dict[str, Any]] = {}
        repeat_dir_index: dict[str, dict[str, Any]] = {}
        for item in entries.values():
            existing_run_dir = self._resolve_path_or_none(item.get("run_dir"))
            if existing_run_dir:
                run_dir_index[existing_run_dir] = item
            repeat_dir = self._resolve_path_or_none(item.get("repeat_dir"))
            if repeat_dir:
                repeat_dir_index[repeat_dir] = item

        for resolved_run_dir in eval_run_dirs:
            matched: dict[str, Any] | None = None
            matched = run_dir_index.get(resolved_run_dir)
            if matched is None:
                inferred_repeat_dir = self._infer_repeat_dir_from_run_dir(resolved_run_dir)
                if inferred_repeat_dir:
                    matched = repeat_dir_index.get(inferred_repeat_dir)
            if matched is None:
                ancestor_repeat_dir = self._find_indexed_ancestor_dir(resolved_run_dir, repeat_dir_index)
                if ancestor_repeat_dir:
                    matched = repeat_dir_index.get(ancestor_repeat_dir)
            if matched is not None:
                matched["run_dir"] = resolved_run_dir
                run_dir_index[resolved_run_dir] = matched
                continue
            _upsert({"run_dir": resolved_run_dir})

        detached_historical_eval = bool(task.metadata.get("historical")) and task.status in {"error", "cancelled", "timeout"}

        runs: list[dict[str, Any]] = []
        for item in entries.values():
            if "sink_types" not in item and configured_sink_types:
                item["sink_types"] = list(configured_sink_types)

            exec_state = str(item.get("exec_state") or "").strip().lower()
            status = str(item.get("status") or "").strip().lower()
            if not exec_state:
                if status in {"running", "starting", "queued", "finishing"}:
                    exec_state = "running"
                elif status == "force_paused":
                    exec_state = "pending"
                elif status in {"success", "warning", "error", "timeout", "cancelled", "skipped", "harness_error"}:
                    exec_state = "completed"
                else:
                    exec_state = "pending"
            if detached_historical_eval and (
                exec_state == "running" or not is_terminal_eval_run_status(status)
            ):
                item["status"] = "error"
                item["exec_state"] = "interrupted"
                item["interrupted"] = True
            else:
                item["exec_state"] = exec_state
            runs.append(item)

        runs = copy.deepcopy(runs)
        runs.sort(
            key=lambda x: (
                int(x.get("task_index") or 10**9),
                int(x.get("repeat_idx") or 10**9),
                str(x.get("mode") or ""),
                str(x.get("source_id") or ""),
                str(x.get("run_dir") or ""),
            )
        )
        summary_payload = {"task_id": task.task_id, "detail": "summary", "runs": runs}
        self._eval_runs_cache[summary_cache_key] = {
            "signature": base_signature,
            "payload": copy.deepcopy(summary_payload),
        }
        if detail_norm == "summary":
            return summary_payload

        normalized_signatures = tuple(
            self._file_signature(Path(str(item["repeat_dir"])) / "normalized_case.json")
            for item in runs
            if isinstance(item.get("repeat_dir"), str) and str(item.get("repeat_dir")).strip()
        )
        full_signature = (base_signature, normalized_signatures)
        full_cache_key = (str(task.task_id), "full")
        cached_full = self._eval_runs_cache.get(full_cache_key)
        if cached_full and cached_full.get("signature") == full_signature:
            return copy.deepcopy(cached_full["payload"])

        full_runs = copy.deepcopy(runs)
        for item in full_runs:
            repeat_dir_raw = item.get("repeat_dir")
            if not isinstance(repeat_dir_raw, str) or not repeat_dir_raw.strip():
                continue
            normalized_case_path = Path(repeat_dir_raw).expanduser().resolve() / "normalized_case.json"
            if not normalized_case_path.exists() or not normalized_case_path.is_file():
                continue
            try:
                payload = json.loads(safe_read_text(normalized_case_path, max_bytes=2_000_000))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                self._merge_eval_run_metadata(item, payload)
        full_payload = {"task_id": task.task_id, "detail": "full", "runs": full_runs}
        self._eval_runs_cache[full_cache_key] = {
            "signature": full_signature,
            "payload": copy.deepcopy(full_payload),
        }
        return full_payload

    def read_task_artifact(self, task: StudioTask, path: str, *, max_bytes: int = 2_000_000) -> dict[str, Any]:
        if task.kind != "eval":
            raise ValueError("Only eval tasks support reading artifacts")
        resolved = self.resolve_task_artifact_path(task, path)
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        if resolved.is_dir():
            return {"path": self._display_artifact_path(resolved, self._public_eval_roots(task)), "type": "dir"}
        raw_text = safe_read_text(resolved, max_bytes=max_bytes)
        text, json_payload = self._public_artifact_content(resolved, raw_text)
        eval_roots = self._public_eval_roots(task)
        payload: dict[str, Any] = {
            "path": self._display_artifact_path(resolved, eval_roots),
            "type": "text",
            "content": text,
            "truncated": len(raw_text.encode("utf-8", errors="ignore")) >= max_bytes,
        }
        if json_payload is not None:
            payload["json"] = json_payload
        return payload

    def tail_task_artifact(
        self,
        task: StudioTask,
        path: str,
        offset: int = 0,
        *,
        max_bytes: int = 512_000,
        from_end: bool = False,
    ) -> dict[str, Any]:
        if task.kind != "eval":
            raise ValueError("Only eval tasks support reading artifacts")
        resolved = self.resolve_task_artifact_path(task, path)
        eval_roots = self._public_eval_roots(task)
        if not resolved.exists() or not resolved.is_file():
            return {
                "path": self._display_artifact_path(resolved, eval_roots),
                "offset": offset,
                "text": "",
                "exists": False,
            }
        size = 0
        try:
            size = int(resolved.stat().st_size)
        except Exception:
            size = 0
        start_offset = max(0, size - max_bytes) if from_end else max(0, int(offset or 0))
        read_offset = start_offset
        read_max_bytes = max_bytes
        if resolved.suffix in PUBLIC_TEXT_ARTIFACT_SUFFIXES and start_offset > 0:
            read_offset = max(0, start_offset - PUBLIC_TAIL_REDACTION_CONTEXT_BYTES)
            read_max_bytes = max_bytes + (start_offset - read_offset)
        cursor = TailCursor(path=str(resolved), offset=read_offset)
        delta = tail_text_delta(resolved, cursor, max_bytes=read_max_bytes)
        if resolved.suffix in PUBLIC_TEXT_ARTIFACT_SUFFIXES:
            if read_offset < start_offset:
                prefix_text = ""
                try:
                    with resolved.open("rb") as fh:
                        fh.seek(read_offset)
                        prefix_text = fh.read(start_offset - read_offset).decode("utf-8", errors="replace")
                except Exception:
                    prefix_text = ""
                redacted_delta = self._redact_public_text(delta)
                redacted_prefix = self._redact_public_text(prefix_text)
                if redacted_prefix and redacted_delta.startswith(redacted_prefix):
                    delta = redacted_delta[len(redacted_prefix):]
                else:
                    delta = ""
            else:
                delta = self._redact_public_text(delta)
        return {
            "path": self._display_artifact_path(resolved, eval_roots),
            "offset": cursor.offset,
            "start_offset": start_offset,
            "size": size,
            "truncated_head": bool(start_offset > 0),
            "text": delta,
            "exists": True,
        }

    def resolve_task_artifact_path(self, task: StudioTask, path: str) -> Path:
        eval_roots = self._public_eval_roots(task)
        if not eval_roots:
            raise PermissionError("task has no public eval roots")
        requested = Path(path)
        if not requested.is_absolute():
            candidates = [(root / requested).expanduser().resolve() for root in eval_roots]
            existing = next((item for item in candidates if item.exists()), None)
            requested = existing or candidates[0]
        else:
            requested = requested.expanduser().resolve()
        req_str = str(requested)
        for root in eval_roots:
            try:
                common = os.path.commonpath([str(root), req_str])
            except Exception:
                continue
            if common == str(root):
                if self._is_public_artifact_path(requested, eval_roots):
                    return requested
                raise PermissionError(f"artifact is not public: {self._display_artifact_path(requested, eval_roots)}")
        raise PermissionError("path is outside task roots")

    def _public_eval_roots(self, task: StudioTask) -> list[Path]:
        roots: list[Path] = []
        for value in (
            task.paths.get("eval_root"),
            task.paths.get("eval_dir"),
            task.metadata.get("eval_dir"),
        ):
            if isinstance(value, str) and value.strip():
                try:
                    roots.append(Path(value).expanduser().resolve())
                except Exception:
                    continue
        return self._dedupe_paths(roots)

    def _display_artifact_path(self, path: Path, eval_roots: Iterable[Path]) -> str:
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return str(path)
        for root in eval_roots:
            try:
                return resolved.relative_to(root.expanduser().resolve()).as_posix() or "."
            except Exception:
                continue
        return resolved.name

    def _is_public_artifact_path(self, path: Path, eval_roots: Iterable[Path]) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return False
        for root in eval_roots:
            try:
                root_resolved = root.expanduser().resolve()
                rel = resolved.relative_to(root_resolved)
            except Exception:
                continue
            parts = rel.parts
            if not parts:
                return False
            if any(part in PUBLIC_DENY_ARTIFACT_PARTS or part.startswith(".") for part in parts):
                return False
            name = resolved.name
            if len(parts) == 1:
                return name in PUBLIC_EVAL_ROOT_ARTIFACT_NAMES
            if len(parts) >= 3 and parts[0] == "knowledge_scope" and parts[1] == "skills" and resolved.suffix == ".md":
                return True
            if len(parts) >= 5 and parts[-2] == "analysis_log_rag":
                return name in PUBLIC_RAG_ARTIFACT_NAMES and len(parts) >= 7 and parts[-4] == "runs"
            if name not in PUBLIC_EVAL_RUN_ARTIFACT_NAMES:
                return False
            if "runs" in parts:
                run_idx = len(parts) - 3
                return run_idx >= 0 and parts[run_idx] == "runs" and len(parts[-2]) > 0
            return any(part.startswith("repeat-") for part in parts[:-1])
        return False

    @staticmethod
    def _redact_public_text(text: str) -> str:
        updated = PUBLIC_SENSITIVE_PATH_RE.sub("xxx", str(text or ""))
        updated = PUBLIC_SECRET_JSON_RE.sub(lambda match: f"{match.group(1)}xxx{match.group(3)}", updated)
        updated = PUBLIC_AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}xxx", updated)
        updated = PUBLIC_SECRET_TEXT_RE.sub(lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + "=xxx", updated)
        updated = PUBLIC_INTERNAL_JSON_FIELD_RE.sub(lambda match: f"{match.group(1)}xxx{match.group(1)}: \"xxx\"", updated)
        updated = PUBLIC_INTERNAL_MARKER_RE.sub("xxx", updated)
        return updated

    def _public_artifact_content(self, path: Path, text: str) -> tuple[str, Any | None]:
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                payload = self._public_json_value(json.loads(text))
            except Exception:
                return self._redact_public_text(text), None
            return json.dumps(payload, ensure_ascii=False, indent=2), payload
        if suffix == ".jsonl":
            lines: list[str] = []
            for line in text.splitlines():
                if not line.strip():
                    lines.append("")
                    continue
                try:
                    public_row = self._public_json_value(json.loads(line))
                except Exception:
                    lines.append(self._redact_public_text(line))
                else:
                    lines.append(json.dumps(public_row, ensure_ascii=False))
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), None
        if suffix in PUBLIC_TEXT_ARTIFACT_SUFFIXES:
            return self._redact_public_text(text), None
        return text, None

    def _public_event_value(self, key: str, value: Any, *, task: StudioTask | None) -> Any:
        if key in {"status", "exec_state"}:
            return self._public_status_value(value)
        if isinstance(value, str) and key in {"path", "run_dir", "repeat_dir", "eval_root", "eval_dir", "out_dir"} and task:
            path = self._path_or_none(value)
            eval_roots = self._public_eval_roots(task)
            if path is not None:
                for root in eval_roots:
                    try:
                        path.relative_to(root)
                        return self._display_artifact_path(path, eval_roots)
                    except Exception:
                        continue
            return self._public_root_label(value)
        return self._public_value(value)

    @staticmethod
    def _public_status_value(value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() == "force_paused":
            return "paused"
        return value

    def _public_task_params(self, task: StudioTask) -> dict[str, Any]:
        params = {
            key: self._public_value(value)
            for key, value in dict(task.params or {}).items()
            if key in PUBLIC_TASK_PARAM_KEYS
        }
        params["experiment_preset"] = self._public_experiment_preset(task)
        return params

    def _public_experiment_preset(self, task: StudioTask) -> str:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        source_payloads = [
            task.params if isinstance(task.params, dict) else {},
            metadata.get("effective_params") if isinstance(metadata.get("effective_params"), dict) else {},
            metadata.get("requested_params") if isinstance(metadata.get("requested_params"), dict) else {},
        ]
        for payload in source_payloads:
            explicit = self._normalize_public_experiment_preset(payload.get("experiment_preset"))
            if explicit:
                return explicit

        merged: dict[str, Any] = {}
        for payload in reversed(source_payloads):
            merged.update(payload)
        modes = self._public_mode_set(merged.get("modes"))
        if "naive" in modes and "flowark" not in modes:
            return "naive"
        packaging = str(merged.get("knowledge_packaging_mode") or "").strip().lower()
        if packaging == "analysis_log_rag" or packaging == "analysis_log_rag_initial":
            return "analysis_log_rag"
        if packaging == "embedding":
            return "m2_embedding"
        distill = str(merged.get("knowledge_distillation_mode") or "").strip().lower()
        if distill == "generic":
            return "m1_generic"
        runtime = str(merged.get("runtime_injection_mode") or "").strip().lower()
        if runtime == "start_only":
            return "m3_start_only"
        return "flowark_full"

    @staticmethod
    def _normalize_public_experiment_preset(value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if text in PUBLIC_EXPERIMENT_PRESET_VALUES:
            return text
        if text == "analysis_log_rag_initial":
            return "analysis_log_rag"
        return None

    @staticmethod
    def _public_mode_set(value: Any) -> set[str]:
        raw_items = value if isinstance(value, list) else str(value or "").split(",")
        modes: set[str] = set()
        for item in raw_items:
            text = str(item or "").strip().lower()
            if not text:
                continue
            modes.add("naive" if text == "native" else text)
        return modes

    def _public_task_paths(self, task: StudioTask) -> dict[str, str]:
        eval_roots = self._public_eval_roots(task)
        if not eval_roots:
            return {}
        root = eval_roots[0]
        paths: dict[str, str] = {
            "eval_root": ".",
            "eval_dir": ".",
        }
        for key, filename in PUBLIC_TASK_PATH_FILES.items():
            candidate = self._path_or_none((task.paths or {}).get(key))
            if candidate is None:
                candidate = root / filename
            if self._is_public_artifact_path(candidate, eval_roots):
                paths[key] = self._display_artifact_path(candidate, eval_roots)
        return paths

    def _public_root_label(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        text = value.strip()
        try:
            path = Path(text).expanduser()
            if path.is_absolute():
                return path.name
        except Exception:
            pass
        return self._redact_public_text(text)

    def _public_value(self, value: Any) -> Any:
        if isinstance(value, str):
            if self._is_absolute_local_path_text(value):
                return "xxx"
            return self._redact_public_text(value)
        if isinstance(value, list):
            return [self._public_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._public_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): "xxx" if self._is_public_secret_key(str(key)) else self._public_value(item)
                for key, item in value.items()
            }
        return value

    def _public_json_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            updated: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {"status", "exec_state"}:
                    updated[key_text] = self._public_status_value(item)
                    continue
                if self._is_public_internal_key(key_text):
                    continue
                if self._is_public_secret_key(key_text):
                    updated[key_text] = "xxx"
                else:
                    updated[key_text] = self._public_json_value(item)
            return updated
        if isinstance(value, list):
            return [self._public_json_value(item) for item in value]
        return self._public_value(value)

    @staticmethod
    def _is_public_secret_key(key: str) -> bool:
        return bool(PUBLIC_SECRET_FIELD_RE.search(str(key or "")))

    @staticmethod
    def _is_public_internal_key(key: str) -> bool:
        return bool(PUBLIC_INTERNAL_MARKER_RE.search(str(key or "")))

    @staticmethod
    def _is_absolute_local_path_text(value: str) -> bool:
        text = str(value or "").strip()
        if not text or "://" in text:
            return False
        try:
            return Path(text).expanduser().is_absolute()
        except Exception:
            return text.startswith("/")

    @staticmethod
    def _path_or_none(value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return Path(value).expanduser().resolve()
        except Exception:
            return None

    @staticmethod
    def _resolve_path_or_none(value: Any, *, base_dir: Path | None = None) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            path = Path(value).expanduser()
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            return str(path.resolve())
        except Exception:
            text = str(value).strip()
            if base_dir is not None and text and not Path(text).is_absolute():
                return str(base_dir / text)
            return text

    @staticmethod
    def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
        out: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            try:
                resolved = path.expanduser().resolve()
            except Exception:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    @staticmethod
    def _file_signature(path: Path) -> tuple[str, bool, int, int]:
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            resolved = path
        try:
            st = resolved.stat()
            return (str(resolved), True, int(st.st_size), int(st.st_mtime_ns))
        except Exception:
            return (str(resolved), False, 0, 0)

    @staticmethod
    def _infer_repeat_dir_from_run_dir(run_dir: str) -> str | None:
        try:
            p = Path(run_dir).expanduser().resolve()
        except Exception:
            return None
        if p.parent.name == "runs":
            return str(p.parent.parent)
        return None

    @staticmethod
    def _find_indexed_ancestor_dir(path_value: str, index: dict[str, Any]) -> str | None:
        try:
            current = Path(path_value).expanduser().resolve()
        except Exception:
            return None
        while True:
            key = str(current)
            if key in index:
                return key
            if current.parent == current:
                return None
            current = current.parent

    @staticmethod
    def _is_path_inside_dir(path_value: str, dir_value: str) -> bool:
        try:
            Path(path_value).relative_to(Path(dir_value))
            return True
        except Exception:
            return False

    @staticmethod
    def _merge_eval_run_metadata(rec: dict[str, Any], raw_obj: dict[str, Any]) -> None:
        query = raw_obj.get("query")
        if not (isinstance(query, str) and query.strip()):
            query = raw_obj.get("generated_query")
        if isinstance(query, str) and query.strip():
            rec["query"] = query

        source: Any = None
        source_obj = raw_obj.get("source") if isinstance(raw_obj.get("source"), dict) else None
        if isinstance(source_obj, dict):
            value = source_obj.get("description")
            if isinstance(value, str) and value.strip():
                source = value
        if source is None:
            for candidate in ("source_description", "generated_source", "source"):
                value = raw_obj.get(candidate)
                if isinstance(value, str) and value.strip():
                    source = value
                    break
        if isinstance(source, str) and source.strip():
            rec["source"] = source

        sink_types = raw_obj.get("sink_types")
        if sink_types is None and isinstance(raw_obj.get("dataflows"), list):
            derived_sink_types = []
            for item in raw_obj.get("dataflows") or []:
                if not isinstance(item, dict):
                    continue
                sink = item.get("sink") if isinstance(item.get("sink"), dict) else {}
                sink_type = sink.get("sink_type")
                if isinstance(sink_type, str) and sink_type.strip():
                    derived_sink_types.append(sink_type.strip())
            if derived_sink_types:
                sink_types = list(dict.fromkeys(derived_sink_types))
        if sink_types is None:
            sink_types = raw_obj.get("sink_type")
        if isinstance(sink_types, str) and sink_types.strip():
            rec["sink_types"] = sink_types
        elif isinstance(sink_types, list) and sink_types:
            rec["sink_types"] = sink_types

        health_issues = raw_obj.get("health_issues")
        if isinstance(health_issues, list):
            rec["health_issues"] = [item for item in health_issues if isinstance(item, dict)]
