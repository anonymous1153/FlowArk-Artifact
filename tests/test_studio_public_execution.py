from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from flowark.state_paths import get_workspace_state_paths
from flowark_studio.api.routes.tasks import create_task_router
from flowark_studio.common.config_presets import apply_experiment_preset_to_params, get_eval_schema
from flowark_studio.common.eval_progress import rebuild_eval_progress_metadata_from_results_file
from flowark_studio.common.event_bus import EventBus
from flowark_studio.common.models import StudioEvent, StudioTask
from flowark_studio.common.paper_experiments import control_inputs, paper_eval_jobs
from flowark_studio.process.command_codec import StudioCommandCodec
from flowark_studio.process.inspection import StudioTaskInspection
from flowark_studio.process.manager import ProcessManager


REPO = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def record(index=1, cost=1.25, *, included=True):
    repeat = f"flowark/case-{index}/repeat-01"
    end = {"input_tokens": 100, "cache_read_input_tokens": 10,
           "cache_creation_input_tokens": 5, "output_tokens": 7}
    if cost is not None:
        end["total_cost_usd_sum"] = cost
    return {"entry_id": f"entry-{index}", "task_index": index, "task_total": index,
            "app_name": "app", "source_id": f"source-{index}", "repeat_idx": 1,
            "mode": "flowark", "status": "success", "paper_included": included,
            "repeat_dir": repeat, "run_dir": f"{repeat}/runs/run-{index}",
            "metrics": {"end_to_end": end}}


def write_results(root: Path, rows) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "results.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def cases(count=1284):
    return [{"app_name": f"app-{index % 15}", "case_id": f"case-{index}",
             "source_id": f"case-{index}"} for index in range(count)]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "artifact"
    root.mkdir()
    monkeypatch.setenv("FLOWARK_DATA_ROOT", str(root / "artifact-data/studio-state"))
    write_json(root / "artifact-data/benchmarks/source-first-v3.2-strat15.json", {"cases": cases()})
    write_json(root / "artifact-data/benchmarks/source-first-v3.2-main50.json", {"cases": cases()})
    return root


@pytest.fixture
def manager(workspace, monkeypatch):
    instance = ProcessManager(workspace_root=workspace)
    monkeypatch.setattr(instance, "_capture_workspace_git_snapshot", lambda: {})
    monkeypatch.setattr(instance, "_refresh_dispatch_locked", lambda: [])
    monkeypatch.setattr(instance, "_schedule_historical_refresh", lambda **kwargs: None)

    async def no_process(*args, **kwargs):
        raise AssertionError("The test must not start a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    return instance


def gateway_params(**extra):
    return {"runtime_backend_base_url": "https://gateway.example/v1",
            "runtime_backend_auth_token": "test-key-kept-in-memory",
            "opencode_provider": "openai", "opencode_model": "glm-5.3", **extra}


def flag(command, name):
    return command[command.index(name) + 1]


def prepare_control_inputs(workspace):
    settings = {"agent_adapter": "opencode", "opencode_model": "glm-5.3", "opencode_structured_output": False,
                "knowledge_mode": "warm", "auto_knowledge_cycle": True,
                "knowledge_reuse_digest_mode": "live_corridor_v2", "knowledge_top_k": 3,
                "knowledge_recall_top_m": 8, "timeout_seconds": 1800}
    for number in (1, 2, 3):
        root = workspace / "artifact-data/reproduction/knowledge-control/execution" / f"r{number}"
        write_json(root / "benchmark.json", {"cases": cases(314)})
        slots = []
        for index, row in enumerate(cases(314)):
            fixture = f"fixtures/{index}.json"
            write_json(root / fixture, {"source_settings": settings})
            slots.append({"status": "data_ready", "app_name": row["app_name"], "flow_id": row["source_id"],
                          "fixture_path": fixture})
        write_json(root / "manifest.json", {
            "schema_version": "flowark-task-start-content-preparation-v1", "launch_ready": True,
            "case_count": 314, "benchmark_path": "benchmark.json", "slots": slots,
        })


def test_method_defaults_and_explicit_choice(workspace):
    standard = apply_experiment_preset_to_params(params={"experiment_preset": "naive"}, kind="eval")
    full = apply_experiment_preset_to_params(params={"experiment_preset": "flowark_full"}, kind="eval")
    assert standard["serialize_within_app"] is False
    assert full["serialize_within_app"] is True
    assert standard["knowledge_allow_repeat_injection_within_session"] is False
    assert full["knowledge_allow_repeat_injection_within_session"] is False
    explicit = apply_experiment_preset_to_params(
        params={"experiment_preset": "flowark_full", "serialize_within_app": False,
                "knowledge_allow_repeat_injection_within_session": True}, kind="eval")
    assert explicit["serialize_within_app"] is False
    assert explicit["knowledge_allow_repeat_injection_within_session"] is True
    fields = {row["name"] for row in get_eval_schema(workspace_root=workspace)["fields"]}
    assert {"paper_experiment", "paper_condition", "independent_run"} <= fields


@pytest.mark.asyncio
async def test_repeated_experiment_api_and_cli(manager, workspace):
    from main import build_parser

    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_task_router())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://studio") as client:
        response = await client.post("/api/eval", json={
            "params": gateway_params(paper_experiment="glm53_repeated", paper_condition="all", independent_run="all"),
            "dispatch_mode": "force_parallel",
        })
    assert response.status_code == 200, response.text
    ids = response.json()["task_ids"]
    assert len(ids) == len(set(ids)) == 6
    tasks = [manager._tasks[task_id] for task_id in ids]
    assert len({task.metadata["pair_id"] for task in tasks}) == 3
    outputs = set()
    for task in tasks:
        parsed = build_parser().parse_args(task.command[4:])
        assert parsed.repeats == 1
        assert parsed.opencode_model == "glm-5.3"
        assert parsed.max_sources == 1284 and parsed.max_apps == 15
        assert flag(task.command, "--app-names") == ""
        assert task.metadata["dispatch_mode"] == "force_parallel"
        assert flag(task.command, "--knowledge-allow-repeat-injection-within-session") == "off"
        expected_serial = "off" if task.params["paper_condition"] == "standard" else "on"
        assert flag(task.command, "--serialize-within-app") == expected_serial
        assert flag(task.command, "--timeout-seconds") == "1800"
        output = Path(flag(task.command, "--out-dir"))
        assert output.is_relative_to(get_workspace_state_paths(workspace).evals_dir / "executions")
        outputs.add(output)
        assert "test-key-kept-in-memory" not in json.dumps(task.to_dict())
    assert len(outputs) == 6
    assert "test-key-kept-in-memory" not in manager._runtime_state_path.read_text()
    from flowark.eval.harness.knowledge import _prepare_knowledge_scope
    scopes = []
    for task in tasks:
        if task.params["paper_condition"] != "flowark":
            continue
        harness = SimpleNamespace(_dataset_slug=lambda cases: "sub15")
        evaluation = Path(flag(task.command, "--out-dir")) / "evaluation"
        scope = _prepare_knowledge_scope(harness, eval_root=evaluation, cases=[], modes=["flowark"])
        skills = Path(scope["skills_dir"])
        assert not list(skills.iterdir())
        (skills / "learned.md").write_text("knowledge from this evaluation")
        scopes.append(skills)
    assert len(set(scopes)) == 3


@pytest.mark.asyncio
async def test_control_series_has_nine_independent_conditions(manager, workspace):
    from main import build_parser

    prepare_control_inputs(workspace)
    ids = await manager.start_paper_evals(gateway_params(
        paper_experiment="knowledge_control", paper_condition="all", independent_run="all"))
    assert len(ids) == 9
    outputs = set()
    for task_id in ids:
        task = manager._tasks[task_id]
        parsed = build_parser().parse_args(task.command[4:])
        condition = task.params["paper_condition"]
        assert parsed.knowledge_control_mode == {"standard": "off", "flowark": "matched", "irrelevant": "irrelevant"}[condition]
        assert flag(task.command, "--opencode-structured-output") == "off"
        assert flag(task.command, "--auto-knowledge-cycle") == ("off" if condition == "standard" else "on")
        assert flag(task.command, "--knowledge-reuse-digest-mode") == ("off" if condition == "standard" else "live_corridor_v2")
        assert flag(task.command, "--modes") == ("naive" if condition == "standard" else "flowark")
        assert flag(task.command, "--serialize-within-app") == "off"
        assert flag(task.command, "--repeats") == "1"
        assert parsed.max_sources == 314 and parsed.max_apps == 15
        assert Path(parsed.knowledge_control_manifest).parent.name == "r" + str(task.params["independent_run"])
        assert len(json.loads(Path(flag(task.command, "--input")).read_text())["cases"]) == 314
        outputs.add(flag(task.command, "--out-dir"))
    assert len(outputs) == 9


@pytest.mark.asyncio
async def test_series_validates_all_inputs_before_queueing(manager, workspace):
    prepare_control_inputs(workspace)
    bad = workspace / "artifact-data/reproduction/knowledge-control/execution/r3/manifest.json"
    payload = json.loads(bad.read_text())
    payload["launch_ready"] = False
    write_json(bad, payload)
    with pytest.raises(ValueError, match="ready"):
        await manager.start_paper_evals(gateway_params(paper_experiment="knowledge_control"))
    assert not manager._tasks


def test_control_benchmark_cannot_escape_prepared_directory(workspace):
    prepare_control_inputs(workspace)
    path = workspace / "artifact-data/reproduction/knowledge-control/execution/r1/manifest.json"
    payload = json.loads(path.read_text())
    payload["benchmark_path"] = "../outside.json"
    write_json(path, payload)
    write_json(path.parent.parent / "outside.json", {"cases": cases(314)})
    with pytest.raises(ValueError, match="beside"):
        control_inputs(workspace, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("max_cases", 1), ("max_apps", 1), ("max_sources", 1),
                                        ("app_names", ["one-app"]), ("classification_filter", "source_has_true_flow")])
async def test_fixed_series_rejects_all_task_filters(manager, field, value):
    params = gateway_params(paper_experiment="glm53_repeated", **{field: value})
    with pytest.raises(ValueError, match="Paper experiments"):
        await manager.start_paper_evals(params)
    assert not manager._tasks
    if field == "max_cases":
        task_id = await manager.start_eval(gateway_params(experiment_preset="naive", max_cases=value))
        assert flag(manager._tasks[task_id].command, "--max-cases") == str(value)


def test_wrong_model_and_round_are_rejected():
    with pytest.raises(ValueError, match="Independent run"):
        paper_eval_jobs({"paper_experiment": "glm53_repeated", "independent_run": "4"})
    with pytest.raises(ValueError, match="glm-5.3"):
        apply_experiment_preset_to_params(params={"paper_experiment": "glm53_repeated",
            "paper_condition": "standard", "independent_run": "1", "opencode_model": "another-model"}, kind="eval")


def published_fixture(workspace, *, count=1):
    root = get_workspace_state_paths(workspace).evals_dir / "evals/paper-sample"
    rows = [record(index + 1) for index in range(count)]
    write_json(workspace / "artifact-data/reproduction/catalog.json", {"experiments": [
        {"eval_root": root.name, "workload": "Sub15", "experiment": "model-comparison", "tasks": count}]})
    write_json(root / "manifest.json", {"created_at": "2026-01-01T00:00:00+00:00", "case_count": count})
    write_json(root / "config.json", {"modes": ["flowark"], "opencode_model": "glm-5.3"})
    write_json(root / "summary.json", {"task_count": count, "completed_task_count": count, "success_count": count})
    write_json(root / "planned_runs.json", {"runs": rows})
    write_results(root, rows)
    return root, rows


@pytest.mark.asyncio
async def test_published_import_and_all_write_controls_are_read_only(manager, workspace):
    root, rows = published_fixture(workspace)
    write_json(root / rows[0]["run_dir"] / "config.json", {"not_an_eval": True})
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert await manager.load_historical_tasks() == 1
    task = next(iter(manager._tasks.values()))
    assert task.metadata["read_only"] is True
    assert task.metadata["label"] == "paper-sample"
    assert await manager.stop_task(task.task_id) is False
    assert await manager.pause_eval(task.task_id) is False
    task.status = "paused"
    assert await manager.resume_eval(task.task_id) is False
    with pytest.raises(ValueError, match="read-only"):
        await manager.set_task_tags(task.task_id, ["change"])
    with pytest.raises(PermissionError, match="read-only"):
        manager._write_eval_state(root, {"status": "running"})
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert "_eval_progress_completed_records" not in task.metadata


def test_fresh_private_state_imports_published_results_at_app_startup(workspace, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from flowark_studio.api.app import create_app

    root, _ = published_fixture(workspace)
    private_state = tmp_path / "private-state"
    monkeypatch.setenv("FLOWARK_DATA_ROOT", str(private_state))
    static = workspace / "flowark_studio/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html>Studio</html>")
    app = create_app(workspace_root=workspace)
    assert not app.state.manager._state_paths.evals_dir.exists()
    with TestClient(app) as client:
        response = client.get("/api/tasks")
        assert response.status_code == 200
        tasks = response.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["metadata"]["read_only"] is True
        assert tasks[0]["metadata"]["label"] == root.name
    assert not app.state.manager._state_paths.evals_dir.exists()


def test_codec_rejects_published_output_override(workspace):
    root, _ = published_fixture(workspace)
    codec = StudioCommandCodec(workspace_root=workspace, state_paths=get_workspace_state_paths(workspace))
    with pytest.raises(ValueError, match="executions"):
        codec.build_eval_command({"experiment_preset": "naive", "out_dir": str(root)})


def test_costs_keep_unknown_values_and_the_paired_denominator(tmp_path):
    rows = [record(1, 1.125), record(2, None), record(3, None, included=False)]
    metadata = {}
    rebuild_eval_progress_metadata_from_results_file(metadata, write_results(tmp_path / "eval", rows))
    cost = metadata["eval_open_code_cost"]
    assert metadata["eval_progress"]["completed_count"] == 3
    assert cost["cost_record_count"] == 2
    assert cost["cost_missing_count"] == 1
    assert cost["completed_display_cost_usd"] is None
    assert cost["completed_end_to_end_cost_usd"] is None
    assert cost["tracked_display_cost_usd"] == 1.125
    assert cost["completed_input_tokens"] == 230
    assert cost["paper_metric_excluded_count"] == 1


def test_task_indices_keep_distinct_records_with_shared_entry_labels(tmp_path):
    rows = [record(1, 1.0), record(2, 2.0)]
    rows[1]["entry_id"] = rows[0]["entry_id"]
    metadata = {}
    rebuild_eval_progress_metadata_from_results_file(metadata, write_results(tmp_path / "eval", rows))
    assert metadata["eval_open_code_cost"]["cost_record_count"] == 2
    assert metadata["eval_open_code_cost"]["completed_display_cost_usd"] == 3.0


def test_mem0_cost_is_included_and_missing_backend_is_unknown(tmp_path):
    complete = record(1, 2.0)
    complete["metrics"]["end_to_end"]["mem0_total_cost_usd_sum"] = 1.2
    complete["metrics"]["mem0_backend"] = {"cost_tracked": True, "status": "ok"}
    metadata = {}
    path = write_results(tmp_path / "mem0-eval", [complete])
    rebuild_eval_progress_metadata_from_results_file(metadata, path)
    assert metadata["eval_open_code_cost"]["completed_display_cost_usd"] == pytest.approx(3.2)
    assert metadata["eval_open_code_cost"]["completed_input_tokens"] == 115
    incomplete = record(1, 2.0)
    incomplete["metrics"]["mem0_backend"] = {"cost_tracked": False, "status": "missing"}
    write_results(path.parent, [incomplete])
    rebuild_eval_progress_metadata_from_results_file(metadata, path)
    assert metadata["eval_open_code_cost"]["completed_display_cost_usd"] is None
    assert metadata["eval_open_code_cost"]["completed_end_to_end_cost_usd"] == 2.0


def test_relative_cost_reference_uses_owning_eval_and_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / "eval"
    row = record(1, None)
    row["paths"] = {"cost_summary_json": "evidence/cost_summary.json"}
    write_json(root / row["paths"]["cost_summary_json"], {"aggregated_metrics": {"main_agent": {"total_cost_usd_sum": 4.5}}})
    results = write_results(root, [row])
    monkeypatch.chdir(tmp_path.parent)
    metadata = {}
    rebuild_eval_progress_metadata_from_results_file(metadata, results)
    assert metadata["eval_open_code_cost"]["completed_display_cost_usd"] == 4.5
    row["paths"]["cost_summary_json"] = "../outside.json"
    write_json(tmp_path / "outside.json", {"aggregated_metrics": {"main_agent": {"total_cost_usd_sum": 99}}})
    write_results(root, [row])
    rebuild_eval_progress_metadata_from_results_file(metadata, results)
    assert metadata["eval_open_code_cost"]["completed_display_cost_usd"] is None


def inspection_task(root, name="task"):
    return StudioTask(task_id=name, kind="eval", status="success", created_at="2026-01-01", params={},
                      paths={"eval_root": str(root)}, metadata={"historical": True})


def test_result_reader_is_not_truncated_and_cache_is_bounded(workspace):
    root = workspace / "large-eval"
    rows = [record(1), record(2)]
    rows[0]["unused_report_text"] = "x" * 20_000_100
    path = write_results(root, rows)
    inspector = StudioTaskInspection(workspace_root=workspace)
    task = inspection_task(root)
    terminal = lambda status: status == "success"
    first = inspector.list_task_eval_runs(task, is_terminal_eval_run_status=terminal)
    assert len(first["runs"]) == 2
    assert all("unused_report_text" not in row for row in first["runs"])
    rows[1]["metrics"]["end_to_end"]["total_cost_usd_sum"] = 5.0
    write_results(root, rows)
    changed = inspector.list_task_eval_runs(task, is_terminal_eval_run_status=terminal)
    assert changed["runs"][1]["display_cost_usd"] == 5.0
    for index in range(9):
        other = workspace / f"eval-{index}"
        write_results(other, [record()])
        inspector.list_task_eval_runs(inspection_task(other, f"task-{index}"), is_terminal_eval_run_status=terminal)
    assert len(inspector._eval_source_caches) == 8
    assert ("task", "summary") not in inspector._eval_runs_cache


def test_prepared_evidence_is_readable_but_external_paths_are_not(workspace):
    root, rows = published_fixture(workspace)
    task = inspection_task(root)
    inspector = StudioTaskInspection(workspace_root=workspace)
    for relative in ["fixtures/case/fixture.json", "knowledge-inputs/history/report.json",
                     "knowledge-outputs/run/knowledge.json", "knowledge_scope/data/entry.json",
                     rows[0]["run_dir"] + "/opencode_turns/01-analysis/messages.json"]:
        write_json(root / relative, {"description": "Forerunner evidence"})
        assert inspector.read_task_artifact(task, relative)["json"]["description"] == "Forerunner evidence"
    outside = workspace / "artifact-data/source-code/app/source.json"
    write_json(outside, {"outside": True})
    link = root / "fixtures/case/outside.json"
    link.symlink_to(outside)
    with pytest.raises(PermissionError):
        inspector.read_task_artifact(task, str(link))
    with pytest.raises(PermissionError):
        inspector.read_task_artifact(task, str(outside))


def test_selected_run_artifacts_include_declared_guidance_and_input_snapshots(workspace):
    root, rows = published_fixture(workspace)
    row = rows[0]
    guidance = row["run_dir"] + "/historical_reuse_guidance.md"
    snapshot = "knowledge-inputs/run-1/skills/app/history.md"
    row["paths"] = {"historical_reuse_guidance_md": guidance, "knowledge_inputs": "knowledge-inputs/run-1"}
    write_json(root / row["repeat_dir"] / "result.json", row)
    for relative in (guidance, snapshot):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Recorded knowledge evidence\n")
    inspector = StudioTaskInspection(workspace_root=workspace)
    task = inspection_task(root)
    found = inspector.list_task_artifacts(task, selected_run_dir=row["run_dir"])
    paths = {item["path"] for item in found["artifacts"]}
    assert {guidance, snapshot} <= paths
    assert inspector.read_task_artifact(task, guidance)["content"] == "Recorded knowledge evidence\n"


def test_source_expression_survives_redaction_while_credentials_are_masked(workspace):
    inspector = StudioTaskInspection(workspace_root=workspace)
    source = "password = binding.passwordEdit.text.toString()"
    payload = {"source": source, "query": f"Track the source `{source}`.", "password": "actual-secret"}
    public = inspector._public_json_value(payload)
    assert public["source"] == source and source in public["query"]
    assert public["password"] == "xxx"
    for text in ("password='actual-secret'", "password=actual-secret", "api_key=eyJheader.eyJpayload.signature",
                 "Authorization: Bearer actual-secret", '{"password":"actual-secret"}'):
        redacted = inspector._redact_public_text(text)
        assert "actual-secret" not in redacted and "eyJheader" not in redacted and "xxx" in redacted


@pytest.mark.asyncio
@pytest.mark.parametrize("global_stream", [False, True])
async def test_event_subscriber_releases_on_replay_cancel(global_stream):
    bus = EventBus()
    event = StudioEvent(event_id="one", task_id="task", kind="eval", type="task_status", ts="now", seq=1)
    await bus.publish(event)
    stream = bus.subscribe_all(replay_last=1) if global_stream else bus.subscribe_task("task", replay_last=1)
    assert await anext(stream) is event
    assert len(bus._subscribers) == 1
    await stream.aclose()
    assert not bus._subscribers


@pytest.mark.asyncio
async def test_zero_replay_waits_for_new_event():
    bus = EventBus()
    await bus.publish(StudioEvent(event_id="old", task_id="task", kind="eval", type="task_status", ts="now", seq=1))
    stream = bus.subscribe_task("task", replay_last=0)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    await bus.publish(StudioEvent(event_id="new", task_id="task", kind="eval", type="task_status", ts="now", seq=2))
    assert (await pending).event_id == "new"
    await stream.aclose()
    assert not bus._subscribers


@pytest.mark.asyncio
async def test_sse_response_closes_subscribed_generator():
    closed = []

    async def events():
        try:
            yield StudioEvent(event_id="event", task_id="task", kind="eval", type="task_status", ts="now", seq=1)
        finally:
            closed.append(True)

    async def subscribe(**kwargs):
        return events()

    async def public(event):
        return {"task_id": event.task_id}

    manager = SimpleNamespace(subscribe_all_events=subscribe, public_event_payload=public)
    async def disconnected():
        return False
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(manager=manager)), is_disconnected=disconnected)
    endpoint = next(route.endpoint for route in create_task_router().routes if route.path == "/api/events")
    response = await endpoint(request, replay_last=0)
    stream = response.body_iterator
    await anext(stream)
    assert b'task_status' in await anext(stream)
    await stream.aclose()
    assert closed == [True]


def test_javascript_unknown_cost_and_method_defaults():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    program = r'''
const fs = require('fs'); const vm = require('vm'); const assert = require('assert');
const root = process.argv[1]; const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(root + '/flowark_studio/static/app/tasks/tasks-list.js', 'utf8'), ctx);
const missing = {metadata:{eval_progress:{completed_count:2}, eval_open_code_cost:{completed_display_cost_usd:null,cost_missing_count:1,cost_record_count:2}}};
assert.strictEqual(ctx.getEvalTaskOpenCodeCostText(missing), 'Cost incomplete');
assert.strictEqual(ctx.getEvalTaskOpenCodeAverageCostText(missing), '');
const known = {metadata:{eval_progress:{completed_count:3},eval_open_code_cost:{completed_display_cost_usd:4,cost_record_count:2}}};
assert.strictEqual(ctx.getEvalTaskOpenCodeAverageCostText(known), 'avg $2.0000');
vm.runInContext(fs.readFileSync(root + '/flowark_studio/static/app/tasks/tasks-form.js', 'utf8'), ctx);
ctx.renderForm = () => {}; ctx.renderFormConditionalHint = () => {};
const values = {experiment_preset:'naive',serialize_within_app:true};
ctx.handleFormFieldChanged('experiment_preset', values);
assert.strictEqual(values.serialize_within_app, false);
'''
    subprocess.run([node, "-e", program, str(REPO)], check=True)


def test_javascript_uses_recorded_knowledge_and_keeps_missing_metrics_unknown():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    program = r'''
const fs = require('fs'); const vm = require('vm'); const assert = require('assert');
const root = process.argv[1];
const task = {task_id:'published',kind:'eval',paths:{eval_root:'.'}};
const run = {run_dir:'flowark/case/repeat-01/runs/run-1',repeat_dir:'flowark/case/repeat-01',paths:{}};
const state = {selectedTaskId:task.task_id,selectedTaskSnapshot:task,evalRuns:[run],
  knowledgeInjectionByRunDir:{},knowledgeSkillContentByRunDir:{},knowledgeSkillContentLoadingByRunDir:{},runPerformanceByKey:{}};
const ctx = {state,URLSearchParams,escapeHtml:String,formatParsedObject:JSON.stringify};
vm.createContext(ctx);
for (const file of ['base/base-task.js','transcript/transcript-knowledge.js','eval/eval.js','summary/summary-common.js','runtime/panes.js']) {
  vm.runInContext(fs.readFileSync(root + '/flowark_studio/static/app/' + file, 'utf8'), ctx);
}
ctx.resolveEvalRunArtifactDir = (row) => row.run_dir;
ctx.evalRunKey = (row) => row.run_dir;
ctx.selectedEvalRunEntry = () => run;
state.selectedEvalRunDir = run.run_dir;
let requests = 0; let reads = [];
ctx.api = async () => {requests++; return {artifacts:[]};};
ctx.readArtifactAsText = async (taskId,path) => {reads.push(path); return 'recorded input';};
const inline = {injected_skill_ids:['history'],details:[{type:'skill',skill_id:'history',skill_file_snapshot:'same event snapshot'}]};
(async () => {
  state.knowledgeInjectionByRunDir[run.run_dir] = [inline];
  await ctx.ensureKnowledgeSkillContentsForRun(task,run.run_dir);
  assert.strictEqual(requests,0); assert.strictEqual(reads.length,0);
  state.knowledgeInjectionByRunDir[run.run_dir] = [{matched_skill_ids:['missing']}];
  await ctx.ensureKnowledgeSkillContentsForRun(task,run.run_dir);
  assert.strictEqual(requests,0); assert.strictEqual(reads.length,0);
  const missingHtml = ctx.renderKnowledgeEventSection(state.knowledgeInjectionByRunDir[run.run_dir]);
  assert(missingHtml.includes('MATCHED') && missingHtml.includes('no available knowledge content snapshot'));
  run.paths.knowledge_inputs = 'knowledge-inputs/run-1';
  state.knowledgeSkillContentByRunDir[run.run_dir] = {};
  const snapshot = 'knowledge-inputs/run-1/skills/app/history.md';
  const other = 'knowledge-inputs/run-2/skills/app/history.md';
  const global = 'knowledge_scope/skills/app/history.md';
  const artifacts = [snapshot,other,global].map(path => ({path,exists:true,is_dir:false}));
  ctx.api = async () => {requests++; return {artifacts};};
  state.knowledgeInjectionByRunDir[run.run_dir] = [{matched_skill_ids:['history','missing']}];
  await ctx.ensureKnowledgeSkillContentsForRun(task,run.run_dir);
  assert.strictEqual(requests,1); assert.strictEqual(reads.length,1); assert.strictEqual(reads[0],snapshot);
  const inputHtml = ctx.renderKnowledgeEventSection(state.knowledgeInjectionByRunDir[run.run_dir]);
  assert(inputHtml.includes('recorded input snapshot')); assert(!inputHtml.includes('current file on disk'));
  assert(ctx.renderKnowledgeEventSection([inline]).includes('same event snapshot'));
  assert.strictEqual(ctx.filterArtifactsBySelectedEvalRun(task,artifacts).length,1);
  for (const fn of ['formatUsd','formatNumberCompact','formatMsAsSeconds','formatSeconds','formatDurationCompact']) {
    assert.strictEqual(ctx[fn](null),''); assert.strictEqual(ctx[fn](undefined),'');
  }
  const perf = {end_to_end:{input_tokens:100,cache_read_input_tokens:20,cache_creation_input_tokens:5}};
  assert.strictEqual(ctx.getPerformanceMetricRawValue(perf,'end_to_end','input_tokens_including_cache'),125);
  assert.strictEqual(ctx.getPerformanceMetricRawValue({end_to_end:{}},'end_to_end','input_tokens_including_cache'),null);
  assert.strictEqual(ctx.getEvalRunDisplayEndToEndCostValue({display_cost_usd:3.2}),3.2);
  assert.strictEqual(ctx.getEvalRunDisplayEndToEndCostValue({display_cost_usd:null}),null);
})().catch(error => {console.error(error);process.exitCode=1;});
'''
    subprocess.run([node, "-e", program, str(REPO)], check=True)
