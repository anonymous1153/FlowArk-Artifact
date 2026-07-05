"""知识管理器模块。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from flowark.knowledge.content_utils import extract_core_conclusion, extract_followup_summary
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    KNOWLEDGE_PACKAGING_METADATA_KEY,
    normalize_knowledge_packaging_mode,
)
from flowark.knowledge.rule_matcher import (
    audit_match_rules,
    compile_match_rule,
    has_recallable_require_any,
    normalize_match_rules,
    rank_rule_candidates,
    summarize_match_rules,
)
from flowark.types import EgressMap, MatchRules, egress_map_from_dict, match_rules_from_dict, to_jsonable

SKILL_SCHEMA_V4 = "flowark-skill-v4"
SKILL_SCHEMA_V5 = "flowark-skill-v5"
_LEGACY_REL_IDS_KEY = "related" + "_note_ids"

_V4_ALLOWED_SKILL_FRONTMATTER_KEYS = {
    "schema_version",
    "id",
    "node_type",
    "app_name",
    "name",
    "version",
    "validation_status",
    "match_rules",
    "entry_condition",
    _LEGACY_REL_IDS_KEY,
}
_V4_REQUIRED_SKILL_FRONTMATTER_KEYS = {
    "schema_version",
    "id",
    "node_type",
    "name",
    "validation_status",
    "match_rules",
    "entry_condition",
    _LEGACY_REL_IDS_KEY,
}
_V5_ALLOWED_SKILL_FRONTMATTER_KEYS = {
    "schema_version",
    "id",
    "app_name",
    "name",
    "version",
    "validation_status",
    "knowledge_packaging_mode",
    "match_rules",
    "entry_condition",
}
_V5_REQUIRED_SKILL_FRONTMATTER_KEYS = {
    "schema_version",
    "id",
    "name",
    "validation_status",
    "match_rules",
    "entry_condition",
}
_VALID_NODE_TYPES = {"note"}
_VALIDATION_STATUS_VALUES = {"auto_synth", "validated", "revise", "rejected", "pass", "approved"}
_GLOBAL_SCOPE_NAME = "_global"
_SCOPE_DIR_RE = re.compile(r"[^0-9A-Za-z._-]+")
_DEFAULT_ACCEPTED_SCHEMA_VERSIONS = frozenset({SKILL_SCHEMA_V4, SKILL_SCHEMA_V5})


def normalize_app_name(app_name: str | None) -> str | None:
    text = str(app_name or "").strip()
    return text or None


def scope_name_for_app(app_name: str | None) -> str:
    return normalize_app_name(app_name) or _GLOBAL_SCOPE_NAME


def scope_dir_name_for_app(app_name: str | None) -> str:
    scope_name = scope_name_for_app(app_name)
    if scope_name == _GLOBAL_SCOPE_NAME:
        return _GLOBAL_SCOPE_NAME
    sanitized = _SCOPE_DIR_RE.sub("_", scope_name).strip("._-")
    return sanitized or _GLOBAL_SCOPE_NAME


def scoped_key_for(skill_id: str, app_name: str | None) -> str:
    scope_name = scope_name_for_app(app_name).casefold()
    return f"{scope_name}:{str(skill_id or '').strip()}"


class SkillRecord:
    """单个知识文件记录。"""

    def __init__(
        self,
        id: str,
        name: str,
        metadata: dict,
        content: str,
        file_path: Path,
        *,
        egress_map: EgressMap | None = None,
        egress_map_path: Path | None = None,
        match_rules: MatchRules | None = None,
        provenance: dict[str, Any] | None = None,
        provenance_path: Path | None = None,
        scoped_id: str | None = None,
        scope_name: str | None = None,
    ):
        self.id = id
        self.name = name
        self.metadata = metadata
        self.content = content
        self.file_path = file_path
        self.egress_map = egress_map
        self.egress_map_path = egress_map_path
        self.match_rules = match_rules
        self.provenance = provenance or {}
        self.provenance_path = provenance_path
        self.scoped_id = str(scoped_id or "").strip() or scoped_key_for(id, metadata.get("app_name"))
        self.scope_name = str(scope_name or "").strip() or scope_name_for_app(metadata.get("app_name"))

    @staticmethod
    def _normalized_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    def get_schema_version(self) -> str:
        return str(self.metadata.get("schema_version") or SKILL_SCHEMA_V5).strip()

    def get_app_name(self) -> str | None:
        raw = self.metadata.get("app_name")
        text = str(raw or "").strip()
        return text or None

    def get_validation_status(self) -> str:
        status = str(self.metadata.get("validation_status") or "").strip().lower()
        return status

    def get_knowledge_packaging_mode(self) -> str:
        return normalize_knowledge_packaging_mode(
            str(self.metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
        )

    def get_match_rules(self) -> MatchRules | None:
        return self.match_rules

    def get_entry_condition(self) -> str:
        return str(self.metadata.get("entry_condition") or "").strip()

    def get_egress_map(self) -> EgressMap | None:
        return self.egress_map

    def get_key_apis(self) -> list[str]:
        egress = self.get_egress_map()
        if egress is None:
            return []
        return self._normalized_str_list(egress.key_apis)

    def get_boundary_summary(self) -> str:
        egress = self.get_egress_map()
        if egress is None:
            return ""
        return str(egress.boundary_summary or "").strip()

    def get_core_conclusion(self) -> str:
        return extract_core_conclusion(self.content)

    def get_followup_summary(self, max_chars: int = 220) -> str:
        return extract_followup_summary(self.content, max_chars=max_chars)

    def get_keywords(self) -> list[str]:
        return summarize_match_rules(self.get_match_rules() or MatchRules(), limit=8)

    def get_symbols(self) -> list[str]:
        return self.get_keywords()

    def get_routing_keywords(self) -> list[str]:
        return self.get_keywords()

    def get_routing_symbols(self) -> list[str]:
        return self.get_keywords()

    def get_scope(self) -> list[str]:
        return []

    def get_description(self) -> str:
        entry = self.get_entry_condition()
        if entry:
            return entry
        summary = self.get_summary(max_chars=160)
        return summary

    def is_note(self) -> bool:
        return True

    def is_validated(self) -> bool:
        return self.get_validation_status() in {"validated", "pass", "approved"}

    def is_auto_synth(self) -> bool:
        return self.get_validation_status() == "auto_synth"

    def is_runtime_eligible(self) -> bool:
        return self.get_validation_status() in {"validated", "pass", "approved"}

    def is_embedding_packaged(self) -> bool:
        return self.get_knowledge_packaging_mode() == KNOWLEDGE_PACKAGING_EMBEDDING

    def is_dsl_rule_packaged(self) -> bool:
        return self.get_knowledge_packaging_mode() == KNOWLEDGE_PACKAGING_DSL_RULE

    def is_legacy_task_specific(self) -> bool:
        match_rules_text = ""
        if self.match_rules is not None:
            match_rules_text = json.dumps(to_jsonable(self.match_rules), ensure_ascii=False)
        text_fields = " ".join(
            [
                self.name or "",
                self.get_entry_condition() or "",
                match_rules_text,
            ]
        )
        lower = text_fields.lower()
        banned_prefixes = ("source_trigger_", "sink_type_")
        if any(str(k).startswith(banned_prefixes) for k in self.metadata.keys()):
            return True
        if "source_description" in lower or "sink_types" in lower:
            return True
        return False

    def get_constraint_mode(self) -> str:
        return "trusted_guidance" if self.is_validated() else "advisory"

    def get_summary(self, max_chars: int = 320) -> str:
        pieces: list[str] = []
        if self.get_entry_condition():
            pieces.append(self.get_entry_condition())
        core = self.get_core_conclusion()
        if core:
            pieces.append(core)
        followup = self.get_followup_summary(max_chars=max_chars)
        if followup:
            pieces.append(followup)
        summary = " ".join(pieces).strip()
        if len(summary) > max_chars:
            summary = summary[: max_chars - 1].rstrip() + "…"
        return summary

    def get_last_validation_reasons(self) -> list[str]:
        value = self.provenance.get("last_validation_reasons")
        return self._normalized_str_list(value)

    def get_last_evidence_summary(self) -> str:
        return str(self.provenance.get("last_evidence_summary") or "").strip()


class KnowledgeManager:
    """负责加载与检索知识文件。"""

    def __init__(
        self,
        skills_dir: Path,
        *,
        accepted_schema_versions: set[str] | frozenset[str] | None = None,
    ):
        self.skills_dir = Path(skills_dir)
        self.egress_dir = self.skills_dir.parent / "egress"
        self.provenance_dir = self.skills_dir.parent / "provenance"
        self.accepted_schema_versions = frozenset(
            str(item).strip()
            for item in (accepted_schema_versions or _DEFAULT_ACCEPTED_SCHEMA_VERSIONS)
            if str(item).strip()
        ) or _DEFAULT_ACCEPTED_SCHEMA_VERSIONS
        self.skills: dict[str, SkillRecord] = {}
        self._skills_by_bare_id: dict[str, dict[str, SkillRecord]] = {}
        self._load_skills()

    def _register_skill(self, skill: SkillRecord) -> None:
        self.skills[skill.scoped_id] = skill
        scope_token = scope_name_for_app(skill.get_app_name()).casefold()
        bucket = self._skills_by_bare_id.setdefault(skill.id, {})
        bucket[scope_token] = skill

    def _iter_skill_files(self) -> list[Path]:
        if not self.skills_dir.exists():
            return []
        files = sorted(
            [path for path in self.skills_dir.rglob("*.md") if path.is_file()],
            key=lambda path: (
                0 if path.parent == self.skills_dir else 1,
                str(path.relative_to(self.skills_dir)),
            ),
        )
        return files

    def _load_skills(self) -> None:
        if not self.skills_dir.exists():
            return
        for skill_file in self._iter_skill_files():
            try:
                content = skill_file.read_text(encoding="utf-8")
                parts = content.split("---", 2)
                if len(parts) < 3:
                    continue
                _, frontmatter, body = parts
                metadata = yaml.safe_load(frontmatter)
                if not metadata or "id" not in metadata:
                    continue
                if not self._is_valid_skill_schema(metadata, skill_file):
                    continue
                packaging_mode = normalize_knowledge_packaging_mode(
                    str(metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
                )
                match_rules = None
                if packaging_mode != KNOWLEDGE_PACKAGING_EMBEDDING:
                    match_rules = self._load_match_rules(metadata, skill_file)
                if (
                    str(metadata.get("schema_version") or "").strip() in _DEFAULT_ACCEPTED_SCHEMA_VERSIONS
                    and packaging_mode != KNOWLEDGE_PACKAGING_EMBEDDING
                    and match_rules is None
                ):
                    continue
                skill_id = str(metadata["id"])
                app_name = normalize_app_name(metadata.get("app_name"))
                runtime_metadata = self._normalize_runtime_metadata(metadata)
                scope_name = scope_name_for_app(app_name)
                scoped_id = scoped_key_for(skill_id, app_name)
                egress_map, egress_map_path = self._load_egress_map(
                    skill_id=skill_id,
                    app_name=app_name,
                    skill_file=skill_file,
                )
                provenance, provenance_path = self._load_provenance(
                    skill_id=skill_id,
                    app_name=app_name,
                    skill_file=skill_file,
                )
                self._register_skill(
                    SkillRecord(
                        id=str(metadata["id"]),
                        name=str(metadata.get("name") or ""),
                        metadata=runtime_metadata,
                        content=body.strip(),
                        file_path=skill_file,
                        egress_map=egress_map,
                        egress_map_path=egress_map_path,
                        match_rules=match_rules,
                        provenance=provenance,
                        provenance_path=provenance_path,
                        scoped_id=scoped_id,
                        scope_name=scope_name,
                    )
                )
            except Exception as exc:
                print(f"Warning: Failed to load skill file {skill_file}: {exc}")

    def _legacy_sidecar_path_for(self, skill_id: str) -> Path:
        return self.egress_dir / f"{skill_id}.json"

    def _sidecar_path_for(self, *, skill_id: str, app_name: str | None) -> Path:
        return self.egress_dir / scope_dir_name_for_app(app_name) / f"{skill_id}.json"

    def _legacy_provenance_path_for(self, skill_id: str) -> Path:
        return self.provenance_dir / f"{skill_id}.json"

    def _provenance_path_for(self, *, skill_id: str, app_name: str | None) -> Path:
        return self.provenance_dir / scope_dir_name_for_app(app_name) / f"{skill_id}.json"

    def _load_egress_map(
        self,
        *,
        skill_id: str,
        app_name: str | None,
        skill_file: Path,
    ) -> tuple[EgressMap | None, Path | None]:
        sidecar_path = self._sidecar_path_for(skill_id=skill_id, app_name=app_name)
        if (not sidecar_path.exists() or not sidecar_path.is_file()) and skill_file.parent == self.skills_dir:
            sidecar_path = self._legacy_sidecar_path_for(skill_id)
        if not sidecar_path.exists() or not sidecar_path.is_file():
            return None, None
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Warning: Invalid egress sidecar {sidecar_path}: {exc}")
            return None, None
        if not isinstance(payload, dict):
            print(f"Warning: Invalid egress sidecar {sidecar_path}: top-level must be object")
            return None, None
        if not self._is_valid_egress_schema(payload, sidecar_path=sidecar_path, expected_note_id=skill_id):
            return None, None
        try:
            return egress_map_from_dict(payload), sidecar_path
        except Exception as exc:
            print(f"Warning: Failed to parse egress sidecar {sidecar_path}: {exc}")
            return None, None

    def _load_provenance(
        self,
        *,
        skill_id: str,
        app_name: str | None,
        skill_file: Path,
    ) -> tuple[dict[str, Any], Path | None]:
        provenance_path = self._provenance_path_for(skill_id=skill_id, app_name=app_name)
        if (not provenance_path.exists() or not provenance_path.is_file()) and skill_file.parent == self.skills_dir:
            provenance_path = self._legacy_provenance_path_for(skill_id)
        if not provenance_path.exists() or not provenance_path.is_file():
            return {}, None
        try:
            payload = json.loads(provenance_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Warning: Invalid provenance {provenance_path}: {exc}")
            return {}, None
        if not isinstance(payload, dict):
            print(f"Warning: Invalid provenance {provenance_path}: top-level must be object")
            return {}, None
        return payload, provenance_path

    def _is_valid_egress_schema(
        self,
        payload: dict[str, Any],
        *,
        sidecar_path: Path,
        expected_note_id: str,
    ) -> bool:
        if str(payload.get("schema_version") or "").strip() != "flowark-egress-map-v2":
            print(
                f"Warning: Invalid egress sidecar {sidecar_path}: "
                f"unsupported schema_version={payload.get('schema_version')}"
            )
            return False
        note_id = str(payload.get("note_id") or "").strip()
        if note_id != expected_note_id:
            print(
                f"Warning: Invalid egress sidecar {sidecar_path}: note_id mismatch "
                f"(expected {expected_note_id}, got {note_id or '-'})"
            )
            return False
        if "selector_kind" in payload:
            print(f"Warning: Invalid egress sidecar {sidecar_path}: selector_kind 已废弃")
            return False
        if not isinstance(payload.get("boundary_summary"), str):
            print(f"Warning: Invalid egress sidecar {sidecar_path}: boundary_summary must be string")
            return False
        if not isinstance(payload.get("key_apis"), list):
            print(f"Warning: Invalid egress sidecar {sidecar_path}: key_apis must be list")
            return False
        if not isinstance(payload.get("cases"), list):
            print(f"Warning: Invalid egress sidecar {sidecar_path}: cases must be list")
            return False
        for index, case in enumerate(payload.get("cases") or [], start=1):
            if not isinstance(case, dict):
                print(f"Warning: Invalid egress sidecar {sidecar_path}: cases[{index}] must be object")
                return False
            if not isinstance(case.get("selectors"), list):
                print(f"Warning: Invalid egress sidecar {sidecar_path}: cases[{index}].selectors must be list")
                return False
            if not isinstance(case.get("negative_selectors", []), list):
                print(
                    f"Warning: Invalid egress sidecar {sidecar_path}: "
                    f"cases[{index}].negative_selectors must be list"
                )
                return False
            if not isinstance(case.get("next_hops"), list):
                print(f"Warning: Invalid egress sidecar {sidecar_path}: cases[{index}].next_hops must be list")
                return False
            if not isinstance(case.get("summary", ""), str):
                print(f"Warning: Invalid egress sidecar {sidecar_path}: cases[{index}].summary must be string")
                return False
            if not isinstance(case.get("evidence_refs", []), list):
                print(
                    f"Warning: Invalid egress sidecar {sidecar_path}: "
                    f"cases[{index}].evidence_refs must be list"
                )
                return False
        return True

    def _load_match_rules(self, metadata: dict[str, Any], skill_file: Path) -> MatchRules | None:
        schema_version = str(metadata.get("schema_version") or "").strip()
        if schema_version not in {SKILL_SCHEMA_V4, SKILL_SCHEMA_V5}:
            return None
        try:
            rules = match_rules_from_dict(metadata.get("match_rules") or {})
            rules = normalize_match_rules(rules)
            for rule in [*rules.require_all, *rules.require_any, *rules.exclude]:
                compile_match_rule(rule)
        except Exception as exc:
            print(f"Warning: Skip skill {skill_file}: invalid match_rules ({exc})")
            return None
        if not rules.require_all and not rules.require_any:
            print(f"Warning: Skip skill {skill_file}: match_rules.require_all or require_any must not be empty")
            return None
        audit_issues = audit_match_rules(rules)
        if audit_issues:
            issue_types = ", ".join(sorted({str(issue.get("type") or "") for issue in audit_issues if issue.get("type")}))
            print(f"Warning: Skip skill {skill_file}: match_rules 审计未通过 ({issue_types})")
            return None
        if has_recallable_require_any(rules):
            return rules
        print(f"Warning: Skip skill {skill_file}: match_rules 缺少可召回的 require_any 组合")
        return None

    @staticmethod
    def _normalize_runtime_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(metadata)
        normalized["schema_version"] = SKILL_SCHEMA_V5
        normalized.pop("node_type", None)
        normalized.pop(_LEGACY_REL_IDS_KEY, None)
        return normalized

    def _is_valid_skill_schema(self, metadata: dict[str, Any], skill_file: Path) -> bool:
        if not isinstance(metadata, dict):
            print(f"Warning: Skip skill {skill_file}: frontmatter is not a dict")
            return False

        schema_version = str(metadata.get("schema_version") or "").strip()
        if schema_version not in self.accepted_schema_versions:
            print(f"Warning: Skip skill {skill_file}: unsupported schema_version={schema_version}")
            return False
        packaging_mode = normalize_knowledge_packaging_mode(
            str(metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) or KNOWLEDGE_PACKAGING_DSL_RULE)
        )
        if (
            metadata.get(KNOWLEDGE_PACKAGING_METADATA_KEY) is not None
            and schema_version != SKILL_SCHEMA_V5
        ):
            print(f"Warning: Skip skill {skill_file}: knowledge_packaging_mode requires schema_version={SKILL_SCHEMA_V5}")
            return False

        if schema_version == SKILL_SCHEMA_V4:
            allowed_keys = _V4_ALLOWED_SKILL_FRONTMATTER_KEYS
            required_keys = _V4_REQUIRED_SKILL_FRONTMATTER_KEYS
            list_keys = (_LEGACY_REL_IDS_KEY,)
        elif schema_version == SKILL_SCHEMA_V5:
            allowed_keys = _V5_ALLOWED_SKILL_FRONTMATTER_KEYS
            required_keys = set(_V5_REQUIRED_SKILL_FRONTMATTER_KEYS)
            if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
                required_keys.discard("match_rules")
            list_keys: tuple[str, ...] = ()
        else:
            print(f"Warning: Skip skill {skill_file}: unsupported schema_version={schema_version}")
            return False

        keys = {str(k) for k in metadata.keys()}
        missing = sorted(required_keys - keys)
        extra = sorted(keys - allowed_keys)
        if missing:
            print(f"Warning: Skip skill {skill_file}: missing required keys {missing}")
            return False
        if extra:
            print(f"Warning: Skip skill {skill_file}: unsupported keys {extra}")
            return False

        if schema_version == SKILL_SCHEMA_V4:
            node_type = str(metadata.get("node_type") or "note").strip().lower()
            if node_type not in _VALID_NODE_TYPES:
                print(f"Warning: Skip skill {skill_file}: invalid node_type={node_type}")
                return False

        status = str(metadata.get("validation_status") or "").strip().lower()
        if status not in _VALIDATION_STATUS_VALUES:
            print(f"Warning: Skip skill {skill_file}: invalid validation_status={status}")
            return False

        for list_key in list_keys:
            if not isinstance(metadata.get(list_key), list):
                print(f"Warning: Skip skill {skill_file}: `{list_key}` must be list")
                return False

        if packaging_mode != KNOWLEDGE_PACKAGING_EMBEDDING and not isinstance(metadata.get("match_rules"), dict):
            print(f"Warning: Skip skill {skill_file}: `match_rules` must be object")
            return False

        if "app_name" in metadata and not (
            metadata.get("app_name") is None or isinstance(metadata.get("app_name"), str)
        ):
            print(f"Warning: Skip skill {skill_file}: `app_name` must be string")
            return False

        if not isinstance(metadata.get("entry_condition"), str):
            print(f"Warning: Skip skill {skill_file}: `entry_condition` must be string")
            return False
        return True

    def search_by_keywords(self, query: str, context: str = "", limit: int = 3) -> list[SkillRecord]:
        text = "\n".join(part for part in [query or "", context or ""] if part).strip()
        if not text:
            return []
        candidates = [
            (skill.scoped_id, skill.get_match_rules())
            for skill in self.skills.values()
            if skill.get_match_rules() is not None
        ]
        ranked = rank_rule_candidates(
            [
                (candidate_id, rules)
                for candidate_id, rules in candidates
                if rules is not None
            ],
            text,
        )
        results: list[SkillRecord] = []
        for item in ranked:
            if not item.matched:
                continue
            skill = self.skills.get(item.candidate_id)
            if skill is None:
                continue
            results.append(skill)
            if len(results) >= limit:
                break
        return results

    def get_skill_by_id(self, skill_id: str, *, current_app_name: str | None = None) -> SkillRecord | None:
        bucket = self._skills_by_bare_id.get(str(skill_id or "").strip())
        if not bucket:
            return None
        app_token = scope_name_for_app(current_app_name).casefold() if normalize_app_name(current_app_name) else None
        if app_token and app_token in bucket:
            return bucket[app_token]
        if _GLOBAL_SCOPE_NAME.casefold() in bucket:
            return bucket[_GLOBAL_SCOPE_NAME.casefold()]
        if len(bucket) == 1:
            return next(iter(bucket.values()))
        return bucket[sorted(bucket.keys())[0]]

    def get_all_skills(self, *, current_app_name: str | None = None) -> list[SkillRecord]:
        if not normalize_app_name(current_app_name):
            return list(self.skills.values())
        selected: list[SkillRecord] = []
        app_token = scope_name_for_app(current_app_name).casefold()
        seen_ids: set[str] = set()
        for skill in self.skills.values():
            scope_token = scope_name_for_app(skill.get_app_name()).casefold()
            if scope_token == app_token:
                selected.append(skill)
                seen_ids.add(skill.id)
        for skill in self.skills.values():
            scope_token = scope_name_for_app(skill.get_app_name()).casefold()
            if scope_token != _GLOBAL_SCOPE_NAME.casefold():
                continue
            if skill.id in seen_ids:
                continue
            selected.append(skill)
        return selected

    def get_runtime_eligible_skills(self, *, current_app_name: str | None = None) -> list[SkillRecord]:
        return [skill for skill in self.get_all_skills(current_app_name=current_app_name) if skill.is_runtime_eligible()]

    def get_validated_skills(self, *, current_app_name: str | None = None) -> list[SkillRecord]:
        return [skill for skill in self.get_all_skills(current_app_name=current_app_name) if skill.is_validated()]

    def reload(self) -> None:
        self.skills.clear()
        self._skills_by_bare_id.clear()
        self._load_skills()
