#!/usr/bin/env python3
"""Enumerate the final Main49 relative-position grid and verify Strat15."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import hashlib
from itertools import combinations
import json
from pathlib import Path
from typing import Any


GRID_PERCENT = tuple(range(10, 101, 10))
REPORT_PATH = Path("data/strat15-sampling-analysis.json")


def round_position(percent: int, band_size: int) -> int:
    """One-based round-half-up, computed without floating-point rounding."""
    if not 0 < percent <= 100 or band_size <= 0:
        raise ValueError("Percent must be in (0, 100] and band size must be positive")
    return (percent * band_size + 50) // 100


def subtype_tvd(sample: Counter, reference: Counter) -> Fraction:
    """Occurrence-weighted TVD; absent subtypes have count zero."""
    sample_total, reference_total = sum(sample.values()), sum(reference.values())
    if sample_total <= 0 or reference_total <= 0:
        raise ValueError("TVD requires two nonempty source distributions")
    return sum(
        (abs(Fraction(sample[key], sample_total) - Fraction(reference[key], reference_total))
         for key in sorted(sample.keys() | reference.keys())),
        Fraction(0),
    ) / 2


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    if not cases:
        raise ValueError("Benchmark cases must not be empty")
    for case in cases:
        if any(not isinstance(case.get(key), str) or not case[key]
               for key in ("app_name", "case_id", "source_subtype")):
            raise ValueError("Cases require app_name, case_id, and source_subtype")
    if len({(case["app_name"], case["case_id"]) for case in cases}) != len(cases):
        raise ValueError("Benchmark case identities must be unique")


def enumerate_schemes(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare every increasing triple on the fixed 10%-step grid."""
    _validate_cases(cases)
    profiles: dict[str, Counter] = defaultdict(Counter)
    for case in cases:
        profiles[case["app_name"]][case["source_subtype"]] += 1
    sizes = {app: sum(profile.values()) for app, profile in profiles.items()}
    ranked = sorted(profiles, key=lambda app: (-sizes[app], app))
    bands = [ranked[start:start + 10] for start in range(0, len(ranked), 10)]
    reference = Counter(case["source_subtype"] for case in cases)
    candidates = []
    invalid = 0
    for triple in combinations(GRID_PERCENT, 3):
        positions = [[round_position(percent, len(band)) for percent in triple] for band in bands]
        if any(len(set(ps)) != 3 or not all(1 <= p <= len(band) for p in ps)
               for band, ps in zip(bands, positions)):
            invalid += 1
            continue
        selected = [band[p - 1] for band, ps in zip(bands, positions) for p in ps]
        sample: Counter = Counter()
        for app in selected:
            sample.update(profiles[app])
        candidates.append({
            "positions_percent": triple,
            "positions_by_band": positions,
            "selected_app_ids": selected,
            "source_occurrences": sum(sample.values()),
            "source_subtypes": len(sample),
            "distance": subtype_tvd(sample, reference),
        })
    if not candidates:
        raise ValueError("No valid three-position schemes for this frame")
    # Exact TVD first, then a deterministic tie-break independent of Strat15.
    candidates.sort(key=lambda row: (row["distance"], row["positions_percent"]))
    ranks: dict[Fraction, int] = {}
    for index, row in enumerate(candidates, 1):
        ranks.setdefault(row["distance"], index)
        row["rank"] = ranks[row["distance"]]
    return {
        "ranked_apps": ranked,
        "app_source_counts": sizes,
        "band_sizes": list(map(len, bands)),
        "reference_counts": dict(sorted(reference.items())),
        "candidates": candidates,
        "invalid_candidate_count": invalid,
    }


def _public_scheme(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "positions_percent": list(row["positions_percent"]),
        "positions_by_band": row["positions_by_band"],
        "app_count": len(row["selected_app_ids"]),
        "source_occurrences": row["source_occurrences"],
        "source_subtypes": row["source_subtypes"],
        "source_subtype_tvd_exact": str(row["distance"]),
        "source_subtype_tvd": float(row["distance"]),
        "rank": row["rank"],
    }


def analyze_cases(main: list[dict[str, Any]], strat: list[dict[str, Any]]) -> dict[str, Any]:
    _validate_cases(strat)
    result = enumerate_schemes(main)
    if result["band_sizes"] != [10, 10, 10, 10, 9]:
        raise ValueError("The final Main49 frame must have bands of 10/10/10/10/9 apps")
    best = result["candidates"][0]
    selected_apps = set(best["selected_app_ids"])
    reproduced = [case for case in main if case["app_name"] in selected_apps]
    if reproduced != strat:
        raise ValueError("The minimum-TVD scheme does not exactly reproduce Strat15 cases and order")
    selected = _public_scheme(best)
    selected.update({
        "app_ids": best["selected_app_ids"],
        "global_ranks": [index for index, app in enumerate(result["ranked_apps"], 1)
                         if app in selected_apps],
        "subtype_counts": dict(sorted(Counter(case["source_subtype"] for case in strat).items())),
        "matches_published_cases_and_order": True,
    })
    minima = [list(row["positions_percent"]) for row in result["candidates"] if row["rank"] == 1]
    return {
        "schema_version": "flowark-strat15-sampling-v1",
        "frame": {
            "name": "Main49", "app_count": len(result["ranked_apps"]),
            "source_occurrences": len(main), "band_sizes": result["band_sizes"],
            "source_subtype_counts": result["reference_counts"],
        },
        "method": {
            "app_order": "Descending source count; ties by ascending app_name.",
            "candidate_grid_percent": list(GRID_PERCENT),
            "candidate_rule": "Use the same strictly increasing triple in every band.",
            "rounding": "One-based round-half-up: floor(percent * band_size / 100 + 0.5).",
            "validity": "Require three distinct in-range positions in every band.",
            "distance": "Occurrence-weighted source_subtype TVD; absent subtype counts are zero.",
            "distance_comparison": "Exact rational arithmetic, with no floating-point tolerance.",
            "tvd_ties": "Report all exact minima; select the lexicographically smallest percentage triple.",
            "rank_ties": "Equal exact TVD values share the rank 1 + number of strictly smaller values.",
            "execution_order": "Retain the original Main49 case order after selecting app membership.",
        },
        "candidate_counts": {
            "total": len(result["candidates"]) + result["invalid_candidate_count"],
            "valid": len(result["candidates"]), "invalid": result["invalid_candidate_count"],
        },
        "minimum_positions_percent": minima,
        "unique_minimum": len(minima) == 1,
        "selected": selected,
        "app_ranking": [{"rank": index, "app_name": app,
                         "source_occurrences": result["app_source_counts"][app]}
                        for index, app in enumerate(result["ranked_apps"], 1)],
        "candidates": [_public_scheme(row) for row in result["candidates"]],
    }


def build_report(root: Path) -> dict[str, Any]:
    corpus = json.loads((root / "data/source-corpus-summary.json").read_text())
    inputs = {}
    cases = {}
    for name in ("Main49", "Strat15"):
        relative = corpus["workloads"][name]["benchmark"]
        raw = (root / relative).read_bytes()
        data = json.loads(raw)
        if data["name"] != name:
            raise ValueError(f"Benchmark name does not match {name}")
        cases[name] = data["cases"]
        inputs[name] = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}
    report = analyze_cases(cases["Main49"], cases["Strat15"])
    return {"inputs": inputs, **report}


def verify_report(root: Path) -> dict[str, Any]:
    report = build_report(root)
    stored = json.loads((root / REPORT_PATH).read_text())
    if report != stored:
        raise ValueError("Stored sampling analysis differs from the current benchmark computation")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Write the reproducible analysis JSON.")
    mode.add_argument("--check", action="store_true", help="Verify the checked-in analysis JSON.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = verify_report(root) if args.check else build_report(root)
    if args.write:
        (root / REPORT_PATH).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "frame": report["frame"]["name"],
        "candidate_counts": report["candidate_counts"],
        "minimum_positions_percent": report["minimum_positions_percent"],
        "unique_minimum": report["unique_minimum"],
        "selected_source_occurrences": report["selected"]["source_occurrences"],
        "source_subtype_tvd_exact": report["selected"]["source_subtype_tvd_exact"],
        "source_subtype_tvd": report["selected"]["source_subtype_tvd"],
        "matches_published_cases_and_order": report["selected"]["matches_published_cases_and_order"],
        "analysis_file": str(REPORT_PATH),
    }, indent=2))


if __name__ == "__main__":
    main()
