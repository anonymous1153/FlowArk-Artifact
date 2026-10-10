"""Local I/O helpers for benchmark_builder v2."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))


def now_tz8_iso() -> str:
    return datetime.now(TZ_UTC_PLUS_8).isoformat()


def timestamp_slug_tz8() -> str:
    return datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%dT%H%M%S")


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML 对象: {path}")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, items: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for item in items:
            fp.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fp:
        for raw in fp:
            text = raw.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


def slugify(text: str, max_len: int = 64) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text or "").strip())
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    if not value:
        value = "item"
    return value[:max_len]
