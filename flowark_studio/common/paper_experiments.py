from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


PAPER_EXPERIMENT_LABELS = {
    "custom": "Single evaluation",
    "glm53_repeated": "GLM-5.3: three paired runs",
    "knowledge_control": "Knowledge content control",
}
PAPER_CONDITION_LABELS = {
    "all": "All conditions",
    "standard": "Standard",
    "flowark": "FlowArk",
    "irrelevant": "Cross-app knowledge",
}


@lru_cache(maxsize=8)
def _catalog_rows(path: str, modified: int, size: int) -> tuple[dict[str, Any], ...]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("Experiment catalog must be an object")
    return tuple(dict(row) for row in payload.get("experiments", []) if isinstance(row, dict))


def paper_catalog(workspace_root: Path) -> dict[str, dict[str, Any]]:
    path = workspace_root / "artifact-data/reproduction/catalog.json"
    try:
        stat = path.stat()
        rows = _catalog_rows(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError):
        return {}
    return {
        row["eval_root"]: dict(row)
        for row in rows
        if isinstance(row.get("eval_root"), str)
        and Path(row["eval_root"]).name == row["eval_root"]
    }


def published_result(workspace_root: Path, eval_root: Path) -> dict[str, Any] | None:
    root = eval_root.expanduser().resolve()
    data = (workspace_root / "artifact-data/studio-state").resolve()
    if not root.is_relative_to(data):
        return None
    return paper_catalog(workspace_root).get(root.name)


def published_eval_roots(workspace_root: Path) -> list[Path]:
    data = workspace_root / "artifact-data/studio-state"
    catalog = paper_catalog(workspace_root)
    if not data.is_dir() or not catalog:
        return []
    roots: set[Path] = set()
    for workspace in data.iterdir():
        if not workspace.is_dir() or workspace.name.startswith("."):
            continue
        for parent in (workspace / "evals", workspace / "evals/evals"):
            for name in catalog:
                root = parent / name
                if root.is_dir() and published_result(workspace_root, root) is not None:
                    roots.add(root.resolve())
    return sorted(roots)


def paper_eval_jobs(params: dict[str, Any]) -> list[dict[str, Any]]:
    experiment = str(params.get("paper_experiment") or "custom")
    if experiment not in PAPER_EXPERIMENT_LABELS:
        raise ValueError("Unknown paper experiment")
    if experiment == "custom":
        return [dict(params)]
    for key in ("max_cases", "max_apps", "max_sources"):
        if params.get(key) is not None and params.get(key) != "":
            raise ValueError("Paper experiments require the complete workload; task limits are not allowed")
    app_names = params.get("app_names")
    if app_names and (not isinstance(app_names, (list, tuple)) or any(str(name).strip() for name in app_names)):
        raise ValueError("Paper experiments require all workload applications")
    if str(params.get("classification_filter") or "all").strip().lower() != "all":
        raise ValueError("Paper experiments require all workload classifications")
    available = ("standard", "flowark", "irrelevant") if experiment == "knowledge_control" else ("standard", "flowark")
    selected = str(params.get("paper_condition") or "all")
    if selected != "all" and selected not in available:
        raise ValueError("The selected condition does not belong to this experiment")
    selected_round = str(params.get("independent_run") or "all")
    if selected_round not in {"all", "1", "2", "3"}:
        raise ValueError("Independent run must be 1, 2, 3, or all")
    conditions = available if selected == "all" else (selected,)
    rounds = ("1", "2", "3") if selected_round == "all" else (selected_round,)
    return [dict(params, paper_experiment=experiment, paper_condition=condition, independent_run=number)
            for number in rounds for condition in conditions]


def paper_eval_defaults(params: dict[str, Any]) -> dict[str, Any]:
    updated = dict(params)
    experiment = str(updated.get("paper_experiment") or "custom")
    if experiment == "custom":
        return updated
    jobs = paper_eval_jobs(updated)
    if len(jobs) != 1:
        raise ValueError("Choose one condition and independent run for a single evaluation")
    condition = str(updated["paper_condition"])
    model = str(updated.get("opencode_model") or "glm-5.3").strip()
    if model != "glm-5.3":
        raise ValueError("The paper experiment uses the glm-5.3 model")
    updated.update(
        opencode_model="glm-5.3",
        experiment_preset="naive" if condition == "standard" else "flowark_full",
        dataset_preset="strat15",
        app_names=[],
        classification_filter="all",
        repeats=1,
        timeout_seconds=1800,
        knowledge_allow_repeat_injection_within_session=False,
        serialize_within_app=condition != "standard" and experiment != "knowledge_control",
        parallel=updated.get("parallel") or 12,
    )
    return updated


def control_inputs(workspace_root: Path, independent_run: Any) -> tuple[Path, Path]:
    number = str(independent_run)
    if number not in {"1", "2", "3"}:
        raise ValueError("Knowledge control requires an independent run numbered 1–3")
    directory = (workspace_root / "artifact-data/reproduction/knowledge-control/execution" / f"r{number}").resolve()
    if not directory.is_relative_to(workspace_root.resolve() / "artifact-data"):
        raise ValueError("Knowledge-control input directory must stay inside artifact-data")
    manifest = directory / "manifest.json"
    if not manifest.resolve().is_relative_to(directory):
        raise ValueError("Knowledge-control manifest must remain in its prepared directory")
    if not manifest.is_file():
        raise ValueError("Prepare the knowledge-control execution inputs before starting this experiment")
    payload = json.loads(manifest.read_text())
    if payload.get("schema_version") != "flowark-task-start-content-preparation-v1" or payload.get("launch_ready") is not True:
        raise ValueError("Knowledge-control inputs are not ready for execution")
    slots = payload.get("slots")
    if payload.get("case_count") != 314 or not isinstance(slots, list) or len(slots) != 314 or any(not isinstance(slot, dict) or slot.get("status") != "data_ready" for slot in slots):
        raise ValueError("Knowledge control requires 314 prepared task slots")
    benchmark = directory / str(payload.get("benchmark_path") or "benchmark.json")
    benchmark = benchmark.resolve()
    if not benchmark.is_relative_to(directory) or not benchmark.is_file():
        raise ValueError("Knowledge-control benchmark must be supplied beside its manifest")
    cases = json.loads(benchmark.read_text()).get("cases")
    if not isinstance(cases, list) or len(cases) != 314:
        raise ValueError("Knowledge-control benchmark must contain 314 tasks")
    return benchmark, manifest


@lru_cache(maxsize=8)
def _read_control_settings(files: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    from flowark.eval.harness.injection_control import ALIGNED_CONFIG_FIELDS

    common = None
    for filename, _modified, _size in files:
        fixture = json.loads(Path(filename).read_text())
        settings = fixture.get("source_settings")
        if not isinstance(settings, dict):
            raise ValueError("Knowledge-control fixture is missing its protocol settings")
        selected = {key: settings[key] for key in ALIGNED_CONFIG_FIELDS if key in settings}
        if common is None:
            common = selected
        elif common != selected:
            raise ValueError("Knowledge-control tasks use different protocol settings")
    return common or {}


def control_protocol_settings(workspace_root: Path, independent_run: Any) -> dict[str, Any]:
    _benchmark, manifest = control_inputs(workspace_root, independent_run)
    payload = json.loads(manifest.read_text())
    files = []
    for slot in payload["slots"]:
        path = (manifest.parent / str(slot.get("fixture_path") or "")).resolve()
        if not path.is_relative_to(manifest.parent) or not path.is_file():
            raise ValueError("Knowledge-control fixture must stay in its prepared directory")
        stat = path.stat()
        files.append((str(path), stat.st_mtime_ns, stat.st_size))
    return dict(_read_control_settings(tuple(files)))


def standard_control_setting(field: str) -> bool:
    return field == "agent_adapter" or field.startswith("opencode_") or field in {
        "timeout_seconds", "sink_categories", "code_recall_intensity",
    }
