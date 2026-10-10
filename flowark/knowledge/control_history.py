"""Validate frozen historical evidence for task-start knowledge controls."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


TASK_START_SCHEMA = "flowark-task-start-content-preparation-v1"


def control_instant(value: object) -> datetime:
    instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("知识对照时间必须包含时区")
    return instant


def control_path(value: str | Path, *, base_dir: Path, allowed_roots: Sequence[Path]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
        raise ValueError("知识对照证据路径超出指定材料目录")
    boundaries = {root.expanduser().absolute() for root in allowed_roots}
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("知识对照证据不允许符号链接")
        if component in boundaries:
            break
    return resolved


def verified_history_reports(
    fixture: dict[str, Any], *, allowed_root: Path | Sequence[Path], base_dir: Path | None = None,
) -> list[dict[str, Any]]:
    roots = [allowed_root] if isinstance(allowed_root, Path) else list(allowed_root)
    base = base_dir or roots[0]
    manifest_path = control_path(fixture["history_reports_manifest"], base_dir=base, allowed_roots=roots)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cutoff = control_instant(fixture["cutoff_time"])
    if control_instant(manifest["cutoff_time"]) != cutoff:
        raise ValueError("知识对照历史报告截止时间不一致")
    reports = manifest.get("reports")
    if not isinstance(reports, list):
        raise ValueError("知识对照历史报告清单格式错误")
    verified = []
    for row in reports:
        if row.get("app_name") != fixture["app_name"] or row.get("source_id") == fixture["source_id"]:
            raise ValueError("知识对照历史报告必须来自同应用的其他 source")
        if control_instant(row["available_at"]) > cutoff:
            raise ValueError("知识对照历史报告晚于任务起点")
        path = control_path(row["copied_report_path"], base_dir=manifest_path.parent, allowed_roots=roots)
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("知识对照历史报告哈希不一致")
        verified.append(dict(row, copied_report_path=str(path)))
    return verified


def load_trial_history(
    fixture_path: Path | None, *, app_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if fixture_path is None:
        return None
    fixture_path = Path(fixture_path).resolve(strict=True)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != TASK_START_SCHEMA:
        raise ValueError("知识对照 fixture schema 不一致")
    if fixture.get("execution_mode") != "task_start" or fixture.get("app_name") != app_name:
        raise ValueError("知识对照任务起点或应用身份不一致")
    reports = verified_history_reports(fixture, allowed_root=fixture_path.parent)
    return reports, {
        "history_report_scope": "frozen_task_start_same_app_reports",
        "history_report_cutoff": fixture["cutoff_time"],
        "history_reports_manifest": fixture["history_reports_manifest"],
        "history_report_selection": "available_before_task_start_excluding_target_source",
    }
