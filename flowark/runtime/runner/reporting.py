"""FlowArkRunner 的报告与评测分支辅助。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from flowark.prompt_loader import render_prompt
from flowark.reporting import ReportNormalizer
from flowark.runtime.config import AnalysisRequest
from flowark.semantics.models import FinalReportPhaseInput, Phase, PhaseSpec, SessionHandle
from flowark.types import FinalDataflowReport


class RunnerReportingMixin:
    @staticmethod
    def _build_final_report_request(request: AnalysisRequest) -> str:
        sink_types = request.sink_types or ["unknown"]
        sink_types_json = json.dumps(sink_types, ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        return render_prompt(
            "final_report",
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
        )

    async def _request_final_report_json(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
        run_dir: Path | None,
        echo: bool = True,
    ) -> tuple[dict[str, Any] | None, list[str], str | None, list[dict[str, Any]], str | None, SessionHandle]:
        phase_result = await self._continue_phase(session=session, phase_spec=phase_spec)
        current_session = phase_result.session
        messages = list(phase_result.outcome.messages or [])
        report_turn_metrics_list: list[dict[str, Any]] = [
            dict(item) for item in (phase_result.outcome.turn_metrics or []) if isinstance(item, dict)
        ]

        raw_text = self._turn_outcome_raw_text(phase_result.outcome)
        raw_path = run_dir / "final_report_raw.txt" if run_dir else None
        if raw_path:
            raw_path.write_text(raw_text + "\n", encoding="utf-8")

        json_text = self._extract_json_object_text(raw_text)
        data: dict[str, Any] | None = None
        parse_exc: Exception | None = None
        if not json_text:
            parse_exc = ValueError("final report 未返回可提取的 JSON 对象")
        else:
            try:
                data = json.loads(json_text)
                if not isinstance(data, dict):
                    raise ValueError("JSON 顶层不是对象")
            except Exception as exc:
                parse_exc = exc

        if parse_exc is not None:
            fix_prompt = (
                "【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。\n"
                "你上一条输出不是合法 JSON 对象。请不要重新分析代码，只修复格式并重新输出一个合法 JSON 对象。"
                "仍然必须严格遵守 schema_version=flowark-final-report-v2 的固定字段。"
            )
            fix_spec = self._derive_phase_spec(
                phase_spec,
                instruction=fix_prompt,
                turn_name="final_report_fix",
                echo=echo,
            )
            fix_result = await self._continue_phase(session=current_session, phase_spec=fix_spec)
            current_session = fix_result.session
            fix_messages = list(fix_result.outcome.messages or [])
            messages.extend(fix_messages)
            report_turn_metrics_list.extend(
                [dict(item) for item in (fix_result.outcome.turn_metrics or []) if isinstance(item, dict)]
            )
            raw_text = self._turn_outcome_raw_text(fix_result.outcome)
            if raw_path:
                raw_path.write_text(raw_text + "\n", encoding="utf-8")
            json_text = self._extract_json_object_text(raw_text)
            if not json_text:
                return (
                    None,
                    messages,
                    raw_text,
                    report_turn_metrics_list,
                    f"修复后仍无 JSON 对象: {parse_exc}",
                    current_session,
                )
            try:
                data = json.loads(json_text)
                if not isinstance(data, dict):
                    raise ValueError("JSON 顶层不是对象")
            except Exception as exc2:
                return (
                    None,
                    messages,
                    raw_text,
                    report_turn_metrics_list,
                    f"JSON 解析失败: {exc2}",
                    current_session,
                )

        return data, messages, raw_text, report_turn_metrics_list, None, current_session

    @staticmethod
    def _prefix_branch_messages(branch_name: str, messages: list[str]) -> list[str]:
        if not messages:
            return []
        prefix = f"[{branch_name}] "
        return [prefix + m for m in messages]

    async def _run_final_report_eval_phase(
        self,
        *,
        session: SessionHandle,
        request: AnalysisRequest,
        run_dir: Path | None,
    ) -> tuple[dict[str, Any] | None, str | None, list[str], list[dict[str, Any]], SessionHandle | None]:
        branch_messages: list[str] = []
        branch_turn_metrics: list[dict[str, Any]] = []
        report_payload: dict[str, Any] | None = None
        report_parse_error: str | None = None

        phase_spec = self._semantic_engine().build_phase_spec(
            Phase.FINAL_REPORT,
            phase_input=FinalReportPhaseInput(request=request),
        )
        final_report_timeout_seconds = int(phase_spec.turn_contract.timeout_seconds or 180)
        next_session: SessionHandle | None = session
        try:
            (
                report_result_payload,
                report_messages,
                _report_raw_text,
                report_turn_metrics_list,
                report_parse_error,
                next_session,
            ) = await asyncio.wait_for(
                self._request_final_report_json(
                    session=session,
                    phase_spec=phase_spec,
                    run_dir=run_dir,
                    echo=False,
                ),
                timeout=final_report_timeout_seconds,
            )
            branch_messages.extend(self._prefix_branch_messages("phase:final_report", report_messages))
            branch_turn_metrics.extend([m for m in report_turn_metrics_list if isinstance(m, dict)])
            report_payload = report_result_payload
            if report_parse_error:
                branch_messages.append(
                    "[phase:final_report] [FlowArk] structured final report unavailable; falling back to legacy markdown: "
                    f"{report_parse_error}"
                )
        except asyncio.TimeoutError:
            branch_messages.append(
                f"[phase:final_report] [FlowArk] final report timeout after {final_report_timeout_seconds}s"
            )
            report_parse_error = f"final report timeout after {final_report_timeout_seconds}s"
        except Exception as exc:
            branch_messages.append(
                f"[phase:final_report] [FlowArk] final report failed: {exc}"
            )
            report_parse_error = f"final report failed: {exc}"
        return (
            report_payload,
            report_parse_error,
            branch_messages,
            branch_turn_metrics,
            next_session,
        )

    @staticmethod
    def _extract_json_object_text(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None

        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.S)
        if fenced:
            return fenced.group(1).strip()

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            return stripped[start:end + 1].strip()
        return None

    def _normalize_report(
        self,
        *,
        report_payload: dict[str, Any] | None,
        fallback_report_text: str,
        request: AnalysisRequest,
        run_dir: Path | None,
        run_summary: dict,
        parse_error: str | None = None,
    ) -> FinalDataflowReport:
        normalizer = ReportNormalizer()
        knowledge_usage = self._collect_knowledge_usage(run_dir)
        if isinstance(report_payload, dict):
            report = normalizer.normalize_structured_payload(
                raw_payload=report_payload,
                query=request.query,
                source_description=request.source,
                app_name=request.app_name,
                sink_types=request.sink_types,
                knowledge_usage=knowledge_usage,
                run_summary=run_summary,
                parse_error=parse_error,
            )
            report.final_report_markdown = normalizer.render_markdown(report)
            return report

        return normalizer.normalize(
            final_markdown=fallback_report_text,
            query=request.query,
            source_description=request.source,
            sink_types=request.sink_types,
            app_name=request.app_name,
            knowledge_usage=knowledge_usage,
            run_summary=run_summary,
            parse_error=parse_error,
        )
