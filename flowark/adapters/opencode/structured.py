"""Structured output helpers for OpenCode post phases."""

from __future__ import annotations

import json
import re
from typing import Any

from flowark.semantics.models import Phase


STRUCTURED_PHASES = {
    Phase.FINAL_REPORT,
    Phase.KNOWLEDGE_SYNTH,
    Phase.KNOWLEDGE_RULE_REPAIR,
}


def structured_format_for_phase(phase: Phase) -> dict[str, Any]:
    """Return a permissive JSON object schema for OpenCode StructuredOutput."""

    if phase not in STRUCTURED_PHASES:
        raise ValueError(f"OpenCode structured output is not configured for phase {phase.value!r}")
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "schema_version": {"type": "string"},
            },
        },
    }


def structured_payload_to_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def select_structured_output_text(
    *,
    phase: Phase,
    structured_payload: Any,
    text_fallback: str | None,
) -> tuple[str | None, bool]:
    """Select usable post-phase JSON text.

    OpenCode can report a StructuredOutput payload even when the model also
    emitted the real JSON as assistant text and only called StructuredOutput
    with a schema-only object. Treat such payloads as incomplete and recover
    from the assistant text when it contains a complete phase payload.
    """

    if structured_payload is None:
        return None, False
    if _phase_payload_complete(phase=phase, payload=structured_payload):
        return structured_payload_to_text(structured_payload), True

    text_payload = _extract_json_payload_from_text(text_fallback)
    if _phase_payload_complete(phase=phase, payload=text_payload):
        return structured_payload_to_text(text_payload), False
    return None, False


def extract_structured_payload(*, response: Any, messages: list[dict[str, Any]]) -> Any | None:
    payload = _structured_from_message(response)
    if payload is not None:
        return payload
    for message in reversed(messages):
        payload = _structured_from_message(message)
        if payload is not None:
            return payload
    return None


def extract_structured_error(*, response: Any, messages: list[dict[str, Any]]) -> str | None:
    error = _structured_error_from_message(response)
    if error:
        return error
    for message in reversed(messages):
        error = _structured_error_from_message(message)
        if error:
            return error
    return None


def _message_info(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    info = message.get("info")
    return info if isinstance(info, dict) else message


def _structured_from_message(message: Any) -> Any | None:
    info = _message_info(message)
    if "structured" in info:
        return _decode_json_string_values(info.get("structured"))
    return None


def _decode_json_string_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _decode_json_string_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_string_values(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        decoded = json.loads(text)
    except Exception:
        return value
    return _decode_json_string_values(decoded)


def _extract_json_payload_from_text(text: str | None) -> Any | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.S)
    candidates: list[str] = []
    if fenced:
        candidates.append(fenced.group(1).strip())

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", stripped):
        candidate = stripped[match.start():]
        try:
            _parsed, end_idx = decoder.raw_decode(candidate)
        except Exception:
            continue
        candidates.append(candidate[:end_idx].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return _decode_json_string_values(payload)
    return None


def _phase_payload_complete(*, phase: Phase, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if phase == Phase.FINAL_REPORT:
        return "dataflows" in payload
    if phase == Phase.KNOWLEDGE_SYNTH:
        return "candidates" in payload and "reason" in payload
    if phase == Phase.KNOWLEDGE_RULE_REPAIR:
        return any(key in payload for key in ("candidate", "repaired_candidate", "candidates", "results"))
    return any(key != "schema_version" for key in payload)


def _structured_error_from_message(message: Any) -> str | None:
    info = _message_info(message)
    error = info.get("error")
    if isinstance(error, dict):
        name = str(error.get("name") or error.get("type") or "").strip()
        message_text = str(error.get("message") or "").strip()
        if name or message_text:
            return ": ".join(part for part in (name, message_text) if part)
        return json.dumps(error, ensure_ascii=False, sort_keys=True)
    if isinstance(error, str):
        return error.strip() or None
    return None


__all__ = [
    "STRUCTURED_PHASES",
    "extract_structured_error",
    "extract_structured_payload",
    "select_structured_output_text",
    "structured_format_for_phase",
    "structured_payload_to_text",
]
