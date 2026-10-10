"""Prepare isolated task state from a validated knowledge-control manifest."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from flowark.knowledge.control_history import (
    TASK_START_SCHEMA, control_instant, control_path, verified_history_reports,
)
from flowark.knowledge.injection_control import load_control_fixture
from flowark.state_paths import get_workspace_state_paths


ALIGNED_CONFIG_FIELDS = (
    "agent_adapter", "opencode_model", "opencode_after_tool_delivery",
    "opencode_bash_policy", "opencode_post_phase_mode", "opencode_structured_output",
    "knowledge_mode", "knowledge_allow_repeat_injection_within_session", "auto_knowledge_cycle",
    "runtime_injection_mode", "knowledge_distillation_mode", "knowledge_packaging_mode",
    "auto_knowledge_validate_mode", "knowledge_reuse_digest_mode",
    "knowledge_repeat_summary_react_gap", "knowledge_repeat_full_react_gap", "code_recall_intensity",
    "knowledge_top_k", "knowledge_recall_top_m", "timeout_seconds", "sink_categories",
)


def load_control_manifest(path: Path | str) -> tuple[Path, dict[str, Any]]:
    path = Path(path).expanduser().resolve(strict=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != TASK_START_SCHEMA or manifest.get("launch_ready") is not True:
        raise ValueError("知识内容对照清单尚未通过任务起点启动验收")
    slots = manifest.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("知识内容对照清单缺少任务")
    keys = [(row.get("app_name"), row.get("source_id")) for row in slots]
    if len(keys) != len(set(keys)) or any(not app or not source for app, source in keys):
        raise ValueError("知识内容对照清单的任务身份缺失或重复")
    if any(row.get("status") != "data_ready" or not row.get("fixture_path") for row in slots):
        raise ValueError("知识内容对照任务材料尚未就绪")
    if manifest.get("case_count", len(slots)) != len(slots):
        raise ValueError("知识内容对照清单任务数不一致")
    return path, manifest


def validate_control_cohort(config: Any, cases: list[Any]) -> None:
    if config.knowledge_control_manifest is None:
        return
    _, manifest = load_control_manifest(config.knowledge_control_manifest)
    expected = [(row["app_name"], row["source_id"]) for row in manifest["slots"]]
    actual = [(case.app_name, case.source_id) for case in cases]
    if actual != expected:
        raise ValueError("知识内容对照输入必须与准备清单的任务身份及顺序一致")


def _input_roots(harness: Any, manifest_path: Path) -> list[Path]:
    return [manifest_path.parent, harness.workspace_root / "artifact-data",
            get_workspace_state_paths(harness.workspace_root).workspace_state_root]


def _copytree(source: Path, target: Path, roots: list[Path]) -> None:
    for path in [source, *source.rglob("*")]:
        control_path(path, base_dir=source, allowed_roots=roots)
        if path.is_symlink():
            raise ValueError("知识对照副本不允许符号链接")
    shutil.copytree(source, target)


def _validate_fixture(harness: Any, task: Any, slot: dict[str, Any], fixture_path: Path,
                      roots: list[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    from flowark.experiments.content_control_preparation import execution_fingerprint

    fixture = load_control_fixture(fixture_path, allowed_roots=roots)
    fixture.pop("_fixture_path", None)
    fixture.pop("_fixture_sha256", None)
    fixture.pop("_donor_pool_sha256", None)
    donors = fixture.pop("donors")
    for field, expected in (("app_name", task.case.app_name), ("source_id", task.case.source_id)):
        if fixture.get(field) != expected:
            raise ValueError(f"知识对照 fixture {field} 与任务不一致")
    cutoff = control_instant(fixture["cutoff_time"])
    for field in ("case_input_path", "skills_dir", "donor_pool_path", "history_reports_manifest"):
        fixture[field] = str(control_path(fixture[field], base_dir=fixture_path.parent, allowed_roots=roots))
    input_path = Path(fixture["case_input_path"])
    case_input = json.loads(input_path.read_text(encoding="utf-8"))
    if case_input != task.case.raw:
        raise ValueError("知识对照原始任务输入与当前任务不一致")
    settings = fixture["source_settings"]
    for field, supported in (
        ("opencode_knowledge_synth_effort", "default"),
        ("knowledge_injection_char_budget", 4000),
        ("knowledge_delta_char_budget", None),
    ):
        if field in settings and settings[field] != supported:
            raise ValueError(f"知识内容对照配置 {field} 超出公开执行器支持的固定值")
    for field in ALIGNED_CONFIG_FIELDS:
        if field not in settings:
            continue
        actual, expected = getattr(harness.config, field, None), settings[field]
        if field == "opencode_model":
            actual, expected = str(actual or "").removeprefix("anthropic/"), str(expected or "").removeprefix("anthropic/")
        if actual != expected:
            raise ValueError(f"知识内容对照配置 {field} 与冻结协议不一致")
    skills = fixture.get("historical_skills")
    if not isinstance(skills, list) or not skills:
        raise ValueError("任务起点对照缺少冻结知识清单")
    skills_root = Path(fixture["skills_dir"])
    expected_paths = set()
    for skill in skills:
        if skill["app_name"] != fixture["app_name"] or control_instant(skill["available_at"]) > cutoff:
            raise ValueError("知识对照原始知识身份或可见时间不一致")
        path = control_path(skill["skill_file_path"], base_dir=fixture_path.parent, allowed_roots=[skills_root])
        if hashlib.sha256(path.read_bytes()).hexdigest() != skill["source_sha256"]:
            raise ValueError("知识对照原始知识内容不一致")
        skill["skill_file_path"] = str(path)
        expected_paths.add(path)
    if {path.resolve() for path in skills_root.rglob("*.md")} != expected_paths:
        raise ValueError("知识对照知识目录与冻结清单不一致")
    from flowark.knowledge.manager import KnowledgeManager

    loaded = KnowledgeManager(skills_root)
    expected_ids = {(str(skill["app_name"]), str(skill["id"])) for skill in skills}
    loaded_ids = {(skill.get_app_name(), skill.id) for skill in loaded.skills.values()}
    if loaded_ids != expected_ids or len(loaded.skills) != len(skills):
        raise ValueError("知识对照冻结知识无法完整加载")
    if not donors:
        raise ValueError("知识对照供体池为空")
    for donor in donors:
        if donor["app_name"] == fixture["app_name"] or control_instant(donor["available_at"]) > cutoff:
            raise ValueError("知识对照供体身份或可见时间不一致")
        for text_field, hash_field in (("skill_file_snapshot", "source_sha256"), ("note_text", "note_sha256"), ("summary_text", "summary_sha256")):
            if hashlib.sha256(donor[text_field].encode("utf-8")).hexdigest() != donor[hash_field]:
                raise ValueError("知识对照供体内容不一致")
    reports = verified_history_reports(fixture, allowed_root=roots, base_dir=fixture_path.parent)
    fingerprint = execution_fingerprint(case_input, settings, skills, donors, reports)
    if fingerprint != fixture.get("execution_fingerprint") or fingerprint != slot.get("execution_fingerprint"):
        raise ValueError("知识对照执行指纹不一致")
    return fixture, {"reports": reports, "validated_config_fields": [field for field in ALIGNED_CONFIG_FIELDS if field in settings]}


def prepare_control_case(harness: Any, task: Any) -> tuple[Path, Path]:
    manifest_path, manifest = load_control_manifest(harness.config.knowledge_control_manifest)
    matches = [row for row in manifest["slots"]
               if row["app_name"] == task.case.app_name and row["source_id"] == task.case.source_id]
    if len(matches) != 1:
        raise ValueError("每个知识对照任务必须对应唯一 fixture")
    if harness.config.serialize_within_app or harness.config.repeats != 1:
        raise ValueError("任务起点对照要求关闭同应用串行且 repeats=1")
    roots = _input_roots(harness, manifest_path)
    fixture_path = control_path(matches[0]["fixture_path"], base_dir=manifest_path.parent, allowed_roots=roots)
    fixture, verified = _validate_fixture(harness, task, matches[0], fixture_path, roots)
    fixture = deepcopy(fixture)
    source_skills = Path(fixture["skills_dir"])
    repeat_dir = Path(task.repeat_dir).resolve()
    trial_dir = repeat_dir / f"control-state-{uuid4().hex[:12]}"
    trial_dir.mkdir(parents=True, exist_ok=False)
    knowledge_root = trial_dir / "knowledge"
    for name in ("skills", "egress", "provenance"):
        source = source_skills if name == "skills" else source_skills.parent / name
        target = knowledge_root / name
        if source.is_dir():
            control_path(source, base_dir=fixture_path.parent, allowed_roots=roots)
            _copytree(source, target, roots)
        else:
            target.mkdir(parents=True)
    for field, filename in (("case_input_path", "case_input.json"), ("donor_pool_path", "donors.json")):
        target = trial_dir / filename
        shutil.copyfile(fixture[field], target)
        fixture[field] = str(target)
    reports = []
    for index, row in enumerate(verified["reports"]):
        source = Path(row["copied_report_path"])
        source_id = str(row["source_id"])
        if Path(source_id).name != source_id or source_id in {".", ".."}:
            raise ValueError("知识对照 source_id 不能作为安全的案例目录名")
        run_name = Path(str(row.get("run_id") or row.get("source_run_dir") or source.parent.name)).name
        if run_name in {"", ".", ".."}:
            raise ValueError("知识对照历史报告缺少运行身份")
        target = trial_dir / "history-reports" / str(index) / source_id / "repeat-01" / "runs" / run_name / "final_report.json"
        target.parent.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(source, target)
        reports.append(dict(row, copied_report_path=str(target)))
    history_manifest = trial_dir / "history-reports.json"
    history_manifest.write_text(json.dumps({"cutoff_time": fixture["cutoff_time"], "reports": reports}, ensure_ascii=False, indent=2) + "\n")
    fixture["history_reports_manifest"] = str(history_manifest)
    for skill in fixture["historical_skills"]:
        skill["skill_file_path"] = str(knowledge_root / "skills" / Path(skill["skill_file_path"]).relative_to(source_skills))
    fixture.update({"skills_dir": str(knowledge_root / "skills"), "knowledge_root": str(knowledge_root),
                    "trial_control_mode": harness.config.knowledge_control_mode,
                    "source_fixture_copy": str(fixture_path),
                    "source_fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                    "new_knowledge_scope": "private_trial_only_no_cross_case_feedback",
                    "runtime_validation": {"status": "verified", "source_execution_fingerprint": fixture["execution_fingerprint"],
                                           "core_config_fields": verified["validated_config_fields"], "historical_report_count": len(reports)}})
    target_fixture = trial_dir / "fixture.json"
    target_fixture.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n")
    return target_fixture, knowledge_root / "skills"
