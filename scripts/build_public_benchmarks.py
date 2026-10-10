#!/usr/bin/env python3
"""Build public benchmark JSON files from sanitized paper evaluation roots."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from verify_eval_data import load_allowed_apps


DEFAULT_EVALS_DIR = Path("artifact-data/studio-state")
MAIN50_ROOT = "paper-main50-standard-opencode"
STRAT15_ROOT = "paper-strat15-glm-4-7-standard-opencode"
SINK_CATEGORIES = ["log", "network", "icc", "file", "database", "storage", "others"]
SOURCE_MANIFEST = Path(__file__).resolve().parents[1] / "data/source-archives-manifest.csv"


def retain_corpus_apps(
    cases: list[dict[str, Any]], allowed_apps: set[str],
) -> list[dict[str, Any]]:
    """Project historical inputs onto the published source corpus."""
    return [case for case in cases if case.get("app_name") in allowed_apps]


def select_strat15_by_relative_rank(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reproduce the fixed subset at rounded 30/60/90% positions of each band.

    This is a deterministic restatement of the historical selection, including
    its nine-app final band; no random draw is performed.
    """
    sizes = Counter(case["app_name"] for case in cases)
    if len(sizes) not in {49, 50}:
        raise ValueError("The fixed Strat15 rule expects a 49- or 50-app main workload")
    ranked_apps = sorted(sizes, key=lambda app: (-sizes[app], app))
    selected_apps: set[str] = set()
    for start in range(0, len(ranked_apps), 10):
        band = ranked_apps[start:start + 10]
        # One-based round-half-up of 0.3*m, 0.6*m, 0.9*m, using exact integers.
        positions = [(3 * multiplier * len(band) + 5) // 10 for multiplier in (1, 2, 3)]
        selected_apps.update(band[position - 1] for position in positions)
    # Preserve the benchmark's app/source execution order after selecting membership.
    return [case for case in cases if case["app_name"] in selected_apps]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_results(eval_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    results_path = eval_root / "results.jsonl"
    with results_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
    return sorted(rows, key=lambda item: int(item.get("task_index") or 0))


def _source_archive_name(case: dict[str, Any]) -> str:
    app_name = str(case.get("app_name") or "").strip()
    if not app_name:
        raise ValueError("case is missing app_name")
    return f"{app_name}_src.tar.gz"


def _public_case(case: dict[str, Any]) -> dict[str, Any]:
    public = dict(case)
    archive = _source_archive_name(public)
    public["source_archive"] = archive
    public["source_dir"] = f"${{FLOWARK_SOURCE_ROOT}}/{archive}"
    public.setdefault("benchmark_family", "source_first_mixed")
    public.setdefault("target_sink_categories", list(SINK_CATEGORIES))
    return public


def build_cases(eval_root: Path) -> list[dict[str, Any]]:
    rows = _iter_results(eval_root)
    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for row in rows:
        rel_path = ((row.get("paths") or {}).get("case_input") or "").strip()
        if not rel_path:
            continue
        case_path = eval_root / rel_path
        if not case_path.exists():
            raise FileNotFoundError(f"missing case_input: {case_path}")
        case = _load_json(case_path)
        case_id = str(case.get("case_id") or case.get("source_id") or "").strip()
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        cases.append(_public_case(case))
    return cases


def write_benchmark(
    path: Path, *, name: str, cases: list[dict[str, Any]],
    workload_revision: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "flowark-benchmark-v1",
        "name": name,
        "benchmark_family": "source_first_mixed",
        "default_sink_categories": list(SINK_CATEGORIES),
        "source_dir_template": "${FLOWARK_SOURCE_ROOT}/{source_archive}",
        "cases": cases,
    }
    if workload_revision is not None:
        payload["workload_revision"] = workload_revision
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main49_revision() -> dict[str, Any]:
    summary_path = SOURCE_MANIFEST.with_name("source-corpus-summary.json")
    corpus = _load_json(summary_path)
    main = corpus["workloads"]["Main49"]
    return {
        "id": corpus["revision"],
        "source_corpus_app_count": corpus["app_count"],
        "eligible_source_count": corpus["eligible_source_occurrences"],
        "app_count": 49,
        "source_count": 4649,
        "source_coverage_percent": main["source_coverage_percent"],
        "strat15_policy": "Preserve the frozen app membership and source order.",
        "strat15_reproduction": "Sort apps by (-source_count, app_id), form consecutive bands of at most 10, select one-based round-half-up positions at 30%, 60%, and 90% of each band's size.",
        "compatibility": "The main50 filename and preset key remain legacy aliases for Main49.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paper-evals-dir",
        type=Path,
        required=True,
        help="Directory that contains paper-* evaluation roots.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/benchmarks"),
        help="Output directory for public benchmark JSON templates.",
    )
    args = parser.parse_args()

    paper_evals_dir = args.paper_evals_dir.expanduser().resolve()
    allowed_apps = load_allowed_apps(SOURCE_MANIFEST)
    main49_cases = retain_corpus_apps(build_cases(paper_evals_dir / MAIN50_ROOT), allowed_apps)
    strat15_cases = select_strat15_by_relative_rank(main49_cases)
    frozen_strat15_cases = build_cases(paper_evals_dir / STRAT15_ROOT)
    if len(main49_cases) != 4649 or len({case["app_name"] for case in main49_cases}) != 49:
        raise ValueError("Main49 must contain 49 apps and 4,649 source occurrences")
    if strat15_cases != frozen_strat15_cases:
        raise ValueError("Relative-rank selection differs from the frozen Strat15 cases or order")

    write_benchmark(
        args.out_dir / "source-first-v3.2-main50.template.json",
        name="Main49",
        cases=main49_cases,
        workload_revision=main49_revision(),
    )
    write_benchmark(
        args.out_dir / "source-first-v3.2-strat15.template.json",
        name="Strat15",
        cases=strat15_cases,
    )
    print(f"wrote Main49 cases: {len(main49_cases)} (legacy main50 filename)")
    print(f"wrote Strat15 cases: {len(strat15_cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
