from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowark_studio.common.models import TailCursor


def tail_text_delta(path: Path, cursor: TailCursor, *, max_bytes: int = 256_000) -> str:
    """Return appended text since last cursor offset (best effort UTF-8)."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ""

    size = int(stat.st_size)
    if size < cursor.offset:
        cursor.offset = 0
    if size <= cursor.offset:
        return ""

    to_read = min(max_bytes, size - cursor.offset)
    with path.open("rb") as fh:
        fh.seek(cursor.offset)
        data = fh.read(to_read)
        cursor.offset = fh.tell()

    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def tail_jsonl_objects(path: Path, cursor: TailCursor, *, max_bytes: int = 256_000) -> list[dict[str, Any]]:
    delta = tail_text_delta(path, cursor, max_bytes=max_bytes)
    if not delta:
        return []
    items: list[dict[str, Any]] = []
    for line in delta.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def find_new_subdirs(parent: Path, before_names: set[str]) -> list[Path]:
    try:
        candidates = [p for p in parent.iterdir() if p.is_dir() and p.name not in before_names]
    except FileNotFoundError:
        return []
    except Exception:
        return []
    candidates.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, p.name))
    return candidates


def safe_read_text(path: Path, *, max_bytes: int = 2_000_000) -> str:
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")
