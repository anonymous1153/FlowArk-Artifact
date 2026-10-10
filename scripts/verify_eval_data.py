#!/usr/bin/env python3
"""Validate evaluation records against the published source corpus."""


from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def load_allowed_apps(manifest_path: Path) -> set[str]:
    with manifest_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    apps = {f"{row['package_name']}_{row['version_code']}" for row in rows}
    if not apps or len(apps) != len(rows):
        raise ValueError("source manifest must contain distinct application versions")
    if len({row["package_name"] for row in rows}) != len(rows):
        raise ValueError("source manifest must contain each package_name only once")
    for row in rows:
        if row["filename"] != f"{row['package_name']}_{row['version_code']}_src.tar.gz":
            raise ValueError("source manifest filename does not match application identity")
    return apps

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def _read_results(root: Path) -> list[dict[str, Any]]:
    with (root / "results.jsonl").open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows or any(not row.get("app_name") or not row.get("source_id") or not row.get("repeat_dir") for row in rows):
        raise ValueError("evaluation results require app_name, source_id, and repeat_dir")
    if len({row["repeat_dir"] for row in rows}) != len(rows):
        raise ValueError("evaluation repeat directories must be unique")
    return rows

def _relative_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("evaluation artifact path must be relative to the evaluation root")
    result = root / path
    if result.resolve() == root.resolve() or root.resolve() not in result.resolve().parents:
        raise ValueError("evaluation artifact path escapes the evaluation root")
    return result

def _repeat_path(root: Path, row: dict[str, Any]) -> Path:
    case_input = (row.get("paths") or {}).get("case_input")
    if not case_input:
        raise ValueError("result is missing its case_input path")
    case_path = _relative_path(root, case_input)
    repeat = _relative_path(root, row["repeat_dir"])
    if case_path.parent != repeat:
        raise ValueError("case_input and repeat_dir disagree")
    return repeat

def _verify_app_metadata(value: Any, allowed_apps: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _verify_app_metadata(item, allowed_apps)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "app_name" and item and item not in allowed_apps:
                raise ValueError("evaluation metadata contains an application outside the published corpus")
            if key == "app_names" and isinstance(item, list) and any(app not in allowed_apps for app in item):
                raise ValueError("evaluation metadata contains an application outside the published corpus")
            _verify_app_metadata(item, allowed_apps)


def verify_public_eval_root(root: Path, allowed_apps: set[str]) -> dict[str, int]:
    """Check identities and declared inputs without changing an evaluation."""
    rows = _read_results(root)
    if any(row["app_name"] not in allowed_apps for row in rows):
        raise ValueError("evaluation contains an application outside the published source corpus")
    for name in ("config.json", "manifest.json", "planned_runs.json"):
        _verify_app_metadata(_read_json(root / name), allowed_apps)
    expected: set[Path] = set()
    for row in rows:
        repeat = _repeat_path(root, row)
        expected.add(repeat / "case_input.json")
        case = _read_json(repeat / "case_input.json")
        if (case.get("app_name"), case.get("source_id")) != (row["app_name"], row["source_id"]):
            raise ValueError("evaluation case and result identities disagree")
        for name in ("normalized_case.json", "result.json"):
            path = repeat / name
            if path.is_file():
                payload = _read_json(path)
                if payload.get("app_name") and payload["app_name"] != row["app_name"]:
                    raise ValueError("evaluation sidecar application identity disagrees")
        fixture_path = (row.get("paths") or {}).get("control_fixture")
        if fixture_path:
            fixture = _read_json(_relative_path(root, fixture_path))
            if (fixture.get("app_name"), fixture.get("source_id")) != (row["app_name"], row["source_id"]):
                raise ValueError("control fixture and result identities disagree")
            fixture_case = _relative_path(root, fixture["case_input_path"])
            if not fixture_case.is_file():
                raise ValueError("control fixture is missing its case input")
            fixture_input = _read_json(fixture_case)
            _verify_app_metadata(fixture_input, allowed_apps)
            if (fixture_input.get("app_name"), fixture_input.get("source_id")) != (row["app_name"], row["source_id"]):
                raise ValueError("control input and result identities disagree")
            expected.add(fixture_case)
    if set(root.rglob("case_input.json")) != expected:
        raise ValueError("evaluation case files do not match its result rows and control fixtures")
    return {"task_count": len(rows), "app_count": len({row["app_name"] for row in rows})}
