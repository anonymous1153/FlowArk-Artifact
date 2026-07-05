"""Bridge OpenCode plugin hooks to the FlowArk augmentation semantics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from flowark.knowledge.runtime_augment import (
    append_injection_skip_record,
    reset_injection_record_extras,
    set_injection_record_extras,
)
from flowark.runtime.config import normalize_runtime_injection_mode
from flowark.runtime.hook_context import HookRuntimeContext, hook_runtime_context_from_payload
from flowark.semantics.knowledge_augmentation import KnowledgeAugmentationSemantics
from flowark.semantics.models import (
    AfterToolContext,
    AugmentDecision,
    AugmentDeliveryResult,
    AugmentRuntimeConfig,
    AugmentationPayload,
    DeliveryStatus,
    Phase,
    RequestSubmitContext,
    SessionHandle,
)
from flowark.timeutil import now_tz8_iso


PLUGIN_SENTINEL = "flowark-opencode-runtime-plugin-v1"
RUNTIME_CONTEXT_MARKER = "<flowark-runtime-context"
KNOWLEDGE_CONTEXT_MARKER = "<flowark-knowledge-injection"
SUPPORTED_EVENTS = {
    "chat.message",
    "tool.execute.before",
    "tool.execute.after",
    "experimental.chat.messages.transform",
}
POST_TOOL_ALLOWED_TOOLS = {"read", "grep", "glob", "bash"}
BASH_POLICY_READ_ONLY_GUARDED = "read_only_guarded"
BASH_KIND_CODE_CONTEXT = "code_context"
BASH_KIND_TRACE_ONLY = "trace_only"
BASH_KIND_BLOCKED = "blocked"
MAX_COMMAND_EXCERPT_CHARS = 240

_BASH_BLOCKED_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|chmod|chown|ln|truncate|dd|tee|apply_patch)\b",
    re.IGNORECASE,
)
_BASH_GIT_BLOCKED_RE = re.compile(r"\bgit\s+(?:reset|clean|checkout|apply|restore|switch)\b", re.IGNORECASE)
_BASH_SED_INPLACE_RE = re.compile(r"\bsed\b[^\n;&|]*\s-i(?:\s|$)", re.IGNORECASE)
_BASH_PERL_INPLACE_RE = re.compile(r"\bperl\b[^\n;&|]*\s-pi(?:\s|$)", re.IGNORECASE)
_BASH_WRITE_REDIRECT_RE = re.compile(r"(^|[\s;&|])(?:\d*)>>?\s*(?!&)(?P<target>\S+)")
_BASH_HEREDOC_RE = re.compile(r"<<-?\s*\w+")
_BASH_FIND_DELETE_RE = re.compile(r"\bfind\b[^\n;&|]*\s-delete\b", re.IGNORECASE)
_BASH_FIND_EXEC_MUTATING_RE = re.compile(
    r"\bfind\b[^\n;&|]*\s-exec\s+(?:"
    r"rm|mv|cp|mkdir|touch|chmod|chown|ln|truncate|dd|tee|apply_patch|bash|sh|zsh|python|python3|perl|sed"
    r"|git\s+(?:reset|clean|checkout|apply|restore|switch)"
    r")\b",
    re.IGNORECASE,
)
_BASH_CODE_CONTEXT_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:cat|head|tail|nl|rg|grep|find|ls|tree|sed|awk)\b"
    r"|\bgit\s+(?:grep|show|diff|blame)\b",
    re.IGNORECASE,
)
_BASH_TRACE_ONLY_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:pytest|tox|nox|make|cmake|xcodebuild)\b"
    r"|\b(?:python|python3|uv)\s+(?:run\s+)?(?:-m\s+)?pytest\b"
    r"|\b(?:npm|pnpm|yarn)\s+(?:test|install|run)\b"
    r"|\b(?:go|cargo|mvn|gradle)\s+(?:test|build|install|run)\b"
    r"|\bpip\s+install\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BashCommandClassification:
    kind: str
    policy_action: str
    reason: str
    command_excerpt: str
    command: str
    workdir: str | None = None
    description: str | None = None

    def trace_payload(self) -> dict[str, Any]:
        return {
            "bash_kind": self.kind,
            "bash_policy_action": self.policy_action,
            "bash_policy_reason": self.reason,
            "command_excerpt": self.command_excerpt,
            "workdir": self.workdir,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class OpenCodePluginBridgeResult:
    event: str
    action: str
    sentinel: str
    output: dict[str, Any]
    text: str | None = None
    delivery_surface: str | None = None
    delivery: AugmentDeliveryResult | None = None
    trace: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event,
            "action": self.action,
            "sentinel": self.sentinel,
            "output": self.output,
            "text": self.text,
            "delivery_surface": self.delivery_surface,
            "trace": dict(self.trace or {}),
        }
        if self.delivery is not None:
            payload["delivery"] = {
                "status": self.delivery.status.value,
                "reason": self.delivery.reason,
                "trace": dict(self.delivery.trace or {}),
            }
        else:
            payload["delivery"] = None
        return payload


def classify_bash_command(
    command: Any,
    *,
    workdir: Any = None,
    description: Any = None,
) -> BashCommandClassification:
    text = str(command or "").strip()
    excerpt = _command_excerpt(text)
    normalized_workdir = str(workdir).strip() if workdir not in {None, ""} else None
    normalized_description = str(description).strip() if description not in {None, ""} else None
    if not text:
        return BashCommandClassification(
            kind=BASH_KIND_TRACE_ONLY,
            policy_action="trace_only",
            reason="empty_command",
            command_excerpt=excerpt,
            command=text,
            workdir=normalized_workdir,
            description=normalized_description,
        )
    blocked_reason = _blocked_bash_reason(text)
    if blocked_reason:
        return BashCommandClassification(
            kind=BASH_KIND_BLOCKED,
            policy_action="blocked",
            reason=blocked_reason,
            command_excerpt=excerpt,
            command=text,
            workdir=normalized_workdir,
            description=normalized_description,
        )
    if _BASH_CODE_CONTEXT_RE.search(text):
        return BashCommandClassification(
            kind=BASH_KIND_CODE_CONTEXT,
            policy_action="allow",
            reason="code_context_command",
            command_excerpt=excerpt,
            command=text,
            workdir=normalized_workdir,
            description=normalized_description,
        )
    if _BASH_TRACE_ONLY_RE.search(text):
        return BashCommandClassification(
            kind=BASH_KIND_TRACE_ONLY,
            policy_action="trace_only",
            reason="trace_only_command",
            command_excerpt=excerpt,
            command=text,
            workdir=normalized_workdir,
            description=normalized_description,
        )
    return BashCommandClassification(
        kind=BASH_KIND_TRACE_ONLY,
        policy_action="trace_only",
        reason="bash_command_not_code_context",
        command_excerpt=excerpt,
        command=text,
        workdir=normalized_workdir,
        description=normalized_description,
    )


def _blocked_bash_reason(command: str) -> str | None:
    policy_command = _strip_shell_quoted_segments(command)
    if _BASH_HEREDOC_RE.search(policy_command):
        return "heredoc_not_allowed"
    for match in _BASH_WRITE_REDIRECT_RE.finditer(policy_command):
        target = str(match.group("target") or "").strip().strip("'\"")
        if target != "/dev/null":
            return "write_redirection_not_allowed"
    if _BASH_FIND_DELETE_RE.search(policy_command):
        return "find_delete_not_allowed"
    if _BASH_FIND_EXEC_MUTATING_RE.search(policy_command):
        return "find_exec_mutating_command"
    if _BASH_BLOCKED_COMMAND_RE.search(policy_command):
        return "write_or_destructive_command"
    if _BASH_GIT_BLOCKED_RE.search(policy_command):
        return "git_mutating_command"
    if _BASH_SED_INPLACE_RE.search(policy_command) or _BASH_PERL_INPLACE_RE.search(policy_command):
        return "in_place_edit_command"
    return None


def _strip_shell_quoted_segments(command: str) -> str:
    """Remove quoted text so grep patterns do not look like shell operators."""
    chars: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            chars.append(" " if quote else ch)
            escaped = False
            continue
        if ch == "\\":
            chars.append(" " if quote else ch)
            escaped = quote == '"'
            continue
        if quote:
            if ch == quote:
                quote = None
            chars.append(" ")
            continue
        if ch in {"'", '"'}:
            quote = ch
            chars.append(" ")
            continue
        chars.append(ch)
    return "".join(chars)


def _command_excerpt(command: str, limit: int = MAX_COMMAND_EXCERPT_CHARS) -> str:
    compact = " ".join(str(command or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def handle_plugin_event(payload: dict[str, Any]) -> OpenCodePluginBridgeResult:
    try:
        return asyncio.run(_handle_plugin_event(payload))
    except Exception as exc:  # pragma: no cover - defensive CLI guard
        event = str(payload.get("event") or "").strip() if isinstance(payload, dict) else ""
        output = _hook_output(payload)
        result = _result(
            event=event,
            action="error",
            output=output,
            delivery=AugmentDeliveryResult(
                status=DeliveryStatus.FAILED,
                reason=f"{type(exc).__name__}: {exc}",
                trace={"skip_type": "bridge_error"},
            ),
            trace={"error_type": type(exc).__name__, "error": str(exc)},
        )
        _append_trace_from_payload(
            payload,
            {
                "phase": "error",
                "event": event,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return result


def handle_plugin_event_json(line: str) -> str:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("OpenCode plugin bridge payload must be a JSON object")
    return json.dumps(handle_plugin_event(payload).to_dict(), ensure_ascii=False, sort_keys=True)


async def _handle_plugin_event(payload: dict[str, Any]) -> OpenCodePluginBridgeResult:
    started = time.monotonic()
    event = str(payload.get("event") or "").strip()
    output = _hook_output(payload)
    context_payload, hook_context = _load_context(payload)
    current_phase = _current_phase(context_payload)
    await _append_trace(
        context_payload,
        _trace_record(
            payload,
            phase="received",
            event=event,
            hook_context=hook_context,
            current_phase=current_phase,
        ),
    )

    if event not in SUPPORTED_EVENTS:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="unsupported_event",
            started=started,
        )
    if event == "tool.execute.before":
        return await _handle_before_tool(
            payload,
            context_payload=context_payload,
            started=started,
        )
    if current_phase != Phase.ANALYSIS.value and event in {"chat.message", "tool.execute.after"}:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="non_analysis_phase",
            started=started,
        )
    if not bool(context_payload.get("active")):
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason=str(context_payload.get("inactive_reason") or "inactive"),
            started=started,
        )
    if hook_context is None:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="missing_hook_runtime_context",
            started=started,
        )
    if event == "experimental.chat.messages.transform":
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="messages_transform_not_used",
            started=started,
        )
    if _contains_flowark_context(payload):
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="synthetic_flowark_context",
            started=started,
        )
    if event == "chat.message":
        return await _handle_chat_message(
            payload,
            context_payload=context_payload,
            hook_context=hook_context,
            started=started,
        )
    if event == "tool.execute.after":
        return await _handle_after_tool(
            payload,
            context_payload=context_payload,
            hook_context=hook_context,
            started=started,
        )
    return await _skip(
        payload,
        context_payload,
        event=event,
        output=output,
        reason="unhandled_event",
        started=started,
    )


async def _handle_before_tool(
    payload: dict[str, Any],
    *,
    context_payload: dict[str, Any],
    started: float,
) -> OpenCodePluginBridgeResult:
    event = "tool.execute.before"
    output = _hook_output(payload)
    input_data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    tool_name = str(input_data.get("tool") or payload.get("tool") or "").strip()
    if tool_name != "bash":
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="before_tool_not_guarded",
            started=started,
            tool_name=tool_name,
        )
    if _bash_policy(context_payload) != BASH_POLICY_READ_ONLY_GUARDED:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="unsupported_bash_policy",
            started=started,
            tool_name=tool_name,
        )
    bash = _bash_classification_from_payload(payload, output)
    if bash.kind == BASH_KIND_BLOCKED:
        delivery = AugmentDeliveryResult(
            status=DeliveryStatus.FAILED,
            reason=f"bash_policy_blocked:{bash.reason}",
            trace={"skip_type": "platform", **bash.trace_payload()},
        )
        trace = _trace_record(
            payload,
            phase="completed",
            event=event,
            delivery=delivery,
            injected_length=0,
            original_output_length=_output_length(output),
            bridge_ms=_elapsed_ms(started),
            tool_name=tool_name,
            current_phase=_current_phase(context_payload),
            bash_classification=bash,
        )
        await _append_trace(context_payload, trace)
        return _result(
            event=event,
            action="blocked",
            output=output,
            delivery=delivery,
            trace=trace,
        )
    reason = "bash_policy_allowed" if bash.kind == BASH_KIND_CODE_CONTEXT else "bash_policy_trace_only"
    return await _skip(
        payload,
        context_payload,
        event=event,
        output=output,
        reason=reason,
        started=started,
        tool_name=tool_name,
        bash_classification=bash,
    )


async def _handle_chat_message(
    payload: dict[str, Any],
    *,
    context_payload: dict[str, Any],
    hook_context: HookRuntimeContext,
    started: float,
) -> OpenCodePluginBridgeResult:
    event = "chat.message"
    output = _hook_output(payload)
    prompt = _parts_text(payload.get("parts"))
    if not _phase_allows(context_payload, "allow_request_submit_augment"):
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="request_submit_augment_disabled",
            started=started,
        )
    if not prompt.strip():
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="empty_prompt",
            started=started,
        )
    session_id = _session_id(payload)
    extras_token = set_injection_record_extras(
        {
            "agent_adapter": "opencode",
            "delivery_surface": "chat_message_tail",
            "tool_call_id": None,
            "opencode_session_id": session_id or None,
        }
    )
    try:
        semantics = _semantics_from_context(
            hook_context,
            request_submit_wrap_payload_fn=_render_request_submit_payload,
        )
        decision = await semantics.request_submit_augment(
            RequestSubmitContext(
                session=SessionHandle(adapter_name="opencode", session_id=session_id),
                phase=Phase.ANALYSIS,
                user_prompt=prompt,
                transcript_path=_transcript_path(context_payload),
                app_name=hook_context.analysis_app_name,
                source=hook_context.analysis_source,
                sink_types=list(hook_context.analysis_sink_types or []),
            )
        )
    finally:
        reset_injection_record_extras(extras_token)
    rendered = _render_request_submit_payload(decision.payload) if decision.payload is not None else ""
    return await _deliver_or_skip(
        payload,
        context_payload,
        event=event,
        output=output,
        decision=decision,
        rendered=rendered,
        delivery_surface="chat_message_tail",
        started=started,
    )


async def _handle_after_tool(
    payload: dict[str, Any],
    *,
    context_payload: dict[str, Any],
    hook_context: HookRuntimeContext,
    started: float,
) -> OpenCodePluginBridgeResult:
    event = "tool.execute.after"
    output = _hook_output(payload)
    input_data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    tool_name = str(input_data.get("tool") or payload.get("tool") or "").strip()
    if not _phase_allows(context_payload, "allow_after_tool_augment"):
        runtime_injection_mode = normalize_runtime_injection_mode(
            str(context_payload.get("runtime_injection_mode") or hook_context.runtime_injection_mode)
        )
        reason = (
            "runtime_injection_mode_start_only"
            if runtime_injection_mode == "start_only"
            else "after_tool_augment_disabled"
        )
        if runtime_injection_mode == "start_only":
            session_id = _session_id(payload)
            runtime_config = _runtime_config_from_context(hook_context)
            append_injection_skip_record(
                runtime_config=runtime_config,
                hook_event_name="PostToolUse",
                delivery_reason=reason,
                delta=True,
                query_excerpt=str(tool_name or ""),
                extras={
                    "agent_adapter": "opencode",
                    "delivery_surface": str(context_payload.get("delivery") or ""),
                    "tool_name": tool_name or None,
                    "tool_call_id": input_data.get("callID") or payload.get("callID"),
                    "opencode_session_id": session_id or None,
                },
            )
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason=reason,
            started=started,
            delivery_surface=str(context_payload.get("delivery") or ""),
            tool_name=tool_name,
            call_id=input_data.get("callID") or payload.get("callID"),
        )
    if tool_name not in POST_TOOL_ALLOWED_TOOLS:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="unsupported_tool",
            started=started,
            tool_name=tool_name,
        )
    delivery_surface = str(context_payload.get("delivery") or "no_reply_context").strip()
    if delivery_surface not in {"no_reply_context", "tool_output_append"}:
        return await _skip(
            payload,
            context_payload,
            event=event,
            output=output,
            reason="unsupported_delivery_surface",
            started=started,
            tool_name=tool_name,
        )

    session_id = _session_id(payload)
    call_id = str(input_data.get("callID") or payload.get("callID") or "").strip()
    bash_classification = None
    if tool_name == "bash":
        if _bash_policy(context_payload) != BASH_POLICY_READ_ONLY_GUARDED:
            return await _skip(
                payload,
                context_payload,
                event=event,
                output=output,
                reason="unsupported_bash_policy",
                started=started,
                delivery_surface=delivery_surface,
                tool_name=tool_name,
                call_id=call_id,
            )
        bash_classification = _bash_classification_from_payload(payload, output)
        if bash_classification.kind == BASH_KIND_BLOCKED:
            return await _skip(
                payload,
                context_payload,
                event=event,
                output=output,
                reason="bash_policy_blocked",
                started=started,
                delivery_surface=delivery_surface,
                tool_name=tool_name,
                call_id=call_id,
                bash_classification=bash_classification,
            )
        if bash_classification.kind != BASH_KIND_CODE_CONTEXT:
            return await _skip(
                payload,
                context_payload,
                event=event,
                output=output,
                reason="bash_trace_only",
                started=started,
                delivery_surface=delivery_surface,
                tool_name=tool_name,
                call_id=call_id,
                bash_classification=bash_classification,
            )
    extras_token = set_injection_record_extras(
        {
            "agent_adapter": "opencode",
            "delivery_surface": delivery_surface,
            "tool_call_id": call_id or None,
            "opencode_session_id": session_id or None,
            **(
                {
                    "tool_name": "bash",
                    "bash_kind": bash_classification.kind,
                    "bash_policy_action": bash_classification.policy_action,
                    "command_excerpt": bash_classification.command_excerpt,
                }
                if bash_classification is not None
                else {}
            ),
        }
    )
    try:
        semantics = _semantics_from_context(
            hook_context,
            after_tool_wrap_payload_fn=lambda candidate: _render_after_tool_payload(
                candidate,
                tool_name=tool_name,
                delivery_surface=delivery_surface,
                bash_classification=bash_classification,
            ),
        )
        decision = await semantics.after_tool_augment(
            AfterToolContext(
                session=SessionHandle(adapter_name="opencode", session_id=session_id),
                phase=Phase.ANALYSIS,
                tool_name=tool_name,
                tool_input=input_data.get("args"),
                tool_output=output,
                transcript_path=_transcript_path(context_payload),
                app_name=hook_context.analysis_app_name,
                source=hook_context.analysis_source,
                sink_types=list(hook_context.analysis_sink_types or []),
            )
        )
    finally:
        reset_injection_record_extras(extras_token)

    rendered = (
        _render_after_tool_payload(
            decision.payload,
            tool_name=tool_name,
            delivery_surface=delivery_surface,
            bash_classification=bash_classification,
        )
        if decision.payload is not None
        else ""
    )
    if decision.should_deliver and decision.payload is not None and delivery_surface == "tool_output_append":
        mutated_output = _append_tool_output(
            output,
            rendered=rendered,
            tool_name=tool_name,
            call_id=call_id,
            decision=decision,
            delivery_surface=delivery_surface,
            bash_classification=bash_classification,
        )
    else:
        mutated_output = output
    return await _deliver_or_skip(
        payload,
        context_payload,
        event=event,
        output=mutated_output,
        decision=decision,
        rendered=rendered,
        delivery_surface=delivery_surface,
        started=started,
        tool_name=tool_name,
        call_id=call_id,
        bash_classification=bash_classification,
    )


def _semantics_from_context(
    context: HookRuntimeContext,
    *,
    request_submit_wrap_payload_fn=None,
    after_tool_wrap_payload_fn=None,
) -> KnowledgeAugmentationSemantics:
    runtime_config = _runtime_config_from_context(context)
    return KnowledgeAugmentationSemantics(
        runtime_config,
        request_submit_wrap_payload_fn=request_submit_wrap_payload_fn,
        after_tool_wrap_payload_fn=after_tool_wrap_payload_fn,
    )


def _runtime_config_from_context(context: HookRuntimeContext) -> AugmentRuntimeConfig:
    if context.augment_runtime_config is not None:
        return replace(
            context.augment_runtime_config,
            skills_dir=context.augment_runtime_config.skills_dir or context.skills_dir,
            knowledge_injection_log_path=(
                context.augment_runtime_config.knowledge_injection_log_path
                or context.knowledge_injection_log_path
            ),
        )
    return AugmentRuntimeConfig(
        knowledge_mode=context.knowledge_mode,
        skills_dir=context.skills_dir,
        runtime_injection_mode=normalize_runtime_injection_mode(
            context.runtime_injection_mode
        ),
        knowledge_packaging_mode=context.knowledge_packaging_mode,
        knowledge_top_k=context.knowledge_top_k,
        knowledge_min_score=context.knowledge_min_score,
        knowledge_injection_char_budget=context.knowledge_injection_char_budget,
        knowledge_delta_char_budget=context.knowledge_delta_char_budget,
        knowledge_allow_repeat_within_session=context.knowledge_allow_repeat_within_session,
        knowledge_repeat_summary_hook_gap=context.knowledge_repeat_summary_hook_gap,
        knowledge_repeat_full_hook_gap=context.knowledge_repeat_full_hook_gap,
        knowledge_repeat_summary_react_gap=context.knowledge_repeat_summary_react_gap,
        knowledge_repeat_full_react_gap=context.knowledge_repeat_full_react_gap,
        knowledge_realtime_min_interval_ms=context.knowledge_realtime_min_interval_ms,
        knowledge_injection_log_path=context.knowledge_injection_log_path,
        knowledge_recall_top_m=context.knowledge_recall_top_m,
        reuse_embed_base_url=context.reuse_embed_base_url,
        reuse_embed_api_key=context.reuse_embed_api_key,
        reuse_embed_model=context.reuse_embed_model,
        reuse_embed_verify_ssl=context.reuse_embed_verify_ssl,
    )


async def _deliver_or_skip(
    payload: dict[str, Any],
    context_payload: dict[str, Any],
    *,
    event: str,
    output: dict[str, Any],
    decision: AugmentDecision,
    rendered: str,
    delivery_surface: str,
    started: float,
    tool_name: str | None = None,
    call_id: str | None = None,
    bash_classification: BashCommandClassification | None = None,
) -> OpenCodePluginBridgeResult:
    if decision.should_deliver and decision.payload is not None and rendered.strip():
        delivery = AugmentDeliveryResult(
            status=DeliveryStatus.DELIVERED,
            trace={
                "variant": "request_submit" if event == "chat.message" else "after_tool",
                "matched_ids": list(decision.payload.matched_ids or []),
                "matched_rules": list(decision.payload.matched_rules or []),
                **(bash_classification.trace_payload() if bash_classification is not None else {}),
            },
        )
        trace = _trace_record(
            payload,
            phase="completed",
            event=event,
            delivery=delivery,
            delivery_surface=delivery_surface,
            injected_length=len(rendered),
            original_output_length=_output_length(_hook_output(payload)),
            bridge_ms=_elapsed_ms(started),
            tool_name=tool_name,
            call_id=call_id,
            current_phase=_current_phase(context_payload),
            bash_classification=bash_classification,
            runtime_injection_mode=str(
                context_payload.get("runtime_injection_mode") or "context_aware"
            ),
        )
        await _append_trace(context_payload, trace)
        return _result(
            event=event,
            action="delivered",
            output=output,
            text=rendered,
            delivery_surface=delivery_surface,
            delivery=delivery,
            trace=trace,
        )
    reason = decision.reason or "semantic_skip"
    return await _skip(
        payload,
        context_payload,
        event=event,
        output=output,
        reason=reason,
        started=started,
        delivery_surface=delivery_surface,
        tool_name=tool_name,
        call_id=call_id,
        bash_classification=bash_classification,
    )


async def _skip(
    payload: dict[str, Any],
    context_payload: dict[str, Any],
    *,
    event: str,
    output: dict[str, Any],
    reason: str,
    started: float,
    delivery_surface: str | None = None,
    tool_name: str | None = None,
    call_id: str | None = None,
    bash_classification: BashCommandClassification | None = None,
) -> OpenCodePluginBridgeResult:
    platform_prefixes = (
        "unsupported",
        "missing",
        "synthetic",
        "non_analysis",
        "request_submit_augment_disabled",
        "after_tool_augment_disabled",
        "runtime_plugin_disabled",
        "runtime_injection_mode_",
        "naive_agent_mode",
        "knowledge_mode_disabled",
        "inactive",
        "context_file",
        "bash_",
        "before_tool_",
    )
    delivery = AugmentDeliveryResult(
        status=DeliveryStatus.SKIPPED,
        reason=reason,
        trace={"skip_type": "platform" if reason.startswith(platform_prefixes) else "semantic"},
    )
    trace = _trace_record(
        payload,
        phase="completed",
        event=event,
        delivery=delivery,
        delivery_surface=delivery_surface,
        injected_length=0,
        original_output_length=_output_length(output),
        bridge_ms=_elapsed_ms(started),
        tool_name=tool_name,
        call_id=call_id,
        current_phase=_current_phase(context_payload),
        bash_classification=bash_classification,
        runtime_injection_mode=str(
            context_payload.get("runtime_injection_mode") or "context_aware"
        ),
    )
    await _append_trace(context_payload, trace)
    return _result(
        event=event,
        action="skipped",
        output=output,
        delivery_surface=delivery_surface,
        delivery=delivery,
        trace=trace,
    )


def _result(
    *,
    event: str,
    action: str,
    output: dict[str, Any],
    text: str | None = None,
    delivery_surface: str | None = None,
    delivery: AugmentDeliveryResult | None = None,
    trace: dict[str, Any] | None = None,
) -> OpenCodePluginBridgeResult:
    return OpenCodePluginBridgeResult(
        event=event,
        action=action,
        sentinel=PLUGIN_SENTINEL,
        output=dict(output),
        text=text,
        delivery_surface=delivery_surface,
        delivery=delivery,
        trace=dict(trace or {}),
    )


def _render_request_submit_payload(payload: AugmentationPayload | None) -> str:
    if payload is None:
        return ""
    lines = [
        '<flowark-knowledge-injection source="flowark" trigger="request-submit">',
        "下面是 FlowArk 在用户请求提交时注入的可复用知识，用于减少重复探索。",
    ]
    body = str(payload.text or "").strip()
    if body:
        lines.extend(["", body])
    lines.append("</flowark-knowledge-injection>")
    return "\n".join(lines).strip()


def _render_after_tool_payload(
    payload: AugmentationPayload | None,
    *,
    tool_name: str,
    delivery_surface: str,
    bash_classification: BashCommandClassification | None = None,
) -> str:
    if payload is None:
        return ""
    tag = "flowark-runtime-context" if delivery_surface == "no_reply_context" else "flowark-knowledge-injection"
    attributes = [
        'source="flowark"',
        'trigger="after-tool"',
        f'tool="{_xml_attr(tool_name)}"',
        f'delivery="{_xml_attr(delivery_surface)}"',
    ]
    if bash_classification is not None:
        attributes.append(f'bash-kind="{_xml_attr(bash_classification.kind)}"')
    lines = [
        f"<{tag} {' '.join(attributes)}>",
        "这是 FlowArk 根据刚刚完成的工具调用补充的运行时知识，不是用户的新需求，也不是工具原始输出。",
    ]
    if bash_classification is not None:
        lines.append(f"命令类型: {bash_classification.kind}")
        if bash_classification.command_excerpt:
            lines.append(f"命令摘要: {bash_classification.command_excerpt}")
    matched_rules = list(payload.matched_rules or [])
    if matched_rules:
        lines.append(f"命中规则: {', '.join(matched_rules[:12])}")
    body = str(payload.text or "").strip()
    if body:
        lines.extend(["", body])
    lines.append(f"</{tag}>")
    return "\n".join(lines).strip()


def _append_tool_output(
    output: dict[str, Any],
    *,
    rendered: str,
    tool_name: str,
    call_id: str,
    decision: AugmentDecision,
    delivery_surface: str,
    bash_classification: BashCommandClassification | None = None,
) -> dict[str, Any]:
    mutated = dict(output)
    original = str(mutated.get("output") or "")
    mutated["output"] = f"{original.rstrip()}\n\n{rendered}" if original.strip() else rendered
    metadata = dict(mutated.get("metadata") if isinstance(mutated.get("metadata"), dict) else {})
    metadata["flowark"] = {
        "sentinel": PLUGIN_SENTINEL,
        "delivery_surface": delivery_surface,
        "trigger": "after-tool",
        "tool": tool_name,
        "call_id": call_id or None,
        "injected_chars": len(rendered),
        "matched_ids": list(decision.payload.matched_ids if decision.payload is not None else []),
        "matched_rules": list(decision.payload.matched_rules if decision.payload is not None else []),
    }
    if bash_classification is not None:
        metadata["flowark"].update(bash_classification.trace_payload())
    mutated["metadata"] = metadata
    return mutated


def _load_context(payload: dict[str, Any]) -> tuple[dict[str, Any], HookRuntimeContext | None]:
    context_file = str(
        payload.get("context_file")
        or payload.get("contextFile")
        or os.environ.get("FLOWARK_OPENCODE_HOOK_CONTEXT_FILE")
        or ""
    ).strip()
    if not context_file:
        return {"active": False, "inactive_reason": "missing_context_file"}, None
    path = Path(context_file).expanduser()
    if not path.exists():
        return {"active": False, "inactive_reason": "context_file_not_found"}, None
    context_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(context_payload, dict):
        return {"active": False, "inactive_reason": "invalid_context_file"}, None
    raw_hook_context = context_payload.get("hook_runtime_context")
    hook_context = (
        hook_runtime_context_from_payload(raw_hook_context)
        if isinstance(raw_hook_context, dict)
        else None
    )
    return context_payload, hook_context


def _current_phase(context_payload: dict[str, Any]) -> str:
    phase = str(context_payload.get("current_phase") or context_payload.get("phase") or "").strip()
    return phase or Phase.ANALYSIS.value


def _phase_allows(context_payload: dict[str, Any], key: str) -> bool:
    return _current_phase(context_payload) == Phase.ANALYSIS.value and bool(
        context_payload.get(key, context_payload.get("active"))
    )


def _bash_policy(context_payload: dict[str, Any]) -> str:
    return str(context_payload.get("bash_policy") or BASH_POLICY_READ_ONLY_GUARDED).strip().lower().replace("-", "_")


def _bash_args_from_payload(payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    input_data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    args = input_data.get("args")
    if isinstance(args, dict):
        return args
    output_args = output.get("args")
    if isinstance(output_args, dict):
        return output_args
    state = output.get("state") if isinstance(output.get("state"), dict) else {}
    state_input = state.get("input")
    if isinstance(state_input, dict):
        return state_input
    return {}


def _bash_classification_from_payload(
    payload: dict[str, Any],
    output: dict[str, Any],
) -> BashCommandClassification:
    args = _bash_args_from_payload(payload, output)
    return classify_bash_command(
        args.get("command"),
        workdir=args.get("workdir"),
        description=args.get("description"),
    )


async def _append_trace(context_payload: dict[str, Any], record: dict[str, Any]) -> None:
    path_text = str(context_payload.get("hook_trace_path") or "").strip()
    if not path_text:
        raw_hook_context = context_payload.get("hook_runtime_context")
        if isinstance(raw_hook_context, dict):
            path_text = str(raw_hook_context.get("command_hook_trace_path") or "").strip()
    if not path_text:
        return
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _append_trace_from_payload(payload: dict[str, Any], record: dict[str, Any]) -> None:
    try:
        context_payload, _hook_context = _load_context(payload)
        path_text = str(context_payload.get("hook_trace_path") or "").strip()
        if not path_text:
            return
        path = Path(path_text).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return


def _trace_record(
    payload: dict[str, Any],
    *,
    phase: str,
    event: str,
    hook_context: HookRuntimeContext | None = None,
    delivery: AugmentDeliveryResult | None = None,
    delivery_surface: str | None = None,
    injected_length: int | None = None,
    original_output_length: int | None = None,
    bridge_ms: int | None = None,
    tool_name: str | None = None,
    call_id: str | None = None,
    current_phase: str | None = None,
    bash_classification: BashCommandClassification | None = None,
    runtime_injection_mode: str | None = None,
) -> dict[str, Any]:
    input_data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    output = _hook_output(payload)
    runtime_mode = runtime_injection_mode
    if runtime_mode is None and hook_context is not None:
        runtime_mode = hook_context.runtime_injection_mode
    record: dict[str, Any] = {
        "timestamp": now_tz8_iso(),
        "phase": phase,
        "current_phase": current_phase,
        "event": event,
        "agent_adapter": "opencode",
        "runtime_injection_mode": normalize_runtime_injection_mode(runtime_mode),
        "session_id": _session_id(payload),
        "opencode_session_id": _session_id(payload),
        "message_id": input_data.get("messageID") or payload.get("messageID"),
        "tool": tool_name or input_data.get("tool") or payload.get("tool"),
        "call_id": call_id or input_data.get("callID") or payload.get("callID"),
        "tool_call_id": call_id or input_data.get("callID") or payload.get("callID"),
        "delivery_surface": delivery_surface,
        "original_output_length": original_output_length if original_output_length is not None else _output_length(output),
        "injected_length": int(injected_length or 0),
        "bridge_ms": bridge_ms,
    }
    if hook_context is not None:
        record["knowledge_mode"] = hook_context.knowledge_mode
        record["app_name"] = hook_context.analysis_app_name
    if delivery is not None:
        record["delivery_status"] = delivery.status.value
        record["delivery_reason"] = delivery.reason
        record["delivery_trace"] = dict(delivery.trace or {})
    if bash_classification is not None:
        record.update(bash_classification.trace_payload())
    return record


def _hook_output(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    return dict(output) if isinstance(output, dict) else {}


def _session_id(payload: dict[str, Any]) -> str:
    input_data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    return str(input_data.get("sessionID") or payload.get("sessionID") or "").strip() or "unknown-session"


def _parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    chunks = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = str(part.get("text") or "").strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _contains_flowark_context(value: Any) -> bool:
    if isinstance(value, dict):
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        if metadata.get("flowark_context_message") is True:
            return True
        return any(_contains_flowark_context(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_flowark_context(item) for item in value)
    text = str(value or "")
    return RUNTIME_CONTEXT_MARKER in text or KNOWLEDGE_CONTEXT_MARKER in text


def _output_length(output: dict[str, Any]) -> int:
    value = output.get("output")
    if isinstance(value, str):
        return len(value)
    if value is None:
        return 0
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _transcript_path(context_payload: dict[str, Any]) -> str | None:
    text = str(context_payload.get("transcript_path") or "").strip()
    return text or None


def _elapsed_ms(started: float) -> int:
    return int(max(0.0, time.monotonic() - started) * 1000)


def _xml_attr(value: Any) -> str:
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def main(argv: list[str] | None = None) -> int:
    _ = argv
    raw = sys.stdin.read()
    try:
        sys.stdout.write(handle_plugin_event_json(raw))
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "event": "",
                    "action": "error",
                    "sentinel": PLUGIN_SENTINEL,
                    "output": {},
                    "delivery": {
                        "status": DeliveryStatus.FAILED.value,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "trace": {"skip_type": "bridge_error"},
                    },
                    "trace": {"error_type": type(exc).__name__, "error": str(exc)},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "PLUGIN_SENTINEL",
    "SUPPORTED_EVENTS",
    "POST_TOOL_ALLOWED_TOOLS",
    "BASH_KIND_CODE_CONTEXT",
    "BASH_KIND_TRACE_ONLY",
    "BASH_KIND_BLOCKED",
    "BashCommandClassification",
    "OpenCodePluginBridgeResult",
    "classify_bash_command",
    "handle_plugin_event",
    "handle_plugin_event_json",
    "main",
]
