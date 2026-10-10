from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowark.experiments.content_control_preparation import find_published_evals, prepare_published_round
from flowark.experiments.control_fixtures import FixtureError, sha256


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def published(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    evaluations = tmp_path / "published"
    root = evaluations / "paper-knowledge-content-irrelevant-r1"
    case = root / "fixtures/one"
    skill_path = case / "knowledge_scope/skills/app/known.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nschema_version: ldfc-skill-v5\nid: known\napp_name: app\nversion: 1\n---\nEvidence.\n")
    donor_text = "---\nschema_version: ldfc-skill-v5\nid: donor\napp_name: other\nversion: 2\n---\nOther evidence.\n"
    cutoff, earlier = "2026-09-26T12:00:00+08:00", "2026-09-26T11:00:00+08:00"
    write_json(case / "case_input.json", {"app_name": "app", "source_id": "source", "source_dir": "source.tar.gz", "path_base": "evaluation_root"})
    write_json(case / "history-reports/report.json", {"flows": [{"source": "previous"}]})
    write_json(case / "history-reports.json", {"cutoff_time": cutoff, "reports": [{
        "app_name": "app", "source_id": "previous", "available_at": earlier,
        "copied_report_path": "fixtures/one/history-reports/report.json", "source_run_dir": "flowark/previous/repeat-01/runs/run-1",
    }]})
    write_json(case / "donors.json", {"cutoff_time": cutoff, "generic_knowledge_filter": False,
        "token_count_matching": False, "donors": [{
            "id": "donor", "app_name": "other", "version": 2, "available_at": earlier,
            "skill_file_snapshot": donor_text, "note_text": "### NOTE: Other evidence",
            "summary_text": "### NOTE_SUMMARY: Other evidence", "anchors": ["OtherClass"],
        }]})
    write_json(case / "fixture.json", {
        "schema_version": "ldfc-task-start-content-preparation-v1", "execution_mode": "task_start",
        "path_base": "evaluation_root", "historical_repeat": "r1", "app_name": "app", "source_id": "source",
        "cutoff_time": cutoff, "case_input_path": "fixtures/one/case_input.json",
        "skills_dir": "fixtures/one/knowledge_scope/skills", "donor_pool_path": "fixtures/one/donors.json",
        "history_reports_manifest": "fixtures/one/history-reports.json", "forbidden_anchors": ["app"],
        "historical_skills": [{"id": "known", "app_name": "app", "version": 1, "available_at": earlier,
                               "skill_file_path": "fixtures/one/knowledge_scope/skills/app/known.md"}],
        "source_settings": {"opencode_model": "anthropic/glm-5.3", "knowledge_allow_repeat_injection_within_session": False},
        "initial_injection": "replace", "after_tool_injection": "replace", "generic_knowledge_filter": False,
        "token_count_matching": False, "length_metric": "unicode_codepoints", "character_length_ratio": [0.8, 1.25],
    })
    (root / "results.jsonl").write_text(json.dumps({"app_name": "app", "source_id": "source", "paths": {"control_fixture": "fixtures/one/fixture.json"}}) + "\n")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "app_src.tar.gz").mkdir()
    (sources / "app_src.tar.gz/Main.java").write_text("class Main {}\n")
    return evaluations, sources, tmp_path / "execution", case


def prepare(published: tuple[Path, Path, Path, Path]) -> dict:
    evaluations, sources, output, _ = published
    return prepare_published_round(evaluations, output, round_index=1, source_root=sources, expected_case_count=1)


def test_execution_inputs_preserve_evidence_and_are_idempotent(published):
    evaluations, sources, output, _ = published
    before = {path.relative_to(evaluations): path.read_bytes() for path in evaluations.rglob("*") if path.is_file()}
    manifest = prepare(published)
    assert prepare(published) == manifest
    assert {path.relative_to(evaluations): path.read_bytes() for path in evaluations.rglob("*") if path.is_file()} == before
    benchmark = json.loads((output / "benchmark.json").read_text())
    assert benchmark["cases"][0]["source_dir"] == str(sources / "app_src.tar.gz")
    assert "path_base" not in benchmark["cases"][0]
    fixture_path = output / manifest["slots"][0]["fixture_path"]
    fixture = json.loads(fixture_path.read_text())
    assert fixture["source_settings"]["opencode_model"] == "glm-5.3"
    assert fixture["execution_fingerprint"] == manifest["slots"][0]["execution_fingerprint"]
    donors = json.loads((fixture_path.parent / "donors.json").read_text())["donors"]
    assert donors[0]["source_sha256"] == sha256(donors[0]["skill_file_snapshot"])
    history = json.loads((fixture_path.parent / "history-reports.json").read_text())["reports"]
    assert history[0]["run_id"] == "run-1"
    assert history[0]["sha256"] == sha256(Path(history[0]["copied_report_path"]).read_bytes())


def test_later_donors_do_not_produce_a_launchable_manifest(published):
    _, _, output, case = published
    pool = json.loads((case / "donors.json").read_text())
    pool["donors"][0]["available_at"] = "2026-09-26T13:00:00+08:00"
    write_json(case / "donors.json", pool)
    with pytest.raises(FixtureError, match="after the task"):
        prepare(published)
    assert not (output / "manifest.json").exists()


def test_changed_evidence_cannot_overwrite_existing_execution_inputs(published):
    _, _, output, case = published
    prepare(published)
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    pool = json.loads((case / "donors.json").read_text())
    pool["donors"][0]["note_text"] += " Changed content."
    write_json(case / "donors.json", pool)
    with pytest.raises(FixtureError, match="already exists with different contents"):
        prepare(published)
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_input_symlink_cannot_escape_the_published_evaluation(published):
    _, _, output, case = published
    outside = output.parent / "outside.json"
    write_json(outside, {"app_name": "app", "source_id": "source"})
    (case / "external.json").symlink_to(outside)
    fixture = json.loads((case / "fixture.json").read_text())
    fixture["case_input_path"] = "fixtures/one/external.json"
    write_json(case / "fixture.json", fixture)
    with pytest.raises(FixtureError, match="escapes"):
        prepare(published)
    assert not (output / "manifest.json").exists()


def test_discovery_uses_the_local_workspace_directory_and_rejects_ambiguity(tmp_path):
    state = tmp_path / "state"
    first = state / "workspace-a/evals/evals"
    (first / "paper-knowledge-content-irrelevant-r1").mkdir(parents=True)
    assert find_published_evals(state, round_index=1) == first
    with pytest.raises(FixtureError, match="--evals-dir"):
        find_published_evals(state, round_index=2)
    (state / "workspace-b/evals/paper-knowledge-content-irrelevant-r1").mkdir(parents=True)
    with pytest.raises(FixtureError, match="--evals-dir"):
        find_published_evals(state, round_index=1)
