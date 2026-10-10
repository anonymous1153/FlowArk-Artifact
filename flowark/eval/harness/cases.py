"""Case loading and normalization for evaluation harness."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import (
    DEFAULT_SINK_CATEGORIES,
    _normalize_optional_positive_int,
    normalize_classification_filter,
)
from .models import EvalCase

BENCHMARK_SCHEMA_V1 = "flowark-benchmark-v1"
DEFAULT_BENCHMARK_FAMILY = "reuse_rich"

_QUERY_SINK_LABELS = {
    "log": "日志",
    "network": "网络",
    "icc": "组件间通信（startActivity/startService/sendBroadcast）",
    "file": "文件",
    "database": "数据库",
    "storage": "存储（Bundle/SharedPreferences）",
    "others": "其他（others）",
}


def _pick_nested(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_label_classification(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text in {"TRUE", "FALSE", "UNKNOWN", "MIXED"}:
        return text
    return text


def _normalize_source_classification(entries: list[dict[str, Any]]) -> str | None:
    classifications = {
        cls
        for entry in entries
        if isinstance(entry, dict)
        and (cls := _normalize_label_classification(entry.get("classification")))
    }
    if not classifications:
        return None
    if len(classifications) == 1:
        return next(iter(classifications))
    return "MIXED"


def _is_true_classification(value: Any) -> bool:
    return _normalize_label_classification(value) == "TRUE"


def _normalize_benchmark_family(value: Any, *, default: str = DEFAULT_BENCHMARK_FAMILY) -> str:
    text = str(value or "").strip().lower()
    return text or default


def _derive_positive_sink_categories(entries: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not _is_true_classification(entry.get("classification")):
            continue
        cat = str(entry.get("sink_category") or "").strip().lower()
        if not cat or cat in seen:
            continue
        seen.add(cat)
        out.append(cat)
    return out


def normalize_case_record(record: dict[str, Any]) -> EvalCase:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    sink = record.get("sink") if isinstance(record.get("sink"), dict) else {}
    if not isinstance(source, dict):
        source = {}
    if not isinstance(sink, dict):
        sink = {}

    flow_id = str(record.get("flow_id") or "").strip()
    if not flow_id:
        raise ValueError("样本缺少 flow_id")
    source_dir = str(record.get("source_dir") or "").strip()
    if not source_dir:
        raise ValueError(f"{flow_id}: 缺少 source_dir")

    source_statement = _pick_nested(
        source,
        "statementgeneric",
        "statement",
        "statementfull",
        "targetName",
    )
    sink_statement = _pick_nested(
        sink,
        "statementgeneric",
        "statement",
        "statementfull",
        "targetName",
    )

    gt_cat = map_sink_category_from_fields(
        sink_statement,
        _pick_nested(sink, "method", "methodName"),
        _pick_nested(sink, "classname", "className"),
    )
    sink_entry = {
        "flow_id": flow_id,
        "label_id": flow_id,
        "sink": sink,
        "classification": record.get("classification"),
        "sink_category": gt_cat,
    }
    classification = _normalize_label_classification(record.get("classification"))

    return EvalCase(
        flow_id=flow_id,
        dataset=str(record.get("dataset") or "unknown").strip() or "unknown",
        app_name=str(record.get("app_name") or "unknown").strip() or "unknown",
        apk_name=str(record.get("apk_name") or "unknown").strip() or "unknown",
        source_dir=source_dir,
        benchmark_family=DEFAULT_BENCHMARK_FAMILY,
        classification=classification,
        source_method=_pick_nested(source, "method", "methodName"),
        source_classname=_pick_nested(source, "classname", "className"),
        source_statement=source_statement,
        sink_method=_pick_nested(sink, "method", "methodName"),
        sink_classname=_pick_nested(sink, "classname", "className"),
        sink_statement=sink_statement,
        sink_entries=[sink_entry],
        ground_truth_sink_categories=([gt_cat] if gt_cat and _is_true_classification(classification) else []),
        raw=record,
    )


def normalize_aggregated_source_record(source_record: dict[str, Any]) -> EvalCase:
    source = source_record.get("source") if isinstance(source_record.get("source"), dict) else {}
    if not isinstance(source, dict):
        source = {}

    source_id = str(source_record.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("聚合样本缺少 source_id")
    source_dir = str(source_record.get("source_dir") or "").strip()
    if not source_dir:
        raise ValueError(f"{source_id}: 缺少 source_dir")

    sinks_raw = source_record.get("sinks")
    sinks: list[dict[str, Any]] = []
    if isinstance(sinks_raw, list):
        for item in sinks_raw:
            if isinstance(item, dict):
                sink_entry = dict(item)
                sink_entry["classification"] = _normalize_label_classification(item.get("classification"))
                sink_entry["sink_category"] = (
                    str(item.get("sink_category") or "").strip().lower()
                    or map_sink_category_from_sink_record(item)
                )
                sink_entry["label_id"] = (
                    str(item.get("label_id") or item.get("flow_id") or "").strip()
                    or None
                )
                sinks.append(sink_entry)

    source_statement = _pick_nested(
        source,
        "statementgeneric",
        "statement",
        "statementfull",
        "targetName",
    )
    classification = _normalize_source_classification(sinks)

    return EvalCase(
        flow_id=source_id,
        source_id=source_id,
        dataset=str(source_record.get("dataset") or "unknown").strip() or "unknown",
        app_name=str(source_record.get("app_name") or "unknown").strip() or "unknown",
        apk_name=str(source_record.get("apk_name") or "unknown").strip() or "unknown",
        source_dir=source_dir,
        benchmark_family=DEFAULT_BENCHMARK_FAMILY,
        classification=classification,
        source_method=_pick_nested(source, "method", "methodName"),
        source_classname=_pick_nested(source, "classname", "className"),
        source_statement=source_statement,
        sink_entries=sinks,
        ground_truth_sink_categories=_derive_positive_sink_categories(sinks),
        raw=source_record,
    )


def normalize_benchmark_case_record(
    case_record: dict[str, Any],
    *,
    benchmark_family: str,
    default_sink_categories: list[str],
) -> EvalCase:
    source = case_record.get("source") if isinstance(case_record.get("source"), dict) else {}
    if not isinstance(source, dict):
        source = {}

    case_id = str(case_record.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("benchmark case 缺少 case_id")
    source_id = str(case_record.get("source_id") or "").strip() or case_id
    source_dir = str(case_record.get("source_dir") or "").strip()
    if not source_dir:
        raise ValueError(f"{case_id}: 缺少 source_dir")

    target_sink_categories = _normalize_str_list(case_record.get("target_sink_categories")) or list(
        default_sink_categories
    )

    source_statement = _pick_nested(
        source,
        "statementgeneric",
        "statement",
        "statementfull",
        "targetName",
    )

    return EvalCase(
        flow_id=case_id,
        source_id=source_id,
        dataset=str(case_record.get("dataset") or "unknown").strip() or "unknown",
        app_name=str(case_record.get("app_name") or "unknown").strip() or "unknown",
        apk_name=str(case_record.get("apk_name") or "unknown").strip() or "unknown",
        source_dir=source_dir,
        benchmark_family=_normalize_benchmark_family(case_record.get("benchmark_family"), default=benchmark_family),
        target_sink_categories=target_sink_categories,
        classification=(
            str(case_record.get("classification") or "").strip().upper() or None
        ),
        source_method=_pick_nested(source, "method", "methodName"),
        source_classname=_pick_nested(source, "classname", "className"),
        source_statement=source_statement,
        sink_entries=[],
        ground_truth_sink_categories=[],
        raw=case_record,
    )


def _case_has_true_flow(case: EvalCase) -> bool:
    for item in (case.sink_entries or []):
        if not isinstance(item, dict):
            continue
        cls = str(item.get("classification") or "").strip().upper()
        if cls == "TRUE":
            return True
    return str(case.classification or "").strip().upper() == "TRUE"


def _count_apps(cases: list[EvalCase]) -> int:
    return len({(str(c.app_name or "").strip() or "unknown") for c in cases})


def select_cases(
    cases: list[EvalCase],
    *,
    max_apps: int | None = None,
    max_sources: int | None = None,
    app_names: list[str] | None = None,
    classification_filter: str = "all",
) -> list[EvalCase]:
    """Select cases using source/app granularity (preserving input order).

    Order of application:
    1) app_names exact filter (source-level, optional)
    2) classification filter (source-level)
    3) max_apps (first N distinct app_name in remaining input order)
    4) max_sources (global cap on selected sources)
    """
    selected = list(cases)
    app_name_filters = [str(v).strip() for v in (app_names or []) if str(v).strip()]
    app_name_filter_keys = {v.casefold() for v in app_name_filters}
    filter_mode = normalize_classification_filter(classification_filter)
    max_apps = _normalize_optional_positive_int(max_apps, field_name="max_apps")
    max_sources = _normalize_optional_positive_int(max_sources, field_name="max_sources")

    if app_name_filter_keys:
        selected = [
            case
            for case in selected
            if (str(case.app_name or "").strip() or "unknown").casefold() in app_name_filter_keys
        ]

    if filter_mode == "source_has_true_flow":
        selected = [case for case in selected if _case_has_true_flow(case)]

    if max_apps is not None:
        selected_app_names: set[str] = set()
        filtered: list[EvalCase] = []
        for case in selected:
            app_name = str(case.app_name or "").strip() or "unknown"
            if app_name not in selected_app_names and len(selected_app_names) >= max_apps:
                continue
            selected_app_names.add(app_name)
            filtered.append(case)
        selected = filtered

    if max_sources is not None:
        selected = selected[:max_sources]

    return selected


def load_cases(
    input_path: Path,
    *,
    max_apps: int | None = None,
    max_sources: int | None = None,
    app_names: list[str] | None = None,
    classification_filter: str = "all",
) -> list[EvalCase]:
    path = Path(input_path).expanduser().resolve()
    cases: list[EvalCase] = []
    for file_path in resolve_eval_input_paths(path):
        cases.extend(_load_cases_from_file(file_path))

    return select_cases(
        cases,
        max_apps=max_apps,
        max_sources=max_sources,
        app_names=app_names,
        classification_filter=classification_filter,
    )


def resolve_eval_input_paths(input_path: Path) -> list[Path]:
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"评估输入不存在: {path}")
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"评估输入既不是文件也不是目录: {path}")

    files = sorted(
        (
            p.resolve()
            for p in path.rglob("*")
            if p.is_file() and p.suffix.lower() in {".json", ".jsonl"}
        ),
        key=lambda p: str(p.relative_to(path)),
    )
    if not files:
        raise ValueError(f"目录中未找到任何 JSON/JSONL 文件: {path}")
    return files


def _load_cases_from_file(path: Path) -> list[EvalCase]:
    text = path.read_text(encoding="utf-8")
    items: list[dict[str, Any]] = []
    aggregated_sources: list[dict[str, Any]] | None = None
    benchmark_cases: list[dict[str, Any]] | None = None
    benchmark_family = DEFAULT_BENCHMARK_FAMILY
    default_sink_categories: list[str] = []

    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
        if (
            isinstance(data, dict)
            and str(data.get("schema_version") or "").strip() == BENCHMARK_SCHEMA_V1
            and isinstance(data.get("cases"), list)
        ):
            benchmark_cases = [item for item in data.get("cases", []) if isinstance(item, dict)]
            benchmark_family = _normalize_benchmark_family(data.get("benchmark_family"))
            default_sink_categories = _normalize_str_list(data.get("default_sink_categories"))
        elif isinstance(data, dict) and isinstance(data.get("sources"), list):
            aggregated_sources = [item for item in data.get("sources", []) if isinstance(item, dict)]
        elif isinstance(data, dict):
            items.append(data)
        else:
            raise ValueError("JSON 顶层对象格式不支持")
    elif stripped.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON 顶层必须是数组")
        for item in data:
            if isinstance(item, dict):
                items.append(item)
    else:
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                items.append(obj)

    cases: list[EvalCase] = []
    if benchmark_cases is not None:
        for case_item in benchmark_cases:
            try:
                cases.append(
                    normalize_benchmark_case_record(
                        case_item,
                        benchmark_family=benchmark_family,
                        default_sink_categories=default_sink_categories,
                    )
                )
            except Exception:
                continue
    elif aggregated_sources is not None:
        for source_item in aggregated_sources:
            try:
                cases.append(normalize_aggregated_source_record(source_item))
            except Exception:
                continue
    else:
        for item in items:
            try:
                cases.append(normalize_case_record(item))
            except Exception:
                continue
    return cases


def effective_case_sink_categories(
    case: EvalCase,
    *,
    fallback: list[str] | None = None,
) -> list[str]:
    categories = _normalize_str_list(case.target_sink_categories)
    if categories:
        return categories
    if fallback:
        return _normalize_str_list(fallback) or list(DEFAULT_SINK_CATEGORIES)
    return list(DEFAULT_SINK_CATEGORIES)


def build_eval_query(case: EvalCase) -> str:
    sink_categories = _normalize_str_list(case.target_sink_categories)
    if not sink_categories:
        return (
            "请从给定 source 起点出发，分析数据流是否会流向以下 sink 类型："
            "日志、网络、组件间通信（startActivity/startService/sendBroadcast）、"
            "文件、数据库、存储（Bundle/SharedPreferences）。"
        )

    sink_desc = "、".join(
        [_QUERY_SINK_LABELS.get(category, category) for category in sink_categories]
    )
    return (
        "请从给定 source 起点出发，分析数据流是否会流向以下 sink 类型："
        f"{sink_desc}。"
    )


def build_source_description(case: EvalCase) -> str:
    parts: list[str] = []
    if case.source_method:
        parts.append(f"source方法: {case.source_method}")
    if case.source_classname:
        parts.append(f"source类: {case.source_classname}")
    if case.source_statement:
        parts.append(f"source语句/调用: {case.source_statement}")
    if not parts:
        parts.append(f"flow_id: {case.flow_id}")
    return "；".join(parts)


def _sink_text_from_fields(
    sink_statement: str | None,
    sink_method: str | None,
    sink_classname: str | None,
) -> str:
    values = [
        sink_statement or "",
        sink_method or "",
        sink_classname or "",
    ]
    return " ".join(values).lower()


def map_sink_category_from_fields(
    sink_statement: str | None,
    sink_method: str | None,
    sink_classname: str | None,
) -> str | None:
    text = _sink_text_from_fields(sink_statement, sink_method, sink_classname)
    if not text.strip():
        return None
    if "android.util.log" in text or re.search(r"\blog\.[a-z]+\b", text):
        return "log"
    if any(token in text for token in ["startactivity", "startservice", "sendbroadcast"]):
        return "icc"
    if any(token in text for token in ["sharedpreferences", "bundle", "putextra", "getextras"]):
        return "storage"
    if any(
        token in text
        for token in [
            "java.io.",
            "outputstreamwriter",
            "fileoutputstream",
            "inputstream",
            "writer.write",
            " write(",
        ]
    ):
        return "file"
    if any(
        token in text
        for token in [
            "sqlite",
            "android.database",
            "room",
            "dao",
            "@query",
        ]
    ):
        return "database"
    if any(
        token in text
        for token in [
            "urlconnection",
            "http",
            "okhttp",
            "retrofit",
            "socket",
        ]
    ):
        return "network"
    return None


def map_sink_category_from_sink_record(sink_record: dict[str, Any]) -> str | None:
    explicit = str(
        sink_record.get("sink_category")
        or sink_record.get("category")
        or sink_record.get("sink_type")
        or ""
    ).strip().lower()
    if explicit:
        return explicit
    sink = sink_record.get("sink") if isinstance(sink_record.get("sink"), dict) else sink_record
    if not isinstance(sink, dict):
        return None
    sink_statement = _pick_nested(
        sink,
        "statementgeneric",
        "statement",
        "statementfull",
        "targetName",
    )
    sink_method = _pick_nested(sink, "method", "methodName")
    sink_classname = _pick_nested(sink, "classname", "className")
    return map_sink_category_from_fields(sink_statement, sink_method, sink_classname)
