#!/usr/bin/env python3
"""Verify the published source corpus, benchmark membership, and coverage."""

from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path

from build_public_benchmarks import select_strat15_by_relative_rank
from analyze_strat15_sampling import verify_report
from verify_eval_data import load_allowed_apps


def verify(root: Path) -> dict:
    summary = json.loads((root / "data/source-corpus-summary.json").read_text())
    manifest_path = root / summary["source_manifest"]
    allowed = load_allowed_apps(manifest_path)
    with manifest_path.open(newline="", encoding="utf-8") as fp:
        archives = list(csv.DictReader(fp))
    assert len(archives) == len(allowed) == summary["app_count"]
    assert len({row["package_name"] for row in archives}) == len(archives)
    assert sum(int(row["size_bytes"]) for row in archives) == summary["source_archive_total_bytes"]
    digests = dict(line.split(maxsplit=1)[::-1] for line in
                   (root / "data/source-archives-sha256.txt").read_text().splitlines() if line.strip())
    assert digests == {row["filename"]: row["sha256"] for row in archives}

    inventory_path = root / summary["source_inventory"]
    raw = inventory_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == summary["source_inventory_sha256"]
    inventory = [json.loads(line) for line in raw.splitlines() if line.strip()]
    by_key = {(row["app_name"], row["occurrence_id"]): row for row in inventory}
    sizes = Counter(row["app_name"] for row in inventory)
    assert len(inventory) == len(by_key) == summary["eligible_source_occurrences"]
    assert set(sizes) <= allowed
    assert len(sizes) == summary["apps_with_eligible_sources"]
    assert len(allowed - set(sizes)) == summary["apps_without_eligible_sources"]
    assert dict(Counter(row["source_kind"] for row in inventory)) == summary["source_family_counts"]
    assert len({row["source_subtype"] for row in inventory}) == summary["source_subtypes"]

    cases_by_workload = {}
    for name, specification in summary["workloads"].items():
        payload = json.loads((root / specification["benchmark"]).read_text())
        cases = payload["cases"]
        assert payload["name"] == name
        assert len(cases) == specification["source_occurrences"]
        assert len({case["app_name"] for case in cases}) == specification["app_count"]
        assert len({(case["app_name"], case["case_id"]) for case in cases}) == len(cases)
        for case in cases:
            assert case["app_name"] in allowed
            row = by_key[(case["app_name"], case["source_id"])]
            assert case["source_archive"] == row["source_archive"]
            for field in ("source_kind", "source_subtype", "rule_id", "boundary_type", "alignment_tier"):
                assert case[field] == row[field], (name, case["case_id"], field)
            for field, value in case["source"].items():
                assert value == row[field], (name, case["case_id"], field)
        assert dict(Counter(case["source_kind"] for case in cases)) == specification["source_family_counts"]
        assert len({case["source_subtype"] for case in cases}) == specification["source_subtypes"]
        assert specification["coverage_numerator"] == len(cases)
        assert specification["coverage_denominator"] == len(inventory)
        assert math.isclose(specification["source_coverage_percent"],
                            100 * len(cases) / len(inventory), rel_tol=0, abs_tol=1e-12)
        cases_by_workload[name] = cases

    main = cases_by_workload["Main49"]
    main_sizes = Counter(case["app_name"] for case in main)
    top_apps = sorted(sizes, key=lambda app: (-sizes[app], app))[:len(main_sizes)]
    assert set(main_sizes) == set(top_apps)
    assert all(main_sizes[app] == sizes[app] for app in main_sizes)
    assert select_strat15_by_relative_rank(main) == cases_by_workload["Strat15"]
    sampling = verify_report(root)

    return {
        "app_count": len(allowed),
        "apps_with_eligible_sources": len(sizes),
        "eligible_source_occurrences": len(inventory),
        "main49_source_occurrences": len(main),
        "main49_source_coverage_percent": summary["workloads"]["Main49"]["source_coverage_percent"],
        "strat15_source_occurrences": len(cases_by_workload["Strat15"]),
        "strat15_valid_position_schemes": sampling["candidate_counts"]["valid"],
        "strat15_source_subtype_tvd": sampling["selected"]["source_subtype_tvd"],
        "strat15_unique_minimum": sampling["unique_minimum"],
        "status": "verified",
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parents[1]), indent=2))
