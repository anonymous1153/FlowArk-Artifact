#!/usr/bin/env python3
"""Build public benchmark JSON files from sanitized paper evaluation roots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_EVALS_DIR = Path("artifact-data/studio-state")
MAIN50_ROOT = "paper-main50-standard-opencode"
STRAT15_ROOT = "paper-strat15-glm-4-7-standard-opencode"
SINK_CATEGORIES = ["log", "network", "icc", "file", "database", "storage", "others"]


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


def write_benchmark(path: Path, *, name: str, cases: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "flowark-benchmark-v1",
        "name": name,
        "benchmark_family": "source_first_mixed",
        "default_sink_categories": list(SINK_CATEGORIES),
        "source_dir_template": "${FLOWARK_SOURCE_ROOT}/{source_archive}",
        "cases": cases,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    main50_cases = build_cases(paper_evals_dir / MAIN50_ROOT)
    strat15_cases = build_cases(paper_evals_dir / STRAT15_ROOT)

    write_benchmark(
        args.out_dir / "source-first-v3.2-main50.template.json",
        name="Main50",
        cases=main50_cases,
    )
    write_benchmark(
        args.out_dir / "source-first-v3.2-strat15.template.json",
        name="Strat15",
        cases=strat15_cases,
    )
    print(f"wrote Main50 cases: {len(main50_cases)}")
    print(f"wrote Strat15 cases: {len(strat15_cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
