"""知识正文格式与摘要工具。"""

from __future__ import annotations

import re

SUMMARY_PREFIX = "知识摘要："
_LEGACY_SUMMARY_PREFIX = "核心结论："
SUMMARY_PREFIXES = (SUMMARY_PREFIX, _LEGACY_SUMMARY_PREFIX)


def _clean_line(text: str) -> str:
    line = str(text or "").strip()
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"^[-*]\s+", "", line)
    return line.strip()


def _strip_summary_prefix(line: str) -> str:
    text = str(line or "").strip()
    for prefix in SUMMARY_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def extract_core_conclusion(content: str) -> str:
    for raw_line in str(content or "").splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        return _strip_summary_prefix(line)
    return ""


def extract_followup_summary(content: str, *, max_chars: int = 220) -> str:
    found_first = False
    pieces: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        if not found_first:
            found_first = True
            continue
        pieces.append(line)
        summary = " ".join(pieces).strip()
        if len(summary) >= max_chars:
            break
    summary = " ".join(pieces).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def ensure_core_conclusion(content: str, *, fallback: str | None = None) -> str:
    lines = str(content or "").splitlines()
    normalized_lines = [str(line).rstrip() for line in lines]

    first_non_empty_idx: int | None = None
    for idx, raw_line in enumerate(normalized_lines):
        if raw_line.strip():
            first_non_empty_idx = idx
            break

    if first_non_empty_idx is None:
        core = str(fallback or "").strip() or "请结合 entry_condition 使用本知识。"
        return f"{SUMMARY_PREFIX}{core}\n"

    first_line = _clean_line(normalized_lines[first_non_empty_idx])
    core = _strip_summary_prefix(first_line) or str(fallback or "").strip() or "请结合 entry_condition 使用本知识。"
    normalized_lines[first_non_empty_idx] = f"{SUMMARY_PREFIX}{core}"
    return "\n".join(normalized_lines).rstrip() + "\n"
