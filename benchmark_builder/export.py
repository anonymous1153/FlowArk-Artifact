"""Export inventory to legacy-compatible shard/benchmark JSON."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from benchmark_builder.io import now_tz8_iso, read_jsonl, slugify, write_json
from benchmark_builder.schemas import (
    BenchmarkCase,
    BenchmarkDraftCase,
    BenchmarkOutput,
    BenchmarkShardOutput,
    BenchmarkSource,
    ExportBuildConfig,
    ExportBuildResult,
    InventoryOccurrence,
)


def export_inventory(config: ExportBuildConfig) -> ExportBuildResult:
    inventory_root = config.inventory_root.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    inventory_path = inventory_root / "inventory.jsonl"
    rows = [InventoryOccurrence.model_validate(item) for item in read_jsonl(inventory_path)]
    enabled_kinds = {kind.value for kind in config.source_kinds}
    filtered = [row for row in rows if row.source_kind.value in enabled_kinds]

    by_app_and_kind: dict[str, dict[str, list[InventoryOccurrence]]] = defaultdict(lambda: defaultdict(list))
    by_app: dict[str, list[InventoryOccurrence]] = defaultdict(list)
    for row in filtered:
        by_app_and_kind[row.app_name][row.source_kind.value].append(row)
        by_app[row.app_name].append(row)

    app_names = sorted(by_app)
    for app_name in app_names:
        app_root = out_dir / "runs" / slugify(app_name)
        shard_dir = app_root / "shards"
        shard_dir.mkdir(parents=True, exist_ok=True)
        merged_cases: list[BenchmarkCase] = []
        for source_kind in sorted(by_app_and_kind[app_name]):
            ordered_rows = _sort_occurrences(by_app_and_kind[app_name][source_kind], config.sort_fields)
            selected = _select_rows(ordered_rows, config.max_cases_per_kind)
            shard = _build_shard_output(selected)
            write_json(shard_dir / f"{slugify(source_kind)}.json", shard.model_dump(mode="json"))
            merged_cases.extend(_to_benchmark_cases(selected, config))
        benchmark = BenchmarkOutput(cases=merged_cases, default_sink_categories=list(config.sink_categories))
        write_json(app_root / "benchmark.json", benchmark.model_dump(mode="json"))

    all_cases: list[BenchmarkCase] = []
    for app_name in app_names:
        ordered_rows = _sort_occurrences(by_app[app_name], config.sort_fields)
        by_kind_rows: dict[str, list[InventoryOccurrence]] = defaultdict(list)
        for row in ordered_rows:
            by_kind_rows[row.source_kind.value].append(row)
        for source_kind in sorted(by_kind_rows):
            all_cases.extend(_to_benchmark_cases(_select_rows(by_kind_rows[source_kind], config.max_cases_per_kind), config))

    benchmark_path = out_dir / "benchmark.json"
    manifest_path = out_dir / "manifest.json"
    summary_path = out_dir / "summary.json"
    write_json(
        benchmark_path,
        BenchmarkOutput(cases=all_cases, default_sink_categories=list(config.sink_categories)).model_dump(mode="json"),
    )
    write_json(
        manifest_path,
        {
            "schema_version": "benchmark-builder-v3.2-export-v1",
            "built_at": now_tz8_iso(),
            "inventory_root": str(inventory_root),
            "out_dir": str(out_dir),
            "benchmark_name": config.benchmark_name,
            "benchmark_family": config.benchmark_family,
            "source_kinds": [str(kind) for kind in config.source_kinds],
            "max_cases_per_kind": config.max_cases_per_kind,
            "app_count": len(app_names),
            "apps": app_names,
        },
    )
    write_json(summary_path, _build_summary(by_app_and_kind, config.max_cases_per_kind))
    return ExportBuildResult(
        manifest_path=manifest_path,
        benchmark_path=benchmark_path,
        summary_path=summary_path,
        app_names=app_names,
    )


def _sort_occurrences(rows: Iterable[InventoryOccurrence], sort_fields: list[str]) -> list[InventoryOccurrence]:
    def key(row: InventoryOccurrence) -> tuple[object, ...]:
        values: list[object] = []
        for field in sort_fields:
            value = getattr(row, field)
            if value is None:
                value = ""
            values.append(value)
        return tuple(values)

    return sorted(rows, key=key)


def _build_shard_output(rows: list[InventoryOccurrence]) -> BenchmarkShardOutput:
    return BenchmarkShardOutput(
        source_kind=rows[0].source_kind.value if rows else "ui_input",
        cases=[
            BenchmarkDraftCase(
                case_id=row.occurrence_id,
                source_id=row.occurrence_id,
                source_kind=row.source_kind,
                source_subtype=row.source_subtype,
                rule_id=row.rule_id,
                boundary_type=row.boundary_type,
                alignment_tier=row.alignment_tier,
                literature_basis=list(row.literature_basis),
                review_state=row.review_state,
                source=BenchmarkSource(
                    file_path=row.file_path,
                    line_number=row.line_number,
                    classname=row.classname,
                    method=row.method,
                    statement=row.statement,
                    description=row.description,
                ),
                notes=[],
            )
            for row in rows
        ],
        warnings=[],
        open_questions=[],
        review_summary="Static-scan export from a high-precision non-exhaustive inventory.",
    )


def _to_benchmark_cases(rows: list[InventoryOccurrence], config: ExportBuildConfig) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for row in rows:
        cases.append(
            BenchmarkCase(
                case_id=row.occurrence_id,
                source_id=row.occurrence_id,
                dataset=row.dataset,
                app_name=row.app_name,
                apk_name=row.apk_name,
                source_dir=row.source_dir,
                source_kind=row.source_kind,
                source_subtype=row.source_subtype,
                rule_id=row.rule_id,
                boundary_type=row.boundary_type,
                alignment_tier=row.alignment_tier,
                literature_basis=list(row.literature_basis),
                review_state=row.review_state,
                source=BenchmarkSource(
                    file_path=row.file_path,
                    line_number=row.line_number,
                    classname=row.classname,
                    method=row.method,
                    statement=row.statement,
                    description=row.description,
                ),
                benchmark_family=config.benchmark_family,
                target_sink_categories=list(config.sink_categories),
                notes=[],
            )
        )
    return cases


def _select_rows(rows: list[InventoryOccurrence], cap: int | None) -> list[InventoryOccurrence]:
    if cap is None:
        return rows
    return rows[:cap]


def _build_summary(by_app_and_kind: dict[str, dict[str, list[InventoryOccurrence]]], cap: int | None) -> dict[str, object]:
    by_source_kind: dict[str, int] = defaultdict(int)
    by_source_subtype: dict[str, int] = defaultdict(int)
    by_boundary_type: dict[str, int] = defaultdict(int)
    by_alignment_tier: dict[str, int] = defaultdict(int)
    by_rule: dict[str, int] = defaultdict(int)
    by_app: dict[str, dict[str, int]] = {}
    app_rules: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    app_subtypes: dict[str, set[str]] = defaultdict(set)
    for app_name, kind_rows in sorted(by_app_and_kind.items()):
        counts: dict[str, int] = {}
        for source_kind, rows in sorted(kind_rows.items()):
            selected = rows if cap is None else rows[:cap]
            count = len(selected)
            counts[source_kind] = count
            by_source_kind[source_kind] += count
            for row in selected:
                by_source_subtype[row.source_subtype] += 1
                by_boundary_type[row.boundary_type] += 1
                by_alignment_tier[row.alignment_tier] += 1
                by_rule[row.rule_id] += 1
                app_rules[app_name][row.rule_id] += 1
                app_subtypes[app_name].add(row.source_subtype)
        by_app[app_name] = counts
    app_rankings: list[dict[str, object]] = []
    for app_name, counts in by_app.items():
        total = sum(counts.values())
        top_rule, top_rule_count = _top_count(app_rules[app_name])
        app_rankings.append(
            {
                "app_name": app_name,
                "total_sources": total,
                "source_kind_count": sum(1 for value in counts.values() if value > 0),
                "source_subtype_count": len(app_subtypes[app_name]),
                "rule_count": len(app_rules[app_name]),
                "top_rule": top_rule,
                "top_rule_count": top_rule_count,
                "top_rule_share": round(top_rule_count / total, 6) if total else 0.0,
            }
        )
    return {
        "schema_version": "benchmark-builder-v3.2-export-summary-v1",
        "built_at": now_tz8_iso(),
        "max_cases_per_kind": cap,
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "by_kind": dict(sorted(by_source_kind.items())),
        "by_source_subtype": dict(sorted(by_source_subtype.items())),
        "by_boundary_type": dict(sorted(by_boundary_type.items())),
        "by_alignment_tier": dict(sorted(by_alignment_tier.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "by_app": by_app,
        "app_rankings": sorted(app_rankings, key=lambda row: (-int(row["total_sources"]), str(row["app_name"]))),
    }


def _top_count(counts: dict[str, int]) -> tuple[str | None, int]:
    if not counts:
        return None, 0
    key, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return key, count
