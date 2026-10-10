from __future__ import annotations

from collections import Counter
from copy import deepcopy
from fractions import Fraction
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sampling(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("analyze_strat15_sampling")


@pytest.fixture
def benchmarks():
    summary = json.loads((ROOT / "data/source-corpus-summary.json").read_text())
    return tuple(json.loads((ROOT / summary["workloads"][name]["benchmark"]).read_text())["cases"]
                 for name in ("Main49", "Strat15"))


def test_half_up_rounding_and_colliding_positions(sampling):
    assert sampling.round_position(50, 9) == 5
    assert sampling.round_position(60, 9) == 5
    assert [sampling.round_position(p, 9) for p in (30, 60, 90)] == [3, 5, 8]
    assert sampling.round_position(100, 9) == 9


def test_missing_subtype_contributes_to_distance(sampling):
    assert sampling.subtype_tvd(Counter(a=2), Counter(a=1, b=1)) == Fraction(1, 2)
    assert sampling.subtype_tvd(Counter(a=1), Counter(b=1)) == 1
    with pytest.raises(ValueError, match="nonempty"):
        sampling.subtype_tvd(Counter(), Counter(a=1))


def test_exact_ties_and_app_tie_order_are_deterministic(sampling):
    cases = [{"app_name": f"example.app{n:02d}_1", "case_id": str(n), "source_subtype": "same"}
             for n in reversed(range(10))]
    result = sampling.enumerate_schemes(cases)
    assert result["ranked_apps"] == sorted(case["app_name"] for case in cases)
    assert len(result["candidates"]) == 120
    assert all(row["rank"] == 1 and row["distance"] == 0 for row in result["candidates"])
    assert [row["positions_percent"] for row in result["candidates"]] == sorted(
        row["positions_percent"] for row in result["candidates"])
    assert result["candidates"][0]["positions_percent"] == (10, 20, 30)


def test_report_lists_all_tied_minima_and_selects_lexicographic_first(sampling):
    main = [{"app_name": f"example.app{n:02d}_1", "case_id": str(n), "source_subtype": "same"}
            for n in reversed(range(49))]
    chosen = {start + offset for start in (0, 10, 20, 30, 40) for offset in (0, 1, 2)}
    strat = [case for case in main if int(case["case_id"]) in chosen]
    report = sampling.analyze_cases(main, strat)
    assert len(report["minimum_positions_percent"]) == 112
    assert not report["unique_minimum"]
    assert report["selected"]["positions_percent"] == [10, 20, 30]
    assert report["selected"]["source_subtype_tvd_exact"] == "0"


def test_current_benchmarks_and_independent_integer_tvd(sampling, benchmarks):
    main, strat = benchmarks
    result = sampling.analyze_cases(main, strat)
    assert result["candidate_counts"] == {"total": 120, "valid": 112, "invalid": 8}
    assert result["minimum_positions_percent"] == [[30, 60, 90]]
    assert result["unique_minimum"]
    assert result["selected"]["matches_published_cases_and_order"]
    assert result["selected"]["source_occurrences"] == 1284
    assert result["selected"]["positions_by_band"] == [[3, 6, 9]] * 4 + [[3, 5, 8]]
    sample = Counter(case["source_subtype"] for case in strat)
    reference = Counter(case["source_subtype"] for case in main)
    independent = Fraction(sum(abs(sample[k] * len(main) - reference[k] * len(strat))
                               for k in reference), 2 * len(main) * len(strat))
    assert independent == Fraction(458279, 5969316)
    assert Fraction(result["selected"]["source_subtype_tvd_exact"]) == independent
    assert result["candidates"][1]["positions_percent"] == [30, 60, 80]
    assert Fraction(result["candidates"][1]["source_subtype_tvd_exact"]) == Fraction(247111, 2998605)
    assert all(not {50, 60} <= set(row["positions_percent"]) for row in result["candidates"])


@pytest.mark.parametrize("change", ["field", "order"])
def test_reference_requires_complete_cases_and_order(sampling, benchmarks, change):
    main, strat = benchmarks
    changed = deepcopy(strat)
    if change == "field":
        changed[0]["source"]["statement"] += " changed"
    else:
        changed[0], changed[1] = changed[1], changed[0]
    with pytest.raises(ValueError, match="exactly reproduce"):
        sampling.analyze_cases(main, changed)


def test_checked_in_report_is_current(sampling):
    assert sampling.verify_report(ROOT)["selected"]["source_subtype_tvd_exact"] == "458279/5969316"
