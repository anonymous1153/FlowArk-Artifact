"""最终报告结构化归一化与 Markdown 渲染。"""

from __future__ import annotations

import json
import re
from typing import Any

from flowark.types import (
    DataflowFinding,
    DataflowHop,
    DataflowSink,
    EvidenceRef,
    FinalDataflowReport,
    KnowledgeUsed,
    ReportSource,
    code_location_from_dict,
    dataflow_finding_from_dict,
    knowledge_used_from_dict,
    location_string_from_value,
    report_source_from_dict,
)


_FILE_REF_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)(?::(?P<line>\d+))?"
)
_KNOWLEDGE_REF_RE = re.compile(r"knowledge://(?P<skill_id>[A-Za-z0-9_.-]+)")


def _normalize_string_list(value: Any, *, limit: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    if limit is not None:
        return items[:limit]
    return items


class ReportNormalizer:
    """将 v2 / v1 / legacy Markdown 报告统一归一化为 v2 结构。"""

    _KNOWN_SINK_TYPES = [
        "network",
        "log",
        "file",
        "db",
        "database",
        "content-provider",
        "clipboard",
        "icc",
        "storage",
        "others",
    ]
    _SINK_TEXT_HINTS: dict[str, tuple[str, ...]] = {
        "network": ("network", "http", "https", "okhttp", "retrofit", "socket", "网络"),
        "log": ("log.", "logger", "logcat", "日志"),
        "icc": ("startactivity", "startservice", "sendbroadcast", "intent", "icc", "组件间通信"),
        "file": ("file", "openfileoutput", "fileoutputstream", "writebytes", "文件"),
        "database": ("database", "sqlite", "room", "dao", "contentprovider", "content-provider", "数据库"),
        "storage": ("storage", "clipboard", "setprimaryclip", "sharedpreferences", "bundle", "存储", "剪贴板"),
        "others": ("others", "other", "ui", "textview", "toast", "binding.", "显示", "用户可见", "其他"),
    }

    @staticmethod
    def _render_scalar(value: str | None) -> str:
        text = str(value or "").strip()
        return text if text else "null"

    @staticmethod
    def _render_location(value: str | None) -> str:
        text = str(value or "").strip()
        return text if text else "null"

    @staticmethod
    def _render_sequence(values: list[str]) -> str:
        return ", ".join(values) if values else "null"

    @classmethod
    def _render_string_section(cls, title: str, items: list[str]) -> list[str]:
        lines = [f"## {title}"]
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- None.")
        lines.append("")
        return lines

    def _extract_evidence_refs(self, markdown: str) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = []
        seen: set[tuple[str, int | None]] = set()
        for match in _FILE_REF_RE.finditer(markdown):
            file_path = match.group("file")
            line = int(match.group("line")) if match.group("line") else None
            key = (file_path, line)
            if key in seen:
                continue
            seen.add(key)
            refs.append(EvidenceRef(file=file_path, line=line))
        for match in _KNOWLEDGE_REF_RE.finditer(markdown):
            ref = f"knowledge://{match.group('skill_id')}"
            key = (ref, None)
            if key in seen:
                continue
            seen.add(key)
            refs.append(EvidenceRef(file=ref, reason="knowledge_bridge"))
        return refs

    def _extract_sink_types(self, markdown: str, requested: list[str] | None = None) -> list[str]:
        if requested:
            return list(dict.fromkeys(str(item).strip() for item in requested if str(item).strip()))
        text = markdown.lower()
        result: list[str] = []
        for sink_type in self._KNOWN_SINK_TYPES:
            if sink_type in text:
                result.append(sink_type)
        return result

    @classmethod
    def _legacy_default_sink_type(cls, fallback_sink_types: list[str]) -> str:
        return fallback_sink_types[0] if len(fallback_sink_types) == 1 else "unknown"

    @classmethod
    def _infer_sink_type_from_text(
        cls,
        text: str,
        *,
        fallback_sink_types: list[str],
    ) -> str | None:
        lowered = str(text or "").casefold()
        if not lowered:
            return None
        requested = {item.casefold() for item in fallback_sink_types if str(item).strip()}
        for sink_type, hints in cls._SINK_TEXT_HINTS.items():
            if requested and sink_type.casefold() not in requested:
                continue
            if any(hint.casefold() in lowered for hint in hints):
                return sink_type
        return None

    @classmethod
    def _legacy_section_sink_type(
        cls,
        line: str,
        *,
        fallback_sink_types: list[str],
    ) -> str | None:
        stripped = str(line or "").strip()
        if not stripped:
            return None
        if stripped.startswith("|"):
            return None
        heading_like = stripped.startswith("#") or bool(re.match(r"^(?:\d+[.)]\s*)?\*{0,2}[^|]{0,80}\([^)]{2,80}\)", stripped))
        if not heading_like:
            return None
        return cls._infer_sink_type_from_text(stripped, fallback_sink_types=fallback_sink_types)

    def _extract_uncertainties(self, markdown: str) -> list[str]:
        uncertainties: list[str] = []
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if any(token in lower for token in ["不确定", "待验证", "uncertain", "todo", "需要进一步"]):
                uncertainties.append(line[:500])
        return uncertainties[:20]

    def _extract_list_items_by_keyword(self, markdown: str, keywords: tuple[str, ...]) -> list[str]:
        results: list[str] = []
        for raw_line in markdown.splitlines():
            line = raw_line.strip("-* \t")
            lower = line.lower()
            if any(keyword in lower for keyword in keywords):
                results.append(line[:500])
        return results[:20]

    def _infer_knowledge_used(
        self,
        *,
        knowledge_usage: list[dict[str, Any]] | None,
        legacy_skill_ids: list[str] | None = None,
        evidence_refs: list[EvidenceRef] | None = None,
    ) -> KnowledgeUsed:
        notes: list[str] = []
        seen_notes: set[str] = set()

        for usage in knowledge_usage or []:
            details = usage.get("details") if isinstance(usage, dict) else None
            if not isinstance(details, list):
                continue
            for item in details:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip() != "skill":
                    continue
                skill_id = str(item.get("skill_id") or "").strip()
                if not skill_id:
                    continue
                if skill_id not in seen_notes:
                    seen_notes.add(skill_id)
                    notes.append(skill_id)

        for skill_id in legacy_skill_ids or []:
            text = str(skill_id or "").strip()
            if not text:
                continue
            if text.startswith("knowledge://"):
                text = text[len("knowledge://") :]
            if text and text not in seen_notes:
                seen_notes.add(text)
                notes.append(text)

        for ref in evidence_refs or []:
            value = str(ref.file or "").strip()
            if value.startswith("knowledge://"):
                skill_id = value[len("knowledge://") :]
                if skill_id and skill_id not in seen_notes:
                    seen_notes.add(skill_id)
                    notes.append(skill_id)

        return KnowledgeUsed(notes=notes[:20])

    @staticmethod
    def _location_string_from_file_line(file_value: str | None, line_value: Any) -> str | None:
        file_text = str(file_value or "").strip()
        if not file_text:
            return None
        if isinstance(line_value, bool):
            line_value = int(line_value)
        if isinstance(line_value, int):
            return f"{file_text}@{line_value}"
        return file_text

    @staticmethod
    def _normalize_text_scalar(value: str | None) -> str | None:
        text = str(value or "").replace("`", "").strip()
        text = re.sub(r"\s+", " ", text)
        return text or None

    @classmethod
    def _decode_json_string_values(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._decode_json_string_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._decode_json_string_values(item) for item in value]
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or text[0] not in "[{":
            return value
        try:
            decoded = json.loads(text)
        except Exception:
            return value
        return cls._decode_json_string_values(decoded)

    @classmethod
    def _normalize_anchor_text(cls, value: str | None, *, role: str) -> str | None:
        text = cls._normalize_text_scalar(value)
        if not text:
            return None
        if "->" in text or "→" in text:
            parts = [part.strip() for part in re.split(r"\s*(?:->|→)\s*", text) if part.strip()]
            if len(parts) >= 2:
                text = parts[0] if role == "from" else parts[-1]
        text = re.sub(r"\s+(?:@|:)\d+(?:-\d+)?$", "", text)
        text = text.strip(" -,:;")
        return text or None

    @classmethod
    def _relativize_to_app_root(cls, path_text: str, *, app_name: str | None) -> str:
        normalized = path_text.replace("\\", "/")
        normalized = re.sub(r"/{2,}", "/", normalized)
        if not app_name:
            return normalized
        lowered = normalized.casefold()
        markers = [
            f"/source code/{app_name}/",
            f"/{app_name}/",
        ]
        for marker in markers:
            idx = lowered.find(marker.casefold())
            if idx != -1:
                return normalized[idx + len(marker) :].lstrip("/")
        return normalized

    @classmethod
    def _normalize_location_text(cls, value: str | None, *, app_name: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("\\", "/")
        text = re.sub(r"/{2,}", "/", text)
        text = re.sub(r":(\d+(?:-\d+)?)$", r"@\1", text)
        suffix = ""
        match = re.search(r"(@\d+(?:-\d+)?)$", text)
        if match:
            suffix = match.group(1)
            text = text[: -len(suffix)]
        text = cls._relativize_to_app_root(text, app_name=app_name).strip()
        text = text.lstrip("./")
        return f"{text}{suffix}" if text else None

    @classmethod
    def _normalize_report_object(cls, report: FinalDataflowReport, *, app_name: str | None) -> FinalDataflowReport:
        report.query = str(report.query or "").strip()
        report.source.description = cls._normalize_text_scalar(report.source.description) or ""
        report.source.method = cls._normalize_text_scalar(report.source.method)
        report.source.location = cls._normalize_location_text(report.source.location, app_name=app_name)
        for item in report.dataflows:
            item.explain = cls._normalize_text_scalar(item.explain) or ""
            item.confidence = cls._normalize_text_scalar(item.confidence)
            item.sink.sink_type = cls._normalize_text_scalar(item.sink.sink_type) or ""
            item.sink.statement = cls._normalize_text_scalar(item.sink.statement)
            item.sink.method = cls._normalize_text_scalar(item.sink.method)
            item.sink.location = cls._normalize_location_text(item.sink.location, app_name=app_name)
            for hop in item.path:
                hop.description = cls._normalize_text_scalar(hop.description) or ""
                hop.from_step = cls._normalize_anchor_text(hop.from_step, role="from")
                hop.to_step = cls._normalize_anchor_text(hop.to_step, role="to")
                hop.location = cls._normalize_location_text(hop.location, app_name=app_name)
        return report

    def _build_hops_from_steps(self, steps: list[str], *, first_location: str | None = None) -> list[DataflowHop]:
        clean_steps = [str(step).strip() for step in steps if str(step).strip()]
        if len(clean_steps) >= 2:
            hops: list[DataflowHop] = []
            for index in range(len(clean_steps) - 1):
                hops.append(
                    DataflowHop(
                        description=f"{clean_steps[index]} -> {clean_steps[index + 1]}",
                        from_step=clean_steps[index],
                        to_step=clean_steps[index + 1],
                        location=(first_location if index == 0 else None),
                    )
                )
            return hops
        if len(clean_steps) == 1:
            return [DataflowHop(description=clean_steps[0], location=first_location)]
        return []

    def _best_sink_match_for_path(
        self,
        *,
        path_payload: dict[str, Any],
        sinks_found: list[dict[str, Any]],
        fallback_sink_types: list[str],
        sink_index: int,
    ) -> dict[str, Any] | None:
        sink_method = str(path_payload.get("sink_method_signature") or "").strip()
        if sink_method:
            for item in sinks_found:
                if str(item.get("method_signature") or "").strip() == sink_method:
                    return item
        if sinks_found:
            if sink_index < len(sinks_found):
                return sinks_found[sink_index]
            return sinks_found[0]
        if fallback_sink_types:
            return {"sink_type": fallback_sink_types[0]}
        return None

    def _normalize_v2_payload(
        self,
        *,
        payload: dict[str, Any],
        query: str,
        source_description: str | None,
        app_name: str | None,
        knowledge_usage: list[dict[str, Any]] | None,
        run_summary: dict[str, Any] | None,
        parse_error: str | None,
    ) -> FinalDataflowReport:
        source_payload = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source = report_source_from_dict(source_payload or {})
        if not source.description:
            source.description = str(source_description or "").strip()
        dataflows = [
            dataflow_finding_from_dict(item)
            for item in payload.get("dataflows", []) or []
            if isinstance(item, dict)
        ]
        knowledge_used = (
            knowledge_used_from_dict(payload.get("knowledge_used") or {})
            if isinstance(payload.get("knowledge_used"), dict)
            else KnowledgeUsed()
        )
        inferred = self._infer_knowledge_used(knowledge_usage=knowledge_usage)
        if not knowledge_used.notes:
            knowledge_used.notes = inferred.notes
        return self._normalize_report_object(
            FinalDataflowReport(
                query=str(payload.get("query") or query),
                schema_version="flowark-final-report-v2",
                source=source,
                dataflows=dataflows,
                uncertainties=_normalize_string_list(payload.get("uncertainties"), limit=20),
            skipped_branches=_normalize_string_list(payload.get("skipped_branches"), limit=20),
            knowledge_used=knowledge_used,
            knowledge_usage=list(knowledge_usage or []),
            run_summary=dict(run_summary or {}),
                parse_error=parse_error,
            ),
            app_name=app_name,
        )

    def _normalize_v1_payload(
        self,
        *,
        payload: dict[str, Any],
        query: str,
        source_description: str | None,
        sink_types: list[str] | None,
        app_name: str | None,
        knowledge_usage: list[dict[str, Any]] | None,
        run_summary: dict[str, Any] | None,
        parse_error: str | None,
    ) -> FinalDataflowReport:
        fallback_sink_types = self._extract_sink_types(
            "",
            payload.get("sink_types") if isinstance(payload.get("sink_types"), list) else sink_types,
        )
        source = ReportSource(
            description=str(payload.get("source_description") or (source_description or "")).strip(),
            method=(
                str(payload.get("source_method_signature")).strip()
                if payload.get("source_method_signature") is not None
                and str(payload.get("source_method_signature")).strip()
                else None
            ),
            location=location_string_from_value(payload.get("source_location")),
        )
        sinks_found = [
            item
            for item in payload.get("sinks_found", []) or []
            if isinstance(item, dict)
        ]
        dataflows: list[DataflowFinding] = []
        for index, path_payload in enumerate(payload.get("paths", []) or []):
            if not isinstance(path_payload, dict):
                continue
            steps = [str(v).strip() for v in (path_payload.get("steps") or []) if str(v).strip()]
            first_evidence = None
            evidence_refs = path_payload.get("evidence_refs")
            if isinstance(evidence_refs, list) and evidence_refs and isinstance(evidence_refs[0], dict):
                first_evidence = self._location_string_from_file_line(
                    evidence_refs[0].get("file"),
                    evidence_refs[0].get("line"),
                )
            sink_payload = self._best_sink_match_for_path(
                path_payload=path_payload,
                sinks_found=sinks_found,
                fallback_sink_types=fallback_sink_types,
                sink_index=index,
            )
            sink = DataflowSink(
                sink_type=str((sink_payload or {}).get("sink_type") or (fallback_sink_types[0] if fallback_sink_types else "unknown")).strip() or "unknown",
                statement=(
                    str((sink_payload or {}).get("description") or path_payload.get("sink") or "").strip() or None
                ),
                method=(
                    str(path_payload.get("sink_method_signature") or (sink_payload or {}).get("method_signature") or "").strip()
                    or None
                ),
                location=self._location_string_from_file_line(
                    (sink_payload or {}).get("file"),
                    (sink_payload or {}).get("line"),
                ),
            )
            hops = self._build_hops_from_steps(steps, first_location=first_evidence)
            if not hops and (path_payload.get("source") or path_payload.get("sink")):
                hops = [
                    DataflowHop(
                        description=(
                            f"{str(path_payload.get('source') or '').strip()} -> "
                            f"{str(path_payload.get('sink') or '').strip()}"
                        ).strip(" ->"),
                        from_step=(
                            str(path_payload.get("source")).strip()
                            if path_payload.get("source") and str(path_payload.get("source")).strip()
                            else None
                        ),
                        to_step=(
                            str(path_payload.get("sink")).strip()
                            if path_payload.get("sink") and str(path_payload.get("sink")).strip()
                            else None
                        ),
                        location=first_evidence,
                    )
                ]
            dataflows.append(
                DataflowFinding(
                    explain=str(path_payload.get("sink") or sink.statement or "Detected dataflow").strip(),
                    confidence=(
                        str(path_payload.get("confidence")).strip()
                        if path_payload.get("confidence") and str(path_payload.get("confidence")).strip()
                        else None
                    ),
                    sink=sink,
                    path=hops,
                )
            )

        if not dataflows:
            for sink_payload in sinks_found:
                sink = DataflowSink(
                    sink_type=str(sink_payload.get("sink_type") or (fallback_sink_types[0] if fallback_sink_types else "unknown")).strip() or "unknown",
                    statement=(
                        str(sink_payload.get("description") or "").strip() or None
                    ),
                    method=(
                        str(sink_payload.get("method_signature") or "").strip() or None
                    ),
                    location=self._location_string_from_file_line(
                        sink_payload.get("file"),
                        sink_payload.get("line"),
                    ),
                )
                dataflows.append(
                    DataflowFinding(
                        explain=sink.statement or "Detected sink",
                        confidence=(
                            str(sink_payload.get("confidence")).strip()
                            if sink_payload.get("confidence") and str(sink_payload.get("confidence")).strip()
                            else None
                        ),
                        sink=sink,
                        path=[],
                    )
                )

        evidence_refs = [
            EvidenceRef(
                file=str(item.get("file") or ""),
                line=item.get("line"),
                symbol=item.get("symbol"),
            )
            for item in payload.get("evidence_refs", []) or []
            if isinstance(item, dict)
        ]
        knowledge_used = self._infer_knowledge_used(
            knowledge_usage=knowledge_usage,
            legacy_skill_ids=_normalize_string_list(payload.get("knowledge_skills_used"), limit=20),
            evidence_refs=evidence_refs,
        )
        return self._normalize_report_object(
            FinalDataflowReport(
                query=str(payload.get("query") or query),
                schema_version="flowark-final-report-v2",
                source=source,
                dataflows=dataflows,
                uncertainties=_normalize_string_list(payload.get("uncertainties"), limit=20),
            skipped_branches=_normalize_string_list(payload.get("skipped_branches"), limit=20),
            knowledge_used=knowledge_used,
            knowledge_usage=list(knowledge_usage or []),
            run_summary=dict(run_summary or {}),
                parse_error=parse_error,
            ),
            app_name=app_name,
        )

    def normalize_structured_payload(
        self,
        *,
        raw_payload: dict[str, Any] | None,
        query: str,
        source_description: str | None = None,
        sink_types: list[str] | None = None,
        app_name: str | None = None,
        knowledge_usage: list[dict[str, Any]] | None = None,
        run_summary: dict[str, Any] | None = None,
        parse_error: str | None = None,
    ) -> FinalDataflowReport:
        decoded_payload = self._decode_json_string_values(raw_payload)
        payload = decoded_payload if isinstance(decoded_payload, dict) else {}
        if isinstance(payload.get("source"), dict) or isinstance(payload.get("dataflows"), list):
            return self._normalize_v2_payload(
                payload=payload,
                query=query,
                source_description=source_description,
                app_name=app_name,
                knowledge_usage=knowledge_usage,
                run_summary=run_summary,
                parse_error=parse_error,
            )
        return self._normalize_v1_payload(
            payload=payload,
            query=query,
            source_description=source_description,
            sink_types=sink_types,
            app_name=app_name,
            knowledge_usage=knowledge_usage,
            run_summary=run_summary,
            parse_error=parse_error,
        )

    def render_markdown(self, report: FinalDataflowReport) -> str:
        lines: list[str] = ["# Final Report", ""]
        lines.extend(
            [
                "## Source",
                f"- Query: {self._render_scalar(report.query)}",
                f"- Description: {self._render_scalar(report.source.description)}",
                f"- Method: {self._render_scalar(report.source.method)}",
                f"- Location: {self._render_location(report.source.location)}",
                f"- Sink Types: {self._render_sequence(report.sink_types)}",
                "",
            ]
        )

        lines.append("## Dataflows")
        if report.dataflows:
            for index, item in enumerate(report.dataflows, start=1):
                lines.extend(
                    [
                        f"### Dataflow {index}",
                        f"- Explain: {self._render_scalar(item.explain)}",
                        f"- Confidence: {self._render_scalar(item.confidence)}",
                        f"- Sink Type: {self._render_scalar(item.sink.sink_type)}",
                        f"- Sink Statement: {self._render_scalar(item.sink.statement)}",
                        f"- Sink Method: {self._render_scalar(item.sink.method)}",
                        f"- Sink Location: {self._render_location(item.sink.location)}",
                        "- Path:",
                    ]
                )
                if item.path:
                    for hop_index, hop in enumerate(item.path, start=1):
                        lines.append(
                            f"  {hop_index}. {self._render_scalar(hop.description)} "
                            f"(from={self._render_scalar(hop.from_step)}, "
                            f"to={self._render_scalar(hop.to_step)}, "
                            f"location={self._render_location(hop.location)})"
                        )
                else:
                    lines.append("  - None.")
        else:
            lines.append("- None.")
        lines.append("")

        lines.extend(self._render_string_section("Uncertainties", report.uncertainties))
        lines.extend(self._render_string_section("Skipped Branches", report.skipped_branches))
        lines.extend(
            [
                "## Knowledge Used",
                f"- Notes: {self._render_sequence(report.knowledge_used.notes)}",
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def normalize(
        self,
        final_markdown: str,
        query: str,
        *,
        source_description: str | None = None,
        sink_types: list[str] | None = None,
        app_name: str | None = None,
        knowledge_usage: list[dict[str, Any]] | None = None,
        run_summary: dict[str, Any] | None = None,
        parse_error: str | None = None,
    ) -> FinalDataflowReport:
        """归一化 legacy Markdown 报告。"""
        try:
            evidence_refs = self._extract_evidence_refs(final_markdown)
            normalized_sink_types = self._extract_sink_types(final_markdown, sink_types)
            uncertainties = self._extract_uncertainties(final_markdown)
            skipped_branches = self._extract_list_items_by_keyword(final_markdown, ("跳过", "skipped", "skip"))
            dataflows: list[DataflowFinding] = []
            current_section_sink_type: str | None = None
            for raw_line in final_markdown.splitlines():
                section_sink_type = self._legacy_section_sink_type(
                    raw_line,
                    fallback_sink_types=normalized_sink_types,
                )
                if section_sink_type:
                    current_section_sink_type = section_sink_type
                line = raw_line.strip("-* \t")
                if "->" not in line and "→" not in line:
                    continue
                arrow = "→" if "→" in line else "->"
                steps = [part.strip() for part in line.split(arrow) if part.strip()]
                if len(steps) < 2:
                    continue
                sink_type = (
                    self._infer_sink_type_from_text(line, fallback_sink_types=normalized_sink_types)
                    or current_section_sink_type
                    or self._legacy_default_sink_type(normalized_sink_types)
                )
                dataflows.append(
                    DataflowFinding(
                        explain=f"{steps[0]} reaches {steps[-1]}",
                        sink=DataflowSink(
                            sink_type=sink_type,
                            statement=steps[-1],
                        ),
                        path=self._build_hops_from_steps(steps),
                    )
                )
            knowledge_used = self._infer_knowledge_used(
                knowledge_usage=knowledge_usage,
                evidence_refs=evidence_refs,
            )
            return self._normalize_report_object(
                FinalDataflowReport(
                    query=query,
                    source=ReportSource(description=source_description or ""),
                    dataflows=dataflows,
                    uncertainties=uncertainties,
                    skipped_branches=skipped_branches,
                knowledge_used=knowledge_used,
                knowledge_usage=knowledge_usage or [],
                run_summary=run_summary or {},
                final_report_markdown=final_markdown,
                parse_error=parse_error,
                ),
                app_name=app_name,
            )
        except Exception as exc:  # pragma: no cover - 容错分支
            return self._normalize_report_object(
                FinalDataflowReport(
                    query=query,
                    source=ReportSource(description=source_description or ""),
                    knowledge_usage=knowledge_usage or [],
                    run_summary=run_summary or {},
                    final_report_markdown=final_markdown,
                    parse_error=parse_error or str(exc),
                ),
                app_name=app_name,
            )
