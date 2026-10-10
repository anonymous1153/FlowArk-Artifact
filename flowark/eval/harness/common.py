"""Shared helpers for the evaluation harness."""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from flowark.timeutil import now_tz8_iso, timestamp_slug_tz8

DEFAULT_SINK_CATEGORIES = ["log", "network", "icc", "file", "database", "storage"]
DEFAULT_TASK_TIMEOUT_SECONDS = 15 * 60
_RESULT_STATUS_COMPLETED = {"success", "warning", "error", "timeout", "cancelled", "skipped", "harness_error"}
_RESULT_STATUS_RESUMABLE = {"force_paused"}


def _slugify(text: str, max_len: int = 64) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    if not s:
        s = "item"
    return s[:max_len]


def _now_utc_iso() -> str:
    # Kept for backward compatibility in local call sites; now uses UTC+8.
    return now_tz8_iso()


def _timestamp_slug() -> str:
    return timestamp_slug_tz8()


def _json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _extract_first_json_object(text: str) -> str | None:
    s = str(text or "")
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(s)):
        ch = s[idx]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : idx + 1]
    return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _normalize_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 不能是布尔值")
    ivalue = int(value)
    if ivalue <= 0:
        raise ValueError(f"{field_name} 必须为正整数")
    return ivalue


def normalize_classification_filter(value: Any) -> str:
    raw = str(value or "all").strip().lower()
    aliases = {
        "none": "all",
        "true": "source_has_true_flow",
        "false": "all",
        "source_true_only": "source_has_true_flow",
        "true_source_only": "source_has_true_flow",
        "has_true_flow": "source_has_true_flow",
        "only_true_flow_sources": "source_has_true_flow",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in {"all", "source_has_true_flow"}:
        raise ValueError(f"不支持的 classification_filter: {value}")
    return normalized
