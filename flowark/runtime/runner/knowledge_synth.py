"""FlowArkRunner 的知识候选生成与解析辅助。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flowark.knowledge.content_utils import ensure_core_conclusion
from flowark.knowledge.manager import KnowledgeManager, SKILL_SCHEMA_V4, SKILL_SCHEMA_V5
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_DSL_RULE,
    KNOWLEDGE_PACKAGING_EMBEDDING,
    KNOWLEDGE_PACKAGING_METADATA_KEY,
    normalize_knowledge_packaging_mode,
)
from flowark.knowledge.reuse_profile import (
    build_current_case_profile_from_report,
    build_historical_profile_cards,
    build_knowledge_profile_cards,
)
from flowark.knowledge.reuse_recall import (
    build_reuse_embedding_config,
    build_historical_recall_candidates,
    build_knowledge_recall_candidates,
)
from flowark.knowledge.reuse_rerank import (
    build_reuse_rerank_config,
    build_historical_rerank_result,
    compose_reuse_guidance_block,
    render_historical_reuse_guidance_block,
    render_similar_existing_knowledge_block,
)
from flowark.knowledge.reuse_digest import (
    LIVE_DIGEST_MIN_COMPLETED_REPORTS,
    LIVE_DIGEST_TOP_K,
    _load_eval_root_metadata,
    build_case_summary,
    build_app_overlap_graph,
    build_reuse_digest,
    infer_case_name_from_report_path,
    load_final_report,
    normalize_text,
    render_compact_reuse_digest_block,
    render_reuse_digest_markdown,
)
from flowark.knowledge.rule_matcher import normalize_match_rules, rank_rule_candidates, summarize_match_rules
from flowark.prompt_loader import render_prompt
from flowark.runtime.config import KNOWLEDGE_DISTILLATION_GENERIC, AnalysisRequest
from flowark.semantics.models import PhaseSpec, SessionHandle
from flowark.types import (
    EvidenceRef,
    KnowledgeCandidate,
    MatchRule,
    MatchRules,
    egress_map_from_dict,
    match_rules_from_dict,
    to_jsonable,
)


CATALOG_FILTER_POLICY_INJECTED_OR_RECALL_TOP5_PLUS_REPAIRABLE_TOP3 = "injected_or_recall_top5_plus_repairable_top3"
CATALOG_FILTER_RECALL_TOP_N = 5
CATALOG_FILTER_REPAIRABLE_TOP_N = 3
CATALOG_FILTER_RULE_MATCH_EXTRA_TOP_N = 10


class RunnerKnowledgeSynthMixin:
    @staticmethod
    def _normalize_synth_reason(value: Any, *, candidate_count: int) -> str:
        text = str(value or "").strip()
        if text:
            return text
        if candidate_count > 0:
            return "模型未提供产出这些知识候选的原因。"
        return "模型未提供未产出知识候选的原因。"

    @staticmethod
    def _slugify_knowledge_candidate_id(text: str, fallback: str = "agent-knowledge") -> str:
        parts = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
        if not parts:
            return fallback
        return "-".join(parts[:10])

    @staticmethod
    def _catalog_tokenize(text: str) -> set[str]:
        return {
            token
            for token in re.split(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", str(text or "").lower())
            if token and len(token) >= 2
        }

    @classmethod
    def _catalog_relevance_score(
        cls,
        skill,
        *,
        request: AnalysisRequest,
    ) -> float:
        query_tokens = cls._catalog_tokenize(
            "\n".join(
                [
                    request.query or "",
                    request.source or "",
                    " ".join(request.sink_types or []),
                ]
            )
        )
        if not query_tokens:
            return 0.0
        skill_tokens = cls._catalog_tokenize(
            "\n".join(
                [
                    skill.name,
                    skill.get_entry_condition(),
                    skill.get_core_conclusion(),
                    " ".join(summarize_match_rules(skill.get_match_rules() or MatchRules(), limit=8)),
                    " ".join(skill.get_key_apis()[:6]),
                ]
            )
        )
        if not skill_tokens:
            return 0.0
        inter = query_tokens & skill_tokens
        if not inter:
            return 0.0
        score = float(len(inter))
        if request.app_name and skill.get_app_name() and skill.get_app_name().casefold() == str(request.app_name).casefold():
            score += 2.0
        score += 0.25
        return score

    @staticmethod
    def _format_validation_reasons(reasons: list[str], *, limit: int = 3) -> str:
        items = [str(reason).strip() for reason in (reasons or []) if str(reason).strip()]
        return " | ".join(items[: max(1, int(limit or 3))])

    def _status_aware_skill_catalog(
        self,
        request: AnalysisRequest,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        manager = KnowledgeManager(self._skills_dir(), accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5})
        validated_catalog: list[dict[str, str]] = []
        repairable_catalog: list[dict[str, str]] = []
        packaging_mode = normalize_knowledge_packaging_mode(
            str(
                getattr(self.config, "knowledge_packaging_mode", KNOWLEDGE_PACKAGING_DSL_RULE)
                or KNOWLEDGE_PACKAGING_DSL_RULE
            )
        )
        ranked_skills = sorted(
            manager.get_all_skills(current_app_name=request.app_name),
            key=lambda skill: (
                0 if skill.get_app_name() and request.app_name and skill.get_app_name().casefold() == str(request.app_name).casefold() else 1,
                skill.id,
            ),
        )
        for skill in ranked_skills:
            if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING:
                if not skill.is_embedding_packaged():
                    continue
            elif not skill.is_dsl_rule_packaged():
                continue
            selector_samples: list[str] = []
            egress = skill.get_egress_map()
            if egress is not None:
                for case in list(egress.cases or [])[:3]:
                    for selector in list(case.selectors or [])[:3]:
                        text = str(selector).strip()
                        if text and text not in selector_samples:
                            selector_samples.append(text)
            egress_path = str(skill.egress_map_path) if getattr(skill, "egress_map_path", None) else ""
            provenance_path = str(skill.provenance_path) if getattr(skill, "provenance_path", None) else ""
            match_rules = skill.get_match_rules()
            rule_summary = ", ".join(summarize_match_rules(match_rules or MatchRules(), limit=6))
            item = {
                "id": skill.id,
                "validation_status": skill.get_validation_status(),
                "name": skill.name,
                "summary": skill.get_core_conclusion(),
                "boundary_summary": skill.get_boundary_summary(),
                "entry_condition": skill.get_entry_condition(),
                "match_rules": rule_summary,
                "key_apis": ", ".join(skill.get_key_apis()[:6]),
                "app_name": skill.get_app_name(),
                "skill_path": str(skill.file_path),
                "provenance_path": provenance_path,
                "egress_path": egress_path,
                "egress_case_count": str(len(egress.cases or [])) if egress is not None else "0",
                "selector_samples": ", ".join(selector_samples[:4]),
                "last_validation_reasons": self._format_validation_reasons(
                    skill.get_last_validation_reasons(),
                    limit=3,
                ),
                "last_evidence_summary": skill.get_last_evidence_summary(),
                "_match_rules_json": (
                    json.dumps(to_jsonable(match_rules), ensure_ascii=False)
                    if match_rules is not None
                    else ""
                ),
            }
            if skill.is_runtime_eligible():
                validated_catalog.append(item)
            else:
                repairable_catalog.append(item)
        return validated_catalog, repairable_catalog

    @staticmethod
    def _read_injected_skill_ids_for_catalog_filter(run_dir: Path | None) -> list[str]:
        if run_dir is None:
            return []
        log_path = run_dir / "knowledge_injection.jsonl"
        if not log_path.exists():
            return []
        skill_ids: list[str] = []
        seen: set[str] = set()
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            for raw_skill_id in item.get("injected_skill_ids") or []:
                skill_id = str(raw_skill_id or "").strip()
                if skill_id and skill_id not in seen:
                    seen.add(skill_id)
                    skill_ids.append(skill_id)
        return skill_ids

    @staticmethod
    def _first_catalog_item_ids(items: list[dict[str, str]] | None, *, limit: int) -> list[str]:
        max_items = max(0, int(limit or 0))
        if max_items <= 0:
            return []
        ids: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("id") or "").strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            ids.append(skill_id)
            if len(ids) >= max_items:
                break
        return ids

    @staticmethod
    def _knowledge_recall_selected_ids_from_payload(
        knowledge_recall: dict[str, Any] | None,
        *,
        limit: int | None = None,
    ) -> list[str]:
        max_items = None if limit is None else max(0, int(limit or 0))
        if max_items == 0:
            return []
        ids: list[str] = []
        seen: set[str] = set()
        payload = knowledge_recall if isinstance(knowledge_recall, dict) else {}
        for item in payload.get("selected") or []:
            if not isinstance(item, dict):
                continue
            card = item.get("card") if isinstance(item.get("card"), dict) else {}
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            skill_id = str(card.get("id") or metadata.get("skill_id") or metadata.get("id") or "").strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            ids.append(skill_id)
            if max_items is not None and len(ids) >= max_items:
                break
        return ids

    @classmethod
    def _knowledge_recall_selected_ids(
        cls,
        historical_reuse_digest_meta: dict[str, Any] | None,
        *,
        limit: int = CATALOG_FILTER_RECALL_TOP_N,
    ) -> list[str]:
        max_items = max(0, int(limit or 0))
        if max_items <= 0:
            return []
        ids: list[str] = []
        seen: set[str] = set()
        meta = historical_reuse_digest_meta if isinstance(historical_reuse_digest_meta, dict) else {}
        for raw_skill_id in meta.get("knowledge_recall_selected_ids") or []:
            skill_id = str(raw_skill_id or "").strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            ids.append(skill_id)
            if len(ids) >= max_items:
                break
        return ids

    @staticmethod
    def _current_profile_summary_text(current_case_profile: dict[str, Any] | None) -> str:
        if not isinstance(current_case_profile, dict):
            return ""
        profile = current_case_profile.get("profile")
        if not isinstance(profile, dict):
            return ""
        return str(profile.get("summary_text") or "").strip()

    @staticmethod
    def _current_profile_app_name(current_case_profile: dict[str, Any] | None) -> str:
        if not isinstance(current_case_profile, dict):
            return ""
        metadata = current_case_profile.get("metadata")
        if not isinstance(metadata, dict):
            return ""
        return str(metadata.get("app_name") or "").strip()

    @classmethod
    def _catalog_rule_matched_ids(
        cls,
        *,
        current_case_profile: dict[str, Any] | None,
        validated_skill_catalog: list[dict[str, str]] | None,
        base_retained_ids: set[str],
        skills_dir: Path | None = None,
        manager: KnowledgeManager | None = None,
        extra_limit: int = CATALOG_FILTER_RULE_MATCH_EXTRA_TOP_N,
    ) -> list[str]:
        summary_text = cls._current_profile_summary_text(current_case_profile)
        if not summary_text:
            return []
        validated_catalog = list(validated_skill_catalog or [])
        catalog_ids = cls._first_catalog_item_ids(
            validated_catalog,
            limit=len(validated_catalog),
        )
        if not catalog_ids:
            return []
        candidates: list[tuple[str, MatchRules]] = []
        missing_rule_ids: list[str] = []
        catalog_id_set = set(catalog_ids)
        for item in validated_catalog:
            skill_id = str(item.get("id") or "").strip()
            if not skill_id or skill_id not in catalog_id_set:
                continue
            raw_rules = str(item.get("_match_rules_json") or "").strip()
            if not raw_rules:
                missing_rule_ids.append(skill_id)
                continue
            try:
                candidates.append((skill_id, match_rules_from_dict(json.loads(raw_rules))))
            except Exception:
                missing_rule_ids.append(skill_id)

        if missing_rule_ids and manager is None:
            if skills_dir is None:
                missing_rule_ids = []
            else:
                try:
                    manager = KnowledgeManager(
                        Path(skills_dir).expanduser().resolve(),
                        accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5},
                    )
                except Exception:
                    missing_rule_ids = []

        if missing_rule_ids and manager is not None:
            app_name = cls._current_profile_app_name(current_case_profile)
            if not app_name:
                app_name = next(
                    (str(item.get("app_name") or "").strip() for item in validated_catalog if item.get("app_name")),
                    "",
                )
            seen_candidate_ids = {candidate_id for candidate_id, _ in candidates}
            for skill_id in missing_rule_ids:
                if skill_id in seen_candidate_ids:
                    continue
                skill = manager.get_skill_by_id(skill_id, current_app_name=app_name)
                if skill is None:
                    continue
                if skill.id not in catalog_id_set:
                    continue
                if not skill.is_dsl_rule_packaged() or not skill.is_runtime_eligible():
                    continue
                rules = skill.get_match_rules()
                if rules is None:
                    continue
                candidates.append((skill.id, rules))

        if not candidates:
            return []

        matched_ids: list[str] = []
        seen: set[str] = set()
        extra_added = 0
        max_extra = max(0, int(extra_limit or 0))
        for item in rank_rule_candidates(candidates, summary_text):
            skill_id = str(item.candidate_id or "").strip()
            if not item.matched or not skill_id or skill_id in seen:
                continue
            if skill_id not in base_retained_ids and extra_added >= max_extra:
                continue
            seen.add(skill_id)
            matched_ids.append(skill_id)
            if skill_id not in base_retained_ids:
                extra_added += 1
        return matched_ids

    @classmethod
    def _filter_synth_catalogs_for_prompt(
        cls,
        *,
        run_dir: Path | None,
        distillation_mode: str,
        validated_skill_catalog: list[dict[str, str]] | None,
        repairable_skill_catalog: list[dict[str, str]] | None,
        historical_reuse_digest_meta: dict[str, Any] | None,
        current_case_profile: dict[str, Any] | None = None,
        skills_dir: Path | None = None,
        manager: KnowledgeManager | None = None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
        validated_catalog = list(validated_skill_catalog or [])
        repairable_catalog = list(repairable_skill_catalog or [])
        normalized_distillation_mode = str(distillation_mode or "").strip().lower()
        original_validated_ids = cls._first_catalog_item_ids(validated_catalog, limit=len(validated_catalog))
        original_repairable_ids = cls._first_catalog_item_ids(repairable_catalog, limit=len(repairable_catalog))
        if normalized_distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
            meta = {
                "catalog_filter_policy": CATALOG_FILTER_POLICY_INJECTED_OR_RECALL_TOP5_PLUS_REPAIRABLE_TOP3,
                "catalog_filter_enabled": False,
                "catalog_filter_reason": "distillation_mode_generic",
                "catalog_original_validated_count": len(validated_catalog),
                "catalog_original_repairable_count": len(repairable_catalog),
                "catalog_filtered_validated_count": len(validated_catalog),
                "catalog_filtered_repairable_count": len(repairable_catalog),
                "catalog_filter_injected_ids": [],
                "catalog_filter_recall_selected_ids": [],
                "catalog_filter_rule_matched_ids": [],
                "catalog_filter_rule_matched_count": 0,
                "catalog_filter_retained_validated_ids": original_validated_ids,
                "catalog_filter_retained_repairable_ids": original_repairable_ids,
            }
            return validated_catalog, repairable_catalog, meta

        injected_ids = cls._read_injected_skill_ids_for_catalog_filter(run_dir)
        recall_selected_ids = cls._knowledge_recall_selected_ids(
            historical_reuse_digest_meta,
            limit=CATALOG_FILTER_RECALL_TOP_N,
        )
        retained_validated_id_set = set(injected_ids) | set(recall_selected_ids)
        rule_matched_ids = cls._catalog_rule_matched_ids(
            current_case_profile=current_case_profile,
            validated_skill_catalog=validated_catalog,
            base_retained_ids=retained_validated_id_set,
            skills_dir=skills_dir,
            manager=manager,
            extra_limit=CATALOG_FILTER_RULE_MATCH_EXTRA_TOP_N,
        )
        retained_validated_id_set |= set(rule_matched_ids)
        filtered_validated_catalog = [
            item
            for item in validated_catalog
            if str(item.get("id") or "").strip() in retained_validated_id_set
        ]
        filtered_repairable_catalog = repairable_catalog[:CATALOG_FILTER_REPAIRABLE_TOP_N]
        retained_validated_ids = cls._first_catalog_item_ids(
            filtered_validated_catalog,
            limit=len(filtered_validated_catalog),
        )
        retained_repairable_ids = cls._first_catalog_item_ids(
            filtered_repairable_catalog,
            limit=len(filtered_repairable_catalog),
        )
        meta = {
            "catalog_filter_policy": CATALOG_FILTER_POLICY_INJECTED_OR_RECALL_TOP5_PLUS_REPAIRABLE_TOP3,
            "catalog_filter_enabled": True,
            "catalog_filter_reason": "ok",
            "catalog_original_validated_count": len(validated_catalog),
            "catalog_original_repairable_count": len(repairable_catalog),
            "catalog_filtered_validated_count": len(filtered_validated_catalog),
            "catalog_filtered_repairable_count": len(filtered_repairable_catalog),
            "catalog_filter_injected_ids": injected_ids,
            "catalog_filter_recall_selected_ids": recall_selected_ids,
            "catalog_filter_rule_matched_ids": rule_matched_ids,
            "catalog_filter_rule_matched_count": len(rule_matched_ids),
            "catalog_filter_retained_validated_ids": retained_validated_ids,
            "catalog_filter_retained_repairable_ids": retained_repairable_ids,
        }
        return filtered_validated_catalog, filtered_repairable_catalog, meta

    @staticmethod
    def _write_catalog_filter_artifact(run_dir: Path | None, *, meta: dict[str, Any]) -> None:
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "knowledge_catalog_filter.json").write_text(
            json.dumps(to_jsonable(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _compact_catalog_text(value: object, *, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return "-"
        if len(text) > limit:
            return text[: max(0, limit - 1)].rstrip() + "…"
        return text

    @classmethod
    def _render_skill_catalog_block(
        cls,
        *,
        title: str,
        items: list[dict[str, str]] | None,
        include_repair_details: bool,
    ) -> str:
        lines: list[str] = []
        for idx, item in enumerate(list(items or []), start=1):
            line = " | ".join(
                [
                    f"{idx}. id={cls._compact_catalog_text(item.get('id'), limit=64)}",
                    f"status={cls._compact_catalog_text(item.get('validation_status'), limit=24)}",
                    f"name={cls._compact_catalog_text(item.get('name'), limit=72)}",
                    f"summary={cls._compact_catalog_text(item.get('summary'), limit=96)}",
                    f"rules={cls._compact_catalog_text(item.get('match_rules'), limit=96)}",
                    f"entry={cls._compact_catalog_text(item.get('entry_condition'), limit=96)}",
                ]
            )
            if include_repair_details:
                line += f" | repair_hint={cls._compact_catalog_text(item.get('last_validation_reasons'), limit=96)}"
            lines.append(line)
        return f"{title}:\n" + ("\n".join(lines) if lines else "（当前为空）") + "\n\n"

    def _existing_skill_catalog(self, request: AnalysisRequest, *, limit: int = 6) -> list[dict[str, str]]:
        validated_catalog, repairable_catalog = self._status_aware_skill_catalog(request)
        return validated_catalog + repairable_catalog

    @staticmethod
    def _resolve_live_reuse_eval_root(skills_dir: Path | None) -> Path | None:
        if skills_dir is None:
            return None
        skills_dir = Path(skills_dir).expanduser().resolve()
        if skills_dir.name != "skills":
            return None
        if skills_dir.parent.name != "knowledge_scope":
            return None
        return skills_dir.parent.parent

    @staticmethod
    def _write_historical_reuse_digest_artifacts(
        run_dir: Path | None,
        *,
        digest: dict[str, Any],
        meta: dict[str, Any],
    ) -> None:
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "historical_reuse_digest.json").write_text(
            json.dumps(to_jsonable(digest), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        guidance_block = str(digest.get("guidance_block") or "").strip()
        markdown_text = (
            f"## Reuse digest for {digest.get('app_name') or 'unknown'}\n\n{guidance_block}".rstrip()
            if guidance_block
            else render_reuse_digest_markdown(digest)
        )
        (run_dir / "historical_reuse_digest.md").write_text(
            markdown_text,
            encoding="utf-8",
        )
        (run_dir / "historical_reuse_digest_meta.json").write_text(
            json.dumps(to_jsonable(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _write_profile_artifacts(
        run_dir: Path | None,
        *,
        current_case_profile: dict[str, Any] | None,
        historical_profile_cards: list[dict[str, Any]],
        knowledge_profile_cards: list[dict[str, Any]],
    ) -> None:
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "current_case_profile.json").write_text(
            json.dumps(
                to_jsonable(
                    current_case_profile
                    or {
                        "metadata": {},
                        "profile": None,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        for file_name, rows in (
            ("historical_profile_cards.jsonl", historical_profile_cards),
            ("knowledge_profile_cards.jsonl", knowledge_profile_cards),
        ):
            path = run_dir / file_name
            if not rows:
                path.write_text("", encoding="utf-8")
                continue
            path.write_text(
                "\n".join(json.dumps(to_jsonable(row), ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _write_recall_artifacts(
        run_dir: Path | None,
        *,
        historical_recall: dict[str, Any] | None,
        knowledge_recall: dict[str, Any] | None,
    ) -> None:
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "query_summary_text": "",
            "candidate_count": 0,
            "selected": [],
            "reason": "not_run",
        }
        (run_dir / "historical_recall_candidates.json").write_text(
            json.dumps(to_jsonable(historical_recall or defaults), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "knowledge_recall_candidates.json").write_text(
            json.dumps(to_jsonable(knowledge_recall or defaults), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _write_rerank_artifacts(
        run_dir: Path | None,
        *,
        historical_rerank: dict[str, Any] | None,
        historical_guidance: str,
        similar_existing_knowledge: str,
    ) -> None:
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "schema_version": "flowark-reuse-rerank-v1",
            "query_summary_text": "",
            "candidate_count": 0,
            "merge_groups": [],
            "selected_order": [],
            "drop_ids": [],
            "selected": [],
            "used_fallback": False,
            "reason": "not_run",
            "llm_metrics": {},
        }
        (run_dir / "historical_rerank_result.json").write_text(
            json.dumps(to_jsonable(historical_rerank or defaults), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "historical_reuse_guidance.md").write_text(
            str(historical_guidance or "").strip(),
            encoding="utf-8",
        )
        (run_dir / "similar_existing_knowledge.md").write_text(
            str(similar_existing_knowledge or "").strip(),
            encoding="utf-8",
        )

    @staticmethod
    def _historical_rerank_failure_result(
        historical_recall: dict[str, Any] | None,
        exc: BaseException,
    ) -> dict[str, Any]:
        recall = historical_recall if isinstance(historical_recall, dict) else {}
        candidates = list(recall.get("selected") or [])
        return {
            "schema_version": "flowark-reuse-rerank-v1",
            "query_summary_text": str(recall.get("query_summary_text") or "").strip(),
            "candidate_count": len(candidates),
            "merge_groups": [],
            "selected_order": [],
            "drop_ids": [],
            "selected": [],
            "used_fallback": True,
            "reason": "rerank_failed",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "llm_metrics": {},
        }

    @staticmethod
    def _live_corridor_v2_recall_failure_reason(*recalls: dict[str, Any] | None) -> str:
        for recall in recalls:
            payload = recall if isinstance(recall, dict) else {}
            reason = str(payload.get("reason") or "").strip()
            if reason in {"embed_error", "embedding_config_missing"}:
                return reason
        return ""

    def _build_live_reuse_digest_context(
        self,
        request: AnalysisRequest,
        *,
        run_dir: Path | None,
        limit: int = LIVE_DIGEST_TOP_K,
        validated_skill_catalog: list[dict[str, str]] | None = None,
        repairable_skill_catalog: list[dict[str, str]] | None = None,
        current_case_profile: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        distillation_mode = str(
            getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
            or "with_selection_rules"
        ).strip().lower()
        mode = (
            "off"
            if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC
            else str(getattr(self.config, "knowledge_reuse_digest_mode", "off") or "off").strip().lower()
        )
        app_name = str(request.app_name or "").strip()
        digest: dict[str, Any] = {
            "schema_version": "flowark-reuse-digest-v2",
            "app_name": app_name or "unknown",
            "digest_count": 0,
            "digests": [],
        }
        meta: dict[str, Any] = {
            "mode": mode,
            "app_name": app_name or "",
            "history_report_count": 0,
            "history_case_count": 0,
            "selected_top_k": max(1, int(limit or LIVE_DIGEST_TOP_K)),
            "source_report_paths": [],
            "digest_schema_version": digest["schema_version"],
            "block_injected": False,
            "reason": "",
            "current_case_profile_written": False,
            "historical_profile_card_count": 0,
            "knowledge_profile_card_count": 0,
            "historical_recall_selected_count": 0,
            "knowledge_recall_selected_count": 0,
            "knowledge_recall_selected_ids": [],
            "historical_rerank_selected_count": 0,
            "knowledge_guidance_selected_count": 0,
        }
        if mode not in {"live_corridor", "live_corridor_v2"}:
            meta["reason"] = (
                "distillation_mode_generic"
                if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC
                else "disabled"
            )
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta
        eval_root = self._resolve_live_reuse_eval_root(self._skills_dir())
        if run_dir is None and mode != "live_corridor_v2":
            meta["reason"] = "missing_run_dir"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta
        if not app_name:
            meta["reason"] = "missing_eval_root_or_context"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        current_profile_metadata = (
            current_case_profile.get("metadata")
            if isinstance(current_case_profile, dict) and isinstance(current_case_profile.get("metadata"), dict)
            else {}
        )
        current_report_path = (
            run_dir / "final_report.json"
            if run_dir is not None
            else Path(str(current_profile_metadata.get("report_path") or (self.config.cwd / "final_report.json")))
        )
        current_case_name = str(current_profile_metadata.get("case_id") or "").strip() or infer_case_name_from_report_path(current_report_path)
        current_run_name = str(current_profile_metadata.get("run_id") or "").strip() or (run_dir.name if run_dir is not None else "")
        case_to_app, default_app = _load_eval_root_metadata(eval_root)
        shell_current_case_profile: dict[str, Any] = {
            "metadata": {
                "app_name": app_name,
                "case_id": current_case_name,
                "run_id": current_run_name,
                "report_path": str(current_report_path),
            },
            "profile": None,
        }
        current_case_profile = current_case_profile or shell_current_case_profile
        historical_profile_cards: list[dict[str, Any]] = []
        knowledge_profile_cards = build_knowledge_profile_cards(
            list(validated_skill_catalog or []) + list(repairable_skill_catalog or [])
        )
        meta["knowledge_profile_card_count"] = len(knowledge_profile_cards)
        if isinstance(current_case_profile.get("profile"), dict):
            meta["current_case_profile_written"] = True
        elif current_report_path.exists():
            try:
                current_report = load_final_report(current_report_path)
                current_case_profile = build_current_case_profile_from_report(
                    report_payload=current_report,
                    case_name=current_case_name,
                    session_name=(eval_root.name if eval_root is not None else run_dir.parent.name),
                    report_path=current_report_path,
                    app_name=app_name,
                )
                meta["current_case_profile_written"] = True
            except Exception:
                current_case_profile = shell_current_case_profile

        if mode == "live_corridor_v2" and not isinstance(current_case_profile.get("profile"), dict):
            self._write_profile_artifacts(
                run_dir,
                current_case_profile=current_case_profile,
                historical_profile_cards=historical_profile_cards,
                knowledge_profile_cards=knowledge_profile_cards,
            )
            self._write_recall_artifacts(
                run_dir,
                historical_recall=None,
                knowledge_recall=None,
            )
            self._write_rerank_artifacts(
                run_dir,
                historical_rerank=None,
                historical_guidance="",
                similar_existing_knowledge="",
            )
            meta["reason"] = "final_report_unavailable"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        if eval_root is None and mode != "live_corridor_v2":
            meta["reason"] = "missing_eval_root_or_context"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        report_paths: list[Path] = []
        if eval_root is not None:
            for report_path in sorted((eval_root / "flowark").rglob("final_report.json")):
                report_run_dir = report_path.parent
                if run_dir is not None and report_run_dir == run_dir:
                    continue
                if report_path.resolve() == current_report_path.resolve():
                    continue
                if infer_case_name_from_report_path(report_path) == current_case_name:
                    continue
                if report_run_dir.name >= current_run_name:
                    continue
                report_paths.append(report_path)

        if mode != "live_corridor_v2" and len(report_paths) < LIVE_DIGEST_MIN_COMPLETED_REPORTS:
            meta["source_report_paths"] = [str(path) for path in report_paths]
            meta["history_report_count"] = len(report_paths)
            meta["reason"] = "cold_start"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        target_app_name = normalize_text(app_name)
        summaries: list[dict[str, Any]] = []
        filtered_report_paths: list[Path] = []
        for report_path in report_paths:
            try:
                report = load_final_report(report_path)
            except Exception:
                continue
            case_name = infer_case_name_from_report_path(report_path)
            summary = build_case_summary(
                case_name,
                report,
                session_name=eval_root.name,
                app_name_override=case_to_app.get(case_name, default_app),
            )
            if normalize_text(summary.get("app_name")) != target_app_name:
                continue
            if not summary.get("path_records"):
                continue
            summaries.append(summary)
            filtered_report_paths.append(report_path)

        meta["source_report_paths"] = [str(path) for path in filtered_report_paths]
        meta["history_report_count"] = len(filtered_report_paths)
        meta["history_case_count"] = len(summaries)
        if mode == "live_corridor_v2":
            historical_profile_cards = build_historical_profile_cards(
                report_paths=filtered_report_paths,
                case_summaries=summaries,
            )
            embedding_config = build_reuse_embedding_config(
                base_url=getattr(self.config, "reuse_embed_base_url", None),
                api_key=getattr(self.config, "reuse_embed_api_key", None),
                model=getattr(self.config, "reuse_embed_model", None),
                verify_ssl=getattr(self.config, "reuse_embed_verify_ssl", False),
            )
            rerank_config = build_reuse_rerank_config(
                base_url=getattr(self.config, "reuse_rerank_base_url", None),
                api_key=getattr(self.config, "reuse_rerank_api_key", None),
                model=getattr(self.config, "reuse_rerank_model", None),
                timeout_seconds=getattr(self.config, "reuse_rerank_timeout_seconds", 60),
            )
            historical_recall = build_historical_recall_candidates(
                current_case_profile=current_case_profile,
                cards=historical_profile_cards,
                app_name=app_name,
                current_case_id=current_case_name,
                current_run_id=current_run_name,
                config=embedding_config,
            )
            knowledge_recall = build_knowledge_recall_candidates(
                current_case_profile=current_case_profile,
                cards=knowledge_profile_cards,
                app_name=app_name,
                config=embedding_config,
            )
            meta["historical_profile_card_count"] = len(historical_profile_cards)
            meta["historical_recall_selected_count"] = len(list(historical_recall.get("selected") or []))
            meta["knowledge_recall_selected_count"] = len(list(knowledge_recall.get("selected") or []))
            meta["knowledge_recall_selected_ids"] = self._knowledge_recall_selected_ids_from_payload(knowledge_recall)
            self._write_profile_artifacts(
                run_dir,
                current_case_profile=current_case_profile,
                historical_profile_cards=historical_profile_cards,
                knowledge_profile_cards=knowledge_profile_cards,
            )
            self._write_recall_artifacts(
                run_dir,
                historical_recall=historical_recall,
                knowledge_recall=knowledge_recall,
            )
            recall_failure_reason = self._live_corridor_v2_recall_failure_reason(
                historical_recall,
                knowledge_recall,
            )
            if recall_failure_reason:
                meta["reason"] = recall_failure_reason
                self._write_rerank_artifacts(
                    run_dir,
                    historical_rerank=None,
                    historical_guidance="",
                    similar_existing_knowledge="",
                )
                self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
                raise RuntimeError(f"live_corridor_v2 recall failed: {recall_failure_reason}")
            try:
                historical_rerank = build_historical_rerank_result(
                    current_case_profile=current_case_profile,
                    historical_recall=historical_recall,
                    config=rerank_config,
                )
            except RuntimeError as exc:
                if "config missing" in str(exc):
                    raise
                historical_rerank = self._historical_rerank_failure_result(historical_recall, exc)
            except ValueError as exc:
                historical_rerank = self._historical_rerank_failure_result(historical_recall, exc)
            historical_guidance = render_historical_reuse_guidance_block(historical_rerank)
            similar_existing_knowledge = render_similar_existing_knowledge_block(knowledge_recall)
            block = compose_reuse_guidance_block(
                historical_guidance,
                similar_existing_knowledge,
            )
            v2_digests: list[dict[str, Any]] = []
            if historical_guidance.strip():
                v2_digests.append(
                    {
                        "kind": "historical_reuse_guidance",
                        "content": historical_guidance.strip(),
                    }
                )
            if similar_existing_knowledge.strip():
                v2_digests.append(
                    {
                        "kind": "similar_existing_knowledge",
                        "content": similar_existing_knowledge.strip(),
                    }
                )
            digest = {
                "schema_version": "flowark-reuse-digest-v2",
                "app_name": app_name or "unknown",
                "digest_count": len(v2_digests),
                "digests": v2_digests,
                "guidance_block": block,
                "historical_reuse_guidance": historical_guidance.strip(),
                "similar_existing_knowledge": similar_existing_knowledge.strip(),
            }
            historical_rerank_metrics = dict(historical_rerank.get("llm_metrics") or {})
            if historical_rerank_metrics:
                meta["historical_rerank_metrics"] = historical_rerank_metrics
            meta["historical_rerank_selected_count"] = len(list(historical_rerank.get("selected") or []))
            meta["knowledge_guidance_selected_count"] = min(2, len(list(knowledge_recall.get("selected") or [])))
            meta["block_injected"] = bool(block)
            if str(historical_rerank.get("reason") or "") == "rerank_failed":
                meta["reason"] = "rerank_failed_with_partial_block" if block else "rerank_failed"
                meta["historical_rerank_error_type"] = historical_rerank.get("error_type")
                meta["historical_rerank_error_message"] = historical_rerank.get("error_message")
            else:
                meta["reason"] = "ok" if block else "empty_block"
            self._write_rerank_artifacts(
                run_dir,
                historical_rerank=historical_rerank,
                historical_guidance=historical_guidance,
                similar_existing_knowledge=similar_existing_knowledge,
            )
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return block, meta

        if len(summaries) < LIVE_DIGEST_MIN_COMPLETED_REPORTS:
            meta["reason"] = "cold_start"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        graph = build_app_overlap_graph(summaries)
        digest = build_reuse_digest(graph, top_k=max(1, int(limit or LIVE_DIGEST_TOP_K)))
        meta["digest_count"] = int(digest.get("digest_count") or 0)
        if int(digest.get("digest_count") or 0) <= 0:
            meta["reason"] = "no_digest_candidates"
            self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
            return "", meta

        block = render_compact_reuse_digest_block(digest, limit=limit)
        meta["block_injected"] = bool(block)
        meta["reason"] = "ok" if block else "empty_block"
        self._write_historical_reuse_digest_artifacts(run_dir, digest=digest, meta=meta)
        return block, meta

    def _filter_duplicate_auto_knowledge_candidates(
        self,
        *,
        candidates: list[KnowledgeCandidate],
    ) -> tuple[list[KnowledgeCandidate], list[dict[str, Any]]]:
        kept: list[KnowledgeCandidate] = []
        skipped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for cand in candidates:
            key = (str(cand.app_name or "").casefold(), str(cand.id or "").strip())
            if key in seen:
                skipped.append(
                    {
                        "candidate_id": cand.id,
                        "candidate_name": cand.name,
                        "reason": "same_scope_same_id_in_batch",
                    }
                )
                continue
            seen.add(key)
            kept.append(cand)
        return kept, skipped

    def _build_auto_knowledge_synth_request(
        self,
        request: AnalysisRequest,
        *,
        historical_reuse_digest_block: str = "",
        validated_skill_catalog: list[dict[str, str]] | None = None,
        repairable_skill_catalog: list[dict[str, str]] | None = None,
    ) -> str:
        sink_types_json = json.dumps(list(request.sink_types or []), ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        validated_catalog_block = self._render_skill_catalog_block(
            title="当前已验证的持久化知识目录（只有这里的知识可视为真正已覆盖）",
            items=validated_skill_catalog,
            include_repair_details=False,
        )
        repairable_catalog_block = self._render_skill_catalog_block(
            title="当前待修复知识目录（仅作修复参考，不算已覆盖）",
            items=repairable_skill_catalog,
            include_repair_details=True,
        )
        historical_reuse_digest_guidance_block = ""
        distillation_mode = str(
            getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
            or "with_selection_rules"
        ).strip().lower()
        packaging_mode = normalize_knowledge_packaging_mode(
            str(getattr(self.config, "knowledge_packaging_mode", "dsl_rule") or "dsl_rule")
        )
        if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
            return render_prompt(
                "knowledge_synth_generic",
                validated_catalog_block=validated_catalog_block,
                repairable_catalog_block=repairable_catalog_block,
                source_desc_json=source_desc_json,
                sink_types_json=sink_types_json,
            )
        if historical_reuse_digest_block.strip():
            mode = str(getattr(self.config, "knowledge_reuse_digest_mode", "off") or "off").strip().lower()
            if mode == "live_corridor_v2":
                historical_reuse_digest_guidance_block = (
                    "- `historical_reuse_digest_block` 用于提供和当前 case 相关的历史复用模式与相似已有知识。\n"
                    "- `相关历史复用模式` 是跨 case 的 family/corridor 线索；它只用于帮助你判断哪里值得总结，不要直接照抄成知识正文。\n"
                    "- `相似已有知识` 仅用于避免重复总结；只有 `validated_catalog_block` 中已有知识才算真正已覆盖。\n"
                )
            else:
                historical_reuse_digest_guidance_block = (
                    "- `historical_reuse_digest_block` 用于提示哪些局部模式在跨 case 重复出现。\n"
                    "- 历史路径重叠摘要里的 bridge node / subpath 只是线索；它的作用是帮助你定位“哪段局部序列反复出现”，而不是要求你直接把那个单点 bridge API 写成知识。\n"
                    "- 若历史路径重叠摘要显示某个桥点或短子路径在多个 case/session 中反复出现，应优先考虑把它总结为可复用知识，而不是仅因为“路径不复杂”就放弃。\n"
                )
        prompt_name = (
            "knowledge_synth-embedding"
            if packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING
            else "knowledge_synth"
        )
        return render_prompt(
            prompt_name,
            validated_catalog_block=validated_catalog_block,
            repairable_catalog_block=repairable_catalog_block,
            historical_reuse_digest_guidance_block=historical_reuse_digest_guidance_block,
            historical_reuse_digest_block=historical_reuse_digest_block,
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
        )

    @staticmethod
    def _merge_unique_strs(*lists: list[str], limit: int) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for values in lists:
            for value in values:
                s = str(value).strip()
                if not s:
                    continue
                key = s.lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(s)
                if len(merged) >= limit:
                    return merged
        return merged

    @classmethod
    def _normalize_str_list(cls, value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _candidate_from_agent_knowledge_item(
        cls,
        *,
        item: Any,
        run_dir: Path | None,
        index: int,
        request: AnalysisRequest | None = None,
        knowledge_packaging_mode: str = "dsl_rule",
        drop_callback: Any | None = None,
    ) -> KnowledgeCandidate | None:
        def _drop(stage: str, reason: str, details: str | None = None) -> None:
            if drop_callback is None:
                return
            drop_callback(stage, reason, details)

        if not isinstance(item, dict):
            _drop("shape", "candidate_not_object")
            return None
        raw_name = str(item.get("name") or "").strip()
        raw_id = str(item.get("id") or "").strip()
        if not raw_name:
            _drop("identity", "missing_name")
            return None

        raw_node_type = item.get("type", item.get("node_type", None))
        if raw_node_type is not None and (str(raw_node_type or "").strip().lower() or "note") != "note":
            _drop("identity", "unsupported_node_type", str(raw_node_type))
            return None
        packaging_mode = normalize_knowledge_packaging_mode(knowledge_packaging_mode)
        match_rules = None
        if packaging_mode != KNOWLEDGE_PACKAGING_EMBEDDING:
            match_rules_raw = item.get("match_rules")
            if not isinstance(match_rules_raw, dict):
                _drop("match_rules", "missing_or_invalid_match_rules")
                return None
            try:
                match_rules = normalize_match_rules(match_rules_from_dict(match_rules_raw))
            except Exception as exc:
                _drop("match_rules", "match_rules_parse_error", str(exc))
                return None
            if not match_rules.require_all and not match_rules.require_any:
                _drop("match_rules", "empty_positive_rules")
                return None
        entry_condition = str(item.get("entry_condition") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            _drop("content", "missing_content")
            return None
        egress_map = None
        if isinstance(item.get("egress_map"), dict):
            try:
                egress_map = egress_map_from_dict(item.get("egress_map"))
            except Exception:
                egress_map = None

        evidence_refs: list[EvidenceRef] = []
        if isinstance(item.get("evidence_refs"), list):
            for ref in item.get("evidence_refs")[:20]:
                if not isinstance(ref, dict):
                    continue
                file_value = str(ref.get("file") or "").strip()
                if not file_value:
                    continue
                line_value = cls._as_int(ref.get("line"))
                symbol_value = str(ref.get("symbol")).strip() if ref.get("symbol") is not None else None
                reason_value = str(ref.get("reason")).strip() if ref.get("reason") is not None else None
                evidence_refs.append(
                    EvidenceRef(
                        file=file_value,
                        line=line_value,
                        symbol=symbol_value or None,
                        reason=reason_value or None,
                    )
                )

        return KnowledgeCandidate(
            id=cls._slugify_knowledge_candidate_id(raw_id or raw_name, fallback=f"agent-knowledge-{index}"),
            name=raw_name,
            match_rules=match_rules,
            entry_condition=entry_condition,
            schema_version=SKILL_SCHEMA_V5,
            app_name=(str(request.app_name).strip() if request and request.app_name else None),
            content=ensure_core_conclusion(
                content,
                fallback=raw_name or "请结合 entry_condition 使用本知识。",
            ),
            sources=[str(run_dir)] if run_dir else [],
            metadata={
                "generated_by": "AgentKnowledgeSynthesizer",
                KNOWLEDGE_PACKAGING_METADATA_KEY: packaging_mode,
            },
            evidence_refs=evidence_refs,
            egress_map=egress_map,
        )

    @classmethod
    def _sanitize_persistent_candidate(
        cls,
        *,
        candidate: KnowledgeCandidate,
        request: AnalysisRequest | None,
    ) -> KnowledgeCandidate:
        cand = candidate
        cand.schema_version = SKILL_SCHEMA_V5
        if request and request.app_name and str(request.app_name).strip():
            cand.app_name = str(request.app_name).strip()
        cand.metadata = dict(cand.metadata or {})
        for key in list(cand.metadata.keys()):
            skey = str(key)
            if skey in {"query", "source", "sink_types", "knowledge_kind", "routing_keywords", "routing_symbols", "description"}:
                cand.metadata.pop(key, None)

        deny_exact: set[str] = set()
        if request and request.source:
            source_raw = str(request.source).strip()
            if source_raw:
                deny_exact.add(source_raw.lower())
                deny_exact.add(source_raw.split("(", 1)[0].strip().lower())
        if request:
            for sink in request.sink_types or []:
                deny_exact.add(str(sink).strip().lower())

        def _filter(values: list[str], limit: int) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for raw in values:
                text = str(raw).strip()
                if not text:
                    continue
                key = text.lower()
                if key in deny_exact:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
                if len(out) >= limit:
                    break
            return out

        def _rule_is_denied(rule: MatchRule) -> bool:
            if rule.kind == "call":
                parts = [rule.receiver or "", rule.method or ""]
            else:
                parts = [rule.value]
            return any(str(part).strip().lower() in deny_exact for part in parts if str(part).strip())

        if cand.match_rules is not None:
            filtered_rules = MatchRules(
                require_all=[rule for rule in list(cand.match_rules.require_all or []) if not _rule_is_denied(rule)],
                require_any=[rule for rule in list(cand.match_rules.require_any or []) if not _rule_is_denied(rule)],
                exclude=[rule for rule in list(cand.match_rules.exclude or []) if not _rule_is_denied(rule)],
            )
            try:
                cand.match_rules = normalize_match_rules(filtered_rules)
            except Exception:
                cand.match_rules = None
        cand.entry_condition = str(cand.entry_condition or "").strip()
        cand.content = ensure_core_conclusion(
            cand.content,
            fallback=cand.name or "请结合 entry_condition 使用本知识。",
        )
        if cand.egress_map is not None:
            egress = cand.egress_map

            def _looks_task_specific_selector(value: str) -> bool:
                text = str(value or "").strip()
                if not text:
                    return True
                lowered = text.lower()
                if lowered in deny_exact:
                    return True
                if any(token in lowered for token in ("source_description", "sink_types", "<", ">", "::")):
                    return True
                if "." in text and "(" in text:
                    return True
                return False

            def _dedupe_egress(values: list[str], *, limit: int, drop_task_specific: bool = False) -> list[str]:
                out: list[str] = []
                seen: set[str] = set()
                for raw in values:
                    text = str(raw).strip()
                    if not text:
                        continue
                    if drop_task_specific and _looks_task_specific_selector(text):
                        continue
                    key = text.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(text)
                    if len(out) >= limit:
                        break
                return out

            egress.note_id = cand.id
            egress.key_apis = _dedupe_egress(list(egress.key_apis or []), limit=12)
            sanitized_cases = []
            for case in list(egress.cases or [])[:8]:
                case.selectors = _dedupe_egress(list(case.selectors or []), limit=6, drop_task_specific=True)
                case.negative_selectors = _dedupe_egress(
                    list(case.negative_selectors or []), limit=6, drop_task_specific=True
                )
                case.next_hops = _dedupe_egress(list(case.next_hops or []), limit=6)
                if not case.selectors or not case.next_hops:
                    continue
                seen_refs: set[tuple[str, int | None, str | None]] = set()
                deduped_refs: list[EvidenceRef] = []
                for ref in list(case.evidence_refs or [])[:12]:
                    file_value = str(ref.file or "").strip()
                    if not file_value:
                        continue
                    key = (file_value, ref.line, ref.symbol)
                    if key in seen_refs:
                        continue
                    seen_refs.add(key)
                    deduped_refs.append(ref)
                case.evidence_refs = deduped_refs
                case.summary = str(case.summary or "").strip()
                sanitized_cases.append(case)
            egress.cases = sanitized_cases
            if not egress.cases:
                cand.egress_map = None
        else:
            cand.egress_map = None
        return cand

    @classmethod
    def _normalize_agent_knowledge_payload(
        cls,
        *,
        raw_payload: dict | None,
        run_dir: Path | None,
        request: AnalysisRequest | None = None,
        manager: KnowledgeManager | None = None,
        knowledge_packaging_mode: str = "dsl_rule",
    ) -> tuple[list[KnowledgeCandidate], dict[str, Any]]:
        payload = dict(raw_payload or {})
        schema_version = str(payload.get("schema_version") or "flowark-knowledge-synth-v5")
        packaging_mode = normalize_knowledge_packaging_mode(knowledge_packaging_mode)
        raw_candidates = payload.get("candidates")
        parsed_candidate_records: list[tuple[KnowledgeCandidate, Any, int]] = []
        raw_candidate_count = len(raw_candidates) if isinstance(raw_candidates, list) else 0
        dropped_candidates: list[dict[str, Any]] = []

        def _candidate_identity(item: Any, *, index: int) -> dict[str, Any]:
            identity: dict[str, Any] = {"index": index}
            if isinstance(item, dict):
                raw_id = str(item.get("id") or "").strip()
                raw_name = str(item.get("name") or "").strip()
                if raw_id:
                    identity["raw_id"] = raw_id
                if raw_name:
                    identity["raw_name"] = raw_name
            else:
                identity["raw_shape"] = type(item).__name__
            return identity

        def _record_drop(
            item: Any,
            *,
            index: int,
            stage: str,
            reason: str,
            details: str | None = None,
        ) -> None:
            record = _candidate_identity(item, index=index)
            record["stage"] = stage
            record["reason"] = reason
            if details:
                record["details"] = details[:240]
            dropped_candidates.append(record)

        if isinstance(raw_candidates, list):
            for idx, item in enumerate(raw_candidates, start=1):
                def _drop(
                    stage: str,
                    reason: str,
                    details: str | None = None,
                    *,
                    _item: Any = item,
                    _idx: int = idx,
                ) -> None:
                    _record_drop(_item, index=_idx, stage=stage, reason=reason, details=details)

                cand = cls._candidate_from_agent_knowledge_item(
                    item=item,
                    run_dir=run_dir,
                    index=idx,
                    request=request,
                    knowledge_packaging_mode=packaging_mode,
                    drop_callback=_drop,
                )
                if cand is not None:
                    cand = cls._sanitize_persistent_candidate(candidate=cand, request=request)
                    if (
                        packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING
                        or (
                            cand.match_rules is not None
                            and (cand.match_rules.require_all or cand.match_rules.require_any)
                        )
                    ):
                        parsed_candidate_records.append((cand, item, idx))
                    else:
                        _record_drop(
                            item,
                            index=idx,
                            stage="sanitize",
                            reason="sanitization_removed_all_require_any",
                        )
        elif raw_candidates is not None:
            _record_drop(
                raw_candidates,
                index=0,
                stage="shape",
                reason="candidates_not_list",
            )
        parsed_candidate_count_before_limit = len(parsed_candidate_records)
        truncated_candidate_count = max(0, parsed_candidate_count_before_limit - 2)
        if truncated_candidate_count:
            for cand, raw_item, raw_index in parsed_candidate_records[2:]:
                record = _candidate_identity(raw_item, index=raw_index)
                record.update(
                    {
                        "parsed_id": cand.id,
                        "parsed_name": cand.name,
                        "stage": "limit",
                        "reason": "candidate_limit_exceeded",
                    }
                )
                dropped_candidates.append(record)
        parsed_candidates = [cand for cand, _raw_item, _raw_index in parsed_candidate_records[:2]]
        del manager
        drop_reason_counts: dict[str, int] = {}
        for record in dropped_candidates:
            reason_key = str(record.get("reason") or "unknown")
            drop_reason_counts[reason_key] = drop_reason_counts.get(reason_key, 0) + 1
        reason = cls._normalize_synth_reason(payload.get("reason"), candidate_count=len(parsed_candidates))
        return parsed_candidates, {
            "schema_version": schema_version,
            "knowledge_packaging_mode": packaging_mode,
            "candidate_count": len(parsed_candidates),
            "raw_candidate_count": raw_candidate_count,
            "parsed_candidate_count": len(parsed_candidates),
            "parsed_candidate_count_before_limit": parsed_candidate_count_before_limit,
            "dropped_candidate_count": len(dropped_candidates),
            "dropped_candidates": dropped_candidates[:20],
            "drop_reason_counts": drop_reason_counts,
            "truncated_candidate_count": truncated_candidate_count,
            "reason": reason,
        }

    async def _request_auto_knowledge_candidates(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
        request: AnalysisRequest,
        run_dir: Path | None,
        validated_skill_catalog: list[dict[str, str]] | None = None,
        repairable_skill_catalog: list[dict[str, str]] | None = None,
        historical_reuse_digest_meta: dict[str, Any] | None = None,
        echo: bool = True,
    ) -> tuple[list[KnowledgeCandidate], list[str], str | None, list[dict[str, Any]], dict[str, Any], SessionHandle]:
        phase_result = await self._continue_phase(session=session, phase_spec=phase_spec)
        current_session = phase_result.session
        messages = list(phase_result.outcome.messages or [])
        synth_turn_metrics_list: list[dict[str, Any]] = [
            dict(item) for item in (phase_result.outcome.turn_metrics or []) if isinstance(item, dict)
        ]

        raw_text = self._turn_outcome_raw_text(phase_result.outcome)
        raw_path = run_dir / "knowledge_synth_raw.txt" if run_dir else None
        if raw_path:
            raw_path.write_text(raw_text + "\n", encoding="utf-8")

        json_text = self._extract_json_object_text(raw_text)
        structured_error_fallback = False
        if not json_text and self._is_opencode_structured_output_error(raw_text):
            json_text = self._extract_json_object_text_from_assistant_messages(messages)
            if json_text:
                structured_error_fallback = True
                raw_text = json_text
                if raw_path:
                    raw_path.write_text(raw_text + "\n", encoding="utf-8")
        if not json_text:
            fix_prompt = (
                "【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。\n"
                "你上一条知识总结不是合法 JSON 对象。不要重新分析代码，只修复为合法 JSON。"
                "仍然必须输出 schema_version=flowark-knowledge-synth-v5 且只输出 JSON 对象。"
            )
            fix_spec = self._derive_phase_spec(
                phase_spec,
                instruction=fix_prompt,
                turn_name="knowledge_synth_fix",
                echo=echo,
            )
            fix_result = await self._continue_phase(session=current_session, phase_spec=fix_spec)
            current_session = fix_result.session
            fix_messages = list(fix_result.outcome.messages or [])
            messages.extend(fix_messages)
            synth_turn_metrics_list.extend(
                [dict(item) for item in (fix_result.outcome.turn_metrics or []) if isinstance(item, dict)]
            )
            raw_text = self._turn_outcome_raw_text(fix_result.outcome)
            if raw_path:
                raw_path.write_text(raw_text + "\n", encoding="utf-8")
            json_text = self._extract_json_object_text(raw_text)
            structured_error_fallback = False

        if not json_text:
            return [], messages, raw_text, synth_turn_metrics_list, {
                "source": "agent_session",
                "parse_error": "knowledge synth 未返回可提取的 JSON 对象",
                "reason": "知识总结阶段没有返回可解析的结构化结果。",
            }, current_session

        try:
            parsed = json.loads(json_text)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 顶层不是对象")
        except Exception as exc:
            decode_fix_prompt = (
                "【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。\n"
                "你上一条知识总结 JSON 存在解析错误。不要重新分析代码、不要重新总结知识；"
                "只修复 JSON 的格式问题并重新输出一个合法 JSON 对象。\n\n"
                f"解析错误信息（供修复参考）: {exc}\n\n"
                "要求：\n"
                "1. 仅输出 JSON 对象，不要输出 Markdown/解释/代码块围栏。\n"
                "2. 保持原有语义与字段结构，schema_version 仍为 flowark-knowledge-synth-v5。\n"
            )
            fix_spec = self._derive_phase_spec(
                phase_spec,
                instruction=decode_fix_prompt,
                turn_name="knowledge_synth_fix_parse",
                echo=echo,
            )
            fix_result = await self._continue_phase(session=current_session, phase_spec=fix_spec)
            current_session = fix_result.session
            fix_messages = list(fix_result.outcome.messages or [])
            messages.extend(fix_messages)
            synth_turn_metrics_list.extend(
                [dict(item) for item in (fix_result.outcome.turn_metrics or []) if isinstance(item, dict)]
            )
            raw_text = self._turn_outcome_raw_text(fix_result.outcome)
            if raw_path:
                raw_path.write_text(raw_text + "\n", encoding="utf-8")
            json_text = self._extract_json_object_text(raw_text)
            if not json_text:
                return [], messages, raw_text, synth_turn_metrics_list, {
                    "source": "agent_session",
                    "parse_error": f"knowledge synth JSON 解析失败，修复后仍无 JSON 对象: {exc}",
                    "reason": "知识总结阶段返回了无法修复的 JSON，无法判断产出原因。",
                }, current_session
            parsed = json.loads(json_text)

        manager = KnowledgeManager(self._skills_dir(), accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5})
        candidates, meta = self._normalize_agent_knowledge_payload(
            raw_payload=parsed,
            run_dir=run_dir,
            request=request,
            manager=manager,
            knowledge_packaging_mode=str(
                getattr(self.config, "knowledge_packaging_mode", "dsl_rule") or "dsl_rule"
            ),
        )
        deduped_candidates, duplicate_skipped = self._filter_duplicate_auto_knowledge_candidates(candidates=candidates)
        candidates = deduped_candidates
        meta = dict(meta)
        meta["source"] = "agent_session"
        meta["validated_skill_catalog_count"] = len(validated_skill_catalog or [])
        meta["repairable_skill_catalog_count"] = len(repairable_skill_catalog or [])
        meta["existing_skill_catalog_count"] = len(validated_skill_catalog or []) + len(repairable_skill_catalog or [])
        meta["historical_reuse_digest"] = dict(historical_reuse_digest_meta or {})
        meta["duplicate_skipped_count"] = len(duplicate_skipped)
        meta["structured_error_plain_text_fallback"] = bool(structured_error_fallback)
        if structured_error_fallback:
            self._mark_recovered_opencode_structured_output_error(synth_turn_metrics_list)
        if duplicate_skipped:
            meta["duplicate_skipped"] = duplicate_skipped[:10]
        final_payload = dict(parsed)
        final_payload["reason"] = str(meta.get("reason") or "")
        final_payload["synth_diagnostics"] = {
            "raw_candidate_count": int(meta.get("raw_candidate_count") or 0),
            "parsed_candidate_count": int(meta.get("parsed_candidate_count") or 0),
            "parsed_candidate_count_before_limit": int(meta.get("parsed_candidate_count_before_limit") or 0),
            "dropped_candidate_count": int(meta.get("dropped_candidate_count") or 0),
            "dropped_candidates": list(meta.get("dropped_candidates") or []),
            "drop_reason_counts": dict(meta.get("drop_reason_counts") or {}),
            "truncated_candidate_count": int(meta.get("truncated_candidate_count") or 0),
        }
        if run_dir:
            (run_dir / "knowledge_synth_response.json").write_text(
                json.dumps(to_jsonable(final_payload), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return candidates, messages, raw_text, synth_turn_metrics_list, meta, current_session

    @staticmethod
    def _is_opencode_structured_output_error(text: str | None) -> bool:
        return "OpenCode structured output failed: StructuredOutputError" in str(text or "")

    @staticmethod
    def _extract_first_json_object_text(text: str) -> str | None:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", str(text or "")):
            candidate = str(text or "")[match.start():]
            try:
                parsed, end_idx = decoder.raw_decode(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return candidate[:end_idx].strip()
        return None

    @classmethod
    def _extract_json_object_text_from_assistant_messages(cls, messages: list[str]) -> str | None:
        for message in reversed(messages):
            text = str(message or "").strip()
            if not text:
                continue
            first_line = text.splitlines()[0] if text.splitlines() else ""
            if "OpenCode assistant" not in first_line:
                continue
            candidates = [
                cls._extract_json_object_text(text),
                cls._extract_first_json_object_text(text),
            ]
            seen: set[str] = set()
            for json_text in candidates:
                if not json_text or json_text in seen:
                    continue
                seen.add(json_text)
                try:
                    parsed = json.loads(json_text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return json_text
        return None
