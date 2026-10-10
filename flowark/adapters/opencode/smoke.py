"""Explicit OpenCode smoke helpers.

The default smoke starts OpenCode and exercises HTTP/session setup only. It
does not send a model prompt unless the caller opts in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from flowark.runtime.config import RunConfig
from flowark.timeutil import timestamp_slug_tz8
from flowark.types import AnalysisRequest, RunArtifacts

from .client import OpenCodeHttpClient
from .server import OpenCodeServerProcess
from .settings import (
    DEFAULT_AFTER_TOOL_DELIVERY,
    DEFAULT_BASH_POLICY,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_OPENCODE_PROVIDER,
    DEFAULT_POST_PHASE_MODE,
)
from .settings import OpenCodeRuntime, build_opencode_runtime


ServerFactory = Callable[..., Any]
ClientFactory = Callable[..., Any]
RunnerFactory = Callable[[RunConfig], Any]
DEFAULT_REAL_SMOKE_DELIVERIES = ("no_reply_context", "tool_output_append")


async def run_opencode_server_smoke(
    *,
    config: RunConfig,
    workspace_root: Path,
    run_dir: Path,
    send_prompt: bool = False,
    prompt: str = "Summarize the current working directory in one sentence.",
    server_factory: ServerFactory | None = None,
    client_factory: ClientFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime = build_opencode_runtime(
            config=config,
            workspace_root=workspace_root,
            run_dir=run_dir,
            environ=environ,
        )
        server_cm = (server_factory or OpenCodeServerProcess)(runtime=runtime)
        prompt_response: dict[str, Any] | None = None
        messages: list[dict[str, Any]] = []
        async with server_cm as server_handle:
            client = (client_factory or OpenCodeHttpClient)(
                base_url=server_handle.url,
                directory=str(config.cwd),
            )
            health = await _maybe_call_dict(client, "health")
            paths = await _maybe_call_dict(client, "paths")
            session = await client.create_session(title="FlowArk OpenCode smoke")
            session_id = _extract_smoke_session_id(session)
            if send_prompt:
                prompt_response = await client.prompt(
                    session_id=session_id,
                    text=prompt,
                    provider_id=runtime.provider_id,
                    model_id=runtime.model_id,
                    tools=runtime.tool_policy,
                    agent="build",
                    no_reply=False,
                )
                messages = await client.messages(session_id=session_id)
            payload = _smoke_payload(
                runtime=runtime,
                server_handle=server_handle,
                health=health,
                paths=paths,
                session=session,
                session_id=session_id,
                send_prompt=send_prompt,
                prompt_response=prompt_response,
                messages=messages,
            )
            prompt_error = _prompt_completion_error(prompt_response=prompt_response, messages=messages)
            if prompt_error is not None:
                payload["ok"] = False
                payload["error_type"] = "OpenCodeEmptyAssistantResponse"
                payload["error"] = prompt_error
                _write_smoke_payload(run_dir / "opencode_smoke.json", payload)
                raise RuntimeError(prompt_error)
        payload["server_closed"] = True
        _write_smoke_payload(run_dir / "opencode_smoke.json", payload)
        return payload
    except Exception as exc:
        smoke_path = run_dir / "opencode_smoke.json"
        if not smoke_path.exists():
            failure = {
                "ok": False,
                "adapter": "opencode",
                "send_prompt": send_prompt,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            _write_smoke_payload(smoke_path, failure)
        raise


def _write_smoke_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def run_opencode_delivery_real_smoke(
    *,
    base_config: RunConfig,
    workspace_root: Path,
    out_dir: Path,
    deliveries: Sequence[str] | None = None,
    runner_factory: RunnerFactory | None = None,
) -> dict[str, Any]:
    """Run a tiny real OpenCode warm-reuse smoke for each delivery surface."""

    from flowark.runtime.runner import FlowArkRunner

    selected_deliveries = _normalize_deliveries(deliveries)
    smoke_root = (out_dir / f"opencode-real-smoke-{timestamp_slug_tz8()}-{uuid4().hex[:8]}").expanduser().resolve()
    smoke_root.mkdir(parents=True, exist_ok=True)
    fixture_dir, skills_dir = _write_real_smoke_fixture(smoke_root)

    results: list[dict[str, Any]] = []
    for delivery in selected_deliveries:
        run_out_dir = smoke_root / delivery / "runs"
        config = _real_smoke_run_config(
            base_config=base_config,
            cwd=fixture_dir,
            out_dir=run_out_dir,
            skills_dir=skills_dir,
            delivery=delivery,
        )
        runner = (runner_factory or FlowArkRunner)(config)
        result = await _run_one_real_smoke_delivery(
            runner=runner,
            delivery=delivery,
            run_out_dir=run_out_dir,
        )
        results.append(result)

    payload = _build_delivery_comparison_payload(
        smoke_root=smoke_root,
        fixture_dir=fixture_dir,
        skills_dir=skills_dir,
        base_config=base_config,
        deliveries=results,
    )
    _write_smoke_payload(smoke_root / "opencode_delivery_comparison.json", payload)
    (smoke_root / "opencode_delivery_comparison.md").write_text(
        _format_delivery_comparison_markdown(payload),
        encoding="utf-8",
    )
    return payload


def _normalize_deliveries(deliveries: Sequence[str] | None) -> tuple[str, ...]:
    raw = deliveries or DEFAULT_REAL_SMOKE_DELIVERIES
    result: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value not in DEFAULT_REAL_SMOKE_DELIVERIES:
            raise ValueError(f"unsupported OpenCode delivery smoke mode: {value!r}")
        if value not in result:
            result.append(value)
    return tuple(result or DEFAULT_REAL_SMOKE_DELIVERIES)


def _write_real_smoke_fixture(smoke_root: Path) -> tuple[Path, Path]:
    fixture_dir = smoke_root / "fixture"
    src_dir = fixture_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "AppFlow.kt").write_text(
        """package demo.opencode.smoke

class TelemetryController {
    fun collectTelemetry(token: String) {
        val packet = SensitiveForwarder().wrap(token)
        NetworkSink.send(packet)
    }
}

class SensitiveForwarder {
    fun wrap(value: String): String = "device=$value"
}

object NetworkSink {
    fun send(payload: String) {
        println("network:$payload")
    }
}
""",
        encoding="utf-8",
    )
    (fixture_dir / "README.md").write_text(
        "OpenCode real smoke fixture. Analyze collectTelemetry to NetworkSink.send.\n",
        encoding="utf-8",
    )

    skills_dir = smoke_root / "knowledge_scope" / "skills" / "opencode_smoke_app"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "sensitive-forwarder-note.md").write_text(
        """---
schema_version: flowark-skill-v5
id: sensitive-forwarder-note
app_name: opencode_smoke_app
name: sensitive-forwarder-note
validation_status: validated
match_rules:
  require_any:
    - kind: exact_symbol
      value: SensitiveForwarder
  require_all:
    - kind: exact_symbol
      value: NetworkSink.send
  exclude: []
entry_condition: "当前分析看到 SensitiveForwarder 时适用。"
---

### 摘要

SensitiveForwarder 是 OpenCode smoke fixture 中的稳定中转点。命中它后，不需要展开字符串包装细节，优先继续确认 `NetworkSink.send` 是否是网络 sink 终点。
""",
        encoding="utf-8",
    )
    return fixture_dir, smoke_root / "knowledge_scope" / "skills"


def _real_smoke_run_config(
    *,
    base_config: RunConfig,
    cwd: Path,
    out_dir: Path,
    skills_dir: Path,
    delivery: str,
) -> RunConfig:
    return RunConfig(
        cwd=cwd,
        out_dir=out_dir,
        agent_adapter="opencode",
        opencode_binary=base_config.opencode_binary,
        opencode_model=base_config.opencode_model or DEFAULT_OPENCODE_MODEL,
        opencode_provider=base_config.opencode_provider or DEFAULT_OPENCODE_PROVIDER,
        opencode_after_tool_delivery=delivery,
        opencode_bash_policy=base_config.opencode_bash_policy or DEFAULT_BASH_POLICY,
        opencode_post_phase_mode=getattr(base_config, "opencode_post_phase_mode", DEFAULT_POST_PHASE_MODE)
        or DEFAULT_POST_PHASE_MODE,
        opencode_structured_output=bool(base_config.opencode_structured_output),
        agent_mode="flowark",
        knowledge_mode="warm",
        auto_knowledge_cycle=False,
        knowledge_reuse_digest_mode="off",
        skills_dir=skills_dir,
    )


async def _run_one_real_smoke_delivery(
    *,
    runner: Any,
    delivery: str,
    run_out_dir: Path,
) -> dict[str, Any]:
    request = AnalysisRequest(
        query=(
            "Analyze the data flow from collectTelemetry input to any network sink. "
            "Use local code search/read before the final answer, and cite the file evidence."
        ),
        source="collectTelemetry entrypoint",
        sink_types=["network"],
        app_name="opencode_smoke_app",
    )
    started_ok = False
    artifacts: RunArtifacts | None = None
    error: dict[str, str] | None = None
    try:
        artifacts = await runner.run_query(request)
        started_ok = True
    except Exception as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    run_dir = artifacts.run_dir if artifacts is not None else _latest_run_dir(run_out_dir)
    summary = _summarize_real_smoke_run(
        delivery=delivery,
        run_dir=run_dir,
        error=error,
    )
    summary["ok"] = bool(started_ok and not summary.get("failed_acceptance"))
    return summary


def _latest_run_dir(runs_parent: Path) -> Path | None:
    if not runs_parent.exists():
        return None
    dirs = [path for path in runs_parent.iterdir() if path.is_dir()]
    if not dirs:
        return None
    return sorted(dirs, key=lambda item: item.stat().st_mtime)[-1]


def _summarize_real_smoke_run(
    *,
    delivery: str,
    run_dir: Path | None,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    cost_summary = _read_json_object(run_dir / "cost_summary.json") if run_dir else {}
    usage_rollup = _read_json_object(run_dir / "opencode_usage_rollup.json") if run_dir else {}
    turn_index = _read_json_object(run_dir / "opencode_turns" / "index.json") if run_dir else {}
    hook_trace = _read_jsonl(run_dir / "opencode_hook_trace.jsonl") if run_dir else []
    injection_records = _read_jsonl(run_dir / "knowledge_injection.jsonl") if run_dir else []
    transcript_text = _read_text(run_dir / "raw_transcript.txt") if run_dir else ""
    usage = usage_rollup.get("usage") if isinstance(usage_rollup.get("usage"), dict) else {}
    bash = usage_rollup.get("bash") if isinstance(usage_rollup.get("bash"), dict) else {}
    main_agent = {}
    agg = cost_summary.get("aggregated_metrics") if isinstance(cost_summary.get("aggregated_metrics"), dict) else {}
    if isinstance(agg.get("main_agent"), dict):
        main_agent = agg["main_agent"]
    structured = _structured_summary(turn_index)
    context_visibility = _context_visibility(run_dir, transcript_text)
    failure_text = _failure_text(error=error, cost_summary=cost_summary, transcript_text=transcript_text, hook_trace=hook_trace)
    backend_limited = _looks_rate_limited(failure_text)
    environment_error = _looks_environment_error(failure_text)
    summary = {
        "delivery": delivery,
        "run_dir": str(run_dir) if run_dir else None,
        "ok": False,
        "failed_acceptance": False,
        "acceptance_failures": [],
        "backend_rate_limited": backend_limited,
        "environment_error": environment_error,
        "error": error,
        "failure_summary": failure_text[:2000] if failure_text else None,
        "assistant_turns": _safe_int(main_agent.get("num_turns_sum")) or _safe_int(usage_rollup.get("turn_count")),
        "tool_calls": _safe_int(cost_summary.get("tool_use_block_count_total")),
        "injection_count": len(injection_records),
        "hook_delivery_count": _count_hook_deliveries(hook_trace),
        "hook_error_count": _count_hook_errors(hook_trace),
        "request_submit_delivery_count": _count_hook_deliveries(hook_trace, event="chat.message"),
        "post_tool_delivery_count": _count_hook_deliveries(hook_trace, event="tool.execute.after"),
        "synthetic_flowark_user_message_count": _synthetic_context_count(usage_rollup),
        "context_visible_in_messages": context_visibility["messages"],
        "context_visible_in_transcript": context_visibility["transcript"],
        "json_parse_mode": "flowark_plain_json",
        "native_structured_output": False,
        "structured_output": structured,
        "usage": {
            "input_tokens": _safe_int(usage.get("input_tokens")),
            "output_tokens": _safe_int(usage.get("output_tokens")),
            "reasoning_tokens": _safe_int(usage.get("reasoning_tokens")),
            "total_tokens": _safe_int(usage.get("total_tokens")),
            "cache_read_input_tokens": _safe_int(usage.get("cache_read_input_tokens")),
            "cache_creation_input_tokens": _safe_int(usage.get("cache_creation_input_tokens")),
        },
        "total_cost_usd": usage_rollup.get("total_cost_usd"),
        "bash": {
            "bash_count": _safe_int(bash.get("bash_count")) or 0,
            "bash_code_context_count": _safe_int(bash.get("bash_code_context_count")) or 0,
            "bash_trace_only_count": _safe_int(bash.get("bash_trace_only_count")) or 0,
            "bash_blocked_count": _safe_int(bash.get("bash_blocked_count")) or 0,
        },
        "artifacts": _real_smoke_artifact_paths(run_dir),
    }
    acceptance_failures = _real_smoke_acceptance_failures(
        summary,
    )
    summary["acceptance_failures"] = acceptance_failures
    summary["failed_acceptance"] = bool(error or acceptance_failures) and not backend_limited and not environment_error
    return summary


def _build_delivery_comparison_payload(
    *,
    smoke_root: Path,
    fixture_dir: Path,
    skills_dir: Path,
    base_config: RunConfig,
    deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    no_reply = next((item for item in deliveries if item.get("delivery") == "no_reply_context"), None)
    append = next((item for item in deliveries if item.get("delivery") == "tool_output_append"), None)
    recommendation = _delivery_recommendation(no_reply=no_reply, append=append)
    backend_limited = all(bool(item.get("backend_rate_limited")) for item in deliveries) if deliveries else False
    environment_error = all(bool(item.get("environment_error")) for item in deliveries) if deliveries else False
    return {
        "ok": all(bool(item.get("ok")) for item in deliveries) if deliveries else False,
        "adapter": "opencode",
        "smoke_root": str(smoke_root),
        "fixture_dir": str(fixture_dir),
        "skills_dir": str(skills_dir),
        "max_concurrency": 1,
        "provider": base_config.opencode_provider or DEFAULT_OPENCODE_PROVIDER,
        "model": base_config.opencode_model or DEFAULT_OPENCODE_MODEL,
        "post_phase_mode": "plain_json_same_surface",
        "json_parse_mode": "flowark_plain_json",
        "native_structured_output": False,
        "structured_output_requested": False,
        "deliveries": deliveries,
        "comparison": _delivery_delta(no_reply=no_reply, append=append),
        "recommended_default": recommendation,
        "backend_rate_limited": backend_limited,
        "environment_error": environment_error,
    }


def _delivery_recommendation(
    *,
    no_reply: dict[str, Any] | None,
    append: dict[str, Any] | None,
) -> dict[str, Any]:
    if no_reply is None:
        return {"delivery": "tool_output_append", "reason": "no_reply_context_not_run"}
    if bool(no_reply.get("environment_error")):
        return {"delivery": DEFAULT_AFTER_TOOL_DELIVERY, "reason": "environment_error_no_decision"}
    if bool(no_reply.get("backend_rate_limited")):
        return {"delivery": DEFAULT_AFTER_TOOL_DELIVERY, "reason": "backend_rate_limited_no_decision"}
    if not bool(no_reply.get("ok")):
        if append is not None and bool(append.get("ok")):
            return {"delivery": "tool_output_append", "reason": "no_reply_context_failed_acceptance"}
        return {"delivery": DEFAULT_AFTER_TOOL_DELIVERY, "reason": "no_reply_context_failed_acceptance_no_decision"}
    if append is not None and bool(append.get("ok")):
        no_reply_tokens = _usage_value(no_reply, "total_tokens")
        append_tokens = _usage_value(append, "total_tokens")
        if no_reply_tokens and append_tokens and no_reply_tokens > append_tokens * 1.25:
            return {"delivery": "tool_output_append", "reason": "no_reply_context_total_tokens_25pct_higher"}
    return {"delivery": "no_reply_context", "reason": "no_reply_context_smoke_stable"}


def _delivery_delta(
    *,
    no_reply: dict[str, Any] | None,
    append: dict[str, Any] | None,
) -> dict[str, Any]:
    if no_reply is None or append is None:
        return {}
    return {
        "total_tokens_delta_no_reply_minus_append": _usage_value(no_reply, "total_tokens")
        - _usage_value(append, "total_tokens"),
        "cache_read_delta_no_reply_minus_append": _usage_value(no_reply, "cache_read_input_tokens")
        - _usage_value(append, "cache_read_input_tokens"),
        "cache_write_delta_no_reply_minus_append": _usage_value(no_reply, "cache_creation_input_tokens")
        - _usage_value(append, "cache_creation_input_tokens"),
        "tool_calls_delta_no_reply_minus_append": int(_safe_int(no_reply.get("tool_calls")) or 0)
        - int(_safe_int(append.get("tool_calls")) or 0),
    }


def _usage_value(entry: dict[str, Any], key: str) -> int:
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    return int(_safe_int(usage.get(key)) or 0)


def _real_smoke_acceptance_failures(
    summary: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if int(_safe_int(summary.get("post_tool_delivery_count")) or 0) <= 0:
        failures.append("post_tool_delivery_missing")
    if int(_safe_int(summary.get("injection_count")) or 0) <= 0:
        failures.append("knowledge_injection_missing")
    if not bool(summary.get("context_visible_in_messages")):
        failures.append("runtime_context_not_visible_in_messages")
    return failures


def _synthetic_context_count(usage_rollup: dict[str, Any]) -> int:
    turns = usage_rollup.get("turns") if isinstance(usage_rollup.get("turns"), list) else []
    total = 0
    for item in turns:
        if isinstance(item, dict):
            total += int(_safe_int(item.get("synthetic_flowark_user_message_count")) or 0)
    return total


def _format_delivery_comparison_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# OpenCode Delivery Real Smoke",
        "",
        f"- provider/model: `{payload.get('provider')}/{payload.get('model')}`",
        f"- max_concurrency: `{payload.get('max_concurrency')}`",
        f"- recommended_default: `{(payload.get('recommended_default') or {}).get('delivery')}`",
        f"- reason: `{(payload.get('recommended_default') or {}).get('reason')}`",
        "",
        "| delivery | ok | rate_limited | post_tool | injections | total_tokens | cache_read | cache_write | tools | bash_code | structured | acceptance_failures |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in payload.get("deliveries") or []:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        structured = item.get("structured_output") if isinstance(item.get("structured_output"), dict) else {}
        lines.append(
            "| {delivery} | {ok} | {limited} | {post_tool} | {inj} | {total} | {cache_r} | {cache_w} | {tools} | {bash_code} | {structured} | {failures} |".format(
                delivery=item.get("delivery"),
                ok=item.get("ok"),
                limited=item.get("backend_rate_limited"),
                post_tool=item.get("post_tool_delivery_count"),
                inj=item.get("injection_count"),
                total=usage.get("total_tokens"),
                cache_r=usage.get("cache_read_input_tokens"),
                cache_w=usage.get("cache_creation_input_tokens"),
                tools=item.get("tool_calls"),
                bash_code=(item.get("bash") or {}).get("bash_code_context_count"),
                structured=structured.get("delivered"),
                failures=", ".join(str(value) for value in item.get("acceptance_failures") or []),
            )
        )
    lines.append("")
    lines.append("## Artifacts")
    for item in payload.get("deliveries") or []:
        lines.append(f"- `{item.get('delivery')}`: `{item.get('run_dir')}`")
    return "\n".join(lines) + "\n"


async def _maybe_call_dict(client: Any, method_name: str) -> dict[str, Any] | None:
    method = getattr(client, method_name, None)
    if not callable(method):
        return None
    payload = await method()
    return payload if isinstance(payload, dict) else {"value": payload}


def _extract_smoke_session_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "sessionID", "session_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            return _extract_smoke_session_id(data)
    raise ValueError(f"OpenCode smoke session response did not include a session id: {payload!r}")


def _smoke_payload(
    *,
    runtime: OpenCodeRuntime,
    server_handle: Any,
    health: dict[str, Any] | None,
    paths: dict[str, Any] | None,
    session: dict[str, Any],
    session_id: str,
    send_prompt: bool,
    prompt_response: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "adapter": "opencode",
        "send_prompt": send_prompt,
        "session_id": session_id,
        "session": session,
        "health": health,
        "paths": paths,
        "message_count": len(messages),
        "prompt_response_present": prompt_response is not None,
        "prompt_response": prompt_response,
        "messages": messages,
        "runtime": {
            **runtime.trace_payload(),
            "auth_content": "<redacted>",
            "config_content": "<redacted>",
        },
        "server": {
            "url": getattr(server_handle, "url", None),
            "pid": getattr(server_handle, "pid", None),
            "log_path": getattr(server_handle, "log_path", None),
            "command": list(getattr(server_handle, "command", []) or []),
        },
    }


def _prompt_completion_error(
    *,
    prompt_response: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> str | None:
    if prompt_response is None:
        return None
    assistant = _latest_assistant_message(messages) or prompt_response
    info = assistant.get("info") if isinstance(assistant.get("info"), dict) else assistant
    parts = assistant.get("parts") if isinstance(assistant.get("parts"), list) else []
    text = "\n".join(
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and str(part.get("text") or "").strip()
    ).strip()
    has_tool = any(isinstance(part, dict) and part.get("type") == "tool" for part in parts)
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    if not tokens:
        for part in reversed(parts):
            if isinstance(part, dict) and part.get("type") == "step-finish" and isinstance(part.get("tokens"), dict):
                tokens = part["tokens"]
                break
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    total_tokens = _as_int(tokens.get("total"))
    if total_tokens is None:
        total_tokens = (
            int(_as_int(tokens.get("input")) or 0)
            + int(_as_int(tokens.get("output")) or 0)
            + int(_as_int(tokens.get("reasoning")) or 0)
            + int(_as_int(cache.get("read")) or 0)
            + int(_as_int(cache.get("write")) or 0)
        )
    if text or has_tool or total_tokens > 0:
        return None
    finish = info.get("finish")
    message_id = info.get("id")
    return (
        "OpenCode prompt produced an empty assistant response with zero token usage "
        f"(finish={finish!r}, message_id={message_id!r})."
    )


def _latest_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        if isinstance(info, dict) and info.get("role") == "assistant":
            return message
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _safe_int(value: Any) -> int | None:
    return _as_int(value)


def _structured_summary(turn_index: dict[str, Any]) -> dict[str, Any]:
    turns = turn_index.get("turns") if isinstance(turn_index.get("turns"), list) else []
    post_turns = [
        item for item in turns
        if isinstance(item, dict) and str(item.get("current_phase") or "") == "final_report"
    ]
    requested = any(bool(item.get("structured_output_requested")) for item in post_turns)
    delivered = any(bool(item.get("structured_output_delivered")) for item in post_turns)
    errors = [
        str(item.get("structured_output_error") or "").strip()
        for item in post_turns
        if str(item.get("structured_output_error") or "").strip()
    ]
    return {
        "requested": requested,
        "delivered": delivered,
        "error": errors[-1] if errors else None,
    }


def _context_visibility(run_dir: Path | None, transcript_text: str) -> dict[str, bool]:
    messages_visible = False
    if run_dir is not None:
        for path in sorted((run_dir / "opencode_turns").glob("*/messages.json")):
            if "<flowark-runtime-context" in _read_text(path) or "<flowark-knowledge-injection" in _read_text(path):
                messages_visible = True
                break
        if not messages_visible:
            root_messages = run_dir / "opencode_messages.json"
            messages_visible = (
                "<flowark-runtime-context" in _read_text(root_messages)
                or "<flowark-knowledge-injection" in _read_text(root_messages)
            )
    return {
        "messages": messages_visible,
        "transcript": (
            "<flowark-runtime-context" in transcript_text
            or "<flowark-knowledge-injection" in transcript_text
        ),
    }


def _count_hook_deliveries(records: Iterable[dict[str, Any]], *, event: str | None = None) -> int:
    count = 0
    for record in records:
        if event is not None and str(record.get("event") or "") != event:
            continue
        if str(record.get("delivery_status") or "") == "delivered":
            count += 1
    return count


def _count_hook_errors(records: Iterable[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        status = str(record.get("delivery_status") or "").strip()
        if status in {"failed", "error"} or record.get("error"):
            count += 1
    return count


def _failure_text(
    *,
    error: dict[str, str] | None,
    cost_summary: dict[str, Any],
    transcript_text: str,
    hook_trace: list[dict[str, Any]],
) -> str:
    chunks: list[str] = []
    if error:
        chunks.append(f"{error.get('type')}: {error.get('message')}")
    agg = cost_summary.get("aggregated_metrics") if isinstance(cost_summary.get("aggregated_metrics"), dict) else {}
    main = agg.get("main_agent") if isinstance(agg.get("main_agent"), dict) else {}
    for key in ("error_phase_names", "terminal_error_phase_names"):
        value = main.get(key)
        if isinstance(value, list) and value:
            chunks.append(f"{key}: {value}")
    for record in hook_trace:
        if record.get("error"):
            chunks.append(str(record.get("error")))
    if transcript_text:
        for needle in ("429", "rate limit", "RateLimit", "StructuredOutputError", "ProviderError"):
            if needle in transcript_text:
                chunks.append(transcript_text[-2000:])
                break
    return "\n".join(chunk for chunk in chunks if str(chunk).strip()).strip()


def _looks_rate_limited(text: str) -> bool:
    value = str(text or "").casefold()
    return "429" in value or "rate limit" in value or "ratelimit" in value


def _looks_environment_error(text: str) -> bool:
    value = str(text or "").casefold()
    return (
        "requires anthropic_base_url" in value
        or "requires anthropic_auth_token" in value
        or "claude settings 源文件不存在" in value
    )


def _real_smoke_artifact_paths(run_dir: Path | None) -> dict[str, str]:
    if run_dir is None:
        return {}
    candidates = {
        "run_dir": run_dir,
        "raw_transcript": run_dir / "raw_transcript.txt",
        "cost_summary": run_dir / "cost_summary.json",
        "opencode_usage_rollup": run_dir / "opencode_usage_rollup.json",
        "opencode_messages": run_dir / "opencode_messages.json",
        "opencode_hook_trace": run_dir / "opencode_hook_trace.jsonl",
        "knowledge_injection": run_dir / "knowledge_injection.jsonl",
        "final_report": run_dir / "final_report.json",
    }
    return {key: str(path) for key, path in candidates.items() if path.exists()}


__all__ = [
    "DEFAULT_REAL_SMOKE_DELIVERIES",
    "run_opencode_delivery_real_smoke",
    "run_opencode_server_smoke",
]
