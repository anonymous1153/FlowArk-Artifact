from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml

from flowark.adapters.opencode.adapter import OpenCodeRunSession
from flowark.adapters.opencode.bridge import _render_after_tool_payload, _render_request_submit_payload
from flowark.eval.harness.health import _collect_knowledge_control_issues
from flowark.eval.harness.injection_control import prepare_control_case, validate_control_cohort
from flowark.experiments.content_control_preparation import execution_fingerprint
from flowark.knowledge.control_history import control_path, load_trial_history
from flowark.knowledge.injection_control import (
    InjectionControlError, TASK_START_CONTROL_SCHEMA, load_control_fixture, transform_injection_text,
)
from flowark.knowledge.manager import KnowledgeManager
from flowark.knowledge.runtime_augment import (
    compute_after_tool_augment, compute_request_submit_augment, format_skills,
)
from flowark.semantics.models import (
    AfterToolContext, AnalysisRunContext, AugmentRuntimeConfig, Phase, RequestSubmitContext, SessionHandle,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value: str | bytes) -> str:
    return sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Offline control verification attempted a network request")

    monkeypatch.setattr(httpx.Client, "send", unexpected)
    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected)


@pytest.fixture
def frozen(tmp_path: Path):
    root = tmp_path / "execution"
    skills = root / "knowledge/skills"
    cutoff, earlier = "2026-09-26T12:00:00+08:00", "2026-09-26T11:00:00+08:00"

    def skill_text(identifier: str, app: str, symbol: str) -> str:
        metadata = {
            "schema_version": "ldfc-skill-v5", "id": identifier, "app_name": app,
            "name": f"Knowledge {identifier}", "version": 1, "validation_status": "validated",
            "match_rules": {"require_any": [{"kind": "exact_symbol", "value": symbol}]},
            "entry_condition": "Inspect the object returned by the selected reader.",
        }
        return "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n核心结论: " + "数据沿对象字段传播。" * 18 + "\n"

    inventory = []
    for identifier, symbol in (("primary", "com.example.PrimaryReader"), ("secondary", "com.example.SecondaryReader")):
        path = skills / "app" / f"{identifier}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = skill_text(identifier, "app", symbol)
        path.write_text(text, encoding="utf-8")
        inventory.append({"id": identifier, "app_name": "app", "version": 1, "available_at": earlier,
                          "skill_file_path": str(path), "source_sha256": digest(text)})
    donor_skills = root / "donor-skills/other"
    donor_skills.mkdir(parents=True)
    donor_snapshot = skill_text("donor-1", "other", "org.other.ReaderFactory")
    (donor_skills / "donor-1.md").write_text(donor_snapshot)
    donor = next(iter(KnowledgeManager(donor_skills.parent).skills.values()))
    note = format_skills([donor]).strip()
    summary = "### NOTE_SUMMARY: Other app\n- Independent evidence. " + "异应用历史知识。" * 18
    donors = [{"id": "donor-1", "app_name": "other", "version": 1, "available_at": earlier,
               "skill_file_snapshot": donor_snapshot, "source_sha256": digest(donor_snapshot),
               "note_text": note, "note_sha256": digest(note), "summary_text": summary,
               "summary_sha256": digest(summary), "anchors": ["org.other.ReaderFactory"]}]
    raw = {"app_name": "app", "source_id": "source", "source_dir": str(tmp_path / "sources")}
    write_json(root / "case_input.json", raw)
    report_path = root / "history/previous/report.json"
    write_json(report_path, {"flows": [], "origin": "previous"})
    reports = [{"app_name": "app", "source_id": "previous", "run_id": "prior-run",
                "available_at": earlier, "copied_report_path": str(report_path), "sha256": digest(report_path.read_bytes())}]
    write_json(root / "history.json", {"cutoff_time": cutoff, "reports": reports})
    write_json(root / "donors.json", {"cutoff_time": cutoff, "generic_knowledge_filter": False,
                                     "token_count_matching": False, "donors": donors})
    settings = {"knowledge_allow_repeat_injection_within_session": False, "opencode_model": "glm-5.3"}
    fingerprint = execution_fingerprint(raw, settings, inventory, donors, reports)
    fixture = {
        "schema_version": TASK_START_CONTROL_SCHEMA, "execution_mode": "task_start", "fixture_id": "example",
        "app_name": "app", "source_id": "source", "cutoff_time": cutoff,
        "initial_injection": "replace", "after_tool_injection": "replace", "generic_knowledge_filter": False,
        "token_count_matching": False, "length_metric": "unicode_codepoints", "character_length_ratio": [0.8, 1.25],
        "case_input_path": str(root / "case_input.json"), "skills_dir": str(skills),
        "donor_pool_path": str(root / "donors.json"), "history_reports_manifest": str(root / "history.json"),
        "historical_skills": inventory, "source_settings": settings, "execution_fingerprint": fingerprint,
        "forbidden_anchors": ["com.example"],
    }
    write_json(root / "fixture.json", fixture)
    manifest = {"schema_version": TASK_START_CONTROL_SCHEMA, "launch_ready": True, "case_count": 1,
                "slots": [{"app_name": "app", "source_id": "source", "status": "data_ready",
                           "fixture_path": "fixture.json", "execution_fingerprint": fingerprint}]}
    write_json(root / "manifest.json", manifest)
    config = SimpleNamespace(knowledge_control_mode="matched", knowledge_control_manifest=root / "manifest.json",
                             serialize_within_app=False, repeats=1, **settings)
    harness = SimpleNamespace(config=config, workspace_root=tmp_path)
    task = SimpleNamespace(case=SimpleNamespace(app_name="app", source_id="source", raw=raw),
                           repeat_dir=tmp_path / "output/repeat-01")
    return SimpleNamespace(root=root, fixture=fixture, harness=harness, task=task, donors=donors)


def test_isolated_task_copies_preserve_frozen_sources_and_history(frozen):
    before = {p.relative_to(frozen.root): p.read_bytes() for p in frozen.root.rglob("*") if p.is_file()}
    validate_control_cohort(frozen.harness.config, [frozen.task.case])
    first, first_skills = prepare_control_case(frozen.harness, frozen.task)
    (first_skills / "app/primary.md").write_text("trial-local update\n")
    frozen.harness.config.knowledge_control_mode = "irrelevant"
    second, second_skills = prepare_control_case(frozen.harness, frozen.task)
    assert first.parent != second.parent
    assert (second_skills / "app/primary.md").read_bytes() == before[Path("knowledge/skills/app/primary.md")]
    assert {p.relative_to(frozen.root): p.read_bytes() for p in frozen.root.rglob("*") if p.is_file()} == before
    copied = load_control_fixture(second)
    assert copied["trial_control_mode"] == "irrelevant"
    assert copied["new_knowledge_scope"] == "private_trial_only_no_cross_case_feedback"
    history, metadata = load_trial_history(second, app_name="app")
    assert len(history) == 1 and history[0]["source_id"] == "previous"
    assert Path(history[0]["copied_report_path"]).is_relative_to(second.parent)
    assert metadata["history_report_scope"] == "frozen_task_start_same_app_reports"


def test_controls_reject_misaligned_cohort_settings_and_changed_inputs(frozen):
    with pytest.raises(ValueError, match="身份及顺序"):
        validate_control_cohort(frozen.harness.config, [])
    frozen.harness.config.opencode_model = "other-model"
    with pytest.raises(ValueError, match="opencode_model"):
        prepare_control_case(frozen.harness, frozen.task)
    frozen.harness.config.opencode_model = "glm-5.3"
    (frozen.root / "knowledge/skills/app/primary.md").write_text("changed source\n")
    with pytest.raises(ValueError, match="原始知识内容"):
        prepare_control_case(frozen.harness, frozen.task)
    assert not frozen.task.repeat_dir.exists()


def test_control_paths_reject_symbolic_links_within_the_allowed_tree(frozen):
    (frozen.root / "linked-history").symlink_to(frozen.root / "history", target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        control_path(frozen.root / "linked-history/previous/report.json", base_dir=frozen.root,
                     allowed_roots=[frozen.root])


@pytest.mark.parametrize("field,value", [
    ("opencode_knowledge_synth_effort", "high"), ("knowledge_injection_char_budget", 8000),
    ("knowledge_delta_char_budget", 2000),
])
def test_task_settings_require_supported_delivery_budgets_and_model_effort(frozen, field, value):
    frozen.fixture["source_settings"][field] = value
    write_json(frozen.root / "fixture.json", frozen.fixture)
    with pytest.raises(ValueError, match=field):
        prepare_control_case(frozen.harness, frozen.task)
    assert not frozen.task.repeat_dir.exists()


@pytest.mark.parametrize("kind", ["NOTE", "NOTE_SUMMARY"])
def test_character_matching_preserves_matched_text_and_selects_deterministic_cross_app_donors(frozen, kind):
    fixture = load_control_fixture(frozen.root / "fixture.json")
    original = f"### {kind}: Original\n" + "目标知识。" * 40
    donor_text = f"### {kind}: Unrelated\n" + "供体知识。" * 40
    field, hash_field = ("note_text", "note_sha256") if kind == "NOTE" else ("summary_text", "summary_sha256")
    valid = dict(fixture["donors"][0], **{field: donor_text, hash_field: digest(donor_text)})
    fixture["donors"] = [dict(valid, id="z"), dict(valid, id="a"), dict(valid, id="same", app_name="app"),
                         dict(valid, id="future", available_at="2026-09-27T00:00:00Z")]
    matched = transform_injection_text(original, mode="matched", fixture=fixture, app_name="app", max_chars=1000)
    assert matched.text == original and matched.audit["original_sha256"] == matched.audit["actual_sha256"]
    actual = transform_injection_text(original, mode="irrelevant", fixture=fixture, app_name="app", max_chars=1000)
    assert actual.text == donor_text
    mapping = actual.audit["donor_mappings"][0]
    assert mapping["donor_id"] == "a" and 0.8 <= mapping["character_length_ratio"] <= 1.25
    assert mapping["actual_chars"] != mapping["actual_utf8_bytes"]
    rejected = {row["donor_id"]: row["reasons"] for row in actual.audit["rejected_candidates"]}
    assert "not_other_app" in rejected["same"] and "available_after_cutoff" in rejected["future"]
    with pytest.raises(InjectionControlError, match="no eligible"):
        transform_injection_text(original, mode="irrelevant", fixture=fixture, max_chars=len(donor_text) - 1)


def runtime_pair(frozen, mode: str, *, budget: int = 4000):
    frozen.harness.config.knowledge_control_mode = mode if mode != "off" else "matched"
    fixture, skills = prepare_control_case(frozen.harness, frozen.task)
    run_dir = fixture.parent / "run"
    runtime = AugmentRuntimeConfig(
        knowledge_control_mode=mode, knowledge_control_fixture=fixture if mode != "off" else None,
        skills_dir=skills, knowledge_allow_repeat_within_session=False, knowledge_realtime_min_interval_ms=0,
        knowledge_injection_char_budget=budget, knowledge_delta_char_budget=budget,
        knowledge_injection_log_path=run_dir / "knowledge_injection.jsonl",
    )
    return runtime, fixture, run_dir


def run_two_hooks(runtime):
    session = SessionHandle(adapter_name="opencode", session_id=uuid4().hex)
    first = compute_request_submit_augment(runtime_config=runtime, ctx=RequestSubmitContext(
        session=session, phase=Phase.ANALYSIS, user_prompt="Read com.example.PrimaryReader", app_name="app",
    ), wrap_payload_fn=_render_request_submit_payload)
    wrap_after = lambda payload: _render_after_tool_payload(payload, tool_name="read", delivery_surface="no_reply_context")
    second_ctx = AfterToolContext(session=session, phase=Phase.ANALYSIS, tool_name="read", tool_input={},
                                  tool_output="Observed com.example.SecondaryReader", app_name="app")
    second = compute_after_tool_augment(runtime_config=runtime, ctx=second_ctx, wrap_payload_fn=wrap_after)
    third = compute_after_tool_augment(runtime_config=runtime, ctx=second_ctx, wrap_payload_fn=wrap_after)
    assert first.should_deliver and second.should_deliver
    assert not third.should_deliver
    return [_render_request_submit_payload(first.payload), wrap_after(second.payload)]


def test_real_initial_and_after_tool_hooks_preserve_matched_and_deliver_controlled_bodies(frozen):
    standard, _, _ = runtime_pair(frozen, "off")
    matched, _, matched_dir = runtime_pair(frozen, "matched")
    unrelated, _, unrelated_dir = runtime_pair(frozen, "irrelevant")
    original_payloads = run_two_hooks(standard)
    assert run_two_hooks(matched) == original_payloads
    actual_payloads = run_two_hooks(unrelated)
    assert all(actual != original for actual, original in zip(actual_payloads, original_payloads))
    assert all(len(actual) <= 4000 and "donor-1" in actual for actual in actual_payloads)
    assert "命中规则:" in original_payloads[1] and "命中规则:" not in actual_payloads[1]
    for mode, run_dir, payloads in (("matched", matched_dir, original_payloads), ("irrelevant", unrelated_dir, actual_payloads)):
        events = rows(run_dir / "knowledge_control_events.jsonl")
        deliveries = rows(run_dir / "knowledge_injection.jsonl")
        delivered = [row for row in deliveries if row.get("injected_chars")]
        assert len(events) == len(delivered) == 2
        assert [event["actual_delivered_payload"] for event in events] == payloads
        assert [event["original_delivered_payload"] for event in events] == original_payloads
        assert all(row["knowledge_control"]["mode"] == mode for row in delivered)
        assert all(row["knowledge_control"]["frozen_selection"]["status"] == "verified" for row in delivered)
        if mode == "irrelevant":
            assert [row["shadow_injected_skill_ids"] for row in delivered] == [["primary"], ["secondary"]]
            assert all(row["injected_skill_ids"] == ["donor-1"] for row in delivered)


def make_session(runtime, fixture, run_dir, messages):
    class Client:
        async def create_session(self, **kwargs):
            return {"id": "fresh-session"}

        async def messages(self, **kwargs):
            return messages

    adapter = SimpleNamespace(name="opencode", _config=SimpleNamespace(
        knowledge_control_mode=runtime.knowledge_control_mode, knowledge_control_fixture=fixture, skills_dir=runtime.skills_dir,
    ), _remember_runtime=lambda **kwargs: None)
    return OpenCodeRunSession(adapter=adapter, runtime=SimpleNamespace(
        isolation_dir=run_dir, hook_context_payload={"hook_runtime_context": {"analysis_app_name": "app"}},
    ), server_handle=None, client=Client(), run_context=AnalysisRunContext(
        run_dir=run_dir, prompt="Trace the selected source.", turn_name="analysis",
    ), trace={})


def test_task_start_requires_a_fresh_session_and_reports_no_injection(frozen):
    runtime, fixture, run_dir = runtime_pair(frozen, "matched")
    session = make_session(runtime, fixture, run_dir, [])
    asyncio.run(session._ensure_session(title="Control"))
    write_json(run_dir / "run_meta.json", {"knowledge_control_mode": "matched"})
    audit = json.loads((run_dir / "knowledge_control_task_start.json").read_text())
    assert audit["empty_session_verified"] and audit["initial_injection"] == "identity"
    assert [issue["code"] for issue in _collect_knowledge_control_issues(run_dir)] == ["knowledge_control_no_intervention"]
    polluted = make_session(runtime, fixture, run_dir, [{"id": "earlier-message"}])
    with pytest.raises(InjectionControlError, match="empty native session"):
        asyncio.run(polluted._ensure_session(title="Control"))
    assert polluted._session is None
    assert "knowledge_control_failed" in {issue["code"] for issue in _collect_knowledge_control_issues(run_dir)}


def test_successful_hook_audits_and_missing_donors_remain_distinguishable(frozen):
    runtime, fixture, run_dir = runtime_pair(frozen, "irrelevant")
    asyncio.run(make_session(runtime, fixture, run_dir, [])._ensure_session(title="Control"))
    write_json(run_dir / "run_meta.json", {"knowledge_control_mode": "irrelevant"})
    run_two_hooks(runtime)
    assert _collect_knowledge_control_issues(run_dir) == []
    runtime, fixture, run_dir = runtime_pair(frozen, "irrelevant")
    asyncio.run(make_session(runtime, fixture, run_dir, [])._ensure_session(title="Control"))
    write_json(run_dir / "run_meta.json", {"knowledge_control_mode": "irrelevant"})
    pool_path = Path(json.loads(fixture.read_text())["donor_pool_path"])
    pool = json.loads(pool_path.read_text())
    pool["donors"] = []
    write_json(pool_path, pool)
    with pytest.raises(InjectionControlError, match="no eligible"):
        run_two_hooks(runtime)
    issues = _collect_knowledge_control_issues(run_dir)
    assert "knowledge_control_failed" in {issue["code"] for issue in issues}
    assert all(issue["severity"] == "error" for issue in issues)


def test_supported_skill_schemas_load_without_rewriting_source_bytes(frozen, capsys):
    egress = frozen.root / "knowledge/egress/app/primary.json"
    provenance = frozen.root / "knowledge/provenance/app/primary.json"
    write_json(egress, {"schema_version": "ldfc-egress-map-v2", "note_id": "primary", "boundary_summary": "",
                        "key_apis": [], "cases": []})
    write_json(provenance, {"schema_version": "ldfc-skill-provenance-v3", "note_id": "primary"})
    original = {path: path.read_bytes() for path in (egress, provenance, frozen.root / "knowledge/skills/app/primary.md")}
    manager = KnowledgeManager(frozen.root / "knowledge/skills")
    skill = manager.get_skill_by_id("primary", current_app_name="app")
    assert skill.metadata["schema_version"] == "flowark-skill-v5"
    assert skill.egress_map is not None and skill.provenance["schema_version"] == "flowark-skill-provenance-v3"
    assert all(path.read_bytes() == content for path, content in original.items())
    assert capsys.readouterr().out == ""
    source = frozen.root / "knowledge/skills/app/secondary.md"
    source.write_text(source.read_text().replace("ldfc-skill-v5", "unknown-skill-v9"))
    assert len(KnowledgeManager(frozen.root / "knowledge/skills").skills) == 1
    assert "unsupported schema_version" in capsys.readouterr().out
