"""OpenCode adapter boundary for FlowArk phase-one integration."""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from typing import Any, AsyncIterator, Callable

from flowark.runtime.config import AnalysisRequest, RunConfig
from flowark.semantics.engine import SemanticEngine
from flowark.semantics.models import (
    AnalysisRunContext,
    AnalysisRunResult,
    Phase,
    PhaseRunResult,
    PhaseSpec,
    SessionHandle,
    TurnOutcome,
)

from ..base import AdapterCapabilities
from .client import OpenCodeHttpClient, OpenCodeHttpError
from .server import OpenCodeServerProcess
from .settings import OpenCodeRuntime, build_opencode_runtime
from .bridge import (
    BASH_KIND_BLOCKED,
    BASH_KIND_CODE_CONTEXT,
    BASH_KIND_TRACE_ONLY,
    classify_bash_command,
)
ServerFactory = Callable[..., Any]
ClientFactory = Callable[..., Any]
TranscriptAppendFn = Callable[..., None]
FLOWARK_CONTEXT_MARKERS = ("<flowark-runtime-context", "<flowark-knowledge-injection")
FLOWARK_CONTEXT_BLOCK_RE = re.compile(
    r"<(flowark-runtime-context|flowark-knowledge-injection)\b[^>]*>.*?</\1>",
    re.DOTALL,
)
OPENCODE_TRANSCRIPT_ERROR_MESSAGE_MAX_CHARS = 500


class OpenCodeRunSession:
    def __init__(
        self,
        *,
        adapter: "OpenCodeAdapter",
        runtime: OpenCodeRuntime,
        server_handle: Any,
        client: Any,
        run_context: AnalysisRunContext,
        trace: dict[str, Any],
    ) -> None:
        self._adapter = adapter
        self._runtime = runtime
        self._server_handle = server_handle
        self._client = client
        self._run_context = run_context
        self._trace = dict(trace)
        self._session: SessionHandle | None = None
        self._analysis_ran = False
        self._turn_index = 0

    async def run_analysis(self) -> AnalysisRunResult:
        if self._analysis_ran:
            raise RuntimeError("analysis turn has already run in this AgentRunSession")
        self._analysis_ran = True
        session = await self._ensure_session(title="FlowArk analysis")
        outcome = await self._run_turn(
            session=session,
            prompt=self._run_context.prompt,
            turn_name=self._run_context.turn_name,
            echo=self._run_context.echo,
            current_phase=Phase.ANALYSIS,
            expect_json=False,
        )
        return AnalysisRunResult(session=session, outcome=outcome, trace=dict(self._trace))

    async def continue_phase(
        self,
        *,
        phase_spec: PhaseSpec,
    ) -> PhaseRunResult:
        session = await self._ensure_session(title="FlowArk analysis")
        outcome = await self._run_turn(
            session=session,
            prompt=phase_spec.instruction,
            turn_name=phase_spec.turn_contract.turn_name or phase_spec.phase.value,
            echo=phase_spec.turn_contract.echo,
            transcript_prefix=phase_spec.turn_contract.transcript_prefix,
            current_phase=phase_spec.phase,
            expect_json=phase_spec.turn_contract.expect_json,
        )
        return PhaseRunResult(phase=phase_spec.phase, session=session, outcome=outcome)

    async def _ensure_session(self, *, title: str) -> SessionHandle:
        if self._session is not None:
            return self._session
        payload = await self._client.create_session(title=title)
        session_id = _extract_session_id(payload)
        self._session = SessionHandle(
            adapter_name=self._adapter.name,
            session_id=session_id,
            lineage_id=session_id,
        )
        self._adapter._remember_runtime(session_id=session_id, runtime=self._runtime)
        return self._session

    async def _run_turn(
        self,
        *,
        session: SessionHandle,
        prompt: str,
        turn_name: str,
        echo: bool,
        transcript_prefix: str | None = None,
        current_phase: Phase = Phase.ANALYSIS,
        expect_json: bool = False,
    ) -> TurnOutcome:
        self._turn_index += 1
        turn_index = self._turn_index
        phase_name = current_phase.value
        expect_plain_json = bool(expect_json and current_phase != Phase.ANALYSIS)
        json_parse_mode = "flowark_plain_json" if expect_plain_json else "text"
        native_structured_output = False
        structured_output_requested = False
        structured_output_delivered = False
        structured_output_error = None
        format_payload = None
        if current_phase == Phase.ANALYSIS:
            self._runtime.write_hook_context(
                current_phase=phase_name,
                active=self._runtime.runtime_plugin_active,
                inactive_reason=(
                    None
                    if self._runtime.runtime_plugin_active
                    else str(self._runtime.hook_context_payload.get("inactive_reason") or "inactive")
                ),
            )
            tools = self._runtime.tool_policy
        else:
            self._runtime.write_hook_context(
                current_phase=phase_name,
                active=False,
                inactive_reason="non_analysis_phase",
            )
            tools = self._runtime.post_phase_tool_policy
        previous_messages, previous_messages_error = await _load_session_messages(
            self._client,
            session_id=session.session_id,
        )
        previous_message_ids = _message_id_set(previous_messages)
        response = await self._client.prompt(
            session_id=session.session_id,
            text=prompt,
            provider_id=self._runtime.provider_id,
            model_id=self._runtime.model_id,
            tools=tools,
            agent="build",
            no_reply=False,
            format_payload=format_payload,
        )
        raw_messages, current_messages_error = await _load_session_messages(
            self._client,
            session_id=session.session_id,
        )
        messages_error = _combine_message_errors(previous_messages_error, current_messages_error)
        if not raw_messages:
            raw_messages = [response] if isinstance(response, dict) else []
        turn_messages, message_scope = _scope_current_turn_messages(
            raw_messages,
            previous_message_ids=previous_message_ids,
            previous_message_count=len(previous_messages),
        )
        if not turn_messages:
            turn_messages = [response] if isinstance(response, dict) else raw_messages
            message_scope = "response_fallback" if turn_messages else "empty"
        transcript_messages = [_format_message(item) for item in turn_messages]
        for text in transcript_messages:
            self._adapter._append_transcript(text, prefix=transcript_prefix)
            if echo:
                print(text)
        text_fallback = _extract_final_text(response=response, messages=turn_messages)
        post_phase_tool_call_violation = _post_phase_tool_call_violation(
            current_phase=current_phase,
            expect_plain_json=expect_plain_json,
            messages=turn_messages,
        )
        final_text = (
            _post_phase_tool_violation_text(current_phase=current_phase)
            if post_phase_tool_call_violation
            else text_fallback
        )
        metrics = _build_turn_metrics(
            name=turn_name,
            current_phase=phase_name,
            session_id=session.session_id,
            response=response,
            messages=turn_messages,
            server_handle=self._server_handle,
            runtime=self._runtime,
            messages_error=messages_error,
            message_scope=message_scope,
            raw_session_message_count=len(raw_messages),
            previous_session_message_count=len(previous_messages),
            json_parse_mode=json_parse_mode,
            native_structured_output=native_structured_output,
            structured_output_requested=structured_output_requested,
            structured_output_delivered=structured_output_delivered,
            structured_output_error=structured_output_error,
            post_phase_tool_call_violation=post_phase_tool_call_violation,
        )
        if isinstance(metrics.get("request_usage_records"), list):
            for record in metrics["request_usage_records"]:
                if isinstance(record, dict):
                    record["turn_index"] = turn_index
        _write_turn_artifacts(
            run_dir=self._run_context.run_dir,
            turn_name=turn_name,
            turn_index=turn_index,
            current_phase=phase_name,
            session_id=session.session_id,
            messages=turn_messages,
            metrics=metrics,
            runtime=self._runtime,
            server_handle=self._server_handle,
            messages_error=messages_error,
            json_parse_mode=json_parse_mode,
            native_structured_output=native_structured_output,
            structured_output_requested=structured_output_requested,
            structured_output_delivered=structured_output_delivered,
            structured_output_error=structured_output_error,
        )
        return TurnOutcome(
            raw_text=final_text,
            messages=transcript_messages,
            turn_metrics=[metrics],
            error=_turn_error(metrics),
        )


class OpenCodeAdapter:
    name = "opencode"
    capabilities = AdapterCapabilities(
        supports_same_session_continuation=True,
        supports_request_submit_augment=True,
        supports_after_tool_augment=True,
        supports_augmentation_delivery=True,
        supports_continuous_run_session=True,
        supports_native_structured_output=False,
    )

    def __init__(
        self,
        *,
        config: RunConfig,
        workspace_root: Path,
        transcript_append_fn: TranscriptAppendFn,
        capture_proxy_factory: Any,
        server_factory: ServerFactory | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._transcript_append_fn = transcript_append_fn
        self._server_factory = server_factory or OpenCodeServerProcess
        self._client_factory = client_factory or OpenCodeHttpClient
        self._runtime_by_session_id: dict[str, OpenCodeRuntime] = {}

    def reset_ephemeral_state(self) -> None:
        self._runtime_by_session_id.clear()

    def _append_transcript(self, text: str, *, prefix: str | None = None) -> None:
        if self._transcript_append_fn is None:
            return
        self._transcript_append_fn(text, prefix=prefix)

    def _remember_runtime(self, *, session_id: str, runtime: OpenCodeRuntime) -> None:
        if session_id.strip():
            self._runtime_by_session_id[session_id] = runtime

    @asynccontextmanager
    async def open_run_session(
        self,
        *,
        request: AnalysisRequest,
        run_context: AnalysisRunContext,
        semantics: SemanticEngine,
    ) -> AsyncIterator[OpenCodeRunSession]:
        last_session_error: BaseException | None = None
        for warmup_attempt in range(1, 3):
            runtime = build_opencode_runtime(
                config=self._config,
                workspace_root=self._workspace_root,
                run_dir=run_context.run_dir,
                request=request,
                augment_runtime_config=semantics.augment_runtime_config(),
            )
            server_cm = self._server_factory(runtime=runtime)
            async with server_cm as server_handle:
                client = self._client_factory(
                    base_url=server_handle.url,
                    directory=str(self._config.cwd),
                )
                health = await _optional_client_call(client, "health")
                paths = await _optional_client_call(client, "paths")
                run_session = OpenCodeRunSession(
                    adapter=self,
                    runtime=runtime,
                    server_handle=server_handle,
                    client=client,
                    run_context=run_context,
                    trace=self._analysis_trace(
                        runtime=runtime,
                        server_handle=server_handle,
                        health=health,
                        paths=paths,
                        session_warmup_attempt=warmup_attempt,
                    ),
                )
                try:
                    await run_session._ensure_session(title="FlowArk analysis")
                except OpenCodeHttpError as exc:
                    if "create_session timed out" not in str(exc):
                        raise
                    last_session_error = exc
                    if warmup_attempt >= 2:
                        raise
                    continue
                yield run_session
                return
        if last_session_error is not None:
            raise last_session_error
        raise RuntimeError("OpenCode run session could not be opened")

    async def run_analysis(
        self,
        *,
        request: AnalysisRequest,
        run_context: AnalysisRunContext,
        semantics: SemanticEngine,
    ) -> AnalysisRunResult:
        async with self.open_run_session(
            request=request,
            run_context=run_context,
            semantics=semantics,
        ) as session:
            return await session.run_analysis()

    async def continue_phase(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
    ) -> PhaseRunResult:
        raise ValueError(
            "OpenCode phase continuation requires an active OpenCode run session in phase one"
        )

    def _analysis_trace(
        self,
        *,
        runtime: OpenCodeRuntime,
        server_handle: Any,
        health: dict[str, Any] | None = None,
        paths: dict[str, Any] | None = None,
        session_warmup_attempt: int | None = None,
    ) -> dict[str, Any]:
        opencode_payload = runtime.trace_payload()
        opencode_payload["post_phases_enabled"] = True
        opencode_payload["post_phase_mode"] = runtime.post_phase_mode
        opencode_payload["json_parse_mode"] = "flowark_plain_json"
        opencode_payload["native_structured_output_enabled"] = False
        opencode_payload["legacy_structured_output_configured"] = runtime.structured_output_enabled
        opencode_payload["structured_output_enabled"] = False
        opencode_payload["phase2_smoke_ready"] = True
        opencode_payload["phase2_artifacts_enabled"] = True
        opencode_payload["server_url"] = getattr(server_handle, "url", None)
        opencode_payload["server_pid"] = getattr(server_handle, "pid", None)
        opencode_payload["runtime_server_log_path"] = getattr(server_handle, "log_path", None)
        opencode_payload["server_log_path"] = (
            str(runtime.preserved_server_log_path)
            if runtime.preserved_server_log_path is not None
            else getattr(server_handle, "log_path", None)
        )
        if health is not None:
            opencode_payload["server_health"] = health
        if paths is not None:
            opencode_payload["server_paths"] = paths
        if session_warmup_attempt is not None:
            opencode_payload["session_warmup_attempt"] = int(session_warmup_attempt)
        return {
            "cwd": str(self._config.cwd),
            "settings_path": None,
            "agent_names": [],
            "capture_proxy_metadata": {"enabled": False},
            "opencode": opencode_payload,
            "opencode_isolation_dir": opencode_payload["isolation_dir"],
            "opencode_command_source": opencode_payload["command_source"],
            "opencode_provider": runtime.provider_id,
            "opencode_model": runtime.model,
            "opencode_after_tool_delivery": runtime.after_tool_delivery,
            "opencode_structured_output_enabled": False,
            "opencode_native_structured_output_enabled": False,
            "opencode_legacy_structured_output_configured": runtime.structured_output_enabled,
            "opencode_json_parse_mode": "flowark_plain_json",
            "opencode_post_phase_mode": runtime.post_phase_mode,
            "opencode_post_phases_enabled": True,
            "opencode_runtime_plugin_enabled": bool(opencode_payload.get("runtime_plugin_enabled")),
            "opencode_post_tool_delivery_active": bool(opencode_payload.get("post_tool_delivery_active")),
        }


async def _optional_client_call(client: Any, method_name: str) -> dict[str, Any] | None:
    method = getattr(client, method_name, None)
    if not callable(method):
        return None
    try:
        payload = await method()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"value": payload}


async def _load_session_messages(client: Any, *, session_id: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        messages = await client.messages(session_id=session_id)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(messages, list):
        return [], f"unexpected messages payload type: {type(messages).__name__}"
    return [item for item in messages if isinstance(item, dict)], None


def _combine_message_errors(*errors: str | None) -> str | None:
    values = [error for error in errors if error]
    if not values:
        return None
    return "; ".join(values)


def _extract_session_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "sessionID", "session_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            return _extract_session_id(data)
    raise ValueError(f"OpenCode session create response did not include a session id: {payload!r}")


def _message_id(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    info = _message_info(message)
    for payload in (info, message):
        for key in ("id", "messageID", "message_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _message_id_set(messages: list[dict[str, Any]]) -> set[str]:
    return {message_id for message in messages if (message_id := _message_id(message))}


def _scope_current_turn_messages(
    messages: list[dict[str, Any]],
    *,
    previous_message_ids: set[str],
    previous_message_count: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    if not previous_message_ids:
        if previous_message_count > 0 and len(messages) > previous_message_count:
            return messages[previous_message_count:], "current_turn_tail_by_count"
        if previous_message_count > 0:
            return messages, "full_session_fallback_no_prior_ids"
        return messages, "full_session_no_prior_ids"
    scoped: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        message_id = _message_id(message)
        if message_id is None:
            if index >= previous_message_count:
                scoped.append(message)
        elif message_id not in previous_message_ids:
            scoped.append(message)
    if scoped:
        return scoped, "current_turn_new_messages"
    if previous_message_count > 0 and len(messages) > previous_message_count:
        return messages[previous_message_count:], "current_turn_tail_by_count"
    return messages, "full_session_fallback_no_new_ids"


def _message_info(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    info = message.get("info")
    return info if isinstance(info, dict) else message


def _message_parts(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    if isinstance(parts, list):
        return [part for part in parts if isinstance(part, dict)]
    return []


def _single_line_error_field(value: Any, *, max_chars: int | None = None) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if max_chars is not None and len(text) > max_chars:
        return text[: max(0, max_chars - 1)].rstrip() + "..."
    return text


def _shallow_error_value(error: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in error:
            return error.get(key)
    body = error.get("body")
    if isinstance(body, dict):
        for key in keys:
            if key in body:
                return body.get(key)
    return None


def _format_transcript_field(key: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:/@+-]+", value):
        return f"{key}={value}"
    return f"{key}={json.dumps(value, ensure_ascii=False)}"


def _normalize_error_status(value: Any) -> str | None:
    text = _single_line_error_field(value, max_chars=40)
    if not text:
        return None
    match = re.search(r"\b(\d{3})\b", text)
    return match.group(1) if match else text


def _format_opencode_error_timestamp(info: dict[str, Any]) -> str | None:
    time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
    value = time_info.get("created")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        timestamp = int(value)
        if 10**9 <= timestamp < 10**13:
            return str(timestamp)
    text = _single_line_error_field(value, max_chars=40)
    if text and re.fullmatch(r"\d{10,13}", text):
        return text
    return None


def _looks_like_rate_limit_error(*values: Any) -> bool:
    text = " ".join(
        field
        for value in values
        if (field := _single_line_error_field(value, max_chars=1000))
    ).lower()
    return any(
        needle in text
        for needle in (
            '"code":"1302"',
            '"code": "1302"',
            '"code":"1305"',
            '"code": "1305"',
            "429",
            "速率限制",
            "请求频率",
            "访问量过大",
            "稍后再试",
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
        )
    )


def _format_opencode_error_line(info: dict[str, Any]) -> str | None:
    error = info.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        name = _single_line_error_field(
            _shallow_error_value(error, "name", "type"),
            max_chars=120,
        )
        message = _single_line_error_field(
            _shallow_error_value(error, "message"),
            max_chars=OPENCODE_TRANSCRIPT_ERROR_MESSAGE_MAX_CHARS,
        )
        code = _single_line_error_field(
            _shallow_error_value(error, "code"),
            max_chars=80,
        )
        status = _normalize_error_status(
            _shallow_error_value(error, "status", "statusCode", "status_code", "error_status")
        )
    else:
        name = None
        message = _single_line_error_field(
            error,
            max_chars=OPENCODE_TRANSCRIPT_ERROR_MESSAGE_MAX_CHARS,
        )
        code = None
        status = None

    rate_limited = _looks_like_rate_limit_error(name, message, code, status) or code in {"1302", "1305"}
    if rate_limited and not (status and re.search(r"\b\d{3}\b", status)):
        status = "429"

    fields = ["[opencode-error]"]
    timestamp = _format_opencode_error_timestamp(info)
    if timestamp:
        fields.append(f"timestamp={timestamp}")
    if name:
        fields.append(_format_transcript_field("name", name))
    if status:
        fields.append(_format_transcript_field("error_status", status))
    if rate_limited:
        fields.append("error=rate_limit")
    if code:
        fields.append(_format_transcript_field("code", code))
    fields.append(
        _format_transcript_field(
            "message",
            message or "<unavailable>",
        )
    )
    return " ".join(fields)


def _format_message(message: Any) -> str:
    info = _message_info(message)
    role = str(info.get("role") or "message")
    message_id = str(info.get("id") or "")
    lines = [f"OpenCode {role}{f' {message_id}' if message_id else ''}".strip()]
    error_line = _format_opencode_error_line(info)
    if error_line:
        lines.append(error_line)
    for part in _message_parts(message):
        part_type = str(part.get("type") or "")
        if part_type == "text":
            text = str(part.get("text") or "").strip()
            if text:
                lines.append(text)
        elif part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "")
            title = str(state.get("title") or part.get("tool") or "").strip()
            lines.append(f"[tool:{part.get('tool')}] {status} {title}".strip())
            if "input" in state:
                lines.append("[tool-input] " + _format_artifact_value(state.get("input")))
            if "output" in state:
                lines.append("[tool-output] " + _format_artifact_value(state.get("output")))
        elif part_type == "step-finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            lines.append(
                "[step-finish] "
                + json.dumps(
                    {
                        "reason": part.get("reason"),
                        "cost": part.get("cost"),
                        "tokens": tokens,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    if len(lines) == 1:
        lines.append(json.dumps(message, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def _extract_text_from_message(message: Any) -> str:
    chunks = [
        str(part.get("text") or "").strip()
        for part in _message_parts(message)
        if part.get("type") == "text" and str(part.get("text") or "").strip()
    ]
    return "\n".join(chunks).strip()


def _extract_final_text(*, response: Any, messages: list[dict[str, Any]]) -> str | None:
    text = _extract_text_from_message(response)
    if text:
        return text
    for message in reversed(messages):
        info = _message_info(message)
        if info.get("role") == "assistant":
            text = _extract_text_from_message(message)
            if text:
                return text
    return None


def _post_phase_tool_call_violation(
    *,
    current_phase: Phase,
    expect_plain_json: bool,
    messages: list[dict[str, Any]],
) -> bool:
    if not expect_plain_json or current_phase == Phase.ANALYSIS:
        return False
    return any(part.get("type") == "tool" for message in messages for part in _message_parts(message))


def _post_phase_tool_violation_text(*, current_phase: Phase) -> str:
    return (
        f"OpenCode post phase {current_phase.value} called tools instead of returning JSON. "
        "Retry with JSON only; do not call tools or continue exploration."
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _token_usage(info: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, int]:
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    if not tokens:
        for part in reversed(parts):
            if part.get("type") == "step-finish" and isinstance(part.get("tokens"), dict):
                tokens = part["tokens"]
                break
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    input_tokens = int(_as_int(tokens.get("input")) or 0)
    output_tokens = int(_as_int(tokens.get("output")) or 0)
    reasoning_tokens = int(_as_int(tokens.get("reasoning")) or 0)
    cache_read_tokens = int(_as_int(cache.get("read")) or 0)
    cache_creation_tokens = int(_as_int(cache.get("write")) or 0)
    total_tokens = _as_int(tokens.get("total"))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_creation_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(total_tokens),
        "reasoning_tokens": reasoning_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "cache_creation_input_tokens": cache_creation_tokens,
    }


def _prompt_total_from_usage(usage: dict[str, Any]) -> int:
    return (
        int(_as_int(usage.get("input_tokens")) or 0)
        + int(_as_int(usage.get("cache_read_input_tokens")) or 0)
        + int(_as_int(usage.get("cache_creation_input_tokens")) or 0)
    )


def _tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        for part in _message_parts(message):
            if part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            entry = {
                "tool": part.get("tool"),
                "call_id": part.get("callID"),
                "status": state.get("status"),
                "title": state.get("title"),
                "metadata": state.get("metadata") if isinstance(state.get("metadata"), dict) else {},
            }
            if "input" in state:
                entry["input"] = state.get("input")
            if "output" in state:
                entry["output"] = state.get("output")
            calls.append(entry)
    return calls


def _build_turn_metrics(
    *,
    name: str,
    current_phase: str,
    session_id: str,
    response: Any,
    messages: list[dict[str, Any]],
    server_handle: Any,
    runtime: OpenCodeRuntime,
    messages_error: str | None = None,
    message_scope: str = "full_session",
    raw_session_message_count: int | None = None,
    previous_session_message_count: int | None = None,
    json_parse_mode: str = "text",
    native_structured_output: bool = False,
    structured_output_requested: bool = False,
    structured_output_delivered: bool = False,
    structured_output_error: str | None = None,
    post_phase_tool_call_violation: bool = False,
) -> dict[str, Any]:
    assistant_messages = [item for item in messages if _message_info(item).get("role") == "assistant"]
    user_messages = [item for item in messages if _message_info(item).get("role") == "user"]
    assistant_info = _message_info(response)
    assistant_parts = _message_parts(response)
    if not assistant_info.get("tokens") and assistant_messages:
        assistant_info = _message_info(assistant_messages[-1])
        assistant_parts = _message_parts(assistant_messages[-1])
    assistant_usage_for_error = _token_usage(assistant_info, assistant_parts)
    assistant_usage_records = _assistant_usage_records(messages)
    request_usage_records = _request_usage_records(
        messages,
        turn_index=None,
        turn_name=name,
        current_phase=current_phase,
        session_id=session_id,
        runtime=runtime,
    )
    usage_records_for_rollup = request_usage_records or assistant_usage_records
    usage = _aggregate_usage_records(usage_records_for_rollup) if usage_records_for_rollup else assistant_usage_for_error
    cost = _aggregate_cost_records(usage_records_for_rollup)
    if request_usage_records and any(_as_float(record.get("cost_usd")) is None for record in request_usage_records):
        fallback_cost = _as_float(assistant_info.get("cost"))
        if fallback_cost is not None:
            cost = fallback_cost
    if cost is None:
        cost = _as_float(assistant_info.get("cost"))
    time_info = assistant_info.get("time") if isinstance(assistant_info.get("time"), dict) else {}
    created = _as_float(time_info.get("created"))
    completed = _as_float(time_info.get("completed"))
    duration_ms = int(max(0.0, completed - created)) if created is not None and completed is not None else None
    calls = _tool_calls(messages)
    injection_stats = _knowledge_injection_stats(messages)
    hook_trace_path = runtime.hook_trace_path if current_phase == Phase.ANALYSIS.value else None
    bash_stats = _merge_bash_stats(
        _bash_stats_from_tool_calls(calls),
        _bash_stats_from_hook_trace(hook_trace_path),
    )
    error = assistant_info.get("error")
    if structured_output_delivered:
        error = None
    elif structured_output_error:
        error = {
            "type": "OpenCodeStructuredOutputError",
            "message": structured_output_error,
        }
    if error is None:
        error = _empty_assistant_error(
            assistant_info=assistant_info,
            assistant_parts=assistant_parts,
            messages=messages,
            usage=assistant_usage_for_error,
        )
    cost_estimate_source = "opencode_provider_cost" if cost and cost > 0 else "tokens_only_or_missing_price"
    result: dict[str, Any] = {
        "session_id": session_id,
        "subtype": "opencode",
        "is_error": error is not None,
        "error": error,
        "duration_ms": duration_ms,
        "duration_api_ms": duration_ms,
        "num_turns": max(1, len(assistant_messages)),
        "total_cost_usd": cost,
        "usage": usage,
        "raw_usage": usage,
        "model_usage": {
            runtime.model: {
                **usage,
                **({"cost_usd": cost} if cost is not None else {}),
            }
        },
        "usage_source": "opencode",
        "cost_estimate_source": cost_estimate_source,
        "json_parse_mode": json_parse_mode,
        "native_structured_output": bool(native_structured_output),
        "native_structured_output_requested": False,
        "native_structured_output_delivered": False,
        "structured_output_requested": bool(structured_output_requested),
        "structured_output_delivered": bool(structured_output_delivered),
        "structured_output_error": structured_output_error,
        "post_phase_tool_call_violation": bool(post_phase_tool_call_violation),
    }
    return {
        "name": name,
        "adapter": "opencode",
        "current_phase": current_phase,
        "raw_message_count": len(messages),
        "raw_session_message_count": raw_session_message_count if raw_session_message_count is not None else len(messages),
        "previous_session_message_count": previous_session_message_count if previous_session_message_count is not None else 0,
        "message_scope": message_scope,
        "json_parse_mode": json_parse_mode,
        "native_structured_output": bool(native_structured_output),
        "native_structured_output_requested": False,
        "native_structured_output_delivered": False,
        "structured_output_requested": bool(structured_output_requested),
        "structured_output_delivered": bool(structured_output_delivered),
        "structured_output_error": structured_output_error,
        "post_phase_tool_call_violation": bool(post_phase_tool_call_violation),
        "tool_use_block_count": len(calls),
        "assistant_message_count": len(assistant_messages),
        "user_message_count": len(user_messages),
        "system_message_count": 0,
        "subagent_runs": [],
        "subagent_run_count": 0,
        "tool_calls": calls,
        "bash": bash_stats,
        **bash_stats,
        "request_usage_records": request_usage_records,
        "request_usage_record_count": len(request_usage_records),
        "assistant_usage_records": assistant_usage_records,
        "unique_assistant_usage_record_count": len(assistant_usage_records),
        "knowledge_injection": injection_stats,
        "messages_error": messages_error,
        "server": {
            "url": getattr(server_handle, "url", None),
            "pid": getattr(server_handle, "pid", None),
            "log_path": getattr(server_handle, "log_path", None),
        },
        "result": result,
    }


def _turn_error(metrics: dict[str, Any]) -> str | None:
    result = metrics.get("result") if isinstance(metrics.get("result"), dict) else {}
    error = result.get("error")
    if error is None:
        return None
    return json.dumps(error, ensure_ascii=False, sort_keys=True)


def _empty_assistant_error(
    *,
    assistant_info: dict[str, Any],
    assistant_parts: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    usage: dict[str, int],
) -> dict[str, Any] | None:
    text = "\n".join(
        str(part.get("text") or "").strip()
        for part in assistant_parts
        if part.get("type") == "text" and str(part.get("text") or "").strip()
    ).strip()
    if text or _assistant_has_tool_part(assistant_parts):
        return None
    if int(usage.get("total_tokens") or 0) > 0 or int(usage.get("output_tokens") or 0) > 0:
        return None
    return {
        "type": "opencode_empty_assistant_response",
        "message": "OpenCode returned an empty assistant response with zero token usage",
        "finish": assistant_info.get("finish"),
        "message_id": assistant_info.get("id"),
    }


def _assistant_has_tool_part(parts: list[dict[str, Any]]) -> bool:
    return any(part.get("type") == "tool" for part in parts)


def _contains_flowark_context(text: Any) -> bool:
    value = str(text or "")
    return any(marker in value for marker in FLOWARK_CONTEXT_MARKERS)


def _extract_flowark_context_blocks(text: Any) -> list[str]:
    value = str(text or "")
    if not _contains_flowark_context(value):
        return []
    blocks = [match.group(0) for match in FLOWARK_CONTEXT_BLOCK_RE.finditer(value)]
    return blocks or [value]


def _part_flowark_context_blocks(part: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    blocks.extend(_extract_flowark_context_blocks(part.get("text")))
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    output = state.get("output")
    if isinstance(output, dict):
        blocks.extend(_extract_flowark_context_blocks(output.get("output")))
    elif output is not None:
        blocks.extend(_extract_flowark_context_blocks(output))
    return blocks


def _part_is_synthetic_flowark_user_context(part: dict[str, Any]) -> bool:
    metadata = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
    if bool(metadata.get("flowark_context_message")):
        return True
    text = str(part.get("text") or "").strip()
    return text.startswith("<flowark-runtime-context")


def _knowledge_injection_stats(messages: list[dict[str, Any]]) -> dict[str, int]:
    injection_count = 0
    injected_chars = 0
    synthetic_user_messages = 0
    for message in messages:
        info = _message_info(message)
        message_has_synthetic_context = False
        for part in _message_parts(message):
            blocks = _part_flowark_context_blocks(part)
            if not blocks:
                continue
            injection_count += len(blocks)
            injected_chars += sum(len(block) for block in blocks)
            if _part_is_synthetic_flowark_user_context(part):
                message_has_synthetic_context = True
        if info.get("role") == "user" and message_has_synthetic_context:
            synthetic_user_messages += 1
    return {
        "knowledge_injection_count": injection_count,
        "knowledge_injected_chars": injected_chars,
        "synthetic_flowark_user_message_count": synthetic_user_messages,
    }


def _empty_bash_stats() -> dict[str, int]:
    return {
        "bash_count": 0,
        "bash_code_context_count": 0,
        "bash_trace_only_count": 0,
        "bash_blocked_count": 0,
    }


def _increment_bash_kind(stats: dict[str, int], kind: str | None) -> None:
    if kind == BASH_KIND_CODE_CONTEXT:
        stats["bash_code_context_count"] += 1
    elif kind == BASH_KIND_BLOCKED:
        stats["bash_blocked_count"] += 1
    else:
        stats["bash_trace_only_count"] += 1


def _bash_kind_from_tool_call(call: dict[str, Any]) -> str:
    metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
    output = call.get("output") if isinstance(call.get("output"), dict) else {}
    output_metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
    flowark_metadata = metadata.get("flowark") if isinstance(metadata.get("flowark"), dict) else {}
    if not flowark_metadata:
        flowark_metadata = output_metadata.get("flowark") if isinstance(output_metadata.get("flowark"), dict) else {}
    kind = str(flowark_metadata.get("bash_kind") or "").strip()
    if kind:
        return kind
    tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
    return classify_bash_command(
        tool_input.get("command"),
        workdir=tool_input.get("workdir"),
        description=tool_input.get("description"),
    ).kind


def _bash_stats_from_tool_calls(tool_calls: Any) -> dict[str, int]:
    stats = _empty_bash_stats()
    for call in [item for item in (tool_calls or []) if isinstance(item, dict)]:
        if str(call.get("tool") or "").strip() != "bash":
            continue
        stats["bash_count"] += 1
        _increment_bash_kind(stats, _bash_kind_from_tool_call(call))
    return stats


def _bash_stats_from_hook_trace(path: Path | None) -> dict[str, int]:
    stats = _empty_bash_stats()
    if path is None or not path.exists():
        return stats
    seen_executed: set[str] = set()
    seen_blocked: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return stats
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or str(record.get("tool") or "").strip() != "bash":
            continue
        if record.get("phase") != "completed":
            continue
        kind = str(record.get("bash_kind") or "").strip()
        action = str(record.get("bash_policy_action") or "").strip()
        event = str(record.get("event") or "").strip()
        call_key = str(record.get("tool_call_id") or record.get("call_id") or "").strip()
        if event == "tool.execute.before" and (kind == BASH_KIND_BLOCKED or action == "blocked"):
            key = call_key or f"blocked-{record.get('command_excerpt')}-{index}"
            if key not in seen_blocked:
                seen_blocked.add(key)
                stats["bash_blocked_count"] += 1
            continue
        if event != "tool.execute.after" or not kind:
            continue
        key = call_key or f"executed-{record.get('command_excerpt')}-{index}"
        if key in seen_executed:
            continue
        seen_executed.add(key)
        _increment_bash_kind(stats, kind)
    executed = stats["bash_code_context_count"] + stats["bash_trace_only_count"]
    stats["bash_count"] = executed + stats["bash_blocked_count"]
    return stats


def _merge_bash_stats(*items: dict[str, int] | None) -> dict[str, int]:
    merged = _empty_bash_stats()
    executed_count = 0
    blocked_count = 0
    code_context_count = 0
    trace_only_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        code_context_count = max(code_context_count, int(_as_int(item.get("bash_code_context_count")) or 0))
        trace_only_count = max(trace_only_count, int(_as_int(item.get("bash_trace_only_count")) or 0))
        blocked_count = max(blocked_count, int(_as_int(item.get("bash_blocked_count")) or 0))
        count = int(_as_int(item.get("bash_count")) or 0)
        if count:
            executed_count = max(executed_count, max(0, count - int(_as_int(item.get("bash_blocked_count")) or 0)))
    merged["bash_code_context_count"] = code_context_count
    merged["bash_trace_only_count"] = trace_only_count
    merged["bash_blocked_count"] = blocked_count
    merged["bash_count"] = max(executed_count, code_context_count + trace_only_count) + blocked_count
    return merged


def _step_finish_cost(parts: list[dict[str, Any]]) -> float | None:
    for part in reversed(parts):
        if part.get("type") != "step-finish":
            continue
        cost = _as_float(part.get("cost"))
        if cost is not None:
            return cost
    return None


def _assistant_usage_records(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        info = _message_info(message)
        if info.get("role") != "assistant":
            continue
        message_id = str(info.get("id") or "").strip()
        dedupe_key = message_id or f"assistant-index-{len(seen)}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        parts = _message_parts(message)
        usage = _token_usage(info, parts)
        cost = _as_float(info.get("cost"))
        if cost is None:
            cost = _step_finish_cost(parts)
        if not any(int(value or 0) for value in usage.values()) and cost is None:
            continue
        records.append(
            {
                "message_id": message_id or None,
                "provider_id": info.get("providerID"),
                "model_id": info.get("modelID"),
                "agent": info.get("agent"),
                "usage": usage,
                "cost_usd": cost,
            }
        )
    return records


def _request_usage_records(
    messages: list[dict[str, Any]],
    *,
    turn_index: int | None,
    turn_name: str,
    current_phase: str,
    session_id: str,
    runtime: OpenCodeRuntime,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message_index, message in enumerate(messages):
        info = _message_info(message)
        if info.get("role") != "assistant":
            continue
        message_id = str(info.get("id") or "").strip()
        dedupe_key = message_id or f"assistant-index-{message_index}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        parts = _message_parts(message)
        step_records: list[dict[str, Any]] = []
        for part_index, part in enumerate(parts):
            if part.get("type") != "step-finish" or not isinstance(part.get("tokens"), dict):
                continue
            usage = _token_usage({"tokens": part.get("tokens")}, [])
            cost = _as_float(part.get("cost"))
            if not any(int(value or 0) for value in usage.values()) and cost is None:
                continue
            step_records.append(
                {
                    "turn_index": turn_index,
                    "turn_name": turn_name,
                    "phase": current_phase,
                    "session_id": session_id,
                    "provider": info.get("providerID") or runtime.provider_id,
                    "model": info.get("modelID") or runtime.model,
                    "message_id": message_id or None,
                    "step_index": part_index,
                    "source": "step_finish",
                    "usage": usage,
                    "cost_usd": cost,
                    "prompt_total": _prompt_total_from_usage(usage),
                }
            )
        if step_records:
            records.extend(step_records)
            continue
        usage = _token_usage(info, parts)
        cost = _as_float(info.get("cost"))
        if cost is None:
            cost = _step_finish_cost(parts)
        if not any(int(value or 0) for value in usage.values()) and cost is None:
            continue
        records.append(
            {
                "turn_index": turn_index,
                "turn_name": turn_name,
                "phase": current_phase,
                "session_id": session_id,
                "provider": info.get("providerID") or runtime.provider_id,
                "model": info.get("modelID") or runtime.model,
                "message_id": message_id or None,
                "step_index": None,
                "source": "fallback_assistant_usage_record",
                "usage": usage,
                "cost_usd": cost,
                "prompt_total": _prompt_total_from_usage(usage),
            }
        )
    return records


def _aggregate_usage_records(records: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    usage = {key: 0 for key in keys}
    for record in records:
        record_usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        for key in keys:
            usage[key] += int(_as_int(record_usage.get(key)) or 0)
    return usage


def _aggregate_cost_records(records: list[dict[str, Any]]) -> float | None:
    values = [_as_float(record.get("cost_usd")) for record in records]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return float(sum(values))


def _write_turn_artifacts(
    *,
    run_dir: Path | None,
    turn_name: str,
    turn_index: int,
    current_phase: str,
    session_id: str,
    messages: list[dict[str, Any]],
    metrics: dict[str, Any],
    runtime: OpenCodeRuntime,
    server_handle: Any,
    messages_error: str | None,
    json_parse_mode: str,
    native_structured_output: bool,
    structured_output_requested: bool,
    structured_output_delivered: bool,
    structured_output_error: str | None,
) -> None:
    if run_dir is None:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    normalized_messages = _normalize_messages(messages)
    tool_summary = _tool_summary(metrics.get("tool_calls") if isinstance(metrics, dict) else [])
    assistant_usage_records = _assistant_usage_records(messages)
    request_usage_records = (
        metrics.get("request_usage_records")
        if isinstance(metrics, dict) and isinstance(metrics.get("request_usage_records"), list)
        else _request_usage_records(
            messages,
            turn_index=turn_index,
            turn_name=turn_name,
            current_phase=current_phase,
            session_id=session_id,
            runtime=runtime,
        )
    )
    for record in request_usage_records:
        if isinstance(record, dict):
            record["turn_index"] = turn_index
    injection_stats = _knowledge_injection_stats(messages)
    metrics_bash = metrics.get("bash") if isinstance(metrics, dict) and isinstance(metrics.get("bash"), dict) else None
    hook_trace_path = runtime.hook_trace_path if current_phase == Phase.ANALYSIS.value else None
    bash_stats = _merge_bash_stats(
        metrics_bash,
        _bash_stats_from_tool_calls(metrics.get("tool_calls") if isinstance(metrics, dict) else []),
        _bash_stats_from_hook_trace(hook_trace_path),
    )
    tool_summary["bash"] = bash_stats
    usage_payload = {
        "adapter": "opencode",
        "turn_index": turn_index,
        "turn_name": turn_name,
        "current_phase": current_phase,
        "session_id": session_id,
        "provider": runtime.provider_id,
        "model": runtime.model,
        "after_tool_delivery": runtime.after_tool_delivery,
        "post_phase_mode": runtime.post_phase_mode,
        "json_parse_mode": json_parse_mode,
        "native_structured_output": bool(native_structured_output),
        "native_structured_output_enabled": False,
        "native_structured_output_requested": False,
        "native_structured_output_delivered": False,
        "legacy_structured_output_configured": runtime.structured_output_enabled,
        "structured_output_enabled": False,
        "structured_output_requested": bool(structured_output_requested),
        "structured_output_delivered": bool(structured_output_delivered),
        "structured_output_error": structured_output_error,
        "runtime_plugin_active": runtime.runtime_plugin_active,
        "post_tool_delivery_active": runtime.runtime_plugin_active,
        "hook_trace_path": str(runtime.hook_trace_path),
        "server": {
            "url": getattr(server_handle, "url", None),
            "pid": getattr(server_handle, "pid", None),
            "log_path": getattr(server_handle, "log_path", None),
        },
        "messages_error": messages_error,
        "message_scope": metrics.get("message_scope") if isinstance(metrics, dict) else None,
        "raw_session_message_count": (
            metrics.get("raw_session_message_count") if isinstance(metrics, dict) else None
        ),
        "previous_session_message_count": (
            metrics.get("previous_session_message_count") if isinstance(metrics, dict) else None
        ),
        "post_phase_tool_call_violation": (
            bool(metrics.get("post_phase_tool_call_violation")) if isinstance(metrics, dict) else False
        ),
        "assistant_usage_records": assistant_usage_records,
        "unique_assistant_usage_record_count": len(assistant_usage_records),
        "request_usage_records": request_usage_records,
        "request_usage_record_count": len(request_usage_records),
        "knowledge_injection_count": injection_stats["knowledge_injection_count"],
        "knowledge_injected_chars": injection_stats["knowledge_injected_chars"],
        "synthetic_flowark_user_message_count": injection_stats["synthetic_flowark_user_message_count"],
        "bash": bash_stats,
        **bash_stats,
        "result": metrics.get("result") if isinstance(metrics, dict) else {},
    }
    _write_json(run_dir / "opencode_messages.json", normalized_messages)
    _write_json(run_dir / "opencode_tool_summary.json", tool_summary)
    _write_json(run_dir / "opencode_usage.json", usage_payload)
    _write_json(run_dir / "opencode_isolation_summary.json", runtime.isolation_summary())
    _write_json(run_dir / "opencode_disabled_flags.json", runtime.disabled_flags())
    events = _events_from_messages(messages, turn_name=turn_name, session_id=session_id)
    with (run_dir / "opencode_events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    turn_dir = run_dir / "opencode_turns" / f"{turn_index:02d}-{_safe_artifact_slug(turn_name)}"
    turn_dir.mkdir(parents=True, exist_ok=True)
    _write_json(turn_dir / "messages.json", normalized_messages)
    _write_json(turn_dir / "tool_summary.json", tool_summary)
    _write_json(turn_dir / "usage.json", usage_payload)
    with (turn_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    _update_turn_artifact_index(
        run_dir=run_dir,
        turn_dir=turn_dir,
        usage_payload=usage_payload,
        metrics=metrics,
    )


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        info = _message_info(message)
        parts = []
        for part_index, part in enumerate(_message_parts(message)):
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            entry: dict[str, Any] = {
                "index": part_index,
                "type": part.get("type"),
            }
            if part.get("type") == "text":
                entry["text"] = part.get("text")
                if "synthetic" in part:
                    entry["synthetic"] = bool(part.get("synthetic"))
                if "ignored" in part:
                    entry["ignored"] = bool(part.get("ignored"))
                if isinstance(part.get("metadata"), dict):
                    entry["metadata"] = part.get("metadata")
            elif part.get("type") == "tool":
                entry.update(
                    {
                        "tool": part.get("tool"),
                        "call_id": part.get("callID"),
                        "status": state.get("status"),
                        "title": state.get("title"),
                        "metadata": state.get("metadata") if isinstance(state.get("metadata"), dict) else {},
                    }
                )
                if "input" in state:
                    entry["input"] = state.get("input")
                if "output" in state:
                    entry["output"] = state.get("output")
            elif part.get("type") == "step-finish":
                entry.update(
                    {
                        "reason": part.get("reason"),
                        "cost": part.get("cost"),
                        "tokens": part.get("tokens") if isinstance(part.get("tokens"), dict) else {},
                    }
                )
            else:
                entry["raw"] = part
            parts.append(entry)
        result.append(
            {
                "index": index,
                "id": info.get("id"),
                "role": info.get("role"),
                "session_id": info.get("sessionID") or info.get("session_id"),
                "provider_id": info.get("providerID"),
                "model_id": info.get("modelID"),
                "agent": info.get("agent"),
                "cost": info.get("cost"),
                "tokens": info.get("tokens") if isinstance(info.get("tokens"), dict) else {},
                "structured": info.get("structured") if "structured" in info else None,
                "error": info.get("error"),
                "time": info.get("time") if isinstance(info.get("time"), dict) else {},
                "part_count": len(parts),
                "parts": parts,
            }
        )
    return result


def _events_from_messages(
    messages: list[dict[str, Any]],
    *,
    turn_name: str,
    session_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        info = _message_info(message)
        message_id = info.get("id")
        role = info.get("role")
        events.append(
            {
                "event": "message",
                "turn_name": turn_name,
                "session_id": session_id,
                "message_index": message_index,
                "message_id": message_id,
                "role": role,
                "part_count": len(_message_parts(message)),
            }
        )
        for part_index, part in enumerate(_message_parts(message)):
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            events.append(
                {
                    "event": "part",
                    "turn_name": turn_name,
                    "session_id": session_id,
                    "message_index": message_index,
                    "message_id": message_id,
                    "role": role,
                    "part_index": part_index,
                    "part_type": part.get("type"),
                    "tool": part.get("tool"),
                    "call_id": part.get("callID"),
                    "status": state.get("status"),
                    "title": state.get("title"),
                }
            )
    return events


def _tool_summary(tool_calls: Any) -> dict[str, Any]:
    calls = [item for item in (tool_calls or []) if isinstance(item, dict)]
    by_tool = Counter(str(item.get("tool") or "unknown") for item in calls)
    by_status = Counter(str(item.get("status") or "unknown") for item in calls)
    return {
        "total": len(calls),
        "by_tool": dict(sorted(by_tool.items())),
        "by_status": dict(sorted(by_status.items())),
        "calls": calls,
    }


def _safe_artifact_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "turn").strip()).strip(".-")
    return text[:80] or "turn"


def _update_turn_artifact_index(
    *,
    run_dir: Path,
    turn_dir: Path,
    usage_payload: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    turns_root = run_dir / "opencode_turns"
    turns_root.mkdir(parents=True, exist_ok=True)
    index_path = turns_root / "index.json"
    existing = _read_json_object(index_path)
    entries = existing.get("turns") if isinstance(existing.get("turns"), list) else []
    turn_index = int(_as_int(usage_payload.get("turn_index")) or 0)
    entry = {
        "turn_index": turn_index,
        "turn_name": usage_payload.get("turn_name"),
        "current_phase": usage_payload.get("current_phase"),
        "session_id": usage_payload.get("session_id"),
        "dir": str(turn_dir),
        "message_scope": usage_payload.get("message_scope"),
        "raw_session_message_count": usage_payload.get("raw_session_message_count"),
        "previous_session_message_count": usage_payload.get("previous_session_message_count"),
        "post_phase_mode": usage_payload.get("post_phase_mode"),
        "json_parse_mode": usage_payload.get("json_parse_mode"),
        "native_structured_output": usage_payload.get("native_structured_output"),
        "native_structured_output_requested": usage_payload.get("native_structured_output_requested"),
        "native_structured_output_delivered": usage_payload.get("native_structured_output_delivered"),
        "structured_output_requested": usage_payload.get("structured_output_requested"),
        "structured_output_delivered": usage_payload.get("structured_output_delivered"),
        "structured_output_error": usage_payload.get("structured_output_error"),
        "post_phase_tool_call_violation": usage_payload.get("post_phase_tool_call_violation"),
        "tool_use_block_count": metrics.get("tool_use_block_count") if isinstance(metrics, dict) else None,
        "assistant_usage_record_count": usage_payload.get("unique_assistant_usage_record_count"),
        "request_usage_record_count": usage_payload.get("request_usage_record_count"),
        "request_usage_records": usage_payload.get("request_usage_records")
        if isinstance(usage_payload.get("request_usage_records"), list)
        else [],
        "knowledge_injection_count": usage_payload.get("knowledge_injection_count"),
        "knowledge_injected_chars": usage_payload.get("knowledge_injected_chars"),
        "synthetic_flowark_user_message_count": usage_payload.get("synthetic_flowark_user_message_count"),
        "bash": usage_payload.get("bash") if isinstance(usage_payload.get("bash"), dict) else _empty_bash_stats(),
        "bash_count": usage_payload.get("bash_count"),
        "bash_code_context_count": usage_payload.get("bash_code_context_count"),
        "bash_trace_only_count": usage_payload.get("bash_trace_only_count"),
        "bash_blocked_count": usage_payload.get("bash_blocked_count"),
        "result": usage_payload.get("result") if isinstance(usage_payload.get("result"), dict) else {},
    }
    replaced = False
    updated_entries: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        if int(_as_int(item.get("turn_index")) or -1) == turn_index:
            updated_entries.append(entry)
            replaced = True
        else:
            updated_entries.append(item)
    if not replaced:
        updated_entries.append(entry)
    updated_entries.sort(key=lambda item: int(_as_int(item.get("turn_index")) or 0))
    index_payload = {
        "adapter": "opencode",
        "turn_count": len(updated_entries),
        "turns": updated_entries,
    }
    _write_json(index_path, index_payload)
    _write_json(run_dir / "opencode_usage_rollup.json", _build_usage_rollup(updated_entries))


def _build_usage_rollup(entries: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost_values: list[float] = []
    assistant_record_count = 0
    request_record_count = 0
    request_usage_records: list[dict[str, Any]] = []
    bash_rollup = _empty_bash_stats()
    for entry in entries:
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        result_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        for key in usage:
            usage[key] += int(_as_int(result_usage.get(key)) or 0)
        cost = _as_float(result.get("total_cost_usd"))
        if cost is not None:
            cost_values.append(cost)
        assistant_record_count += int(_as_int(entry.get("assistant_usage_record_count")) or 0)
        entry_request_records = (
            entry.get("request_usage_records") if isinstance(entry.get("request_usage_records"), list) else []
        )
        request_record_count += int(_as_int(entry.get("request_usage_record_count")) or len(entry_request_records))
        request_usage_records.extend(item for item in entry_request_records if isinstance(item, dict))
        entry_bash = entry.get("bash") if isinstance(entry.get("bash"), dict) else entry
        for key in bash_rollup:
            bash_rollup[key] += int(_as_int(entry_bash.get(key)) or 0)
    return {
        "adapter": "opencode",
        "turn_count": len(entries),
        "unique_assistant_usage_record_count": assistant_record_count,
        "request_usage_record_count": request_record_count,
        "request_usage_records": request_usage_records,
        "usage": usage,
        "total_cost_usd": float(sum(cost_values)) if cost_values else None,
        "bash": bash_rollup,
        **bash_rollup,
        "turns": entries,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _format_artifact_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = ["OpenCodeAdapter", "OpenCodeRunSession"]
