from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from flowark.timeutil import now_tz8_iso

FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_ENV = "FLOWARK_STUDIO_WORKSPACE_GIT_SUBMITTED_JSON"
FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_ENV = "FLOWARK_STUDIO_WORKSPACE_GIT_STARTED_JSON"

_GIT_COMMAND_TIMEOUT_SEC = 4.0


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_COMMAND_TIMEOUT_SEC,
        check=False,
    )


def normalize_git_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    repo_root = _clean_text(value.get("repo_root"))
    git_root = _clean_text(value.get("git_root"))
    commit = _clean_text(value.get("commit"))
    branch = _clean_text(value.get("branch"))
    captured_at = _clean_text(value.get("captured_at"))
    error = _clean_text(value.get("error"))
    action = _clean_text(value.get("action"))
    if repo_root:
        normalized["repo_root"] = repo_root
    if git_root:
        normalized["git_root"] = git_root
    if commit:
        normalized["commit"] = commit
    if branch:
        normalized["branch"] = branch
    if captured_at:
        normalized["captured_at"] = captured_at
    if action:
        normalized["action"] = action
    if "dirty" in value:
        normalized["dirty"] = bool(value.get("dirty"))
    if "detached" in value:
        normalized["detached"] = bool(value.get("detached"))
    normalized["available"] = bool(value.get("available")) or bool(commit)
    if error:
        normalized["error"] = error
    if not normalized.get("available") and not error and not normalized.get("repo_root") and not normalized.get("captured_at"):
        return None
    return normalized


def capture_workspace_git_snapshot(workspace_root: Path | str) -> dict[str, Any]:
    resolved_root = Path(workspace_root).expanduser().resolve()
    snapshot: dict[str, Any] = {
        "repo_root": str(resolved_root),
        "captured_at": now_tz8_iso(),
        "available": False,
    }
    try:
        top_level_proc = _run_git(resolved_root, "rev-parse", "--show-toplevel")
        if top_level_proc.returncode != 0:
            detail = _clean_text(top_level_proc.stderr) or _clean_text(top_level_proc.stdout) or f"git_exit_{top_level_proc.returncode}"
            snapshot["error"] = detail
            return snapshot
        git_root = _clean_text(top_level_proc.stdout)
        if git_root:
            snapshot["git_root"] = git_root
        git_root_path = Path(git_root or resolved_root).expanduser().resolve()

        commit_proc = _run_git(git_root_path, "rev-parse", "HEAD")
        if commit_proc.returncode != 0:
            detail = _clean_text(commit_proc.stderr) or _clean_text(commit_proc.stdout) or f"git_exit_{commit_proc.returncode}"
            snapshot["error"] = detail
            return snapshot
        commit = _clean_text(commit_proc.stdout)
        if commit:
            snapshot["commit"] = commit
            snapshot["available"] = True

        branch_proc = _run_git(git_root_path, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch_proc.returncode == 0:
            branch = _clean_text(branch_proc.stdout)
            if branch:
                snapshot["branch"] = branch
            snapshot["detached"] = False
        else:
            snapshot["detached"] = True

        dirty_proc = _run_git(git_root_path, "status", "--porcelain", "--untracked-files=all")
        if dirty_proc.returncode == 0:
            snapshot["dirty"] = bool(_clean_text(dirty_proc.stdout))
        else:
            detail = _clean_text(dirty_proc.stderr) or _clean_text(dirty_proc.stdout) or f"git_exit_{dirty_proc.returncode}"
            snapshot["error"] = detail
        return snapshot
    except subprocess.TimeoutExpired:
        snapshot["error"] = "git command timed out"
        return snapshot
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot


def load_git_snapshot_from_env(
    env_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    source = environ if environ is not None else None
    raw = _clean_text((source or {}).get(env_name) if source is not None else None)
    if not raw and source is None:
        import os

        raw = _clean_text(os.getenv(env_name))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return normalize_git_snapshot(payload)


def build_git_launch_history(history: Any, *, action: str, snapshot: Any) -> list[dict[str, Any]]:
    normalized_snapshot = normalize_git_snapshot(snapshot)
    normalized_action = _clean_text(action) or "unknown"
    items: list[dict[str, Any]] = []
    if isinstance(history, list):
        for item in history:
            normalized_item = normalize_git_snapshot(item)
            if normalized_item is None:
                continue
            item_action = _clean_text(normalized_item.get("action"))
            normalized_item["action"] = item_action or normalized_action
            items.append(normalized_item)
    if normalized_snapshot is None:
        return items
    items.append({"action": normalized_action, **normalized_snapshot})
    return items
