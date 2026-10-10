#!/usr/bin/env python3
"""Reproduce the app-stratified 20% manual-audit sample and verify its records."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


SEED = "260623-m1-stratified-20pct-v1"
BENCHMARK_PATH = Path("data/benchmarks/source-first-v3.2-strat15.template.json")
REPORT_PATH = Path("data/manual-audit-sampling.json")
AUDIT_PATH = Path("artifact-data/manual-audit/paper-strat15-relative-f1-manual-audit")
StableKey = tuple[str, str, int]


def stable_key(row: dict[str, Any], *, benchmark: bool = False) -> StableKey:
    app = row.get("app_name")
    source = (row.get("source_id") or row.get("case_id")) if benchmark else (
        row.get("flow_id_or_source_id") or row.get("flow_id") or row.get("source_id")
    )
    repeat = row.get("repeat_idx", 1) if benchmark else row.get("repeat_idx")
    if (not isinstance(app, str) or not app or not isinstance(source, str) or not source
            or type(repeat) is not int or repeat < 1):
        raise ValueError("Each task requires app_name, a source identifier, and a positive integer repeat_idx")
    if "|" in app or "|" in source:
        raise ValueError("Task identifiers must not contain the hash separator '|'")
    return app, source, repeat


def hash_key(key: StableKey) -> str:
    return hashlib.sha256(f"{SEED}|20pct|{key[0]}|{key[1]}|{key[2]}".encode("utf-8")).hexdigest()


def sample_keys(keys: list[StableKey]) -> tuple[list[StableKey], list[dict[str, Any]]]:
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("The sampling population must be nonempty and contain unique stable keys")
    strata: dict[str, list[StableKey]] = defaultdict(list)
    for key in keys:
        strata[key[0]].append(key)
    selected = []
    counts = []
    for app in sorted(strata, key=lambda name: (-len(strata[name]), name)):
        candidates = strata[app]
        # round_half_up(n * 0.20), evaluated exactly with integer arithmetic.
        quota = (2 * len(candidates) + 5) // 10
        ranked = sorted(candidates, key=lambda key: (hash_key(key), key))
        selected.extend(sorted(ranked[:quota]))
        counts.append({"app_name": app, "population_count": len(candidates), "sample_count": quota})
    return selected, counts


def build_report(root: Path) -> dict[str, Any]:
    raw = (root / BENCHMARK_PATH).read_bytes()
    benchmark = json.loads(raw)
    keys = [stable_key(row, benchmark=True) for row in benchmark["cases"]]
    selected, counts = sample_keys(keys)
    if len(keys) != 1284 or len(counts) != 15 or len(selected) != 257 or any(key[2] != 1 for key in keys):
        raise ValueError("Expected the Sub15 population of 1,284 tasks in 15 apps and a 257-task sample, all at repeat_idx=1")
    return {
        "schema_version": "flowark-manual-audit-sampling-v1",
        "input": {"path": BENCHMARK_PATH.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()},
        "sampling": {
            "seed": SEED,
            "stratification": "app_name",
            "population": "All Sub15 benchmark cases, each with repeat_idx=1.",
            "stable_key": ["app_name", "flow_id_or_source_id", "repeat_idx"],
            "benchmark_source_identifier": "source_id, falling back to case_id; identifiers are used verbatim.",
            "hash_input": "seed|20pct|app_name|flow_id_or_source_id|repeat_idx",
            "encoding": "UTF-8",
            "selection": "Within each app, order by ascending SHA-256 hexadecimal digest and take round_half_up(n * 0.20).",
            "hash_tie_break": "Ascending stable key.",
            "quota_integer_formula": "(2 * n + 5) // 10",
            "output_order": "Apps by descending population count then app_name; selected keys ascending within each app.",
        },
        "population_count": len(keys),
        "app_count": len(counts),
        "sample_count": len(selected),
        "per_app_counts": counts,
        "selected_keys": [
            {"app_name": app, "flow_id_or_source_id": source, "repeat_idx": repeat}
            for app, source, repeat in selected
        ],
    }


def verify_report(root: Path, report: dict[str, Any]) -> None:
    if json.loads((root / REPORT_PATH).read_text(encoding="utf-8")) != report:
        raise ValueError("The committed sampling manifest differs from the sample recomputed from the benchmark")


def verify_audit(report: dict[str, Any], audit_dir: Path) -> int:
    directory = audit_dir / "final_records"
    files = sorted(directory.glob("*.json"))
    if not files:
        raise ValueError(
            "Manual-audit final_records are missing. Prepare them with: "
            "uv run python scripts/fetch_artifact_data.py --manual-audit-logs "
            "(or select an extracted audit package with --audit-dir)."
        )
    actual: set[StableKey] = set()
    for path in files:
        row = json.loads(path.read_text(encoding="utf-8"))
        key = stable_key(row["case_key"])
        if key in actual:
            raise ValueError(f"Duplicate manual-audit stable key in {path.name}")
        actual.add(key)
    expected = {stable_key(row) for row in report["selected_keys"]}
    if actual != expected:
        raise ValueError(
            f"Manual-audit task set differs from the recomputed sample: "
            f"missing={len(expected - actual)}, extra={len(actual - expected)}; "
            f"expected={len(expected)}, found={len(actual)}"
        )
    return len(actual)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Verify the committed manifest and installed audit records (default).")
    mode.add_argument("--write", action="store_true", help="Write the sampling manifest after verifying the installed audit records.")
    parser.add_argument("--audit-dir", type=Path, help="Extracted manual-audit package containing final_records/.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        report = build_report(root)
        if not args.write:
            verify_report(root, report)
        audit_count = verify_audit(report, args.audit_dir or root / AUDIT_PATH)
        if args.write:
            (root / REPORT_PATH).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Manual-audit sampling verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "verification": "pass",
        "population_count": report["population_count"],
        "app_count": report["app_count"],
        "sample_count": report["sample_count"],
        "audit_record_count": audit_count,
        "missing": 0,
        "extra": 0,
        "manifest": REPORT_PATH.as_posix(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
