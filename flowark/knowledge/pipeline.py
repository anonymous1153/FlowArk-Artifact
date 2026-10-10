"""知识生成/验证/持久化 pipeline。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from flowark.knowledge.content_utils import SUMMARY_PREFIX, SUMMARY_PREFIXES, ensure_core_conclusion
from flowark.knowledge.manager import (
    KnowledgeManager,
    SKILL_SCHEMA_V4,
    SKILL_SCHEMA_V5,
    normalize_app_name,
    scope_dir_name_for_app,
)
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    KNOWLEDGE_PACKAGING_METADATA_KEY,
    normalize_knowledge_packaging_mode,
)
from flowark.knowledge.rule_matcher import (
    audit_match_rules,
    compile_match_rule,
    describe_match_rule,
    has_recallable_require_any,
    is_strong_positive_rule,
    normalize_match_rules,
    rule_matches_text,
)
from flowark.types import (
    EgressCase,
    EgressMap,
    EvidenceRef,
    KnowledgeCandidate,
    MatchRule,
    MatchRules,
    ValidationResult,
    knowledge_candidate_from_dict,
    match_rules_from_dict,
    to_jsonable,
)

_BANNED_PERSISTENT_METADATA_KEYS = {
    "query",
    "source",
    "sink_types",
    "knowledge_kind",
    "routing_keywords",
    "routing_symbols",
    "scope",
    "description",
    "type",
}
_ALLOWED_PERSISTENT_FRONTMATTER_KEYS = [
    "schema_version",
    "id",
    "app_name",
    "name",
    "version",
    "validation_status",
    "knowledge_packaging_mode",
    "match_rules",
    "entry_condition",
]
_REQUIRED_PERSISTENT_FRONTMATTER_KEYS = [
    "schema_version",
    "id",
    "name",
    "validation_status",
    "entry_condition",
]
_LEGACY_REL_IDS_KEY = "related" + "_note_ids"
_BLOCKING_RULE_AUDIT_ISSUES = {
    "invalid_call_shape",
    "framework_prefix_as_strong_rule",
    "generic_symbol_tail_as_strong_rule",
    "broad_require_any_rule",
    "weak_require_any_only",
    "package_prefix_only_positive_evidence",
    "require_all_missing_stable_anchor",
    "require_all_single_weak_anchor",
}
_REVISE_RULE_AUDIT_ISSUES = {
    "generic_runtime_anchor_in_require_any",
    "generic_runtime_anchor_only_require_all",
}
_RULE_AUDIT_REASON_MESSAGES = {
    "invalid_call_shape": "match_rules 中存在格式错误的 call 规则",
    "framework_prefix_as_strong_rule": "require_any 不能只靠通用框架 package_prefix 作为主锚点",
    "generic_symbol_tail_as_strong_rule": "require_any 不能只靠通用角色词/基类名作为主锚点",
    "broad_require_any_rule": "require_any 依赖过宽规则，存在高风险误注入",
    "weak_require_any_only": "require_any 不能只有弱 call(method-only)",
    "package_prefix_only_positive_evidence": "require_any 不能只靠 package_prefix 组合作为正证据",
    "generic_runtime_anchor_in_require_any": "require_any 不能包含泛运行时锚点；请改用 require_all 搭配具体 key/domain/sink 锚点",
    "generic_runtime_anchor_only_require_all": "require_all 不能只由泛运行时锚点组成",
    "require_all_missing_stable_anchor": "require_all 必须包含至少一个非泛、可稳定定位的 domain/key/sink 锚点",
    "require_all_single_weak_anchor": "require_all 不能只靠单条弱锚点触发",
}
_REPAIRABLE_RULE_FAILURE_REASON_MESSAGES = {
    **_RULE_AUDIT_REASON_MESSAGES,
    "missing_positive_rules": "缺少 match_rules.require_all / require_any，无法稳定匹配",
    "missing_recallable_positive_rules": "match_rules 缺少可召回的 require_all / require_any 组合，存在高风险误注入",
}


@dataclass(slots=True)
class _RepoRuleHitSummary:
    require_all_hits: list[str] = field(default_factory=list)
    require_any_strong_hits: list[str] = field(default_factory=list)
    require_any_weak_hits: list[str] = field(default_factory=list)

    def total_hits(self) -> int:
        return (
            len(self.require_all_hits)
            + len(self.require_any_strong_hits)
            + len(self.require_any_weak_hits)
        )

    def has_require_all_hits(self) -> bool:
        return bool(self.require_all_hits)

    def has_require_any_strong_hits(self) -> bool:
        return bool(self.require_any_strong_hits)

    def any_hits(self) -> bool:
        return self.total_hits() > 0


def _dedupe(values: Iterable[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _blocking_rule_audit_issues(rules: MatchRules) -> list[dict[str, object]]:
    return [issue for issue in audit_match_rules(rules) if str(issue.get("type")) in _BLOCKING_RULE_AUDIT_ISSUES]


def _rule_audit_issue_types(rules: MatchRules) -> set[str]:
    return {
        str(issue.get("type") or "").strip()
        for issue in audit_match_rules(rules)
        if str(issue.get("type") or "").strip()
    }


def _rule_audit_reasons(issues: list[dict[str, object]]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        issue_type = str(issue.get("type") or "").strip()
        message = _RULE_AUDIT_REASON_MESSAGES.get(issue_type)
        if not message or message in seen:
            continue
        seen.add(message)
        reasons.append(message)
    return reasons or ["match_rules 存在高风险误注入问题"]


def _normalize_candidate_for_validation(candidate: KnowledgeCandidate) -> KnowledgeCandidate:
    normalized_candidate = knowledge_candidate_from_dict(to_jsonable(candidate))
    normalized_candidate.schema_version = SKILL_SCHEMA_V5
    if normalized_candidate.match_rules is not None:
        try:
            normalized_candidate.match_rules = normalize_match_rules(normalized_candidate.match_rules)
        except Exception:
            # 规则本身可能就是本轮静态拒绝/repair 的目标，不能在归一化阶段直接抛弃候选。
            normalized_candidate.match_rules = normalized_candidate.match_rules
    normalized_candidate.entry_condition = normalized_candidate.entry_condition.strip()
    normalized_candidate.content = ensure_core_conclusion(
        normalized_candidate.content,
        fallback=normalized_candidate.name or "请结合 entry_condition 使用本知识。",
    )
    return normalized_candidate


def _rules_need_strong_require_any(rules: MatchRules | None) -> bool:
    if rules is None:
        return True
    return not has_recallable_require_any(rules)


def _repairable_rule_issue_types(rules: MatchRules | None) -> list[str]:
    issue_types: list[str] = []
    seen: set[str] = set()

    def _append_issue(issue_type: str) -> None:
        text = str(issue_type or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        issue_types.append(text)

    if rules is None or (not rules.require_all and not rules.require_any):
        _append_issue("missing_positive_rules")
        return issue_types

    audit_issue_types = _rule_audit_issue_types(rules)
    for issue_type in sorted(audit_issue_types):
        if issue_type in _BLOCKING_RULE_AUDIT_ISSUES:
            _append_issue(issue_type)

    if _rules_need_strong_require_any(rules) and not (audit_issue_types & _REVISE_RULE_AUDIT_ISSUES):
        _append_issue("missing_recallable_positive_rules")
    return issue_types


def _repairable_rule_issue_reasons(issue_types: Iterable[str]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for issue_type in issue_types:
        message = _REPAIRABLE_RULE_FAILURE_REASON_MESSAGES.get(str(issue_type or "").strip())
        if not message or message in seen:
            continue
        seen.add(message)
        reasons.append(message)
    return reasons


def _candidate_repairable_rule_issue_types(candidate: KnowledgeCandidate) -> list[str]:
    normalized_candidate = _normalize_candidate_for_validation(candidate)
    return _repairable_rule_issue_types(normalized_candidate.match_rules)


def _append_unique_reason(reasons: list[str], seen: set[str], reason: str) -> None:
    text = str(reason or "").strip()
    if not text or text in seen:
        return
    seen.add(text)
    reasons.append(text)


def _extend_unique_reasons(reasons: list[str], seen: set[str], new_reasons: Iterable[str]) -> None:
    for reason in new_reasons:
        _append_unique_reason(reasons, seen, str(reason))


def _append_repo_hit(target: list[str], value: str, *, limit: int) -> None:
    text = str(value or "").strip()
    if not text or text in target or len(target) >= limit:
        return
    target.append(text)


def _strip_kotlin_java_non_code(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""

    result: list[str] = []
    i = 0
    length = len(value)
    while i < length:
        if value.startswith('"""', i):
            result.extend("   ")
            i += 3
            while i < length and not value.startswith('"""', i):
                result.append("\n" if value[i] == "\n" else " ")
                i += 1
            if i < length:
                result.extend("   ")
                i += 3
            continue

        if value.startswith("//", i):
            result.extend("  ")
            i += 2
            while i < length and value[i] != "\n":
                result.append(" ")
                i += 1
            continue

        if value.startswith("/*", i):
            result.extend("  ")
            i += 2
            while i < length and not value.startswith("*/", i):
                result.append("\n" if value[i] == "\n" else " ")
                i += 1
            if i < length:
                result.extend("  ")
                i += 2
            continue

        if value[i] == '"':
            result.append(" ")
            i += 1
            escaped = False
            while i < length:
                ch = value[i]
                if ch == "\n":
                    result.append("\n")
                    i += 1
                    break
                result.append(" ")
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if ch == "\\":
                    escaped = True
                    i += 1
                    continue
                i += 1
                if ch == '"':
                    break
            continue

        if value[i] == "'":
            result.append(" ")
            i += 1
            escaped = False
            while i < length:
                ch = value[i]
                if ch == "\n":
                    result.append("\n")
                    i += 1
                    break
                result.append(" ")
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if ch == "\\":
                    escaped = True
                    i += 1
                    continue
                i += 1
                if ch == "'":
                    break
            continue

        result.append(value[i])
        i += 1

    return "".join(result)


def _bucket_probe_hits(rules: Iterable[MatchRule], text: str) -> list[tuple[str, bool, str]]:
    hits: list[tuple[str, bool, str, int]] = []
    seen: set[str] = set()
    for index, rule in enumerate(rules, start=1):
        if not rule_matches_text(rule, text):
            continue
        matched_probe = describe_match_rule(rule)
        if matched_probe in seen:
            continue
        seen.add(matched_probe)
        hits.append((matched_probe, is_strong_positive_rule(rule), rule.kind, index))
    hits.sort(key=lambda item: (0 if item[1] else 1, item[3]))
    return [(probe, is_strong, kind) for probe, is_strong, kind, _ in hits]


def _format_repo_hit_summary(summary: _RepoRuleHitSummary, *, limit: int = 3) -> str | None:
    parts: list[str] = []
    parts.extend(f"require_all:{hit}" for hit in summary.require_all_hits)
    parts.extend(f"require_any:{hit}" for hit in summary.require_any_strong_hits)
    parts.extend(f"require_any(weak):{hit}" for hit in summary.require_any_weak_hits)
    compact = [part for part in parts if part][:limit]
    return "; ".join(compact) if compact else None


class KnowledgeSynthesizer:
    """已停用的离线候选生成兼容壳。"""

    DISABLED_REASON = (
        "已移除基于 final_report.md 的离线词法启发式候选生成；"
        "请改用 auto knowledge synth 流程或显式提供 knowledge candidates。"
    )

    @classmethod
    def disabled_reason(cls) -> str:
        return cls.DISABLED_REASON

    def propose_from_run(self, run_dir: Path) -> list[KnowledgeCandidate]:
        del run_dir
        return []


class KnowledgeValidator:
    """规则验证器。"""

    @staticmethod
    def _iter_repo_texts(cwd: Path) -> Iterable[tuple[Path, str]]:
        if not cwd.exists():
            return
        text_suffixes = {".go", ".java", ".kt", ".kts"}
        for path in cwd.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            yield path, content

    @classmethod
    def _search_rule_hits_in_repo(
        cls,
        cwd: Path,
        rules: MatchRules,
        *,
        limit: int = 8,
    ) -> _RepoRuleHitSummary:
        summary = _RepoRuleHitSummary()
        if not cwd.exists():
            return summary
        if not list(rules.require_all or []) and not list(rules.require_any or []):
            return summary

        for path, content in cls._iter_repo_texts(cwd):
            if summary.total_hits() >= limit:
                break
            searchable_content = _strip_kotlin_java_non_code(content)
            if any(rule_matches_text(rule, searchable_content) for rule in list(rules.exclude or [])):
                continue
            require_all_rules = list(rules.require_all or [])
            if require_all_rules:
                require_all_hits = _bucket_probe_hits(require_all_rules, searchable_content)
                if len(require_all_hits) == len(require_all_rules):
                    descriptors = ",".join(probe for probe, _is_strong, _kind in require_all_hits)
                    _append_repo_hit(summary.require_all_hits, f"{path}:[{descriptors}]", limit=limit)
                    if summary.total_hits() >= limit:
                        break
            for probe, is_strong, _kind in _bucket_probe_hits(list(rules.require_any or []), searchable_content):
                target = summary.require_any_strong_hits if is_strong else summary.require_any_weak_hits
                _append_repo_hit(target, f"{path}:{probe}", limit=limit)
                if summary.total_hits() >= limit:
                    break
        return summary

    @staticmethod
    def _note_has_actionability(content: str) -> bool:
        return all(token in content for token in ("可跳过", "优先检查", "回退条件"))

    def validate(self, candidate: KnowledgeCandidate, cwd: Path) -> ValidationResult:
        if not candidate.id.strip() or not candidate.name.strip():
            return ValidationResult(candidate_id=candidate.id or "", status="REJECT", reasons=["候选缺少 id/name"])

        normalized_candidate = _normalize_candidate_for_validation(candidate)

        reject_reasons: list[str] = []
        revise_reasons: list[str] = []
        reject_seen: set[str] = set()
        revise_seen: set[str] = set()

        if len(normalized_candidate.content.strip()) < 40:
            _append_unique_reason(revise_reasons, revise_seen, "正文过短，缺少可执行指导或边界信息")

        repo_scan_allowed = False
        audit_issue_types = _rule_audit_issue_types(normalized_candidate.match_rules or MatchRules())
        has_revise_rule_issue = bool(audit_issue_types & _REVISE_RULE_AUDIT_ISSUES)
        if "generic_runtime_anchor_in_require_any" in audit_issue_types:
            _append_unique_reason(
                revise_reasons,
                revise_seen,
                _RULE_AUDIT_REASON_MESSAGES["generic_runtime_anchor_in_require_any"],
            )
        if "generic_runtime_anchor_only_require_all" in audit_issue_types:
            _append_unique_reason(
                revise_reasons,
                revise_seen,
                _RULE_AUDIT_REASON_MESSAGES["generic_runtime_anchor_only_require_all"],
            )

        rule_issue_types = _repairable_rule_issue_types(normalized_candidate.match_rules)
        if rule_issue_types:
            _extend_unique_reasons(
                reject_reasons,
                reject_seen,
                _repairable_rule_issue_reasons(rule_issue_types),
            )
        else:
            repo_scan_allowed = True

        if not self._note_has_actionability(normalized_candidate.content):
            _append_unique_reason(revise_reasons, revise_seen, "note 行动性不足，缺少可跳过/优先检查/回退条件")

        hit_summary = _RepoRuleHitSummary()
        if repo_scan_allowed:
            hit_summary = self._search_rule_hits_in_repo(Path(cwd), normalized_candidate.match_rules)
            if hit_summary.has_require_all_hits() or hit_summary.has_require_any_strong_hits():
                pass
            elif bool(hit_summary.require_any_weak_hits):
                _append_unique_reason(revise_reasons, revise_seen, "仅命中部分弱 require_any 规则，建议人工复核是否足够稳定")
            elif normalized_candidate.evidence_refs:
                _append_unique_reason(
                    revise_reasons,
                    revise_seen,
                    "包含证据引用，但 require_all / require_any 规则未在 cwd 中稳定命中，建议复核边界",
                )
            elif has_revise_rule_issue:
                pass
            else:
                _append_unique_reason(reject_reasons, reject_seen, "未找到强规则或证据支撑，存在误注入风险")

        if reject_reasons:
            status = "REJECT"
            reasons = reject_reasons + revise_reasons
            normalized_candidate.metadata["validation_status"] = "rejected"
        elif revise_reasons:
            status = "REVISE"
            reasons = revise_reasons
            normalized_candidate.metadata["validation_status"] = "revise"
        else:
            status = "PASS"
            if hit_summary.has_require_all_hits():
                reasons = ["require_all 组合可在同一代码文件中完整命中，当前粒度足够支撑复用"]
            else:
                reasons = ["require_any 强规则可在代码库中命中，当前粒度足够支撑复用"]
            normalized_candidate.metadata["validation_status"] = "validated"

        return ValidationResult(
            candidate_id=candidate.id,
            status=status,  # type: ignore[arg-type]
            reasons=reasons,
            normalized_candidate=(normalized_candidate if status != "REJECT" else None),
            evidence_summary=_format_repo_hit_summary(hit_summary),
        )


class KnowledgeStore:
    """本地 Markdown knowledge store。"""

    def __init__(self, skills_dir: Path | None = None) -> None:
        if skills_dir is None:
            raise ValueError("KnowledgeStore 需要显式提供知识 scope 的 skills_dir")
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.egress_dir = self.skills_dir.parent / "egress"
        self.egress_dir.mkdir(parents=True, exist_ok=True)
        self.provenance_dir = self.skills_dir.parent / "provenance"
        self.provenance_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _scope_dir_name(app_name: str | None) -> str:
        return scope_dir_name_for_app(app_name)

    def _skill_path_for(self, *, skill_id: str, app_name: str | None) -> Path:
        return self.skills_dir / self._scope_dir_name(app_name) / f"{skill_id}.md"

    def _legacy_skill_path_for(self, *, skill_id: str) -> Path:
        return self.skills_dir / f"{skill_id}.md"

    @staticmethod
    def _sanitize_content(content: str) -> str:
        return ensure_core_conclusion(content)

    @staticmethod
    def _looks_task_specific_selector(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        lowered = text.lower()
        if lowered.startswith(("source_trigger_", "sink_type_")):
            return True
        if any(token in lowered for token in ("source_description", "sink_types", "<", ">", "::")):
            return True
        if "." in text and "(" in text:
            return True
        return False

    @classmethod
    def _sanitize_egress_map(cls, egress_map: EgressMap | None, *, note_id: str) -> EgressMap | None:
        if egress_map is None:
            return None

        def _dedupe_items(values: list[str], *, limit: int, drop_task_specific: bool = False) -> list[str]:
            result: list[str] = []
            seen: set[str] = set()
            for raw in values:
                text = str(raw).strip()
                if not text:
                    continue
                if drop_task_specific and cls._looks_task_specific_selector(text):
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                result.append(text)
                if len(result) >= limit:
                    break
            return result

        boundary_summary = str(egress_map.boundary_summary or "").strip()
        key_apis = _dedupe_items(list(egress_map.key_apis or []), limit=12)
        sanitized_cases: list[EgressCase] = []
        for case in list(egress_map.cases or [])[:8]:
            selectors = _dedupe_items(list(case.selectors or []), limit=6, drop_task_specific=True)
            next_hops = _dedupe_items(list(case.next_hops or []), limit=6)
            if not selectors or not next_hops:
                continue
            negative_selectors = _dedupe_items(
                list(case.negative_selectors or []), limit=6, drop_task_specific=True
            )
            evidence_refs: list[EvidenceRef] = []
            seen_refs: set[tuple[str, int | None, str | None]] = set()
            for ref in list(case.evidence_refs or [])[:12]:
                file_value = str(ref.file or "").strip()
                if not file_value:
                    continue
                key = (file_value, ref.line, ref.symbol)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                evidence_refs.append(ref)
            sanitized_cases.append(
                EgressCase(
                    selectors=selectors,
                    negative_selectors=negative_selectors,
                    next_hops=next_hops,
                    summary=str(case.summary or "").strip(),
                    evidence_refs=evidence_refs,
                )
            )
        if not sanitized_cases:
            return None
        return EgressMap(
            schema_version="flowark-egress-map-v2",
            note_id=str(note_id or "").strip(),
            boundary_summary=boundary_summary,
            key_apis=key_apis,
            cases=sanitized_cases,
        )

    @staticmethod
    def _sanitize_candidate(candidate: KnowledgeCandidate) -> KnowledgeCandidate:
        normalized = knowledge_candidate_from_dict(to_jsonable(candidate))
        normalized.schema_version = SKILL_SCHEMA_V5
        normalized.metadata = dict(normalized.metadata or {})
        packaging_mode = normalize_knowledge_packaging_mode(
            str(normalized.metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
        )
        if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
            normalized.metadata[KNOWLEDGE_PACKAGING_METADATA_KEY] = packaging_mode
            normalized.match_rules = None
        else:
            normalized.metadata.pop(KNOWLEDGE_PACKAGING_METADATA_KEY, None)
            if normalized.match_rules is None:
                raise ValueError("candidate 缺少 match_rules")
            normalized.match_rules = normalize_match_rules(normalized.match_rules)
            audit_issues = audit_match_rules(normalized.match_rules)
            if audit_issues:
                raise ValueError("; ".join(_rule_audit_reasons(audit_issues)))
            if not normalized.match_rules.require_all and not normalized.match_rules.require_any:
                raise ValueError("candidate.match_rules.require_all / require_any 不能同时为空")
            if _rules_need_strong_require_any(normalized.match_rules):
                raise ValueError("candidate.match_rules 缺少可召回的 require_all / require_any 组合")
        normalized.entry_condition = normalized.entry_condition.strip()
        normalized.content = KnowledgeStore._sanitize_content(normalized.content)
        normalized.egress_map = KnowledgeStore._sanitize_egress_map(normalized.egress_map, note_id=normalized.id)
        return normalized

    def _sidecar_path_for(self, note_id: str, *, app_name: str | None) -> Path:
        return self.egress_dir / self._scope_dir_name(app_name) / f"{note_id}.json"

    def _legacy_sidecar_path_for(self, note_id: str) -> Path:
        return self.egress_dir / f"{note_id}.json"

    def _provenance_path_for(self, *, candidate: KnowledgeCandidate) -> Path:
        return self.provenance_dir / self._scope_dir_name(candidate.app_name) / f"{candidate.id}.json"

    def _legacy_provenance_path_for(self, *, candidate: KnowledgeCandidate) -> Path:
        return self.provenance_dir / f"{candidate.id}.json"

    def _write_egress_sidecar(self, *, candidate: KnowledgeCandidate) -> Path | None:
        if candidate.egress_map is None:
            return None
        sidecar = self._sanitize_egress_map(candidate.egress_map, note_id=candidate.id)
        if sidecar is None:
            return None
        path = self._sidecar_path_for(candidate.id, app_name=candidate.app_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_jsonable(sidecar), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _remove_egress_sidecar_if_exists(
        self,
        note_id: str,
        *,
        app_name: str | None,
        existing_path: Path | None = None,
    ) -> None:
        paths: list[Path] = [self._sidecar_path_for(note_id, app_name=app_name)]
        if existing_path is not None and existing_path not in paths:
            paths.append(existing_path)
        for path in paths:
            if path.exists():
                path.unlink()

    def _write_provenance(
        self,
        *,
        candidate: KnowledgeCandidate,
        target: Path,
        version: int,
        validation_status: str,
        egress_map_path: Path | None = None,
        last_validation_reasons: list[str] | None = None,
        last_evidence_summary: str | None = None,
        last_validation_schema_version: str | None = None,
    ) -> Path:
        path = self._provenance_path_for(candidate=candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "flowark-skill-provenance-v3",
            "skill_id": candidate.id,
            "app_name": candidate.app_name,
            "skill_file": str(target),
            "version": version,
            "validation_status": validation_status,
            "knowledge_packaging_mode": normalize_knowledge_packaging_mode(
                str((candidate.metadata or {}).get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
            ),
            "created_from_run": list(candidate.sources or []),
            "match_rules": to_jsonable(candidate.match_rules) if candidate.match_rules is not None else None,
            "entry_condition": candidate.entry_condition,
            "raw_evidence_refs": [to_jsonable(v) for v in (candidate.evidence_refs or [])],
            "candidate_metadata": dict(candidate.metadata or {}),
            "egress_map_path": str(egress_map_path) if egress_map_path else None,
            "egress_case_count": len(candidate.egress_map.cases) if candidate.egress_map else 0,
            "raw_egress_evidence_refs": [
                [to_jsonable(ref) for ref in (case.evidence_refs or [])]
                for case in (candidate.egress_map.cases if candidate.egress_map else [])
            ],
        }
        if validation_status != "auto_synth":
            payload["last_validation_status"] = validation_status
            payload["last_validation_reasons"] = [
                str(reason).strip()
                for reason in (last_validation_reasons or [])
                if str(reason).strip()
            ]
            payload["last_evidence_summary"] = str(last_evidence_summary or "").strip() or None
            payload["last_validation_schema_version"] = str(last_validation_schema_version or "").strip() or None
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _load_existing_metadata(self, skill_id: str, *, app_name: str | None) -> dict[str, Any] | None:
        path = self._skill_path_for(skill_id=skill_id, app_name=app_name)
        if not path.exists():
            legacy = self._legacy_skill_path_for(skill_id=skill_id)
            if legacy.exists():
                parsed = _split_frontmatter(legacy)
                if not parsed:
                    return None
                metadata, _, _ = parsed
                if normalize_app_name(metadata.get("app_name")) != normalize_app_name(app_name):
                    return None
                return metadata
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) < 3:
                return None
            return yaml.safe_load(parts[1]) or None
        except Exception:
            return None

    def _find_existing_skill_path(self, *, skill_id: str, app_name: str | None) -> Path | None:
        scoped = self._skill_path_for(skill_id=skill_id, app_name=app_name)
        if scoped.exists():
            return scoped
        legacy = self._legacy_skill_path_for(skill_id=skill_id)
        if not legacy.exists():
            return None
        parsed = _split_frontmatter(legacy)
        if not parsed:
            return None
        metadata, _, _ = parsed
        if normalize_app_name(metadata.get("app_name")) != normalize_app_name(app_name):
            return None
        return legacy

    def _find_existing_sidecar_path(self, *, note_id: str, app_name: str | None) -> Path | None:
        scoped = self._sidecar_path_for(note_id, app_name=app_name)
        if scoped.exists():
            return scoped
        legacy = self._legacy_sidecar_path_for(note_id)
        if not legacy.exists():
            return None
        legacy_skill = self._legacy_skill_path_for(skill_id=note_id)
        if not legacy_skill.exists():
            return None
        parsed = _split_frontmatter(legacy_skill)
        if not parsed:
            return None
        metadata, _, _ = parsed
        if normalize_app_name(metadata.get("app_name")) != normalize_app_name(app_name):
            return None
        return legacy
        return None

    def _render_skill_markdown(
        self,
        candidate: KnowledgeCandidate,
        *,
        version: int,
        validation_status: str,
    ) -> str:
        metadata = {
            "schema_version": SKILL_SCHEMA_V5,
            "id": candidate.id,
            "app_name": candidate.app_name,
            "name": candidate.name,
            "version": int(version),
            "validation_status": validation_status,
            "entry_condition": candidate.entry_condition or "",
        }
        packaging_mode = normalize_knowledge_packaging_mode(
            str((candidate.metadata or {}).get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
        )
        if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
            metadata[KNOWLEDGE_PACKAGING_METADATA_KEY] = packaging_mode
        else:
            metadata["match_rules"] = (
                to_jsonable(candidate.match_rules) if candidate.match_rules is not None else None
            )
        if not metadata["app_name"]:
            metadata["app_name"] = None
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        body = self._sanitize_content(candidate.content)
        return f"---\n{frontmatter}\n---\n\n{body}"

    def _write_candidate(
        self,
        candidate: KnowledgeCandidate,
        *,
        validation_status: str,
        last_validation_reasons: list[str] | None = None,
        last_evidence_summary: str | None = None,
        last_validation_schema_version: str | None = None,
    ) -> Path:
        normalized = self._sanitize_candidate(candidate)
        target = self._skill_path_for(skill_id=normalized.id, app_name=normalized.app_name)
        existing_path = self._find_existing_skill_path(skill_id=normalized.id, app_name=normalized.app_name)
        existing = self._load_existing_metadata(normalized.id, app_name=normalized.app_name) or {}
        try:
            current_version = int(existing.get("version", 0) or 0)
        except Exception:
            current_version = 0
        new_version = current_version + 1
        rendered = self._render_skill_markdown(
            normalized,
            version=new_version,
            validation_status=validation_status,
        )
        existing_sidecar_path = self._find_existing_sidecar_path(note_id=normalized.id, app_name=normalized.app_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        if existing_path is not None and existing_path != target and existing_path.exists():
            existing_path.unlink()
        egress_map_path: Path | None = None
        egress_map_path = self._write_egress_sidecar(candidate=normalized)
        if egress_map_path is None:
            self._remove_egress_sidecar_if_exists(
                normalized.id,
                app_name=normalized.app_name,
                existing_path=existing_sidecar_path,
            )
        elif existing_sidecar_path is not None and existing_sidecar_path != egress_map_path and existing_sidecar_path.exists():
            existing_sidecar_path.unlink()
        self._write_provenance(
            candidate=normalized,
            target=target,
            version=new_version,
            validation_status=validation_status,
            egress_map_path=egress_map_path,
            last_validation_reasons=last_validation_reasons,
            last_evidence_summary=last_evidence_summary,
            last_validation_schema_version=last_validation_schema_version,
        )
        legacy_provenance = self._legacy_provenance_path_for(candidate=normalized)
        scoped_provenance = self._provenance_path_for(candidate=normalized)
        if legacy_provenance.exists() and legacy_provenance != scoped_provenance:
            legacy_provenance.unlink()
        return target

    def apply_candidate(
        self,
        candidate: KnowledgeCandidate,
        *,
        validation_status: str,
        last_validation_reasons: list[str] | None = None,
        last_evidence_summary: str | None = None,
        last_validation_schema_version: str | None = None,
    ) -> Path:
        normalized_status = str(validation_status or "auto_synth").strip().lower() or "auto_synth"
        return self._write_candidate(
            candidate,
            validation_status=normalized_status,
            last_validation_reasons=last_validation_reasons,
            last_evidence_summary=last_evidence_summary,
            last_validation_schema_version=last_validation_schema_version,
        )

    def apply_validated_candidate(self, candidate: KnowledgeCandidate) -> Path:
        return self.apply_candidate(candidate, validation_status="validated")

    def apply_auto_synth_candidate(self, candidate: KnowledgeCandidate) -> Path:
        return self.apply_candidate(candidate, validation_status="auto_synth")

    def apply_validation_result(self, result: ValidationResult) -> Path | None:
        if result.status != "PASS" or not result.normalized_candidate:
            return None
        return self.apply_validated_candidate(result.normalized_candidate)


def save_candidates(path: Path, candidates: list[KnowledgeCandidate], *, reason: str | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "flowark-knowledge-candidate-batch-v4",
        "candidates": [to_jsonable(c) for c in candidates],
    }
    reason_text = str(reason or "").strip()
    if reason_text:
        payload["reason"] = reason_text
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_candidates(path: Path) -> list[KnowledgeCandidate]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]]
    if isinstance(data, dict) and "candidates" in data:
        items = list(data.get("candidates") or [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("候选文件格式错误，期望 list 或 {candidates:[...]}")

    candidates: list[KnowledgeCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_node_type = item.get("node_type", item.get("type", None))
        if raw_node_type is not None and (str(raw_node_type or "").strip().lower() or "note") != "note":
            continue
        candidates.append(knowledge_candidate_from_dict(item))
    return candidates


def save_validation_results(path: Path, results: list[ValidationResult]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "flowark-knowledge-validate-v4",
        "results": [to_jsonable(r) for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _split_frontmatter(skill_path: Path) -> tuple[dict[str, Any], str, str] | None:
    try:
        text = skill_path.read_text(encoding="utf-8")
    except Exception:
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    _, frontmatter_raw, body = parts
    try:
        metadata = yaml.safe_load(frontmatter_raw) or {}
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata, frontmatter_raw, body


def _looks_mixed_skill(metadata: dict[str, Any], body: str) -> bool:
    text = " ".join(
        [
            str(metadata.get("name") or ""),
            str(metadata.get("entry_condition") or ""),
            json.dumps(metadata.get("match_rules") or {}, ensure_ascii=False),
            body[:500],
        ]
    ).lower()
    has_mechanism = any(token in text for token in ("机制", "分发", "订阅", "组件", "framework", "eventhub"))
    has_jump = any(token in text for token in ("直接跳", "跳到", "无需展开"))
    return has_mechanism and has_jump


def lint_skills_dir(
    skills_dir: Path | None = None,
    *,
    max_frontmatter_chars: int = 2400,
) -> dict[str, Any]:
    if skills_dir is None:
        raise ValueError("lint_skills_dir 需要显式提供知识 scope 的 skills_dir")
    root = Path(skills_dir).expanduser().resolve()
    manager = KnowledgeManager(root, accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5})
    issues: list[dict[str, Any]] = []

    for skill_path in sorted(root.rglob("*.md")):
        parsed = _split_frontmatter(skill_path)
        if not parsed:
            issues.append(
                {
                    "file": str(skill_path),
                    "skill_id": skill_path.stem,
                    "issue_types": ["parse_error"],
                    "details": [{"type": "parse_error", "message": "无法解析 YAML frontmatter"}],
                }
            )
            continue

        metadata, frontmatter_raw, body = parsed
        skill_id = str(metadata.get("id") or skill_path.stem)
        detail_items: list[dict[str, Any]] = []
        schema_version = str(metadata.get("schema_version") or "").strip()

        frontmatter_len = len(frontmatter_raw)
        if frontmatter_len > max_frontmatter_chars:
            detail_items.append(
                {
                    "type": "frontmatter_too_long",
                    "frontmatter_chars": frontmatter_len,
                    "threshold": max_frontmatter_chars,
                }
            )

        if schema_version == SKILL_SCHEMA_V4:
            allowed_keys = set(_ALLOWED_PERSISTENT_FRONTMATTER_KEYS) | {"node_type", _LEGACY_REL_IDS_KEY}
            required_keys = set(_REQUIRED_PERSISTENT_FRONTMATTER_KEYS) | {"node_type", _LEGACY_REL_IDS_KEY}
            node_type = str(metadata.get("node_type") or "note").strip().lower() or "note"
            if node_type == "note":
                detail_items.append({"type": "legacy_v4_note", "message": "v4 note 可读，但建议迁移为 flowark-skill-v5"})
            else:
                detail_items.append({"type": "legacy_removed_node_type", "message": "旧链路型知识已移除，应归档而不是转换为 note"})
        elif schema_version == SKILL_SCHEMA_V5:
            allowed_keys = set(_ALLOWED_PERSISTENT_FRONTMATTER_KEYS)
            required_keys = set(_REQUIRED_PERSISTENT_FRONTMATTER_KEYS)
            node_type = "note"
        else:
            allowed_keys = set(_ALLOWED_PERSISTENT_FRONTMATTER_KEYS)
            required_keys = set(_REQUIRED_PERSISTENT_FRONTMATTER_KEYS)
            node_type = ""
        packaging_mode = normalize_knowledge_packaging_mode(
            str(metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
        )
        if schema_version != SKILL_SCHEMA_V5 and metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY):
            detail_items.append(
                {
                    "type": "unsupported_packaging_mode_schema",
                    "message": "knowledge_packaging_mode 仅支持 flowark-skill-v5",
                }
            )

        for key in metadata.keys():
            skey = str(key)
            if skey in _BANNED_PERSISTENT_METADATA_KEYS:
                detail_items.append({"type": "banned_frontmatter_key", "key": skey})
            elif skey not in allowed_keys:
                detail_items.append({"type": "unsupported_frontmatter_key", "key": skey})
        if schema_version not in {SKILL_SCHEMA_V4, SKILL_SCHEMA_V5}:
            detail_items.append({"type": "unsupported_schema_version", "schema_version": schema_version or None})
        else:
            missing_required = [key for key in required_keys if key not in metadata]
            if missing_required:
                detail_items.append({"type": "missing_required_frontmatter_key", "keys": missing_required})
            if node_type != "note":
                detail_items.append({"type": "unsupported_node_type", "node_type": node_type or None})
            raw_rules = metadata.get("match_rules")
            if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
                pass
            elif not isinstance(raw_rules, dict):
                detail_items.append({"type": "missing_match_rules", "message": "skill 必须提供 match_rules"})
            else:
                try:
                    rules = normalize_match_rules(match_rules_from_dict(raw_rules))
                    if not rules.require_all and not rules.require_any:
                        detail_items.append(
                            {
                                "type": "empty_positive_rules",
                                "message": "match_rules.require_all / require_any 不能同时为空",
                            }
                        )
                    else:
                        for issue in audit_match_rules(rules):
                            issue_type = str(issue.get("type") or "").strip()
                            detail_items.append(
                                {
                                    "type": issue_type,
                                    "message": _RULE_AUDIT_REASON_MESSAGES.get(issue_type, "match_rules 存在高风险误注入问题"),
                                    "bucket": issue.get("bucket"),
                                    "index": issue.get("index"),
                                }
                            )
                except Exception as exc:
                    detail_items.append({"type": "invalid_match_rules", "message": str(exc)})

        if _looks_mixed_skill(metadata, body):
            detail_items.append({"type": "mixed_note_jump_guidance", "message": "疑似混合了机制层指导与跳跃式指导"})

        stripped_body = str(body or "").lstrip()
        if not any(stripped_body.startswith(prefix) for prefix in SUMMARY_PREFIXES):
            detail_items.append({"type": "missing_summary_line", "message": f"正文首行应为“{SUMMARY_PREFIX}xxx”"})

        skill = manager.get_skill_by_id(skill_id, current_app_name=metadata.get("app_name"))
        if skill and skill.is_legacy_task_specific():
            detail_items.append({"type": "legacy_task_specific", "message": "命中任务特异污染启发式"})

        if detail_items:
            issues.append(
                {
                    "file": str(skill_path),
                    "skill_id": skill_id,
                    "issue_types": sorted({str(item.get("type")) for item in detail_items}),
                    "details": detail_items,
                }
            )

    return {
        "schema_version": "flowark-kb-lint-v2",
        "skills_dir": str(root),
        "checked_count": len(list(root.rglob("*.md"))),
        "issue_count": len(issues),
        "issues": issues,
    }


def migrate_skills_to_archive(
    skills_dir: Path | None = None,
    *,
    archive_dir: Path | None = None,
    dry_run: bool = True,
    max_frontmatter_chars: int = 2400,
) -> dict[str, Any]:
    if skills_dir is None:
        raise ValueError("migrate_skills_to_archive 需要显式提供知识 scope 的 skills_dir")
    root = Path(skills_dir).expanduser().resolve()
    archive_root = Path(archive_dir or (root.parent / "skills_archived")).expanduser().resolve()
    lint_result = lint_skills_dir(root, max_frontmatter_chars=max_frontmatter_chars)

    archive_issue_types = {
        "banned_frontmatter_key",
        "parse_error",
        "unsupported_frontmatter_key",
        "unsupported_schema_version",
        "legacy_removed_node_type",
        "unsupported_node_type",
    }
    entries: list[dict[str, Any]] = []
    if not dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)

    def _archive_destination(src: Path) -> Path:
        try:
            rel_path = src.relative_to(root)
            return archive_root / "skills" / rel_path
        except ValueError:
            try:
                rel_path = src.relative_to(root.parent)
                return archive_root / "_sidecars" / rel_path
            except ValueError:
                return archive_root / src.name

    def _unique_destination(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        idx = 1
        while True:
            candidate = path.with_name(f"{stem}-{idx}{suffix}")
            if not candidate.exists():
                return candidate
            idx += 1

    def _move(src: Path) -> tuple[Path, bool]:
        dst = _unique_destination(_archive_destination(src))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return dst, True
        return dst, False

    def _candidate_sidecars(metadata: dict[str, Any]) -> list[Path]:
        skill_id = str(metadata.get("id") or "").strip()
        if not skill_id:
            return []
        app_name = normalize_app_name(metadata.get("app_name"))
        scope_dir = scope_dir_name_for_app(app_name)
        result: list[Path] = []
        for sidecar_root in (root.parent / "egress", root.parent / "provenance"):
            result.append(sidecar_root / scope_dir / f"{skill_id}.json")
            result.append(sidecar_root / f"{skill_id}.json")
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in result:
            if path in seen:
                continue
            seen.add(path)
            if path.exists() and path.is_file():
                unique.append(path)
        return unique

    def _render_v5_from_v4(metadata: dict[str, Any], body: str) -> str:
        new_metadata = {
            "schema_version": SKILL_SCHEMA_V5,
            "id": str(metadata.get("id") or "").strip(),
            "app_name": normalize_app_name(metadata.get("app_name")),
            "name": str(metadata.get("name") or "").strip(),
            "version": int(metadata.get("version") or 1),
            "validation_status": str(metadata.get("validation_status") or "").strip().lower() or "validated",
            "match_rules": metadata.get("match_rules"),
            "entry_condition": str(metadata.get("entry_condition") or "").strip(),
        }
        frontmatter = yaml.safe_dump(new_metadata, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---{body}"

    for item in lint_result.get("issues") or []:
        if not isinstance(item, dict):
            continue
        issue_types = {str(v) for v in (item.get("issue_types") or [])}
        src = Path(str(item.get("file") or "")).expanduser()
        if not src.exists() or not src.is_file():
            continue
        parsed = _split_frontmatter(src)

        if parsed and "legacy_v4_note" in issue_types and not (issue_types & archive_issue_types):
            metadata, _frontmatter_raw, body = parsed
            converted = False
            if not dry_run:
                src.write_text(_render_v5_from_v4(metadata, body), encoding="utf-8")
                converted = True
            entries.append(
                {
                    "skill_id": item.get("skill_id"),
                    "action": "convert_v4_note_to_v5",
                    "file": str(src),
                    "issue_types": sorted(issue_types),
                    "converted": converted,
                }
            )
            continue

        if not (issue_types & archive_issue_types):
            continue

        metadata = parsed[0] if parsed else {}
        final_dst, move_performed = _move(src)
        sidecars: list[dict[str, Any]] = []
        for sidecar in _candidate_sidecars(metadata):
            sidecar_dst, sidecar_moved = _move(sidecar)
            sidecars.append({"from": str(sidecar), "to": str(sidecar_dst), "moved": sidecar_moved})
        entries.append(
            {
                "skill_id": item.get("skill_id"),
                "action": "archive",
                "from": str(src),
                "to": str(final_dst),
                "issue_types": sorted(issue_types),
                "moved": move_performed,
                "sidecars": sidecars,
            }
        )

    return {
        "schema_version": "flowark-kb-migrate-v2",
        "dry_run": bool(dry_run),
        "skills_dir": str(root),
        "archive_dir": str(archive_root),
        "candidate_count": len(entries),
        "migrated_count": sum(1 for e in entries if e.get("moved") or e.get("converted")),
        "entries": entries,
        "lint": lint_result,
    }
