"""FlowArk thin runner for OpenCode-backed analysis."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
from pathlib import Path
import traceback
from typing import Any
from uuid import uuid4

from flowark.adapters.base import AgentAdapter, AgentRunSession
from flowark.adapters.registry import build_agent_adapter, normalize_agent_adapter_name
from flowark.anthropic_capture import ManagedAnthropicCaptureProxy, start_capture_proxy
from flowark.agent import get_runtime_agents
from flowark.anthropic_env import (
    describe_anthropic_env_conflicts,
)
from flowark.knowledge.analysis_log_rag import append_run_to_analysis_log_rag_corpus
from flowark.knowledge.reuse_digest import infer_case_name_from_report_path
from flowark.knowledge.reuse_profile import build_current_case_profile_from_report
from flowark.knowledge_packaging import (
    is_analysis_log_rag_packaging_mode,
    is_embedding_backed_packaging_mode,
    normalize_knowledge_packaging_mode,
)
from flowark.mem0_metering import collect_mem0_usage_for_run
from flowark.prompt_loader import render_prompt
from flowark.runtime.config import (
    KNOWLEDGE_DISTILLATION_GENERIC,
    AnalysisRequest,
    RunArtifacts,
    RunConfig,
    normalize_knowledge_distillation_mode,
    normalize_knowledge_reuse_digest_mode,
    normalize_runtime_injection_mode,
)
from flowark.semantics import FlowArkSemanticEngine
from flowark.semantics.models import (
    AnalysisRunContext,
    AugmentRuntimeConfig,
    Phase,
    PhaseSpec,
    SessionHandle,
    TurnContract,
)
from flowark.timeutil import now_tz8_iso, timestamp_slug_tz8
from flowark.types import KnowledgeCandidate, ValidationResult, to_jsonable

from .knowledge_pipeline import RunnerKnowledgePipelineMixin
from .knowledge_synth import RunnerKnowledgeSynthMixin
from .reporting import RunnerReportingMixin
from .session import RunnerSessionMixin


class FlowArkRunner(
    RunnerKnowledgePipelineMixin,
    RunnerKnowledgeSynthMixin,
    RunnerReportingMixin,
    RunnerSessionMixin,
):
    """OpenCode-backed FlowArk runner."""

    _UPSTREAM_RATE_LIMIT_MAX_RETRIES = 3
    _UPSTREAM_RATE_LIMIT_BACKOFF_SECONDS = (3, 8, 15)

    def __init__(self, config: RunConfig):
        self.config = config
        self._live_transcript_path: Path | None = None
        self._capture_proxy: ManagedAnthropicCaptureProxy | None = None
        self._agent_adapter_instance: AgentAdapter | None = None
        self._semantic_engine_instance: FlowArkSemanticEngine | None = None
        self._active_run_session: AgentRunSession | None = None

    def _set_live_transcript_path(self, path: Path | None) -> None:
        self._live_transcript_path = path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def _append_live_transcript(self, text: str, *, prefix: str | None = None) -> None:
        path = self._live_transcript_path
        if path is None:
            return
        line = f"[{prefix}] {text}" if prefix else text
        try:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line)
                if not line.endswith("\n"):
                    fp.write("\n")
                fp.flush()
        except Exception:
            pass

    def _is_naive_mode(self) -> bool:
        return str(self.config.agent_mode or "flowark").strip().lower() == "naive"

    def _skills_dir(self) -> Path | None:
        if self.config.skills_dir is None:
            return None
        return Path(self.config.skills_dir).expanduser().resolve()

    def _require_skills_dir(self) -> Path:
        skills_dir = self._skills_dir()
        if skills_dir is None:
            raise ValueError("flowark 模式必须提供知识 scope 的 skills/ 目录路径（skills_dir）")
        return skills_dir

    def _auto_knowledge_validate_mode(self) -> str:
        if self._knowledge_distillation_mode() == KNOWLEDGE_DISTILLATION_GENERIC:
            return "off"
        if is_embedding_backed_packaging_mode(self._knowledge_packaging_mode()):
            return "off"
        mode = str(self.config.auto_knowledge_validate_mode or "static").strip().lower()
        if mode == "full":
            raise ValueError("auto_knowledge_validate_mode=full 已删除，请改用 static 或 off")
        if mode not in {"off", "static"}:
            raise ValueError("auto_knowledge_validate_mode 必须是 off 或 static")
        return mode

    def _knowledge_distillation_mode(self) -> str:
        return normalize_knowledge_distillation_mode(
            str(
                getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
                or "with_selection_rules"
            )
        )

    def _knowledge_packaging_mode(self) -> str:
        return normalize_knowledge_packaging_mode(
            str(getattr(self.config, "knowledge_packaging_mode", "dsl_rule") or "dsl_rule")
        )

    def _runtime_injection_mode(self) -> str:
        return normalize_runtime_injection_mode(
            str(getattr(self.config, "runtime_injection_mode", "context_aware") or "context_aware")
        )

    def _knowledge_reuse_digest_mode(self) -> str:
        if self._knowledge_distillation_mode() == KNOWLEDGE_DISTILLATION_GENERIC:
            return "off"
        if is_analysis_log_rag_packaging_mode(self._knowledge_packaging_mode()):
            return "off"
        return normalize_knowledge_reuse_digest_mode(
            str(getattr(self.config, "knowledge_reuse_digest_mode", "off") or "off")
        )

    def _command_hooks_configured(
        self,
        *,
        request: AnalysisRequest | None,
        naive_mode: bool | None = None,
    ) -> bool:
        if naive_mode is None:
            naive_mode = self._is_naive_mode()
        return (not naive_mode) and request is not None

    def _knowledge_reuse_runtime_active(
        self,
        *,
        request: AnalysisRequest | None,
        naive_mode: bool | None = None,
    ) -> bool:
        if not self._command_hooks_configured(request=request, naive_mode=naive_mode):
            return False
        return str(self.config.knowledge_mode or "warm").strip().lower() == "warm"

    def _knowledge_live_reuse_digest_enabled(self, *, naive_mode: bool | None = None) -> bool:
        if naive_mode is None:
            naive_mode = self._is_naive_mode()
        return (not naive_mode) and self._knowledge_reuse_digest_mode() == "live_corridor"

    def _knowledge_prompt_guidance_enabled(self, *, naive_mode: bool | None = None) -> bool:
        if naive_mode is None:
            naive_mode = self._is_naive_mode()
        return (
            not naive_mode
            and not is_analysis_log_rag_packaging_mode(self._knowledge_packaging_mode())
        )

    def _auto_knowledge_cycle_enabled(self, *, naive_mode: bool | None = None) -> bool:
        if naive_mode is None:
            naive_mode = self._is_naive_mode()
        if is_analysis_log_rag_packaging_mode(self._knowledge_packaging_mode()):
            return False
        return (not naive_mode) and bool(self.config.auto_knowledge_cycle)

    def _effective_runtime_features(
        self,
        *,
        request: AnalysisRequest | None,
        naive_mode: bool | None = None,
    ) -> dict[str, bool]:
        if naive_mode is None:
            naive_mode = self._is_naive_mode()
        auto_knowledge_cycle_enabled = self._auto_knowledge_cycle_enabled(naive_mode=naive_mode)
        runtime_reuse_active = self._knowledge_reuse_runtime_active(
            request=request,
            naive_mode=naive_mode,
        )
        return {
            "command_hooks_configured": self._command_hooks_configured(
                request=request,
                naive_mode=naive_mode,
            ),
            "knowledge_reuse_runtime_active": runtime_reuse_active,
            "runtime_request_submit_injection_enabled": runtime_reuse_active,
            "runtime_after_tool_injection_enabled": (
                runtime_reuse_active and self._runtime_injection_mode() == "context_aware"
            ),
            "knowledge_live_reuse_digest_enabled": self._knowledge_live_reuse_digest_enabled(
                naive_mode=naive_mode
            ),
            "knowledge_prompt_guidance_enabled": self._knowledge_prompt_guidance_enabled(
                naive_mode=naive_mode
            ),
            "final_report_same_session_enabled": True,
            "auto_knowledge_synth_validate_apply_enabled": (
                auto_knowledge_cycle_enabled and not naive_mode
            ),
        }

    def _build_run_meta_payload(
        self,
        *,
        request: AnalysisRequest,
        run_dir: Path | None,
        naive_mode: bool,
        options: Any | None = None,
        analysis_trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        skills_dir = self._skills_dir()
        effective_runtime_features = self._effective_runtime_features(
            request=request,
            naive_mode=naive_mode,
        )
        settings_path = self._analysis_trace_settings_path(analysis_trace)
        if settings_path is None and options is not None:
            settings_path = str(options.settings) if getattr(options, "settings", None) else None
        capture_proxy_metadata = self._analysis_trace_capture_proxy_metadata(analysis_trace)
        if capture_proxy_metadata == {"enabled": False} and self._capture_proxy is not None:
            capture_proxy_metadata = self._capture_proxy.metadata()
        runtime_agents = self._analysis_trace_agent_names(analysis_trace)
        if not runtime_agents:
            runtime_agents = [] if naive_mode else list(get_runtime_agents().keys())
        agent_adapter_name = normalize_agent_adapter_name(self.config.agent_adapter)
        payload = {
            "query": request.query,
            "source": request.source,
            "app_name": request.app_name,
            "sink_types": request.sink_types,
            "cwd": str(self.config.cwd),
            "agent_mode": self.config.agent_mode,
            "knowledge_mode": self.config.knowledge_mode,
            "knowledge_allow_repeat_injection_within_session": (
                self.config.knowledge_allow_repeat_injection_within_session
            ),
            "knowledge_repeat_summary_react_gap": max(
                0,
                int(getattr(self.config, "knowledge_repeat_summary_react_gap", 0)),
            ),
            "knowledge_repeat_full_react_gap": max(
                0,
                int(getattr(self.config, "knowledge_repeat_full_react_gap", 1)),
            ),
            "skills_dir": (str(skills_dir) if skills_dir is not None else None),
            "auto_knowledge_cycle": self.config.auto_knowledge_cycle,
            "runtime_injection_mode": self._runtime_injection_mode(),
            "knowledge_distillation_mode": self._knowledge_distillation_mode(),
            "knowledge_packaging_mode": self._knowledge_packaging_mode(),
            "auto_knowledge_validate_mode": self._auto_knowledge_validate_mode(),
            "knowledge_reuse_digest_mode": self._knowledge_reuse_digest_mode(),
            "effective_runtime_features": effective_runtime_features,
            "naive_mode_effective_disables": {
                "knowledge_hooks": not effective_runtime_features["knowledge_reuse_runtime_active"],
                "knowledge_specific_prompt_guidance": not effective_runtime_features[
                    "knowledge_prompt_guidance_enabled"
                ],
                "same_session_final_report": not effective_runtime_features["final_report_same_session_enabled"],
                "auto_knowledge_synth_validate_apply": not effective_runtime_features[
                    "auto_knowledge_synth_validate_apply_enabled"
                ],
            },
            "created_at": now_tz8_iso(),
            "agent_adapter": agent_adapter_name,
            "runtime_agents": runtime_agents,
            "adapter_settings_effective": settings_path,
            "anthropic_capture_proxy": capture_proxy_metadata,
        }
        opencode_payload = (analysis_trace or {}).get("opencode")
        if agent_adapter_name == "opencode" and isinstance(opencode_payload, dict):
            payload["opencode"] = dict(opencode_payload)
            payload["opencode_isolation_dir"] = opencode_payload.get("isolation_dir")
            payload["opencode_command_source"] = opencode_payload.get("command_source")
            payload["opencode_provider"] = opencode_payload.get("provider")
            payload["opencode_model"] = opencode_payload.get("model")
            payload["opencode_after_tool_delivery"] = opencode_payload.get("after_tool_delivery")
            payload["opencode_bash_policy"] = opencode_payload.get("bash_policy")
            payload["opencode_post_phase_mode"] = opencode_payload.get(
                "post_phase_mode", "plain_json_same_surface"
            )
            payload["opencode_json_parse_mode"] = opencode_payload.get(
                "json_parse_mode", "flowark_plain_json"
            )
            payload["opencode_native_structured_output_enabled"] = bool(
                opencode_payload.get("native_structured_output_enabled", False)
            )
            payload["opencode_legacy_structured_output_configured"] = bool(
                opencode_payload.get("legacy_structured_output_configured", False)
            )
            payload["opencode_structured_output_enabled"] = opencode_payload.get(
                "structured_output_enabled"
            )
            payload["opencode_structured_output"] = opencode_payload.get(
                "structured_output_enabled"
            )
            payload["opencode_post_phases_enabled"] = opencode_payload.get(
                "post_phases_enabled"
            )
        return payload

    def _agent_adapter(self) -> AgentAdapter:
        if self._agent_adapter_instance is None:
            self._agent_adapter_instance = build_agent_adapter(
                name=self.config.agent_adapter,
                config=self.config,
                workspace_root=self._workspace_root(),
                transcript_append_fn=self._append_live_transcript,
                capture_proxy_factory=start_capture_proxy,
            )
        return self._agent_adapter_instance

    def _semantic_engine(self) -> FlowArkSemanticEngine:
        if self._semantic_engine_instance is None:
            self._semantic_engine_instance = FlowArkSemanticEngine(
                runtime_config=AugmentRuntimeConfig(
                    skills_dir=self._skills_dir(),
                    knowledge_mode=str(self.config.knowledge_mode or "warm"),
                    runtime_injection_mode=self._runtime_injection_mode(),
                    knowledge_packaging_mode=str(
                        getattr(self.config, "knowledge_packaging_mode", "dsl_rule")
                        or "dsl_rule"
                    ),
                    knowledge_top_k=max(1, int(self.config.knowledge_top_k or 3)),
                    knowledge_min_score=float(self.config.knowledge_min_score or 1.0),
                    knowledge_injection_char_budget=max(
                        256,
                        int(self.config.knowledge_injection_char_budget or 4000),
                    ),
                    knowledge_delta_char_budget=(
                        max(256, int(self.config.knowledge_delta_char_budget))
                        if self.config.knowledge_delta_char_budget is not None
                        else None
                    ),
                    knowledge_allow_repeat_within_session=bool(
                        self.config.knowledge_allow_repeat_injection_within_session
                    ),
                    knowledge_repeat_summary_hook_gap=max(
                        1,
                        int(self.config.knowledge_repeat_summary_hook_gap or 3),
                    ),
                    knowledge_repeat_full_hook_gap=max(
                        2,
                        int(self.config.knowledge_repeat_full_hook_gap or 10),
                    ),
                    knowledge_repeat_summary_react_gap=max(
                        0,
                        int(getattr(self.config, "knowledge_repeat_summary_react_gap", 0)),
                    ),
                    knowledge_repeat_full_react_gap=max(
                        0,
                        int(getattr(self.config, "knowledge_repeat_full_react_gap", 1)),
                    ),
                    knowledge_realtime_min_interval_ms=max(
                        0,
                        int(self.config.knowledge_realtime_min_interval_ms or 1500),
                    ),
                    knowledge_recall_top_m=max(
                        1,
                        int(self.config.knowledge_recall_top_m or 8),
                    ),
                    reuse_embed_base_url=getattr(self.config, "reuse_embed_base_url", None),
                    reuse_embed_api_key=getattr(self.config, "reuse_embed_api_key", None),
                    reuse_embed_model=getattr(self.config, "reuse_embed_model", None),
                    reuse_embed_verify_ssl=bool(
                        getattr(self.config, "reuse_embed_verify_ssl", False)
                    ),
                ),
                knowledge_distillation_mode=self._knowledge_distillation_mode(),
                knowledge_reuse_digest_mode=self._knowledge_reuse_digest_mode(),
            )
        return self._semantic_engine_instance

    @staticmethod
    def _analysis_trace_cwd(analysis_trace: dict[str, Any] | None) -> str:
        text = str((analysis_trace or {}).get("cwd") or "").strip()
        return text

    @staticmethod
    def _analysis_trace_agent_names(analysis_trace: dict[str, Any] | None) -> list[str]:
        raw = (analysis_trace or {}).get("agent_names")
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @staticmethod
    def _analysis_trace_settings_path(analysis_trace: dict[str, Any] | None) -> str | None:
        text = str((analysis_trace or {}).get("settings_path") or "").strip()
        return text or None

    @staticmethod
    def _analysis_trace_capture_proxy_metadata(analysis_trace: dict[str, Any] | None) -> dict[str, Any]:
        payload = (analysis_trace or {}).get("capture_proxy_metadata")
        return dict(payload) if isinstance(payload, dict) else {"enabled": False}

    @staticmethod
    def _derive_phase_spec(
        phase_spec: PhaseSpec,
        *,
        instruction: str | None = None,
        turn_name: str | None = None,
        transcript_prefix: str | None = None,
        timeout_seconds: int | None = None,
        max_turns: int | None = None,
        echo: bool | None = None,
    ) -> PhaseSpec:
        turn_contract = replace(
            phase_spec.turn_contract,
            turn_name=turn_name if turn_name is not None else phase_spec.turn_contract.turn_name,
            transcript_prefix=(
                transcript_prefix
                if transcript_prefix is not None
                else phase_spec.turn_contract.transcript_prefix
            ),
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else phase_spec.turn_contract.timeout_seconds
            ),
            max_turns=max_turns if max_turns is not None else phase_spec.turn_contract.max_turns,
            echo=echo if echo is not None else phase_spec.turn_contract.echo,
        )
        return replace(
            phase_spec,
            instruction=instruction if instruction is not None else phase_spec.instruction,
            turn_contract=turn_contract,
        )

    @staticmethod
    def _workspace_root() -> Path:
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _compose_query(
        request: AnalysisRequest,
        *,
        include_knowledge_guidance: bool = True,
    ) -> str:
        sink_types = (
            ", ".join(request.sink_types)
            if request.sink_types
            else "未指定（请重点关注常见泄露 sink）"
        )
        source = request.source or "未显式提供（请先从用户查询中识别 source）"
        knowledge_guidance_lines = ""
        if include_knowledge_guidance:
            knowledge_guidance_lines = "\n".join([
                "- 系统会自动注入知识：分析中途系统会向你提供知识参考，若符合知识的适用条件，应优先遵照知识内容并跳过对应的内部实现；非必要不要回读其覆盖的代码，继续执行后续分析。",
            ]) + "\n"
        return render_prompt(
            "start_analysis",
            query=request.query.strip(),
            source=source,
            sink_types=sink_types,
            knowledge_guidance_lines=knowledge_guidance_lines,
        )

    @staticmethod
    def _compose_naive_query(request: AnalysisRequest) -> str:
        return FlowArkRunner._compose_query(request, include_knowledge_guidance=False)

    def _prepare_run_dir(self) -> Path | None:
        if not self.config.out_dir:
            return None
        run_id = timestamp_slug_tz8() + "-" + uuid4().hex[:8]
        run_dir = self.config.out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _write_json(path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(to_jsonable(data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def run_query(self, request: AnalysisRequest) -> RunArtifacts:
        if not request.query.strip():
            raise ValueError("query 不能为空")
        naive_mode = self._is_naive_mode()
        adapter = self._agent_adapter()
        agent_adapter_name = normalize_agent_adapter_name(self.config.agent_adapter)
        semantics = self._semantic_engine()

        run_dir = self._prepare_run_dir()
        final_report_md = run_dir / "final_report.md" if run_dir else None
        final_report_json = run_dir / "final_report.json" if run_dir else None
        cost_summary_path = run_dir / "cost_summary.json" if run_dir else None
        knowledge_log_path = run_dir / "knowledge_injection.jsonl" if run_dir else None
        self._set_live_transcript_path((run_dir / "raw_transcript.txt") if run_dir else None)
        analysis_exit_stack: AsyncExitStack | None = None
        try:
            composed_query = (
                self._compose_naive_query(request)
                if naive_mode
                else self._compose_query(
                    request,
                    include_knowledge_guidance=self._knowledge_prompt_guidance_enabled(
                        naive_mode=naive_mode
                    ),
                )
            )
            auth_env_diagnostics = describe_anthropic_env_conflicts()

            print("=" * 60)
            print(f"FlowArk - {getattr(adapter, 'name', 'agent')} Agent 运行器")
            print("=" * 60)
            print(f"工作目录: {self.config.cwd}")
            print(f"Agent 模式: {self.config.agent_mode}")
            agent_names = [] if naive_mode else list(get_runtime_agents().keys())
            print(f"子 Agent: {', '.join(agent_names) if agent_names else '无'}")
            if naive_mode:
                print("朴素基线模式: 已对齐公共分析 prompt，已禁用知识注入 / 知识相关提示 / 自动知识循环")
            else:
                print(f"知识模式: {self.config.knowledge_mode}")
                print(
                    "AutoKnowledgeCycle: "
                    f"{'on' if self.config.auto_knowledge_cycle else 'off'}"
                )
            for warning in auth_env_diagnostics:
                print(f"[auth-config] {warning}")
                self._append_live_transcript(warning, prefix="auth-config")
            print("-" * 60)

            messages: list[str] = []
            final_result_text: str | None = None
            forked_final_report_payload: dict[str, Any] | None = None
            final_report_parse_error: str | None = None
            analysis_turn_metrics: dict[str, Any] | None = None
            analysis_turn_metrics_all: list[dict[str, Any]] = []
            final_report_turn_metrics_list: list[dict[str, Any]] = []
            knowledge_synth_turn_metrics_list: list[dict[str, Any]] = []
            auto_knowledge_validation_turn_metrics_list: list[dict[str, Any]] = []
            auto_knowledge_candidates: list[KnowledgeCandidate] | None = None
            auto_knowledge_synth_meta: dict[str, Any] | None = None
            auto_knowledge_validation_results: list[ValidationResult] | None = None
            auto_knowledge_validation_meta: dict[str, Any] | None = None
            current_case_profile_for_synth: dict[str, Any] | None = None
            analysis_failed = False
            analysis_session: SessionHandle | None = None
            run_meta_written = False
            rate_limit_retries = 0
            max_retries = self._UPSTREAM_RATE_LIMIT_MAX_RETRIES
            while True:
                attempt_no = rate_limit_retries + 1
                with_retry_note = f"analysis_retry_{attempt_no}" if attempt_no > 1 else "analysis"
                retry_after_seconds: int | None = None

                if analysis_exit_stack is not None:
                    self._active_run_session = None
                    await analysis_exit_stack.aclose()
                analysis_exit_stack = AsyncExitStack()
                run_context = AnalysisRunContext(
                    run_dir=run_dir,
                    prompt=composed_query,
                    turn_name=with_retry_note,
                    echo=True,
                )
                open_run_session_fn = getattr(adapter, "open_run_session", None)
                if callable(open_run_session_fn):
                    run_session = await analysis_exit_stack.enter_async_context(
                        open_run_session_fn(
                            request=request,
                            run_context=run_context,
                            semantics=semantics,
                        )
                    )
                    self._active_run_session = run_session
                    analysis_run_result = await run_session.run_analysis()
                else:
                    self._active_run_session = None
                    analysis_run_result = await adapter.run_analysis(
                        request=request,
                        run_context=run_context,
                        semantics=semantics,
                    )
                analysis_trace = analysis_run_result.trace
                capture_proxy = analysis_trace.get("capture_proxy")
                self._capture_proxy = capture_proxy if isinstance(capture_proxy, ManagedAnthropicCaptureProxy) else None
                if run_dir and not run_meta_written:
                    self._write_json(
                        run_dir / "run_meta.json",
                        self._build_run_meta_payload(
                            request=request,
                            run_dir=run_dir,
                            naive_mode=naive_mode,
                            analysis_trace=analysis_trace,
                        ),
                    )
                    run_meta_written = True

                attempt_messages = list(analysis_run_result.outcome.messages or [])
                attempt_final_result_text = analysis_run_result.outcome.raw_text
                attempt_turn_metrics = (
                    dict(analysis_run_result.outcome.turn_metrics[0])
                    if analysis_run_result.outcome.turn_metrics
                    else {}
                )
                analysis_turn_metrics_all.append(attempt_turn_metrics)
                messages.extend(attempt_messages)

                if self._is_rate_limited_turn(
                    turn_metrics=attempt_turn_metrics,
                    final_result_text=attempt_final_result_text,
                    messages=attempt_messages,
                ):
                    analysis_turn_metrics = attempt_turn_metrics
                    final_result_text = attempt_final_result_text
                    if rate_limit_retries < max_retries:
                        rate_limit_retries += 1
                        retry_after_seconds = self._rate_limit_backoff_seconds(rate_limit_retries)
                        notice = (
                            f"[FlowArk 自动重试] 检测到上游临时错误/限流（分析阶段，第 {attempt_no} 次尝试失败），"
                            f"{retry_after_seconds}s 后重试..."
                        )
                        print(notice)
                        messages.append(notice)
                        self._append_live_transcript(notice, prefix="runner")
                else:
                    analysis_turn_metrics = attempt_turn_metrics
                    final_result_text = attempt_final_result_text
                    analysis_result = (
                        attempt_turn_metrics.get("result")
                        if isinstance(attempt_turn_metrics, dict)
                        else None
                    )
                    analysis_failed = bool(
                        isinstance(analysis_result, dict)
                        and analysis_result.get("is_error") is True
                    )
                    if analysis_run_result.session.session_id.strip():
                        analysis_session = analysis_run_result.session
                    retry_after_seconds = None

                if retry_after_seconds is None:
                    break
                self._active_run_session = None
                if analysis_exit_stack is not None:
                    await analysis_exit_stack.aclose()
                    analysis_exit_stack = None
                await asyncio.sleep(retry_after_seconds)

            if analysis_session is not None and not analysis_failed:
                post_phases_enabled = True
                report_result = await self._run_final_report_eval_phase(
                    session=analysis_session,
                    request=request,
                    run_dir=run_dir,
                )
                if isinstance(report_result, Exception):
                    messages.append(f"[phase:final_report] [FlowArk] phase failed: {report_result}")
                    self._append_live_transcript(
                        f"[FlowArk] phase failed: {report_result}",
                        prefix="phase:final_report",
                    )
                    current_phase_session = analysis_session
                else:
                    (
                        forked_final_report_payload,
                        final_report_parse_error,
                        report_branch_messages,
                        final_report_turn_metrics_list,
                        report_session,
                    ) = report_result
                    messages.extend(report_branch_messages)
                    current_phase_session = report_session or analysis_session
                    if isinstance(forked_final_report_payload, dict):
                        current_report_path = (
                            run_dir / "final_report.json"
                            if run_dir is not None
                            else (self.config.cwd / "final_report.json")
                        )
                        eval_root = self._resolve_live_reuse_eval_root(self._skills_dir())
                        try:
                            current_case_profile_for_synth = build_current_case_profile_from_report(
                                report_payload=forked_final_report_payload,
                                case_name=infer_case_name_from_report_path(current_report_path),
                                session_name=(
                                    eval_root.name
                                    if eval_root is not None
                                    else (
                                        run_dir.parent.name
                                        if run_dir is not None
                                        else self.config.cwd.name
                                    )
                                ),
                                report_path=current_report_path,
                                app_name=request.app_name,
                            )
                        except Exception:
                            current_case_profile_for_synth = None

                if post_phases_enabled and self.config.auto_knowledge_cycle and not naive_mode:
                    try:
                        synth_result = await self._run_auto_knowledge_synth_phase(
                            session=current_phase_session,
                            request=request,
                            run_dir=run_dir,
                            current_case_profile=current_case_profile_for_synth,
                        )
                    except Exception as exc:
                        synth_result = exc
                    if isinstance(synth_result, Exception):
                        messages.append(f"[phase:knowledge_synth] [FlowArk] phase failed: {synth_result}")
                        self._append_live_transcript(
                            f"[FlowArk] phase failed: {synth_result}",
                            prefix="phase:knowledge_synth",
                        )
                        auto_knowledge_candidates = None
                        auto_knowledge_synth_meta = {
                            "source": "agent_session",
                            "parse_error": f"knowledge synth failed: {synth_result}",
                        }
                        synth_session = current_phase_session
                    else:
                        (
                            auto_knowledge_candidates,
                            synth_branch_messages,
                            knowledge_synth_turn_metrics_list,
                            auto_knowledge_synth_meta,
                            synth_session,
                        ) = synth_result
                        messages.extend(synth_branch_messages)
                else:
                    synth_session = current_phase_session

                if (
                    post_phases_enabled
                    and
                    self.config.auto_knowledge_cycle
                    and not naive_mode
                    and auto_knowledge_candidates is not None
                    and synth_session is not None
                    and synth_session.session_id.strip()
                ):
                    validate_result = await self._run_auto_knowledge_validation_pipeline(
                        session=synth_session,
                        request=request,
                        candidates=list(auto_knowledge_candidates or []),
                        run_dir=run_dir,
                    )
                    (
                        auto_knowledge_validation_results,
                        validate_branch_messages,
                        auto_knowledge_validation_turn_metrics_list,
                        auto_knowledge_validation_meta,
                    ) = validate_result
                    messages.extend(validate_branch_messages)
            if run_dir:
                (run_dir / "raw_transcript.txt").write_text("\n".join(messages) + "\n", encoding="utf-8")
            self._set_live_transcript_path(None)

            fallback_report_text = self._select_final_report_text(
                analysis_result_text=final_result_text,
                forked_report_text=None,
                messages=messages,
            )

            all_turn_metrics: list[dict[str, Any]] = []
            if analysis_turn_metrics_all:
                all_turn_metrics.extend(
                    [m for m in analysis_turn_metrics_all if isinstance(m, dict)]
                )
            elif isinstance(analysis_turn_metrics, dict):
                all_turn_metrics.append(analysis_turn_metrics)
            all_turn_metrics.extend(
                [m for m in final_report_turn_metrics_list if isinstance(m, dict)]
            )
            all_turn_metrics.extend(
                [m for m in knowledge_synth_turn_metrics_list if isinstance(m, dict)]
            )
            all_turn_metrics.extend(
                [m for m in auto_knowledge_validation_turn_metrics_list if isinstance(m, dict)]
            )

            run_summary = self._build_run_summary(
                request=request,
                analysis_messages=messages,
                turn_metrics=all_turn_metrics,
            )
            if isinstance(auto_knowledge_synth_meta, dict):
                historical_reuse_digest_meta = auto_knowledge_synth_meta.get("historical_reuse_digest")
                if isinstance(historical_reuse_digest_meta, dict):
                    self._merge_rerank_llm_metrics_into_run_summary(
                        run_summary,
                        historical_reuse_digest_meta.get("historical_rerank_metrics"),
                    )
            final_report = self._normalize_report(
                report_payload=forked_final_report_payload,
                fallback_report_text=fallback_report_text,
                request=request,
                run_dir=run_dir,
                run_summary=run_summary,
                parse_error=final_report_parse_error,
            )

            if final_report_md:
                final_report_md.write_text(final_report.final_report_markdown, encoding="utf-8")

            if final_report_json:
                self._write_json(final_report_json, final_report.to_payload())

            collect_mem0_usage_for_run(run_dir=run_dir, run_summary=run_summary)

            if cost_summary_path:
                self._write_json(cost_summary_path, run_summary)

            if run_dir:
                run_meta_path = run_dir / "run_meta.json"
                if run_meta_path.exists():
                    try:
                        run_meta_payload = json.loads(run_meta_path.read_text(encoding="utf-8"))
                    except Exception:
                        run_meta_payload = {}
                    if isinstance(run_meta_payload, dict):
                        run_meta_payload["final_report_schema_version"] = final_report.schema_version
                        run_meta_payload["final_report_generation_mode"] = (
                            "structured_v2" if forked_final_report_payload is not None else "legacy_fallback"
                        )
                        run_meta_payload["final_report_parse_error"] = final_report.parse_error
                        self._write_json(run_meta_path, run_meta_payload)
                self._write_live_digest_effective_runtime_feature(
                    run_dir,
                    historical_reuse_digest_meta=(
                        dict(auto_knowledge_synth_meta.get("historical_reuse_digest") or {})
                        if isinstance(auto_knowledge_synth_meta, dict)
                        else None
                    ),
                )

            if naive_mode:
                auto_knowledge_result = {
                    "enabled": False,
                    "executed": False,
                    "skipped": True,
                    "reason": "naive_agent_mode",
                }
            else:
                auto_knowledge_result = self._run_auto_knowledge_cycle(
                    run_dir=run_dir,
                    precomputed_candidates=auto_knowledge_candidates,
                    synth_meta=auto_knowledge_synth_meta,
                    precomputed_validation_results=auto_knowledge_validation_results,
                    validation_meta=auto_knowledge_validation_meta,
                )
                if isinstance(auto_knowledge_result, dict):
                    auto_knowledge_result["background"] = False
                    auto_knowledge_result["background_status"] = "disabled"
            if run_dir and auto_knowledge_result:
                auto_kb_path = run_dir / "auto_knowledge_cycle.json"
                self._write_json(auto_kb_path, auto_knowledge_result)

            if (
                run_dir
                and not naive_mode
                and is_analysis_log_rag_packaging_mode(self._knowledge_packaging_mode())
            ):
                if analysis_failed:
                    rag_result = {
                        "enabled": True,
                        "executed": False,
                        "skipped": True,
                        "reason": "analysis_failed",
                    }
                elif final_report.parse_error:
                    rag_result = {
                        "enabled": True,
                        "executed": False,
                        "skipped": True,
                        "reason": "final_report_parse_error",
                        "parse_error": final_report.parse_error,
                    }
                else:
                    rag_result = append_run_to_analysis_log_rag_corpus(
                        run_dir=run_dir,
                        skills_dir=self._require_skills_dir(),
                        app_name=request.app_name or self.config.cwd.name,
                        source_id=request.source,
                        case_id=request.source,
                        run_id=run_dir.name,
                        sink_types=request.sink_types,
                    )
                self._write_json(run_dir / "analysis_log_rag" / "index_update.json", rag_result)

            if run_dir:
                legacy_path = run_dir / "knowledge_injection.json"
                if not legacy_path.exists():
                    self._write_json(
                        legacy_path,
                        {
                            "records": self._read_jsonl(knowledge_log_path) if knowledge_log_path else [],
                            "knowledge_mode": self.config.knowledge_mode,
                            "agent_mode": self.config.agent_mode,
                        },
                    )

            return RunArtifacts(
                run_dir=run_dir,
                final_report_md=final_report_md,
                final_report_json=final_report_json,
                raw_messages=messages,
                knowledge_injection_log=knowledge_log_path,
                cost_summary_json=cost_summary_path,
                final_report=final_report,
            )
        except Exception as exc:
            if run_dir and agent_adapter_name == "opencode":
                error_line = f"[error] {type(exc).__name__}: {exc}"
                try:
                    transcript_path = run_dir / "raw_transcript.txt"
                    with transcript_path.open("a", encoding="utf-8") as fp:
                        fp.write(error_line + "\n")
                    self._write_json(
                        run_dir / "run_error.json",
                        {
                            "agent_adapter": agent_adapter_name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "traceback": "".join(
                                traceback.format_exception(type(exc), exc, exc.__traceback__)
                            ),
                        },
                    )
                except Exception:
                    pass
            raise
        finally:
            self._active_run_session = None
            if analysis_exit_stack is not None:
                await analysis_exit_stack.aclose()
            if self._capture_proxy is not None:
                self._capture_proxy.stop()
                self._capture_proxy = None
            self._set_live_transcript_path(None)
            self._agent_adapter().reset_ephemeral_state()
