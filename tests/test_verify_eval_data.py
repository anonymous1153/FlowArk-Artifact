from __future__ import annotations

import csv
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_eval_data")


def make_eval(root: Path, *, fixture: bool = False) -> dict:
    repeat = root / "flowark/case/repeat-01"
    repeat.mkdir(parents=True)
    case = {"app_name": "example.app_1", "source_id": "case"}
    (repeat / "case_input.json").write_text(json.dumps(case))
    row = {"entry_id": "one", **case, "repeat_dir": "flowark/case/repeat-01",
           "paths": {"case_input": "flowark/case/repeat-01/case_input.json"}}
    if fixture:
        frozen = root / "fixtures/case"
        frozen.mkdir(parents=True)
        (frozen / "case_input.json").write_text(json.dumps(case))
        (frozen / "fixture.json").write_text(json.dumps({**case, "case_input_path": "fixtures/case/case_input.json"}))
        row["paths"]["control_fixture"] = "fixtures/case/fixture.json"
    (root / "results.jsonl").write_text(json.dumps(row) + "\n")
    for name in ("config.json", "manifest.json", "planned_runs.json"):
        (root / name).write_text(json.dumps({"app_names": ["example.app_1"]}))
    return row


def test_declared_control_input_is_valid_without_mutation(tmp_path, verifier):
    make_eval(tmp_path, fixture=True)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert verifier.verify_public_eval_root(tmp_path, {"example.app_1"}) == {"task_count": 1, "app_count": 1}
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_case_path_cannot_follow_symlink_outside_root(tmp_path, verifier):
    root = tmp_path / "eval"
    make_eval(root)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"app_name": "example.app_1"}))
    target = root / "flowark/case/repeat-01/case_input.json"
    target.rename(root / "original-input.json")
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        verifier.verify_public_eval_root(root, {"example.app_1"})


def test_distinct_tasks_can_share_a_display_label(tmp_path, verifier):
    first = make_eval(tmp_path)
    second = {**first, "source_id": "second", "repeat_dir": "flowark/second/repeat-01",
              "paths": {"case_input": "flowark/second/repeat-01/case_input.json"}}
    case = tmp_path / second["paths"]["case_input"]
    case.parent.mkdir(parents=True)
    case.write_text(json.dumps({"app_name": "example.app_1", "source_id": "second"}))
    (tmp_path / "results.jsonl").write_text("\n".join(json.dumps(row) for row in (first, second)) + "\n")
    assert verifier.verify_public_eval_root(tmp_path, {"example.app_1"})["task_count"] == 2
    (tmp_path / "results.jsonl").write_text("\n".join(json.dumps(first) for _ in range(2)) + "\n")
    with pytest.raises(ValueError, match="repeat directories"):
        verifier.verify_public_eval_root(tmp_path, {"example.app_1"})


def test_existing_evaluation_is_not_downloaded_or_replaced(tmp_path, monkeypatch, verifier):
    fetch = importlib.import_module("fetch_artifact_data")
    root = tmp_path / "artifact"
    (root / "data").mkdir(parents=True)
    with (root / "data/source-archives-manifest.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["filename", "package_name", "version_code"])
        writer.writeheader()
        writer.writerow({"filename": "example.app_1_src.tar.gz", "package_name": "example.app", "version_code": "1"})
    with (root / "data/evaluation-archives-manifest.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["filename", "eval_root", "sha256"])
        writer.writeheader()
        writer.writerow({"filename": "paper-example.tar.gz", "eval_root": "paper-example", "sha256": "unused"})
    state = tmp_path / "state"
    monkeypatch.setenv("FLOWARK_DATA_ROOT", str(state))
    target = state / fetch.workspace_id(root) / "evals/evals/paper-example"
    make_eval(target)
    (target / ".flowark_archive_sha256").write_text("unused\n")
    before = (target / "results.jsonl").read_bytes()
    monkeypatch.setattr(fetch, "download_asset", lambda **_: pytest.fail("valid local data must be used without a download"))
    fetch.fetch_eval_logs(root, None)
    assert (target / "results.jsonl").read_bytes() == before


def test_invalid_existing_evaluation_fails_without_replacing_it(tmp_path, monkeypatch, verifier):
    fetch = importlib.import_module("fetch_artifact_data")
    root = tmp_path / "artifact"
    (root / "data").mkdir(parents=True)
    (root / "data/source-archives-manifest.csv").write_text("filename,package_name,version_code\nexample.app_1_src.tar.gz,example.app,1\n")
    (root / "data/evaluation-archives-manifest.csv").write_text("filename,eval_root,sha256\npaper-example.tar.gz,paper-example,unused\n")
    monkeypatch.setenv("FLOWARK_DATA_ROOT", str(tmp_path / "state"))
    target = fetch.studio_data_root(root) / fetch.workspace_id(root) / "evals/evals/paper-example"
    target.mkdir(parents=True)
    sentinel = target / "unfinished.json"
    sentinel.write_text("keep")
    monkeypatch.setattr(fetch, "download_asset", lambda **_: pytest.fail("existing data must not be overwritten"))
    with pytest.raises(FileNotFoundError):
        fetch.fetch_eval_logs(root, None)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("marker", [None, "previous-archive"])
def test_old_or_unverified_local_evaluation_is_not_reused(tmp_path, monkeypatch, verifier, marker):
    fetch = importlib.import_module("fetch_artifact_data")
    root = tmp_path / "artifact"
    (root / "data").mkdir(parents=True)
    (root / "data/source-archives-manifest.csv").write_text("filename,package_name,version_code\nexample.app_1_src.tar.gz,example.app,1\n")
    (root / "data/evaluation-archives-manifest.csv").write_text("filename,eval_root,sha256\npaper-example.tar.gz,paper-example,current-archive\n")
    monkeypatch.setenv("FLOWARK_DATA_ROOT", str(tmp_path / "state"))
    target = fetch.studio_data_root(root) / fetch.workspace_id(root) / "evals/evals/paper-example"
    make_eval(target)
    if marker is not None:
        (target / ".flowark_archive_sha256").write_text(marker)
    original = (target / "results.jsonl").read_bytes()
    monkeypatch.setattr(fetch, "download_asset", lambda **_: pytest.fail("existing data must remain untouched"))
    with pytest.raises(ValueError, match="archive identity"):
        fetch.fetch_eval_logs(root, None)
    assert (target / "results.jsonl").read_bytes() == original
