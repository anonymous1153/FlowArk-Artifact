"""运行时知识信号提取。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_ASSISTANT_TOOL_TURN_RE = re.compile(r"\bAssistantMessage\(.*\bToolUseBlock\(", re.DOTALL)
_MESSAGE_ID_RE = re.compile(r"\bmessage_id=(['\"])([^'\"]+)\1")
_REACT_TOOL_TURN_PREFIX = "__flowark_react_tool_turn_id__:"


def _read_text_safe(path: Path, *, max_size: int = 512_000) -> str | None:
    try:
        if path.stat().st_size > max_size:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _normalize_transcript_fragment(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _has_tool_use_block(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_tool_use_block(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("type") == "tool_use":
        return True
    return any(_has_tool_use_block(child) for child in value.values())


def _message_role(data: dict[str, Any], message: dict[str, Any]) -> str:
    role = str(message.get("role") or data.get("role") or "").strip().lower()
    if role:
        return role
    entry_type = str(data.get("type") or "").strip().lower()
    if entry_type in {"assistant", "user", "system"}:
        return entry_type
    message_type = str(message.get("type") or "").strip().lower()
    if message_type in {"assistant", "user", "system"}:
        return message_type
    return ""


def _extract_json_react_tool_turn_id(line: str) -> str | None:
    line = line.strip()
    if not line or not (line.startswith("{") and line.endswith("}")):
        return None
    try:
        data = json.loads(line)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    raw_message = data.get("message")
    message = raw_message if isinstance(raw_message, dict) else data
    if not isinstance(message, dict):
        return None
    if _message_role(data, message) != "assistant":
        return None

    content = message.get("content")
    if content is None:
        content = data.get("content")
    if not _has_tool_use_block(content):
        return None

    message_id = (
        message.get("id")
        or message.get("message_id")
        or data.get("message_id")
        or data.get("uuid")
        or data.get("id")
    )
    if message_id:
        return f"message:{message_id}"
    digest = hashlib.sha1(line.encode("utf-8", errors="ignore")).hexdigest()
    return f"hash:{digest}"


def _maybe_extract_text_from_json_line(line: str) -> list[str]:
    line = line.strip()
    if not line:
        return []
    if not (line.startswith("{") and line.endswith("}")):
        return []
    try:
        data = json.loads(line)
    except Exception:
        return []

    fragments: list[str] = []
    react_tool_turn_id = _extract_json_react_tool_turn_id(line)
    if react_tool_turn_id:
        fragments.append(f"{_REACT_TOOL_TURN_PREFIX}{react_tool_turn_id}")

    def walk(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text:
                fragments.append(text)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key in ("text", "content", "message", "tool_response", "result", "value"):
                if key in value:
                    walk(value.get(key))
            # 兜底：ToolResultBlock/AssistantMessage/UserMessage 结构可能嵌套在 blocks/items 中
            for key in ("blocks", "items"):
                if key in value:
                    walk(value.get(key))

    walk(data)
    return fragments


def read_transcript_delta(
    transcript_path: str | Path | None,
    *,
    start_offset: int = 0,
    max_bytes: int = 512_000,
) -> tuple[str, int, list[str]]:
    """读取 transcript 新增部分。

    返回:
    - delta_text: 新增文本（适合关键词扫描）
    - next_offset: 下次读取起始偏移
    - message_fingerprints: 新增消息片段指纹（用于去重）
    """
    if not transcript_path:
        return "", int(start_offset or 0), []

    path = Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return "", int(start_offset or 0), []

    try:
        file_size = path.stat().st_size
    except Exception:
        return "", int(start_offset or 0), []

    offset = max(0, int(start_offset or 0))
    if file_size < offset:
        offset = 0  # transcript rotated / recreated
    if file_size == offset:
        return "", file_size, []

    # 限制单次读取量，避免异常大 transcript 造成 hook 开销过大。
    read_start = max(offset, file_size - max_bytes)
    try:
        with path.open("rb") as fp:
            fp.seek(read_start)
            raw = fp.read(max_bytes)
    except Exception:
        return "", file_size, []

    text = raw.decode("utf-8", errors="ignore")
    if not text:
        return "", file_size, []

    fragments: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        extracted = _maybe_extract_text_from_json_line(line)
        if extracted:
            fragments.extend(extracted)
        else:
            fragments.append(line)

    normalized_fragments = [
        _normalize_transcript_fragment(item)
        for item in fragments
        if _normalize_transcript_fragment(item)
    ]
    fingerprints = [
        hashlib.sha1(item.encode("utf-8", errors="ignore")).hexdigest()
        for item in normalized_fragments
    ]
    delta_text = "\n".join(normalized_fragments)
    return delta_text, file_size, fingerprints


def extract_react_tool_turn_ids(text: str) -> list[str]:
    """Return stable IDs for assistant messages that start one or more tool uses."""
    turn_ids: list[str] = []
    seen: set[str] = set()

    def append_turn_id(turn_id: str) -> None:
        turn_text = str(turn_id or "").strip()
        if not turn_text or turn_text in seen:
            return
        seen.add(turn_text)
        turn_ids.append(turn_text)

    for line in str(text or "").splitlines():
        fragment = line.strip()
        if not fragment:
            continue
        if fragment.startswith(_REACT_TOOL_TURN_PREFIX):
            append_turn_id(fragment.removeprefix(_REACT_TOOL_TURN_PREFIX))
            continue
        json_turn_id = _extract_json_react_tool_turn_id(fragment)
        if json_turn_id:
            append_turn_id(json_turn_id)
            continue
        if not _ASSISTANT_TOOL_TURN_RE.search(fragment):
            continue
        message_match = _MESSAGE_ID_RE.search(fragment)
        if message_match:
            turn_id = f"message:{message_match.group(2)}"
        else:
            digest = hashlib.sha1(fragment.encode("utf-8", errors="ignore")).hexdigest()
            turn_id = f"hash:{digest}"
        append_turn_id(turn_id)
    return turn_ids


def stringify_tool_response(tool_response: Any, *, max_chars: int = 12000) -> str:
    """将 PostToolUse tool_response 转成可做关键词扫描的文本。"""
    try:
        if isinstance(tool_response, str):
            text = tool_response
        else:
            text = json.dumps(tool_response, ensure_ascii=False, default=str)
    except Exception:
        text = str(tool_response)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
