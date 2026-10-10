"""Read and verify immutable knowledge-content experiment inputs."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


class FixtureError(ValueError):
    """An experiment input is incomplete or inconsistent."""


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise FixtureError("knowledge availability timestamps require a timezone")
    return result


@lru_cache(maxsize=4096)
def parse_skill(text: str) -> tuple[dict[str, Any], str]:
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise FixtureError("knowledge document has no frontmatter")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or not metadata.get("id"):
        raise FixtureError("knowledge document has no identity")
    return metadata, parts[2].strip()


def write_frozen(path: Path, text: str) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise FixtureError("execution inputs must not use symbolic links")
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FixtureError(f"experiment input already exists with different contents: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def write_json(path: Path, value: Any) -> None:
    write_frozen(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
