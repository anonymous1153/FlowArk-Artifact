"""Prepare task-start knowledge-content controls from the published fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .control_fixtures import FixtureError, parse_skill, read_json, read_jsonl, sha256, timestamp, write_json

PREP_SCHEMA = "flowark-task-start-content-preparation-v1"
CONFIG_FIELDS = (
    "agent_adapter", "opencode_model", "opencode_after_tool_delivery",
    "opencode_bash_policy", "opencode_post_phase_mode", "opencode_structured_output",
    "opencode_knowledge_synth_effort", "knowledge_mode",
    "knowledge_allow_repeat_injection_within_session", "auto_knowledge_cycle",
    "runtime_injection_mode", "knowledge_distillation_mode", "knowledge_packaging_mode",
    "auto_knowledge_validate_mode", "knowledge_reuse_digest_mode",
    "knowledge_repeat_summary_react_gap", "knowledge_repeat_full_react_gap",
    "knowledge_top_k", "knowledge_recall_top_m", "code_recall_intensity", "timeout_seconds",
    "sink_categories", "knowledge_injection_char_budget", "knowledge_delta_char_budget",
)


def content_hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def effective_settings(config: dict[str, Any]) -> dict[str, Any]:
    result = {key: config[key] for key in CONFIG_FIELDS if key in config}
    result["opencode_knowledge_synth_effort"] = result.get("opencode_knowledge_synth_effort") or "default"
    if result.get("opencode_model"):
        result["opencode_model"] = str(result["opencode_model"]).rsplit("/", 1)[-1].casefold()
    return result


def visible_fingerprint(pool: list[dict[str, Any]]) -> str:
    return content_hash(sorted((item["app_name"], item["id"], item["source_sha256"]) for item in pool))


def execution_fingerprint(
    case_input: dict[str, Any], settings: dict[str, Any], skills: list[dict[str, Any]],
    donors: list[dict[str, Any]], reports: list[dict[str, Any]] | None = None,
) -> str:
    identity = {key: value for key, value in case_input.items() if key not in {"source_dir", "path_base"}}
    return content_hash({
        "protocol": PREP_SCHEMA, "input": identity, "settings": effective_settings(settings),
        "target_knowledge": visible_fingerprint(skills), "donor_knowledge": visible_fingerprint(donors),
        "donor_renderings": sorted((item["app_name"], item["id"], item["note_sha256"], item["summary_sha256"]) for item in donors),
        "history_reports": sorted((item["source_id"], item["sha256"]) for item in reports or []),
        "intervention": "initial_and_after_tool_from_task_start", "generic_filter": False,
        "length_metric": "unicode_codepoints", "character_ratio": [0.8, 1.25],
        "serialize_within_app": False,
    })


def _input_path(value: str | Path, root: Path, *, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise FixtureError("published experiment input escapes its evaluation directory")
    if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise FixtureError("published experiment inputs must not use symbolic links")
    if (directory and not resolved.is_dir()) or (not directory and not resolved.is_file()):
        raise FixtureError("published experiment input has the wrong file type")
    return resolved


def _identity(row: dict[str, Any]) -> tuple[str, str]:
    app, source = row.get("app_name"), row.get("source_id")
    if not isinstance(app, str) or not isinstance(source, str) or not app or not source:
        raise FixtureError("experiment inputs require app_name and source_id")
    if Path(app).name != app or app in {".", ".."}:
        raise FixtureError("app_name must identify a source archive")
    return app, source


def _available(row: dict[str, Any], cutoff: str) -> None:
    if timestamp(row["available_at"]) > timestamp(cutoff):
        raise FixtureError("knowledge input became available after the task started")


def _knowledge_inputs(frozen: dict[str, Any], root: Path) -> tuple[Path, list[dict[str, Any]]]:
    skills_root = _input_path(frozen["skills_dir"], root, directory=True)
    skills = []
    for row in frozen["historical_skills"]:
        if row["app_name"] != frozen["app_name"]:
            raise FixtureError("matched knowledge must come from the target app")
        _available(row, frozen["cutoff_time"])
        path = _input_path(row["skill_file_path"], root)
        if not path.is_relative_to(skills_root):
            raise FixtureError("historical skill is outside the declared knowledge directory")
        data = path.read_bytes()
        metadata, _ = parse_skill(data.decode("utf-8"))
        if (metadata.get("id"), metadata.get("app_name"), str(metadata.get("version"))) != (
            row["id"], row["app_name"], str(row["version"])
        ):
            raise FixtureError("historical skill identity differs from its manifest")
        skills.append({key: row[key] for key in ("id", "app_name", "available_at", "version")} | {
            "skill_file_path": str(path), "source_sha256": sha256(data),
        })
    identities = [(row["app_name"], row["id"]) for row in skills]
    if not skills or len(set(identities)) != len(identities):
        raise FixtureError("historical knowledge must have unique nonempty identities")
    if {path.resolve() for path in skills_root.rglob("*.md")} != {Path(row["skill_file_path"]) for row in skills}:
        raise FixtureError("knowledge directory differs from its frozen inventory")
    return skills_root, skills


def _donor_inputs(frozen: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    pool = read_json(_input_path(frozen["donor_pool_path"], root))
    if timestamp(pool["cutoff_time"]) != timestamp(frozen["cutoff_time"]):
        raise FixtureError("donor pool cutoff differs from the task cutoff")
    if pool.get("generic_knowledge_filter") is not False or pool.get("token_count_matching") is not False:
        raise FixtureError("donor pool does not declare the character-matching protocol")
    donors = []
    for row in pool["donors"]:
        if row["app_name"] == frozen["app_name"]:
            raise FixtureError("control donors must come from another app")
        _available(row, frozen["cutoff_time"])
        metadata, _ = parse_skill(row["skill_file_snapshot"])
        if (metadata.get("id"), metadata.get("app_name"), str(metadata.get("version"))) != (
            row["id"], row["app_name"], str(row["version"])
        ):
            raise FixtureError("donor snapshot differs from its declared identity")
        donor = {key: row[key] for key in (
            "id", "app_name", "available_at", "version", "skill_file_snapshot", "note_text", "summary_text", "anchors",
        )}
        for text_field, hash_field in (("skill_file_snapshot", "source_sha256"),
                                       ("note_text", "note_sha256"), ("summary_text", "summary_sha256")):
            donor[hash_field] = sha256(donor[text_field])
        donors.append(donor)
    identities = [(row["app_name"], row["id"]) for row in donors]
    if not donors or len(set(identities)) != len(identities):
        raise FixtureError("donor pool must have unique nonempty identities")
    return donors


def _history_inputs(frozen: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    history = read_json(_input_path(frozen["history_reports_manifest"], root))
    if timestamp(history["cutoff_time"]) != timestamp(frozen["cutoff_time"]):
        raise FixtureError("history cutoff differs from the task cutoff")
    reports = []
    for row in history["reports"]:
        if row["app_name"] != frozen["app_name"] or row["source_id"] == frozen["source_id"]:
            raise FixtureError("historical reports must come from other sources in the same app")
        _available(row, frozen["cutoff_time"])
        path = _input_path(row["copied_report_path"], root)
        run_id = Path(str(row.get("run_id") or row.get("source_run_dir") or path.parent.name)).name
        reports.append({key: row[key] for key in ("app_name", "source_id", "available_at")} | {
            "run_id": run_id, "copied_report_path": str(path), "sha256": sha256(path.read_bytes()),
        })
    return reports


def prepare_published_round(
    published_evals: Path, output: Path, *, round_index: int, source_root: Path,
    expected_case_count: int = 314,
) -> dict[str, Any]:
    """Create local execution inputs from one published, fixed-order cohort.

    Source evidence is read in place. Local paths and integrity checks are written
    to the execution directory; an existing different input is never replaced.
    """
    if round_index not in {1, 2, 3}:
        raise FixtureError("independent run must be 1, 2, or 3")
    published_evals = Path(published_evals).expanduser().resolve(strict=True)
    root = _input_path(f"paper-knowledge-content-irrelevant-r{round_index}", published_evals, directory=True)
    source_root = Path(source_root).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(published_evals) or published_evals.is_relative_to(output):
        raise FixtureError("execution inputs must be outside the published evaluations")
    rows = read_jsonl(root / "results.jsonl")
    keys = [_identity(row) for row in rows]
    if len(rows) != expected_case_count or len(keys) != len(set(keys)):
        raise FixtureError("published knowledge-control cohort has an unexpected size or duplicate tasks")
    slots, cases = [], []
    for row, (app, source) in zip(rows, keys, strict=True):
        fixture_path = _input_path(row["paths"]["control_fixture"], root)
        frozen = read_json(fixture_path)
        if _identity(frozen) != (app, source) or frozen.get("path_base") != "evaluation_root":
            raise FixtureError("published fixture identity or path base differs from its result")
        if frozen.get("execution_mode") != "task_start" or frozen.get("historical_repeat") != f"r{round_index}":
            raise FixtureError("published fixture differs from the selected independent run")
        for field, expected in {
            "initial_injection": "replace", "after_tool_injection": "replace",
            "generic_knowledge_filter": False, "token_count_matching": False,
            "length_metric": "unicode_codepoints", "character_length_ratio": [0.8, 1.25],
        }.items():
            if frozen.get(field) != expected:
                raise FixtureError(f"published fixture differs from the knowledge-control protocol: {field}")
        timestamp(frozen["cutoff_time"])
        case_input = read_json(_input_path(frozen["case_input_path"], root))
        if _identity(case_input) != (app, source):
            raise FixtureError("published source input identity differs from its fixture")
        source_directory = _input_path(f"{app}_src.tar.gz", source_root, directory=True)
        case_input.pop("path_base", None)
        case_input["source_dir"] = str(source_directory)
        settings = effective_settings(frozen["source_settings"])
        for field, expected in {"opencode_knowledge_synth_effort": "default",
                                "knowledge_injection_char_budget": 4000,
                                "knowledge_delta_char_budget": None}.items():
            if field in settings and settings[field] != expected:
                raise FixtureError(f"published knowledge control requires the supported default for {field}")
        if settings.get("knowledge_allow_repeat_injection_within_session") is not False:
            raise FixtureError("knowledge control requires repeat injection to be disabled")
        skills_root, skills = _knowledge_inputs(frozen, root)
        donors = _donor_inputs(frozen, root)
        reports = _history_inputs(frozen, root)
        fingerprint = execution_fingerprint(case_input, settings, skills, donors, reports)
        case_id = sha256(app + "|" + source)[:20]
        directory = output / "cases" / case_id
        fixture = {
            "schema_version": PREP_SCHEMA, "execution_mode": "task_start",
            "fixture_id": f"r{round_index}-{case_id}", "app_name": app, "source_id": source,
            "cutoff_time": frozen["cutoff_time"], "source_settings": settings,
            "case_input_path": "case_input.json", "skills_dir": str(skills_root),
            "historical_skills": skills, "donor_pool_path": "donors.json",
            "history_reports_manifest": "history-reports.json",
            "forbidden_anchors": frozen["forbidden_anchors"],
            "initial_injection": "replace", "after_tool_injection": "replace",
            "generic_knowledge_filter": False, "token_count_matching": False,
            "length_metric": "unicode_codepoints", "character_length_ratio": [0.8, 1.25],
            "new_knowledge_scope": "private_trial_only_no_cross_case_feedback",
            "execution_fingerprint": fingerprint,
        }
        write_json(directory / "case_input.json", case_input)
        write_json(directory / "donors.json", {
            "cutoff_time": frozen["cutoff_time"], "donors": donors,
            "generic_knowledge_filter": False, "token_count_matching": False,
        })
        write_json(directory / "history-reports.json", {"cutoff_time": frozen["cutoff_time"], "reports": reports})
        write_json(directory / "fixture.json", fixture)
        slots.append({"app_name": app, "source_id": source, "status": "data_ready",
                      "fixture_path": str((directory / "fixture.json").relative_to(output)),
                      "execution_fingerprint": fingerprint})
        cases.append(case_input)
    write_json(output / "benchmark.json", {"schema_version": "flowark-benchmark-v1", "cases": cases})
    manifest = {
        "schema_version": PREP_SCHEMA, "launch_ready": True, "independent_run": round_index,
        "case_count": len(slots), "benchmark_path": "benchmark.json", "slots": slots,
        "repeats": 1, "dispatch_mode": "force_parallel", "serialize_within_app": False,
        "knowledge_allow_repeat_injection_within_session": False,
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def find_published_evals(state_root: Path, *, round_index: int) -> Path:
    name = f"paper-knowledge-content-irrelevant-r{round_index}"
    candidates = {
        directory.resolve()
        for workspace in state_root.iterdir() if workspace.is_dir()
        for directory in (workspace / "evals", workspace / "evals/evals")
        if (directory / name).is_dir()
    } if state_root.is_dir() else set()
    if len(candidates) != 1:
        raise FixtureError("use --evals-dir to select the directory containing the published evaluations")
    return candidates.pop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evals-dir", type=Path, help="Directory containing published evaluations; otherwise discover it in artifact-data/studio-state")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for local execution inputs")
    parser.add_argument("--round", required=True, type=int, choices=(1, 2, 3), dest="round_index")
    parser.add_argument("--source-root", required=True, type=Path, help="Directory containing the extracted app source trees")
    args = parser.parse_args()
    evals_dir = args.evals_dir or find_published_evals(
        Path(__file__).resolve().parents[2] / "artifact-data/studio-state", round_index=args.round_index,
    )
    manifest = prepare_published_round(evals_dir, args.output_dir, round_index=args.round_index, source_root=args.source_root)
    print(json.dumps({"independent_run": args.round_index, "case_count": manifest["case_count"],
                      "manifest": str(args.output_dir.resolve() / "manifest.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
