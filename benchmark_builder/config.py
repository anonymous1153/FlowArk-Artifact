"""Local config loading for benchmark_builder v2."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from benchmark_builder.io import load_yaml

DEFAULT_CONFIG_RELATIVE_PATH = Path("benchmark_builder/config.yaml")

DEFAULT_CONFIG: dict[str, Any] = {
    "dataset": "fdroid-open-source-app",
    "benchmark_family": "source_first_mixed",
    "source_root": "/absolute/path/to/apps",
    "out_dir": "benchmark_builder_v2_inventory",
    "export_out_dir": "benchmark_builder_v2_export",
    "source_kinds": [
        "platform_api",
        "ui_input",
        "icc_payload",
        "remote_payload",
        "persistent_storage",
    ],
    "excluded_dirs": [
        ".git",
        ".gradle",
        ".idea",
        ".flutter",
        ".pub-cache",
        ".venv",
        "__pycache__",
        "androidHostTest",
        "androidInstrumentedTest",
        "androidTest",
        "androidUnitTest",
        "build",
        "build-logic",
        "build-plugin",
        "buildSrc",
        "codegen",
        "commonTest",
        "dbTest",
        "debug",
        "daogenerator",
        "decompiled",
        "deplibs",
        "dist",
        "example",
        "examples",
        "generated",
        "GBDaoGenerator",
        "node_modules",
        "submodule",
        "submodules",
        "test",
        "testPlay",
        "tests",
        "testing",
        "third_party",
        "tools",
        "vendor",
    ],
    "include_extensions": [".kt", ".java", ".xml"],
    "app_names": [],
    "app_limit": None,
    "max_cases_per_kind": None,
    "sink_categories": ["log", "network", "icc", "file", "database", "storage", "others"],
    "sort_fields": ["source_subtype", "classname", "method", "file_path", "line_number", "occurrence_id"],
}


def resolve_config_path(workspace_root: Path | str, config_path: str | None = None) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    if config_path:
        return Path(config_path).expanduser().resolve()
    return (root / DEFAULT_CONFIG_RELATIVE_PATH).resolve()


def load_config_from_path(path: Path | str | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if path is None:
        return merged
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        return merged
    data = load_yaml(config_path)
    merged.update(data)
    return merged
