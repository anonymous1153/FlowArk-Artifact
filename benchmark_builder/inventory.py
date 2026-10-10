"""Inventory build pipeline for benchmark_builder v2."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from benchmark_builder.io import now_tz8_iso, write_json, write_jsonl
from benchmark_builder.manifest import build_manifest_index
from benchmark_builder.rule_metadata import metadata_for_rule
from benchmark_builder.rules import RuleCandidate, scan_source_file
from benchmark_builder.schemas import (
    InventoryBuildConfig,
    InventoryBuildResult,
    InventoryOccurrence,
    SourceKind,
    build_occurrence_id,
    normalize_statement,
)

_EARLIEST_BOUNDARY_KINDS = {
    SourceKind.REMOTE_PAYLOAD,
    SourceKind.PERSISTENT_STORAGE,
    SourceKind.PLATFORM_API,
}
_ALWAYS_EXCLUDED_DIRS_LOWER = {
    ".flutter",
    "androidhosttest",
    "androidinstrumentedtest",
    "androidtest",
    "androidunittest",
    "build",
    "build-logic",
    "build-plugin",
    "buildsrc",
    "codegen",
    "commontest",
    "dbtest",
    "debug",
    "decompiled",
    "deplibs",
    "daogenerator",
    "generated",
    "gbdaogenerator",
    "submodule",
    "submodules",
    "test",
    "testplay",
    "tests",
    "testing",
    "third_party",
    "vendor",
}


def build_inventory(config: InventoryBuildConfig) -> InventoryBuildResult:
    source_root = config.source_root.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    app_dirs = _collect_app_dirs(source_root, config.app_names, config.app_limit)
    occurrences: list[InventoryOccurrence] = []
    for app_dir in app_dirs:
        occurrences.extend(_scan_app(config, app_dir))

    manifest_path = out_dir / "manifest.json"
    inventory_path = out_dir / "inventory.jsonl"
    summary_path = out_dir / "summary.json"

    write_json(
        manifest_path,
        {
            "schema_version": "benchmark-builder-v3.2-inventory-v1",
            "built_at": now_tz8_iso(),
            "source_root": str(source_root),
            "out_dir": str(out_dir),
            "dataset": config.dataset,
            "source_kinds": [str(kind) for kind in config.source_kinds],
            "app_count": len(app_dirs),
            "apps": [path.name for path in app_dirs],
            "excluded_dirs": list(config.excluded_dirs),
            "include_extensions": list(config.include_extensions),
            "inventory_quality_tier": "high_precision_non_exhaustive",
        },
    )
    write_jsonl(inventory_path, (row.model_dump(mode="json") for row in occurrences))
    write_json(summary_path, _build_summary(occurrences))
    return InventoryBuildResult(
        manifest_path=manifest_path,
        inventory_path=inventory_path,
        summary_path=summary_path,
        app_names=[path.name for path in app_dirs],
    )


def _collect_app_dirs(source_root: Path, app_names: list[str], app_limit: int | None) -> list[Path]:
    if app_names:
        app_dirs = [(source_root / name).resolve() for name in app_names]
    else:
        app_dirs = sorted([path.resolve() for path in source_root.iterdir() if path.is_dir()], key=lambda path: path.name)
    missing = [path for path in app_dirs if not path.exists() or not path.is_dir()]
    if missing:
        raise ValueError(f"app 目录不存在: {missing[0]}")
    if app_limit is not None:
        app_dirs = app_dirs[:app_limit]
    return app_dirs


def _scan_app(config: InventoryBuildConfig, app_dir: Path) -> list[InventoryOccurrence]:
    enabled_kinds = set(config.source_kinds)
    excluded_dirs = set(config.excluded_dirs)
    manifest_index = build_manifest_index(app_dir, excluded_dirs) if SourceKind.ICC_PAYLOAD in enabled_kinds else None
    raw_candidates: list[RuleCandidate] = []
    for file_path in _iter_source_files(app_dir, config.include_extensions, excluded_dirs):
        raw_candidates.extend(scan_source_file(app_dir, file_path, enabled_kinds, manifest_index=manifest_index))
    normalized_candidates = _apply_earliest_boundary_tie_break(raw_candidates)
    return _materialize_occurrences(config, app_dir, normalized_candidates)


def _iter_source_files(source_dir: Path, include_extensions: list[str], excluded_dirs: set[str]) -> Iterable[Path]:
    allowed = {ext.lower() for ext in include_extensions}
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed:
            continue
        if _is_excluded_relative_path(path.relative_to(source_dir), excluded_dirs):
            continue
        yield path


def _is_excluded_relative_path(relative_path: Path, excluded_dirs: set[str]) -> bool:
    parts = relative_path.parts[:-1]
    excluded_lower = _ALWAYS_EXCLUDED_DIRS_LOWER | {excluded.lower() for excluded in excluded_dirs}
    for index, part in enumerate(parts):
        lowered = part.lower()
        previous = parts[index - 1].lower() if index > 0 else ""
        if lowered in excluded_lower:
            return True
        if previous == "src" and (lowered.startswith("test") or lowered.startswith("androidtest")):
            return True
        if previous == "src" and lowered in {
            "androidhosttest",
            "androidinstrumentedtest",
            "androidunittest",
            "commontest",
            "dbtest",
            "debug",
            "demo",
        }:
            return True
        if previous == "jni" and lowered == "deplibs":
            return True
    return False


def _apply_earliest_boundary_tie_break(candidates: list[RuleCandidate]) -> list[RuleCandidate]:
    grouped: dict[tuple[str, str, str, str, str], list[RuleCandidate]] = defaultdict(list)
    passthrough: list[RuleCandidate] = []
    for candidate in candidates:
        if candidate.source_kind not in _EARLIEST_BOUNDARY_KINDS or not candidate.method or not candidate.boundary_subject:
            passthrough.append(candidate)
            continue
        key = (
            candidate.source_kind.value,
            candidate.file_path,
            candidate.classname or "",
            candidate.method,
            candidate.boundary_subject.lower(),
        )
        grouped[key].append(candidate)

    result: list[RuleCandidate] = list(passthrough)
    for rows in grouped.values():
        rows_sorted = sorted(rows, key=lambda row: (row.boundary_rank, row.line_number, row.rule_id))
        result.append(rows_sorted[0])
    return sorted(result, key=lambda row: (row.file_path, row.line_number, row.rule_id))


def _materialize_occurrences(
    config: InventoryBuildConfig,
    app_dir: Path,
    candidates: list[RuleCandidate],
) -> list[InventoryOccurrence]:
    app_name = app_dir.name
    apk_name = f"{app_name}.apk"
    rows: list[InventoryOccurrence] = []
    seen: set[tuple[str, str, str, str, int, str]] = set()
    for candidate in candidates:
        statement = normalize_statement(candidate.statement)
        dedup_key = (
            app_name,
            candidate.source_kind.value,
            candidate.rule_id,
            candidate.file_path,
            candidate.line_number,
            statement.lower(),
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        metadata = metadata_for_rule(candidate.rule_id)
        if metadata.source_kind != candidate.source_kind:
            raise ValueError(
                f"rule metadata kind mismatch: {candidate.rule_id} emitted "
                f"{candidate.source_kind.value}, registry has {metadata.source_kind.value}"
            )
        occurrence_id = build_occurrence_id(
            app_name=app_name,
            source_kind=candidate.source_kind,
            source_subtype=metadata.source_subtype,
            rule_id=candidate.rule_id,
            file_path=candidate.file_path,
            line_number=candidate.line_number,
            statement=statement,
            classname=candidate.classname,
            method=candidate.method,
        )
        rows.append(
            InventoryOccurrence.model_validate(
                {
                    "app_name": app_name,
                    "apk_name": apk_name,
                    "dataset": config.dataset,
                    "source_dir": str(app_dir),
                    "source_kind": candidate.source_kind,
                    "source_subtype": metadata.source_subtype,
                    "rule_id": candidate.rule_id,
                    "boundary_type": metadata.boundary_type,
                    "alignment_tier": metadata.alignment_tier,
                    "literature_basis": list(metadata.literature_basis),
                    "occurrence_id": occurrence_id,
                    "file_path": candidate.file_path,
                    "line_number": candidate.line_number,
                    "classname": candidate.classname,
                    "method": candidate.method,
                    "statement": statement,
                    "description": candidate.description,
                    "review_state": "auto",
                }
            )
        )
    return rows


def _build_summary(rows: list[InventoryOccurrence]) -> dict[str, object]:
    by_source_kind: dict[str, int] = defaultdict(int)
    by_source_subtype: dict[str, int] = defaultdict(int)
    by_boundary_type: dict[str, int] = defaultdict(int)
    by_alignment_tier: dict[str, int] = defaultdict(int)
    by_rule: dict[str, int] = defaultdict(int)
    by_app: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    app_rules: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    app_subtypes: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        kind = row.source_kind.value
        by_source_kind[kind] += 1
        by_source_subtype[row.source_subtype] += 1
        by_boundary_type[row.boundary_type] += 1
        by_alignment_tier[row.alignment_tier] += 1
        by_rule[row.rule_id] += 1
        by_app[row.app_name][kind] += 1
        app_rules[row.app_name][row.rule_id] += 1
        app_subtypes[row.app_name].add(row.source_subtype)
    app_rankings: list[dict[str, object]] = []
    for app_name, kind_counts in by_app.items():
        total = sum(kind_counts.values())
        top_rule, top_rule_count = _top_count(app_rules[app_name])
        app_rankings.append(
            {
                "app_name": app_name,
                "total_sources": total,
                "source_kind_count": len(kind_counts),
                "source_subtype_count": len(app_subtypes[app_name]),
                "rule_count": len(app_rules[app_name]),
                "top_rule": top_rule,
                "top_rule_count": top_rule_count,
                "top_rule_share": round(top_rule_count / total, 6) if total else 0.0,
            }
        )
    return {
        "schema_version": "benchmark-builder-v3.2-inventory-summary-v1",
        "built_at": now_tz8_iso(),
        "inventory_quality_tier": "high_precision_non_exhaustive",
        "total_occurrences": len(rows),
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "by_kind": dict(sorted(by_source_kind.items())),
        "by_source_subtype": dict(sorted(by_source_subtype.items())),
        "by_boundary_type": dict(sorted(by_boundary_type.items())),
        "by_alignment_tier": dict(sorted(by_alignment_tier.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "by_app": {app: dict(sorted(counts.items())) for app, counts in sorted(by_app.items())},
        "app_rankings": sorted(app_rankings, key=lambda row: (-int(row["total_sources"]), str(row["app_name"]))),
    }


def _top_count(counts: dict[str, int]) -> tuple[str | None, int]:
    if not counts:
        return None, 0
    key, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return key, count
