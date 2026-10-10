"""FlowArk 公共类型定义。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal


ValidationStatus = Literal["PASS", "REVISE", "REJECT"]
MatchRuleKind = Literal["exact_symbol", "symbol_tail", "package_prefix", "call"]
_LEGACY_KNOWLEDGE_METADATA_KEYS = {"node_type", "related" + "_note_ids", "related" + "_note_hints"}


def to_jsonable(value: Any) -> Any:
    """将 dataclass / Path 递归转换为 JSON 可序列化对象。"""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


@dataclass(slots=True)
class AnalysisRequest:
    """单次分析请求。"""

    query: str
    source: str | None = None
    sink_types: list[str] = field(default_factory=list)
    app_name: str | None = None


@dataclass(slots=True)
class EvidenceRef:
    """证据引用。"""

    file: str
    line: int | None = None
    symbol: str | None = None
    snippet: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class CodeLocation:
    """代码位置。"""

    file: str | None = None
    line: int | None = None


@dataclass(slots=True)
class SinkFinding:
    """命中的 sink。"""

    sink_type: str
    description: str
    file: str | None = None
    line: int | None = None
    symbol: str | None = None
    confidence: str | None = None
    method_signature: str | None = None
    endpoint: str | None = None


@dataclass(slots=True)
class DataflowPath:
    """数据流路径。"""

    path_id: str
    source: str | None = None
    sink: str | None = None
    steps: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    status: str = "confirmed"
    confidence: str | None = None
    source_method_signature: str | None = None
    sink_method_signature: str | None = None


@dataclass(slots=True)
class ReportSource:
    """最终报告中的 source 摘要。"""

    description: str = ""
    method: str | None = None
    location: str | None = None


@dataclass(slots=True)
class DataflowSink:
    """最终报告中的 sink 摘要。"""

    sink_type: str = ""
    statement: str | None = None
    method: str | None = None
    location: str | None = None


@dataclass(slots=True)
class DataflowHop:
    """最终报告中的单跳路径。"""

    description: str = ""
    from_step: str | None = None
    to_step: str | None = None
    location: str | None = None


@dataclass(slots=True)
class DataflowFinding:
    """最终报告中的一条 dataflow。"""

    explain: str = ""
    confidence: str | None = None
    sink: DataflowSink = field(default_factory=DataflowSink)
    method_call_chain: list[str] = field(default_factory=list)
    path: list[DataflowHop] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeUsed:
    """最终报告引用到的知识条目。"""

    notes: list[str] = field(default_factory=list)
    flow_facts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeMatch:
    """知识命中结果。"""

    skill_id: str
    skill_name: str
    score: float
    validation_status: str | None = None
    reasons: list[str] = field(default_factory=list)
    match_fields: list[str] = field(default_factory=list)
    summary: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    file_path: str | None = None
    match_stage: str | None = None
    legacy_task_specific: bool = False


@dataclass(slots=True)
class EgressCase:
    """局部出口表中的单个分支。"""

    selectors: list[str] = field(default_factory=list)
    negative_selectors: list[str] = field(default_factory=list)
    next_hops: list[str] = field(default_factory=list)
    summary: str = ""
    evidence_refs: list[EvidenceRef] = field(default_factory=list)


@dataclass(slots=True)
class EgressMap:
    """绑定在 note 上的局部出口表 sidecar。"""

    schema_version: str = "flowark-egress-map-v2"
    note_id: str = ""
    boundary_summary: str = ""
    key_apis: list[str] = field(default_factory=list)
    cases: list[EgressCase] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeInjectionRecord:
    """知识注入记录。"""

    timestamp: str
    mode: str
    query_excerpt: str
    matched_skill_ids: list[str] = field(default_factory=list)
    selected_skill_ids: list[str] = field(default_factory=list)
    dropped_skill_ids: list[str] = field(default_factory=list)
    dropped_skill_reasons: dict[str, str] = field(default_factory=dict)
    injected_skill_ids: list[str] = field(default_factory=list)
    injected_chars: int = 0
    used_summary_only: bool = True
    details: list[dict[str, Any]] = field(default_factory=list)
    hook_event_name: str | None = None
    delta: bool = False
    matched_rules: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeCandidate:
    """知识候选。"""

    id: str
    name: str
    match_rules: MatchRules | None = None
    entry_condition: str = ""
    schema_version: str | None = None
    app_name: str | None = None
    content: str = ""
    sources: list[str] = field(default_factory=list)
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    egress_map: EgressMap | None = None


@dataclass(slots=True)
class MatchRule:
    """知识匹配规则。"""

    kind: MatchRuleKind
    value: str = ""
    receiver: str | None = None
    method: str | None = None


@dataclass(slots=True)
class MatchRules:
    """知识匹配规则集合。"""

    require_all: list[MatchRule] = field(default_factory=list)
    require_any: list[MatchRule] = field(default_factory=list)
    exclude: list[MatchRule] = field(default_factory=list)


@dataclass(slots=True)
class RuleMatchResult:
    """单个规则集的匹配结果。"""

    matched: bool
    score: int
    matched_require_all: list[str] = field(default_factory=list)
    matched_require_any: list[str] = field(default_factory=list)
    matched_exclude: list[str] = field(default_factory=list)
    probe_hits: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuleCandidateMatch:
    """候选知识规则匹配结果。"""

    candidate_id: str
    score: int
    matched: bool
    matched_require_all: list[str] = field(default_factory=list)
    matched_require_any: list[str] = field(default_factory=list)
    matched_exclude: list[str] = field(default_factory=list)


def match_rule_from_dict(data: dict[str, Any]) -> MatchRule:
    """从字典解析匹配规则。"""
    if not isinstance(data, dict):
        raise ValueError("match rule must be object")
    kind = str(data.get("kind") or "").strip()
    if kind not in {"exact_symbol", "symbol_tail", "package_prefix", "call"}:
        raise ValueError(f"unsupported match rule kind: {kind or '-'}")
    value = str(data.get("value") or "").strip()
    receiver_raw = data.get("receiver")
    receiver = str(receiver_raw).strip() if receiver_raw is not None else ""
    method_raw = data.get("method")
    method = str(method_raw).strip() if method_raw is not None else ""
    return MatchRule(
        kind=kind,
        value=value,
        receiver=receiver or None,
        method=method or None,
    )


def match_rules_from_dict(data: dict[str, Any]) -> MatchRules:
    """从字典解析规则集合。"""
    if not isinstance(data, dict):
        raise ValueError("match_rules must be object")
    allowed_keys = {"require_all", "require_any", "exclude"}
    extra_keys = sorted(str(key) for key in data.keys() if str(key) not in allowed_keys)
    if extra_keys:
        raise ValueError(f"unsupported match_rules keys: {', '.join(extra_keys)}")

    def _parse_bucket(name: str) -> list[MatchRule]:
        raw = data.get(name, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"match_rules.{name} must be list")
        parsed: list[MatchRule] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"match_rules.{name} items must be object")
            parsed.append(match_rule_from_dict(item))
        return parsed

    return MatchRules(
        require_all=_parse_bucket("require_all"),
        require_any=_parse_bucket("require_any"),
        exclude=_parse_bucket("exclude"),
    )


@dataclass(slots=True)
class RealtimeKnowledgeState:
    """单个 agent 会话的实时知识注入状态（仅进程内缓存）。"""

    session_id: str
    transcript_path: str | None = None
    last_transcript_offset: int = 0
    seen_terms: set[str] = field(default_factory=set)
    seen_message_fingerprints: set[str] = field(default_factory=set)
    injected_skill_ids: set[str] = field(default_factory=set)
    injected_skill_counts: dict[str, int] = field(default_factory=dict)
    last_injected_ts_by_skill: dict[str, float] = field(default_factory=dict)
    last_injected_hook_by_skill: dict[str, int] = field(default_factory=dict)
    injected_note_case_keys: set[str] = field(default_factory=set)
    injected_note_case_counts: dict[str, int] = field(default_factory=dict)
    last_injected_ts_by_note_case: dict[str, float] = field(default_factory=dict)
    last_injected_hook_by_note_case: dict[str, int] = field(default_factory=dict)
    react_turn_index: int = 0
    seen_react_turn_ids: set[str] = field(default_factory=set)
    last_full_react_turn_by_skill: dict[str, int] = field(default_factory=dict)
    last_summary_react_turn_by_skill: dict[str, int] = field(default_factory=dict)
    analysis_log_rag_initial_request_submit_attempted: bool = False
    last_delta_injection_ts: float = 0.0
    delta_injection_count: int = 0
    hook_index: int = 0


@dataclass(slots=True)
class ValidationResult:
    """知识验证结果。"""

    candidate_id: str
    status: ValidationStatus
    reasons: list[str] = field(default_factory=list)
    normalized_candidate: KnowledgeCandidate | None = None
    evidence_summary: str | None = None


@dataclass(slots=True)
class FinalDataflowReport:
    """最终结构化数据流报告。"""

    query: str
    schema_version: str = "flowark-final-report-v2"
    source: ReportSource = field(default_factory=ReportSource)
    dataflows: list[DataflowFinding] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    skipped_branches: list[str] = field(default_factory=list)
    knowledge_used: KnowledgeUsed = field(default_factory=KnowledgeUsed)
    knowledge_usage: list[dict[str, Any]] = field(default_factory=list)
    run_summary: dict[str, Any] = field(default_factory=dict)
    final_report_markdown: str = ""
    parse_error: str | None = None

    @property
    def sink_types(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in self.dataflows:
            sink_type = str(item.sink.sink_type or "").strip()
            if not sink_type or sink_type in seen:
                continue
            seen.add(sink_type)
            result.append(sink_type)
        return result

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query": self.query,
            "source": {
                "description": self.source.description,
                "method": self.source.method,
                "location": self.source.location,
            },
            "dataflows": [
                {
                    "explain": item.explain,
                    "confidence": item.confidence,
                    "sink": {
                        "sink_type": item.sink.sink_type,
                        "statement": item.sink.statement,
                        "method": item.sink.method,
                        "location": item.sink.location,
                    },
                    "method_call_chain": list(item.method_call_chain),
                    "path": [
                        {
                            "description": hop.description,
                            "from": hop.from_step,
                            "to": hop.to_step,
                            "location": hop.location,
                        }
                        for hop in item.path
                    ],
                }
                for item in self.dataflows
            ],
            "uncertainties": list(self.uncertainties),
            "skipped_branches": list(self.skipped_branches),
            "knowledge_used": {
                "notes": list(self.knowledge_used.notes),
                "flow_facts": list(self.knowledge_used.flow_facts),
            },
        }


@dataclass(slots=True)
class RunArtifacts:
    """单次运行产物。"""

    run_dir: Path | None
    final_report_md: Path | None
    final_report_json: Path | None
    raw_messages: list[str] = field(default_factory=list)
    knowledge_injection_log: Path | None = None
    cost_summary_json: Path | None = None
    final_report: FinalDataflowReport | None = None


def evidence_ref_from_dict(data: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        file=str(data.get("file", "")),
        line=data.get("line"),
        symbol=data.get("symbol"),
        snippet=data.get("snippet"),
        reason=data.get("reason"),
    )


def code_location_from_dict(data: dict[str, Any]) -> CodeLocation:
    line = data.get("line")
    if isinstance(line, bool):
        line = int(line)
    elif not isinstance(line, int):
        line = None
    file_value = data.get("file")
    return CodeLocation(
        file=(str(file_value).strip() if file_value is not None and str(file_value).strip() else None),
        line=line,
    )


def location_string_from_value(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        location = code_location_from_dict(value)
        if not location.file:
            return None
        if isinstance(location.line, int):
            return f"{location.file}@{location.line}"
        return location.file
    return None


def report_source_from_dict(data: dict[str, Any]) -> ReportSource:
    return ReportSource(
        description=str(data.get("description") or "").strip(),
        method=(str(data["method"]).strip() if data.get("method") and str(data["method"]).strip() else None),
        location=location_string_from_value(data.get("location")),
    )


def dataflow_sink_from_dict(data: dict[str, Any]) -> DataflowSink:
    return DataflowSink(
        sink_type=str(data.get("sink_type") or "").strip(),
        statement=(
            str(data["statement"]).strip()
            if data.get("statement") and str(data["statement"]).strip()
            else None
        ),
        method=(str(data["method"]).strip() if data.get("method") and str(data["method"]).strip() else None),
        location=location_string_from_value(data.get("location")),
    )


def dataflow_hop_from_dict(data: dict[str, Any]) -> DataflowHop:
    return DataflowHop(
        description=str(data.get("description") or "").strip(),
        from_step=(str(data["from"]).strip() if data.get("from") and str(data["from"]).strip() else None),
        to_step=(str(data["to"]).strip() if data.get("to") and str(data["to"]).strip() else None),
        location=location_string_from_value(data.get("location")),
    )


def dataflow_finding_from_dict(data: dict[str, Any]) -> DataflowFinding:
    path = [
        dataflow_hop_from_dict(item)
        for item in data.get("path", []) or []
        if isinstance(item, dict)
    ]
    method_call_chain = [
        str(item).strip()
        for item in (data.get("method_call_chain") or [])
        if str(item).strip()
    ]
    return DataflowFinding(
        explain=str(data.get("explain") or "").strip(),
        confidence=(str(data["confidence"]).strip() if data.get("confidence") and str(data["confidence"]).strip() else None),
        sink=dataflow_sink_from_dict(data.get("sink") or {}) if isinstance(data.get("sink"), dict) else DataflowSink(),
        method_call_chain=method_call_chain,
        path=path,
    )


def knowledge_used_from_dict(data: dict[str, Any]) -> KnowledgeUsed:
    notes = [str(v).strip() for v in (data.get("notes") or []) if str(v).strip()]
    flow_facts = [str(v).strip() for v in (data.get("flow_facts") or []) if str(v).strip()]
    return KnowledgeUsed(notes=notes, flow_facts=flow_facts)


def sink_finding_from_dict(data: dict[str, Any]) -> SinkFinding:
    line = data.get("line")
    if isinstance(line, bool):
        line = int(line)
    elif not isinstance(line, int):
        line = None
    return SinkFinding(
        sink_type=str(data.get("sink_type", "")).strip(),
        description=str(data.get("description", "")).strip(),
        file=(str(data["file"]).strip() if data.get("file") and str(data["file"]).strip() else None),
        line=line,
        symbol=(str(data["symbol"]).strip() if data.get("symbol") and str(data["symbol"]).strip() else None),
        confidence=(str(data["confidence"]).strip() if data.get("confidence") and str(data["confidence"]).strip() else None),
        method_signature=(
            str(data["method_signature"]).strip()
            if data.get("method_signature") and str(data["method_signature"]).strip()
            else None
        ),
        endpoint=(str(data["endpoint"]).strip() if data.get("endpoint") and str(data["endpoint"]).strip() else None),
    )


def dataflow_path_from_dict(data: dict[str, Any]) -> DataflowPath:
    evidence_refs = [
        evidence_ref_from_dict(item)
        for item in data.get("evidence_refs", []) or []
        if isinstance(item, dict)
    ]
    steps = [str(v).strip() for v in (data.get("steps") or []) if str(v).strip()]
    return DataflowPath(
        path_id=str(data.get("path_id", "")).strip(),
        source=(str(data["source"]).strip() if data.get("source") and str(data["source"]).strip() else None),
        sink=(str(data["sink"]).strip() if data.get("sink") and str(data["sink"]).strip() else None),
        steps=steps,
        evidence_refs=evidence_refs,
        status=str(data.get("status") or "confirmed").strip() or "confirmed",
        confidence=(str(data["confidence"]).strip() if data.get("confidence") and str(data["confidence"]).strip() else None),
        source_method_signature=(
            str(data["source_method_signature"]).strip()
            if data.get("source_method_signature") and str(data["source_method_signature"]).strip()
            else None
        ),
        sink_method_signature=(
            str(data["sink_method_signature"]).strip()
            if data.get("sink_method_signature") and str(data["sink_method_signature"]).strip()
            else None
        ),
    )


def egress_case_from_dict(data: dict[str, Any]) -> EgressCase:
    evidence_refs = [
        evidence_ref_from_dict(item)
        for item in data.get("evidence_refs", []) or []
        if isinstance(item, dict)
    ]
    return EgressCase(
        selectors=[str(v) for v in (data.get("selectors") or []) if str(v).strip()],
        negative_selectors=[str(v) for v in (data.get("negative_selectors") or []) if str(v).strip()],
        next_hops=[str(v) for v in (data.get("next_hops") or []) if str(v).strip()],
        summary=str(data.get("summary", "")).strip(),
        evidence_refs=evidence_refs,
    )


def egress_map_from_dict(data: dict[str, Any]) -> EgressMap:
    if "selector_kind" in data:
        raise ValueError("flowark-egress-map-v2 不再允许 selector_kind")
    cases = [
        egress_case_from_dict(item)
        for item in data.get("cases", []) or []
        if isinstance(item, dict)
    ]
    return EgressMap(
        schema_version=str(data.get("schema_version") or "flowark-egress-map-v2").strip() or "flowark-egress-map-v2",
        note_id=str(data.get("note_id", "")).strip(),
        boundary_summary=str(data.get("boundary_summary", "")).strip(),
        key_apis=[str(v) for v in (data.get("key_apis") or []) if str(v).strip()],
        cases=cases,
    )


def knowledge_candidate_from_dict(data: dict[str, Any]) -> KnowledgeCandidate:
    if "anchors" in data or "negative_anchors" in data:
        raise ValueError("flowark skill 不再允许 anchors / negative_anchors")
    raw_node_type = data.get("node_type", data.get("type", None))
    if raw_node_type is not None:
        node_type = str(raw_node_type or "").strip().lower() or "note"
        if node_type != "note":
            raise ValueError("flowark skill 只支持 note")
    evidence_refs = [
        evidence_ref_from_dict(item)
        for item in data.get("evidence_refs", []) or []
        if isinstance(item, dict)
    ]
    egress_map = None
    match_rules = None
    if isinstance(data.get("match_rules"), dict):
        match_rules = match_rules_from_dict(data.get("match_rules") or {})
    if isinstance(data.get("egress_map"), dict):
        egress_map = egress_map_from_dict(data.get("egress_map") or {})
    metadata = {
        str(key): value
        for key, value in dict(data.get("metadata") or {}).items()
        if str(key) not in _LEGACY_KNOWLEDGE_METADATA_KEYS
    }
    if data.get("knowledge_packaging_mode") is not None:
        metadata["knowledge_packaging_mode"] = str(data.get("knowledge_packaging_mode") or "").strip()
    return KnowledgeCandidate(
        id=str(data.get("id", "")).strip(),
        name=str(data.get("name", "")).strip(),
        match_rules=match_rules,
        entry_condition=str(data.get("entry_condition", "")).strip(),
        schema_version=(str(data["schema_version"]) if data.get("schema_version") else None),
        app_name=(str(data["app_name"]).strip() if data.get("app_name") and str(data["app_name"]).strip() else None),
        content=str(data.get("content", "")),
        sources=[str(v) for v in (data.get("sources") or []) if str(v).strip()],
        version=int(data.get("version", 1) or 1),
        metadata=metadata,
        evidence_refs=evidence_refs,
        egress_map=egress_map,
    )
