"""FlowArkRunner 的会话执行与统计辅助。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowark.runtime.config import AnalysisRequest
from flowark.semantics.models import PhaseRunResult, PhaseSpec, SessionHandle, TurnOutcome


class RunnerSessionMixin:
    @staticmethod
    def _phase_family_from_name(name: Any) -> str | None:
        phase_name = str(name or "").strip()
        if not phase_name:
            return None
        if phase_name.startswith("analysis"):
            return "analysis"
        if phase_name.startswith("final_report"):
            return "final_report"
        if phase_name.startswith("eval_summary"):
            return "eval_summary"
        if phase_name.startswith("knowledge_synth"):
            return "knowledge_synth"
        if phase_name.startswith("knowledge_rule_repair"):
            return "knowledge_rule_repair"
        return None

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def _collect_knowledge_usage(self, run_dir: Path | None) -> list[dict]:
        if not run_dir:
            return []
        log_path = run_dir / "knowledge_injection.jsonl"
        records = self._read_jsonl(log_path)
        usage: list[dict] = []
        for rec in records:
            injected_ids = rec.get("injected_skill_ids") or []
            details = rec.get("details") or []
            if not injected_ids and not details:
                continue
            skill_details = [
                item for item in details
                if isinstance(item, dict) and str(item.get("type") or "") == "skill"
            ]
            usage.append(
                {
                    "timestamp": rec.get("timestamp"),
                    "skill_ids": injected_ids,
                    "mode": rec.get("mode"),
                    "details": details,
                    "skills": {
                        "ids": injected_ids,
                        "count": len(injected_ids),
                        "details": skill_details,
                    },
                    "matched_skill_ids": rec.get("matched_skill_ids") or [],
                    "selected_skill_ids": rec.get("selected_skill_ids") or [],
                    "dropped_skill_ids": rec.get("dropped_skill_ids") or [],
                    "dropped_skill_reasons": rec.get("dropped_skill_reasons") or {},
                    "injected_chars": rec.get("injected_chars"),
                    "query_excerpt": rec.get("query_excerpt"),
                }
            )
        return usage

    async def _continue_phase(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
    ) -> PhaseRunResult:
        active_run_session = getattr(self, "_active_run_session", None)
        if (
            active_run_session is not None
            and (
                phase_spec.turn_contract.max_turns is None
                or str(session.adapter_name or "").strip().lower() == "opencode"
            )
        ):
            return await active_run_session.continue_phase(phase_spec=phase_spec)

        # Active run sessions do not promise per-turn max_turns reconfiguration.
        # Use the resume fallback when a phase explicitly needs its own limit.
        if active_run_session is not None:
            self._active_run_session = None
        return await self._agent_adapter().continue_phase(
            session=session,
            phase_spec=phase_spec,
        )

    @staticmethod
    def _turn_outcome_raw_text(outcome: TurnOutcome) -> str:
        return (outcome.raw_text or "\n".join(outcome.messages or [])).strip()

    @classmethod
    def _mark_recovered_opencode_structured_output_error(
        cls,
        turn_metrics_list: list[dict[str, Any]],
    ) -> None:
        for turn in turn_metrics_list:
            if not isinstance(turn, dict):
                continue
            result = turn.get("result")
            if not isinstance(result, dict):
                continue
            if result.get("is_error") is not True:
                continue
            error = result.get("error")
            error_type = ""
            error_message = ""
            if isinstance(error, dict):
                error_type = str(error.get("type") or "")
                error_message = str(error.get("message") or "")
            elif error is not None:
                error_message = str(error)
            combined = f"{error_type}\n{error_message}"
            if "StructuredOutputError" not in combined:
                continue
            result["original_is_error"] = True
            result["is_error"] = False
            result["flowark_recovered_structured_output_error"] = True
            turn["flowark_recovered_structured_output_error"] = True

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @staticmethod
    def _usage_fields() -> tuple[str, ...]:
        return (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )

    @staticmethod
    def _model_usage_field_map() -> tuple[tuple[str, str], ...]:
        return (
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("cacheReadInputTokens", "cache_read_input_tokens"),
            ("cacheCreationInputTokens", "cache_creation_input_tokens"),
        )

    @classmethod
    def _normalize_usage_dict(cls, usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return {}
        normalized: dict[str, Any] = {}
        for key in cls._usage_fields():
            value = cls._as_int(usage.get(key))
            if value is not None:
                normalized[key] = value
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            normalized["server_tool_use"] = dict(server_tool_use)
        return normalized

    @classmethod
    def _normalize_model_usage_dict(cls, model_usage: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(model_usage, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for model_name, raw in model_usage.items():
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = {}
            for source_key, target_key in cls._model_usage_field_map():
                value = cls._as_int(raw.get(source_key))
                if value is not None:
                    item[target_key] = value
            cost_usd = cls._as_float(raw.get("costUSD"))
            if cost_usd is not None:
                item["cost_usd"] = cost_usd
            if item:
                normalized[str(model_name)] = item
        return normalized

    @classmethod
    def _aggregate_normalized_model_usage(cls, normalized_model_usage: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(normalized_model_usage, dict):
            return {}
        totals: dict[str, Any] = {}
        has_positive = False
        for key in cls._usage_fields():
            total = 0
            for item in normalized_model_usage.values():
                total += int(cls._as_int(item.get(key)) or 0)
            totals[key] = total
            has_positive = has_positive or total > 0
        total_cost_usd = 0.0
        saw_cost = False
        for item in normalized_model_usage.values():
            value = cls._as_float(item.get("cost_usd"))
            if value is None:
                continue
            total_cost_usd += value
            saw_cost = True
        if saw_cost:
            totals["cost_usd_sum"] = round(total_cost_usd, 10)
        return totals if has_positive or saw_cost else {}

    @classmethod
    def _usage_token_total(cls, usage: Any) -> int:
        if not isinstance(usage, dict):
            return 0
        total = 0
        for key in cls._usage_fields():
            total += int(cls._as_int(usage.get(key)) or 0)
        return total

    @classmethod
    def _extract_usage_views(
        cls,
        *,
        usage: Any,
        model_usage: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, Any], str]:
        raw_usage = cls._normalize_usage_dict(usage)
        normalized_model_usage = cls._normalize_model_usage_dict(model_usage)
        model_usage_totals = cls._aggregate_normalized_model_usage(normalized_model_usage)

        merged_usage = dict(raw_usage)
        if cls._usage_token_total(model_usage_totals) > 0:
            for key in cls._usage_fields():
                merged_usage[key] = max(
                    int(merged_usage.get(key, 0) or 0),
                    int(model_usage_totals.get(key, 0) or 0),
                )

        raw_total = cls._usage_token_total(raw_usage)
        model_total = cls._usage_token_total(model_usage_totals)
        usage_source = "usage"
        if model_total > 0 and raw_total == 0:
            usage_source = "model_usage"
        elif model_total > 0 and raw_total > 0:
            usage_source = "merged"

        return (
            merged_usage,
            raw_usage,
            normalized_model_usage,
            model_usage_totals,
            usage_source,
        )

    @classmethod
    def _empty_usage_aggregate(cls) -> dict[str, Any]:
        result: dict[str, Any] = {key: 0 for key in cls._usage_fields()}
        result["server_tool_use"] = {}
        result["visible_non_cache_tokens"] = 0
        return result

    @classmethod
    def _add_usage_to_aggregate(cls, aggregate: dict[str, Any], usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        for key in cls._usage_fields():
            aggregate[key] = int(aggregate.get(key, 0) or 0) + int(usage.get(key, 0) or 0)

        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            current = aggregate.setdefault("server_tool_use", {})
            for key, value in server_tool_use.items():
                ivalue = cls._as_int(value)
                if ivalue is None:
                    continue
                current[key] = int(current.get(key, 0) or 0) + ivalue

        aggregate["visible_non_cache_tokens"] = (
            int(aggregate.get("input_tokens", 0) or 0)
            + int(aggregate.get("output_tokens", 0) or 0)
            + int(aggregate.get("cache_creation_input_tokens", 0) or 0)
        )

    @classmethod
    def _merge_rerank_llm_metrics_into_run_summary(
        cls,
        run_summary: dict[str, Any] | None,
        rerank_metrics: dict[str, Any] | None,
    ) -> None:
        if not isinstance(run_summary, dict) or not isinstance(rerank_metrics, dict):
            return
        usage = cls._normalize_usage_dict(rerank_metrics.get("usage"))
        total_cost_usd = cls._as_float(rerank_metrics.get("total_cost_usd"))
        latency_ms = cls._as_int(rerank_metrics.get("latency_ms"))
        model = str(rerank_metrics.get("model") or "").strip()
        if not usage and total_cost_usd is None and latency_ms is None and not model:
            return

        aggregated_metrics = run_summary.setdefault("aggregated_metrics", {})
        main_agent = aggregated_metrics.setdefault("main_agent", {})
        combined = aggregated_metrics.setdefault("combined", {})

        main_usage = main_agent.setdefault("usage", cls._empty_usage_aggregate())
        if not isinstance(main_usage, dict):
            main_usage = cls._empty_usage_aggregate()
            main_agent["usage"] = main_usage
        combined_usage = combined.setdefault("usage", cls._empty_usage_aggregate())
        if not isinstance(combined_usage, dict):
            combined_usage = cls._empty_usage_aggregate()
            combined["usage"] = combined_usage

        cls._add_usage_to_aggregate(main_usage, usage)
        cls._add_usage_to_aggregate(combined_usage, usage)

        if total_cost_usd is not None:
            main_agent["total_cost_usd_sum"] = round(
                float(cls._as_float(main_agent.get("total_cost_usd_sum")) or 0.0) + total_cost_usd,
                10,
            )

        rerank_usage = cls._empty_usage_aggregate()
        cls._add_usage_to_aggregate(rerank_usage, usage)
        aggregated_metrics["rerank_llm"] = {
            "request_count": 1,
            "latency_ms_sum": int(latency_ms or 0),
            "total_cost_usd_sum": round(float(total_cost_usd or 0.0), 10),
            "cost_tracked": total_cost_usd is not None,
            "model": model,
            "usage": rerank_usage,
        }

    @classmethod
    def _extract_result_message_metrics(cls, message: object) -> dict[str, Any]:
        usage, raw_usage, model_usage, model_usage_totals, usage_source = cls._extract_usage_views(
            usage=getattr(message, "usage", None),
            model_usage=getattr(message, "model_usage", None),
        )
        return {
            "subtype": getattr(message, "subtype", None),
            "is_error": getattr(message, "is_error", None),
            "session_id": getattr(message, "session_id", None),
            "duration_ms": cls._as_int(getattr(message, "duration_ms", None)),
            "duration_api_ms": cls._as_int(getattr(message, "duration_api_ms", None)),
            "num_turns": cls._as_int(getattr(message, "num_turns", None)),
            "total_cost_usd": cls._as_float(getattr(message, "total_cost_usd", None)),
            "usage": usage,
            "raw_usage": raw_usage,
            "model_usage": model_usage,
            "model_usage_totals": model_usage_totals,
            "usage_source": usage_source,
        }

    @staticmethod
    def _latest_session_id_from_turn_metrics(turn_metrics_list: list[dict[str, Any]] | None) -> str | None:
        for item in reversed(list(turn_metrics_list or [])):
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            session_id = result.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
        return None

    @classmethod
    def _extract_subagent_run_metrics(cls, tool_use_result: Any) -> dict[str, Any] | None:
        if not isinstance(tool_use_result, dict):
            return None
        if not (
            "agentId" in tool_use_result
            or "totalTokens" in tool_use_result
            or "totalToolUseCount" in tool_use_result
        ):
            return None

        result: dict[str, Any] = {
            "status": tool_use_result.get("status"),
            "agent_id": tool_use_result.get("agentId"),
            "total_tokens": cls._as_int(tool_use_result.get("totalTokens")),
            "total_tool_use_count": cls._as_int(tool_use_result.get("totalToolUseCount")),
            "total_duration_ms": cls._as_int(tool_use_result.get("totalDurationMs")),
            "usage": cls._normalize_usage_dict(tool_use_result.get("usage")),
        }
        prompt = tool_use_result.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            result["prompt_excerpt"] = prompt[:200]
        return result

    def _build_run_summary(
        self,
        *,
        request: AnalysisRequest,
        analysis_messages: list[str],
        turn_metrics: list[dict[str, Any]],
    ) -> dict:
        phase_summaries: list[dict[str, Any]] = []
        main_usage_agg = self._empty_usage_aggregate()
        subagent_usage_agg = self._empty_usage_aggregate()
        combined_usage_agg = self._empty_usage_aggregate()

        total_raw_message_count = 0
        total_tool_use_block_count = 0
        total_assistant_message_count = 0
        total_user_message_count = 0
        total_system_message_count = 0

        main_result_count = 0
        main_num_turns_sum = 0
        main_duration_ms_sum = 0
        main_duration_api_ms_sum = 0
        main_total_cost_usd_sum = 0.0
        main_result_subtypes: list[str] = []
        main_error_result_count = 0
        main_error_phase_names: list[str] = []
        terminal_phase_results: dict[str, dict[str, Any]] = {}

        subagent_run_count = 0
        subagent_total_tokens_sum = 0
        subagent_total_tool_use_count_sum = 0
        subagent_total_duration_ms_sum = 0
        opencode_usage_agg = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        opencode_bash_agg = {
            "bash_count": 0,
            "bash_code_context_count": 0,
            "bash_trace_only_count": 0,
            "bash_blocked_count": 0,
        }
        opencode_result_count = 0
        opencode_request_usage_record_count = 0

        for turn in turn_metrics:
            if not isinstance(turn, dict):
                continue

            total_raw_message_count += int(turn.get("raw_message_count", 0) or 0)
            total_tool_use_block_count += int(turn.get("tool_use_block_count", 0) or 0)
            total_assistant_message_count += int(turn.get("assistant_message_count", 0) or 0)
            total_user_message_count += int(turn.get("user_message_count", 0) or 0)
            total_system_message_count += int(turn.get("system_message_count", 0) or 0)

            result = turn.get("result")
            subagent_runs = turn.get("subagent_runs") if isinstance(turn.get("subagent_runs"), list) else []

            phase_summary: dict[str, Any] = {
                "name": turn.get("name"),
                "raw_message_count": int(turn.get("raw_message_count", 0) or 0),
                "tool_use_block_count": int(turn.get("tool_use_block_count", 0) or 0),
                "assistant_message_count": int(turn.get("assistant_message_count", 0) or 0),
                "user_message_count": int(turn.get("user_message_count", 0) or 0),
                "system_message_count": int(turn.get("system_message_count", 0) or 0),
                "subagent_run_count": len(subagent_runs),
            }

            if isinstance(result, dict):
                phase_summary["result"] = result
                request_usage_records = (
                    turn.get("request_usage_records") if isinstance(turn.get("request_usage_records"), list) else []
                )
                if request_usage_records:
                    phase_summary["request_usage_records"] = request_usage_records
                    phase_summary["request_usage_record_count"] = len(request_usage_records)
                main_result_count += 1
                self._add_usage_to_aggregate(main_usage_agg, result.get("usage"))

                num_turns = self._as_int(result.get("num_turns"))
                if num_turns is not None:
                    main_num_turns_sum += num_turns
                duration_ms = self._as_int(result.get("duration_ms"))
                if duration_ms is not None:
                    main_duration_ms_sum += duration_ms
                duration_api_ms = self._as_int(result.get("duration_api_ms"))
                if duration_api_ms is not None:
                    main_duration_api_ms_sum += duration_api_ms
                total_cost_usd = self._as_float(result.get("total_cost_usd"))
                if total_cost_usd is not None:
                    main_total_cost_usd_sum += total_cost_usd
                subtype = result.get("subtype")
                if isinstance(subtype, str):
                    main_result_subtypes.append(subtype)
                if result.get("is_error") is True:
                    main_error_result_count += 1
                    phase_name = str(turn.get("name") or "").strip()
                    if phase_name:
                        main_error_phase_names.append(phase_name)
                phase_name = str(turn.get("name") or "").strip()
                family_name = self._phase_family_from_name(phase_name)
                if family_name:
                    phase_summary["family"] = family_name
                    terminal_phase_results[family_name] = {
                        "phase_name": phase_name,
                        "is_error": result.get("is_error") is True,
                    }
                if turn.get("adapter") == "opencode" or result.get("subtype") == "opencode":
                    opencode_result_count += 1
                    turn_request_records = (
                        turn.get("request_usage_records") if isinstance(turn.get("request_usage_records"), list) else []
                    )
                    opencode_request_usage_record_count += int(
                        self._as_int(turn.get("request_usage_record_count")) or len(turn_request_records)
                    )
                    result_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                    for key in opencode_usage_agg:
                        opencode_usage_agg[key] += int(self._as_int(result_usage.get(key)) or 0)
                    bash_stats = turn.get("bash") if isinstance(turn.get("bash"), dict) else turn
                    for key in opencode_bash_agg:
                        opencode_bash_agg[key] += int(self._as_int(bash_stats.get(key)) or 0)

            normalized_subagent_runs: list[dict[str, Any]] = []
            for sub in subagent_runs:
                if not isinstance(sub, dict):
                    continue
                normalized_subagent_runs.append(sub)
                subagent_run_count += 1

                total_tokens = self._as_int(sub.get("total_tokens"))
                if total_tokens is not None:
                    subagent_total_tokens_sum += total_tokens
                total_tool_use_count = self._as_int(sub.get("total_tool_use_count"))
                if total_tool_use_count is not None:
                    subagent_total_tool_use_count_sum += total_tool_use_count
                total_duration_ms = self._as_int(sub.get("total_duration_ms"))
                if total_duration_ms is not None:
                    subagent_total_duration_ms_sum += total_duration_ms

                self._add_usage_to_aggregate(subagent_usage_agg, sub.get("usage"))

            if normalized_subagent_runs:
                phase_summary["subagent_runs"] = normalized_subagent_runs

            phase_summaries.append(phase_summary)

        self._add_usage_to_aggregate(combined_usage_agg, main_usage_agg)
        self._add_usage_to_aggregate(combined_usage_agg, subagent_usage_agg)

        terminal_error_phase_names = [
            str(item.get("phase_name") or "").strip()
            for item in terminal_phase_results.values()
            if item.get("is_error") is True and str(item.get("phase_name") or "").strip()
        ]
        terminal_error_result_count = len(terminal_error_phase_names)

        aggregated_metrics = {
            "main_agent": {
                "result_count": main_result_count,
                "num_turns_sum": main_num_turns_sum,
                "duration_ms_sum": main_duration_ms_sum,
                "duration_api_ms_sum": main_duration_api_ms_sum,
                "total_cost_usd_sum": round(main_total_cost_usd_sum, 10),
                "error_result_count": main_error_result_count,
                "error_phase_names": main_error_phase_names,
                "has_error_result": bool(main_error_result_count > 0),
                "terminal_error_result_count": terminal_error_result_count,
                "terminal_error_phase_names": terminal_error_phase_names,
                "has_terminal_error_result": bool(terminal_error_result_count > 0),
                "usage": main_usage_agg,
            },
            "subagents": {
                "run_count": subagent_run_count,
                "total_tokens_sum": subagent_total_tokens_sum,
                "total_tool_use_count_sum": subagent_total_tool_use_count_sum,
                "total_duration_ms_sum": subagent_total_duration_ms_sum,
                "usage": subagent_usage_agg,
            },
            "combined": {
                "usage": combined_usage_agg,
            },
        }
        if opencode_result_count:
            aggregated_metrics["opencode"] = {
                "result_count": opencode_result_count,
                "request_usage_record_count": opencode_request_usage_record_count,
                "usage": opencode_usage_agg,
                "bash": opencode_bash_agg,
                **opencode_bash_agg,
            }

        return {
            "agent_mode": self.config.agent_mode,
            "flowark_extensions_enabled": not self._is_naive_mode(),
            "raw_message_count": len(analysis_messages),
            "analysis_turn_raw_message_count": len(analysis_messages),
            "raw_message_count_total": total_raw_message_count,
            "tool_use_block_count_total": total_tool_use_block_count,
            "assistant_message_count_total": total_assistant_message_count,
            "user_message_count_total": total_user_message_count,
            "system_message_count_total": total_system_message_count,
            "stop_reason": "unknown",
            "main_result_subtypes": main_result_subtypes,
            "phases": phase_summaries,
            "aggregated_metrics": aggregated_metrics,
        }

    @staticmethod
    def _extract_result_text(message: object) -> str | None:
        value = getattr(message, "result", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _looks_like_upstream_rate_limit_error(text: str | None) -> bool:
        if not isinstance(text, str):
            return False
        normalized = text.strip().lower()
        if not normalized:
            return False
        needles = [
            '"code":"1302"',
            '"code": "1302"',
            '"code":"1305"',
            '"code": "1305"',
            "速率限制",
            "请求频率",
            "访问量过大",
            "稍后再试",
            "rate limit",
            "too many requests",
            "429",
        ]
        return any(needle in normalized for needle in needles)

    @classmethod
    def _looks_like_upstream_retryable_error(cls, text: str | None) -> bool:
        if cls._looks_like_upstream_rate_limit_error(text):
            return True
        if not isinstance(text, str):
            return False
        normalized = text.strip().lower()
        if not normalized:
            return False
        needles = [
            '"code":"500"',
            '"code": "500"',
            "操作失败",
            "internal server error",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "upstream error",
        ]
        return any(needle in normalized for needle in needles)

    @classmethod
    def _is_rate_limited_turn(
        cls,
        *,
        turn_metrics: dict[str, Any] | None,
        final_result_text: str | None,
        messages: list[str] | None = None,
    ) -> bool:
        result = (turn_metrics or {}).get("result") if isinstance(turn_metrics, dict) else None
        if not isinstance(result, dict):
            return False
        if result.get("is_error") is not True:
            return False
        if cls._looks_like_upstream_retryable_error(final_result_text):
            return True
        if isinstance(messages, list):
            joined_tail = "\n".join(str(m) for m in messages[-3:])
            if cls._looks_like_upstream_retryable_error(joined_tail):
                return True
        error = result.get("error")
        if error is not None:
            if isinstance(error, (dict, list)):
                error_text = json.dumps(error, ensure_ascii=False, sort_keys=True)
            else:
                error_text = str(error)
            if cls._looks_like_upstream_retryable_error(error_text):
                return True
        return False

    @classmethod
    def _rate_limit_backoff_seconds(cls, retry_index: int) -> int:
        if retry_index <= 0:
            return int(cls._UPSTREAM_RATE_LIMIT_BACKOFF_SECONDS[0])
        idx = min(retry_index - 1, len(cls._UPSTREAM_RATE_LIMIT_BACKOFF_SECONDS) - 1)
        return int(cls._UPSTREAM_RATE_LIMIT_BACKOFF_SECONDS[idx])

    @staticmethod
    def _cleanup_final_report_text(text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return stripped

        lines = stripped.splitlines()
        for idx, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                return "\n".join(lines[idx:]).strip()
        return stripped

    @classmethod
    def _select_final_report_text(
        cls,
        *,
        analysis_result_text: str | None,
        forked_report_text: str | None,
        messages: list[str],
    ) -> str:
        analysis_text = cls._cleanup_final_report_text((analysis_result_text or "").strip())
        fork_text = cls._cleanup_final_report_text((forked_report_text or "").strip())

        if fork_text:
            return fork_text
        if analysis_text:
            return analysis_text
        if fork_text:
            return fork_text
        return "\n".join(messages).strip() or "（无输出）"
