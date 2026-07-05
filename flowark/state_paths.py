from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLOWARK_DATA_ROOT_ENV = "FLOWARK_DATA_ROOT"
DEFAULT_FLOWARK_DATA_ROOT = Path(__file__).resolve().parents[1] / "artifact-data" / "studio-state"
_SANITIZE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class WorkspaceStatePaths:
    workspace_root: Path
    data_root: Path
    workspace_id: str
    workspace_state_root: Path
    runs_dir: Path
    evals_dir: Path
    evals_archived_dir: Path
    studio_state_dir: Path


def resolve_workspace_root(workspace_root: Path | str) -> Path:
    return Path(workspace_root).expanduser().resolve()


def resolve_data_root() -> Path:
    raw = str(os.getenv(FLOWARK_DATA_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_FLOWARK_DATA_ROOT


def build_workspace_id(workspace_root: Path | str) -> str:
    resolved = resolve_workspace_root(workspace_root)
    name = _sanitize_component(resolved.name or "workspace")
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def get_workspace_state_paths(workspace_root: Path | str) -> WorkspaceStatePaths:
    resolved_workspace = resolve_workspace_root(workspace_root)
    data_root = resolve_data_root()
    workspace_id = build_workspace_id(resolved_workspace)
    workspace_state_root = data_root / workspace_id
    return WorkspaceStatePaths(
        workspace_root=resolved_workspace,
        data_root=data_root,
        workspace_id=workspace_id,
        workspace_state_root=workspace_state_root,
        runs_dir=workspace_state_root / "runs",
        evals_dir=workspace_state_root / "evals",
        evals_archived_dir=workspace_state_root / "evals_archived",
        studio_state_dir=workspace_state_root / ".flowark_studio",
    )


def resolve_eval_config_out_dir(raw_value: Any, *, workspace_root: Path | str) -> Path:
    text = str(raw_value or "").strip()
    if not text:
        return get_workspace_state_paths(workspace_root).evals_dir
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (get_workspace_state_paths(workspace_root).workspace_state_root / path).resolve()


def ensure_indexable_eval_out_dir(raw_value: Any, *, workspace_root: Path | str) -> Path:
    out_dir = resolve_eval_config_out_dir(raw_value, workspace_root=workspace_root)
    evals_dir = get_workspace_state_paths(workspace_root).evals_dir.resolve()
    try:
        out_dir.relative_to(evals_dir)
    except ValueError as exc:
        raise ValueError(
            "eval.out_dir 必须位于 "
            f"{evals_dir} 之下；请使用默认值 `evals` 或其子目录（如 `evals/source-first`）。"
            f" 当前路径: {out_dir}"
        ) from exc
    return out_dir


def resolve_run_config_out_dir(raw_value: Any, *, workspace_root: Path | str) -> Path:
    text = str(raw_value or "").strip()
    if not text:
        return get_workspace_state_paths(workspace_root).runs_dir
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (get_workspace_state_paths(workspace_root).workspace_state_root / path).resolve()


def legacy_local_state_dirs(workspace_root: Path | str) -> dict[str, Path]:
    resolved = resolve_workspace_root(workspace_root)
    return {
        "runs": resolved / "runs",
        "evals": resolved / "evals",
        ".flowark_studio": resolved / ".flowark_studio",
    }


def find_nonempty_legacy_state_dirs(workspace_root: Path | str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for name, path in legacy_local_state_dirs(workspace_root).items():
        if not path.exists():
            continue
        if path.is_file():
            found[name] = path
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            continue
        except Exception:
            found[name] = path
            continue
        found[name] = path
    return found


def format_legacy_state_dirs_warning(workspace_root: Path | str) -> str | None:
    found = find_nonempty_legacy_state_dirs(workspace_root)
    if not found:
        return None
    paths = get_workspace_state_paths(workspace_root)
    ignored = ", ".join(f"{name}={path}" for name, path in found.items())
    return (
        "检测到仓库内仍存在旧运行态目录；当前版本已只使用外部状态根，请先手工迁移这些目录。"
        f" 忽略的旧路径: {ignored}。"
        f" 当前外部状态根: {paths.workspace_state_root}"
    )


def _sanitize_component(value: str) -> str:
    text = _SANITIZE_COMPONENT_RE.sub("-", str(value or "").strip())
    text = text.strip("._-")
    return text or "workspace"
