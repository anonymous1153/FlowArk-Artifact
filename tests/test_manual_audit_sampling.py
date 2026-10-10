from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sampling(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("sample_manual_audit")


def test_fixed_seed_selection_and_input_order_independence(sampling):
    keys = [(app, f"source-{n}", 1) for app in ("example.a", "example.b") for n in range(13)]
    selected, counts = sampling.sample_keys(keys)
    assert selected == [
        ("example.a", "source-11", 1), ("example.a", "source-7", 1), ("example.a", "source-8", 1),
        ("example.b", "source-1", 1), ("example.b", "source-11", 1), ("example.b", "source-3", 1),
    ]
    assert sampling.sample_keys(list(reversed(keys))) == (selected, counts)
    assert sampling.hash_key(("example.a", "source-8", 1)) == "288024d9f7039e5368790e3e46cff85ee74eb725114ae7fc4e3769fc6222950a"
    assert sampling.hash_key(("example.a", "source-8", 2)) != sampling.hash_key(("example.a", "source-8", 1))


@pytest.mark.parametrize(("population", "quota"), [(1, 0), (2, 0), (3, 1), (7, 1), (8, 2), (13, 3), (236, 47)])
def test_per_app_quota_uses_nearest_twenty_percent(sampling, population, quota):
    selected, counts = sampling.sample_keys([("example.a", str(n), 1) for n in range(population)])
    assert len(selected) == counts[0]["sample_count"] == quota


def test_duplicate_population_is_rejected(sampling):
    with pytest.raises(ValueError, match="unique stable keys"):
        sampling.sample_keys([("example.a", "source-1", 1)] * 2)


@pytest.mark.parametrize("row", [
    {"app_name": "example.a", "source_id": "source", "repeat_idx": True},
    {"app_name": "example.a", "source_id": "source", "repeat_idx": 0},
    {"app_name": "example|a", "source_id": "source"},
    {"app_name": "example.a", "source_id": ""},
])
def test_ambiguous_or_invalid_benchmark_keys_are_rejected(sampling, row):
    with pytest.raises(ValueError):
        sampling.stable_key(row, benchmark=True)


def test_published_manifest_matches_recomputed_sample_and_app_quotas(sampling):
    report = sampling.build_report(ROOT)
    sampling.verify_report(ROOT, report)
    assert (report["population_count"], report["sample_count"], report["app_count"]) == (1284, 257, 15)
    assert [(row["population_count"], row["sample_count"]) for row in report["per_app_counts"]] == [
        (236, 47), (183, 37), (137, 27), (111, 22), (91, 18),
        (79, 16), (68, 14), (61, 12), (58, 12), (53, 11),
        (50, 10), (47, 9), (40, 8), (36, 7), (34, 7),
    ]


def test_manifest_change_is_detected(sampling, tmp_path):
    report = sampling.build_report(ROOT)
    changed = deepcopy(report)
    changed["selected_keys"][0]["flow_id_or_source_id"] = "a-different-source"
    target = tmp_path / sampling.REPORT_PATH
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="committed sampling manifest differs"):
        sampling.verify_report(tmp_path, report)


def test_audit_validation_requires_exact_keys_and_detects_duplicates(sampling, tmp_path):
    keys = [{"app_name": "example.a", "flow_id_or_source_id": f"source-{n}", "repeat_idx": 1} for n in range(2)]
    report = {"selected_keys": keys}
    records = tmp_path / "final_records"
    records.mkdir()
    for n, key in enumerate(keys):
        (records / f"{n}.json").write_text(json.dumps({"case_key": key}))
    assert sampling.verify_audit(report, tmp_path) == 2
    (records / "1.json").write_text(json.dumps({"case_key": {**keys[1], "flow_id_or_source_id": "other"}}))
    with pytest.raises(ValueError, match="missing=1, extra=1"):
        sampling.verify_audit(report, tmp_path)
    (records / "1.json").write_text(json.dumps({"case_key": keys[0]}))
    with pytest.raises(ValueError, match="Duplicate manual-audit stable key"):
        sampling.verify_audit(report, tmp_path)


def test_missing_audit_cli_fails_with_preparation_command(tmp_path):
    process = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/sample_manual_audit.py"), "--check", "--audit-dir", str(tmp_path)],
        text=True, capture_output=True,
    )
    assert process.returncode == 1
    assert "scripts/fetch_artifact_data.py --manual-audit-logs" in process.stderr
    assert "verification" not in process.stdout


def test_installed_audit_records_match_exactly(sampling):
    audit_dir = ROOT / sampling.AUDIT_PATH
    if not (audit_dir / "final_records").is_dir():
        pytest.skip("The optional manual-audit release has not been installed")
    assert sampling.verify_audit(sampling.build_report(ROOT), audit_dir) == 257
