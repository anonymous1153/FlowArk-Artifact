"""FlowArkRunner 的知识验证、落盘与后台 worker 辅助。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from flowark.knowledge.pipeline import (
    KnowledgeStore,
    KnowledgeValidator,
    _candidate_repairable_rule_issue_types,
    _blocking_rule_audit_issues,
    _normalize_candidate_for_validation,
    _rules_need_strong_require_any,
    save_candidates,
    save_validation_results,
)
from flowark.knowledge.reuse_digest import LIVE_DIGEST_TOP_K
from flowark.knowledge.rule_matcher import normalize_match_rules
from flowark.prompt_loader import render_prompt
from flowark.runtime.config import AnalysisRequest
from flowark.semantics.models import (
    KnowledgeRuleRepairPhaseInput,
    KnowledgeSynthPhaseInput,
    Phase,
    PhaseSpec,
    SessionHandle,
)
from flowark.timeutil import now_tz8_iso
from flowark.types import (
    KnowledgeCandidate,
    ValidationResult,
    egress_map_from_dict,
    match_rules_from_dict,
    to_jsonable,
)


class RunnerKnowledgePipelineMixin:
    @staticmethod
    def _write_live_digest_effective_runtime_feature(
        run_dir: Path | None,
        *,
        historical_reuse_digest_meta: dict[str, Any] | None,
    ) -> None:
        if run_dir is None:
            return
        run_meta_path = run_dir / "run_meta.json"
        if not run_meta_path.exists():
            return
        try:
            run_meta_payload = json.loads(run_meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(run_meta_payload, dict):
            return
        effective_runtime_features = run_meta_payload.get("effective_runtime_features")
        if not isinstance(effective_runtime_features, dict):
            return
        effective_runtime_features["knowledge_live_reuse_digest_enabled"] = bool(
            isinstance(historical_reuse_digest_meta, dict)
            and historical_reuse_digest_meta.get("block_injected")
        )
        self = None
        run_meta_payload["effective_runtime_features"] = effective_runtime_features
        run_meta_payload["historical_reuse_digest"] = (
            dict(historical_reuse_digest_meta)
            if isinstance(historical_reuse_digest_meta, dict)
            else None
        )
        run_meta_path.write_text(
            json.dumps(to_jsonable(run_meta_payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _knowledge_restore_snapshot_path(run_dir: Path) -> Path:
        return run_dir / "knowledge_restore_snapshot.json"

    @staticmethod
    def _snapshot_record_key(*, skill_id: str, app_name: str | None) -> str:
        return f"{str(app_name or '').casefold()}::{str(skill_id or '').strip()}"

    @staticmethod
    def _read_optional_text(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    @staticmethod
    def _load_optional_egress_map(path: Path | None):
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return egress_map_from_dict(payload)
        except Exception:
            return None

    @staticmethod
    def _pop_note_egress_update_policy(candidate: KnowledgeCandidate) -> str | None:
        metadata = dict(candidate.metadata or {})
        raw = str(metadata.pop("_normalized_egress_map_policy", "") or "").strip().lower()
        candidate.metadata = metadata
        if raw in {"preserve", "replace", "delete"}:
            return raw
        return None

    def _prepare_candidate_for_apply(
        self,
        *,
        store: KnowledgeStore,
        candidate: KnowledgeCandidate,
    ) -> KnowledgeCandidate:
        policy = self._pop_note_egress_update_policy(candidate)
        if policy == "delete":
            candidate.egress_map = None
            return candidate
        if policy == "preserve" and candidate.egress_map is None:
            existing_sidecar_path = store._find_existing_sidecar_path(
                note_id=candidate.id,
                app_name=candidate.app_name,
            )
            existing_egress = self._load_optional_egress_map(existing_sidecar_path)
            if existing_egress is not None:
                candidate.egress_map = existing_egress
        return candidate

    def _knowledge_restore_targets_for_candidate(
        self,
        *,
        store: KnowledgeStore,
        candidate: KnowledgeCandidate,
    ) -> list[tuple[str, Path]]:
        targets: list[tuple[str, Path]] = [
            ("skill", store._skill_path_for(skill_id=candidate.id, app_name=candidate.app_name)),
            ("legacy_skill", store._legacy_skill_path_for(skill_id=candidate.id)),
            ("provenance", store._provenance_path_for(candidate=candidate)),
            ("legacy_provenance", store._legacy_provenance_path_for(candidate=candidate)),
        ]
        targets.extend(
            [
                ("egress", store._sidecar_path_for(candidate.id, app_name=candidate.app_name)),
                ("legacy_egress", store._legacy_sidecar_path_for(candidate.id)),
            ]
        )
        return targets

    def _record_run_dir_knowledge_restore_snapshot(
        self,
        *,
        run_dir: Path,
        store: KnowledgeStore,
        candidate: KnowledgeCandidate,
    ) -> None:
        snapshot_path = self._knowledge_restore_snapshot_path(run_dir)
        payload: dict[str, Any] = {}
        if snapshot_path.exists():
            try:
                existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except Exception:
                existing = None
            if isinstance(existing, dict):
                payload = dict(existing)

        records = payload.get("records")
        if not isinstance(records, dict):
            records = {}
        payload["schema_version"] = "flowark-knowledge-restore-snapshot-v1"
        payload["records"] = records

        key = self._snapshot_record_key(
            skill_id=candidate.id,
            app_name=candidate.app_name,
        )
        if key in records:
            return

        records[key] = {
            "skill_id": candidate.id,
            "app_name": candidate.app_name,
            "files": [
                {
                    "label": label,
                    "path": str(path),
                    "existed": path.exists(),
                    "content": self._read_optional_text(path),
                }
                for label, path in self._knowledge_restore_targets_for_candidate(store=store, candidate=candidate)
            ],
        }
        snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _build_auto_knowledge_rule_repair_request(
        *,
        request: AnalysisRequest,
        candidate: KnowledgeCandidate,
        static_result: ValidationResult,
        issue_types: list[str],
    ) -> str:
        sink_types_json = json.dumps(list(request.sink_types or []), ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        return render_prompt(
            "knowledge_rule_repair",
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
            candidate_json=json.dumps(to_jsonable(candidate), ensure_ascii=False, indent=2),
            static_reasons_json=json.dumps(list(static_result.reasons or []), ensure_ascii=False, indent=2),
            issue_types_json=json.dumps(list(issue_types or []), ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _normalize_auto_knowledge_rule_repair_payload(
        *,
        raw_payload: dict | None,
        original_candidate: KnowledgeCandidate,
    ) -> tuple[KnowledgeCandidate | None, dict[str, Any]]:
        payload = dict(raw_payload or {})
        schema_version = str(payload.get("schema_version") or "flowark-knowledge-rule-repair-v1")
        candidate_id = str(payload.get("candidate_id") or "").strip()
        if candidate_id != str(original_candidate.id or "").strip():
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": "candidate_id mismatch",
            }

        if payload.get("repairable") is False:
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "repairable": False,
                "notes": [str(v).strip() for v in (payload.get("notes") or []) if str(v).strip()][:10],
            }

        match_rules_raw = payload.get("match_rules")
        if not isinstance(match_rules_raw, dict):
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": "missing_match_rules",
            }

        try:
            repaired_rules = match_rules_from_dict(match_rules_raw)
        except Exception as exc:
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": f"invalid_match_rules: {exc}",
            }

        repaired_candidate = _normalize_candidate_for_validation(original_candidate)
        repaired_candidate.match_rules = normalize_match_rules(repaired_rules)
        if payload.get("entry_condition") is not None:
            repaired_candidate.entry_condition = str(payload.get("entry_condition") or "").strip()
        repaired_candidate.match_rules = (
            repaired_candidate.match_rules
            if repaired_candidate.match_rules is None
            else repaired_candidate.match_rules
        )

        if repaired_candidate.match_rules is None:
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": "normalized_match_rules_missing",
            }
        if _blocking_rule_audit_issues(repaired_candidate.match_rules):
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": "blocking_rule_audit_after_repair",
            }
        if (
            not repaired_candidate.match_rules.require_all
            and not repaired_candidate.match_rules.require_any
            or _rules_need_strong_require_any(repaired_candidate.match_rules)
        ):
            return None, {
                "schema_version": schema_version,
                "source": "agent_session",
                "parse_error": "missing_recallable_positive_rules_after_repair",
            }
        return repaired_candidate, {
            "schema_version": schema_version,
            "source": "agent_session",
            "repairable": True,
            "notes": [str(v).strip() for v in (payload.get("notes") or []) if str(v).strip()][:10],
        }

    async def _request_auto_knowledge_rule_repair(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
        candidate: KnowledgeCandidate,
        run_dir: Path | None,
        echo: bool = False,
    ) -> tuple[KnowledgeCandidate | None, list[str], str | None, list[dict[str, Any]], dict[str, Any], SessionHandle]:
        phase_result = await self._continue_phase(session=session, phase_spec=phase_spec)
        current_session = phase_result.session
        messages = list(phase_result.outcome.messages or [])
        repair_turn_metrics_list: list[dict[str, Any]] = [
            dict(item) for item in (phase_result.outcome.turn_metrics or []) if isinstance(item, dict)
        ]

        raw_text = self._turn_outcome_raw_text(phase_result.outcome)
        raw_path = run_dir / f"knowledge_rule_repair_{candidate.id}.txt" if run_dir else None
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
                "你上一条规则修复输出不是合法 JSON。不要重新分析代码或改写知识正文；"
                "只修复为合法 JSON 对象，并继续只输出规则修复结果。"
            )
            fix_spec = self._derive_phase_spec(
                phase_spec,
                instruction=fix_prompt,
                turn_name="knowledge_rule_repair_fix",
                echo=echo,
            )
            fix_result = await self._continue_phase(session=current_session, phase_spec=fix_spec)
            current_session = fix_result.session
            fix_messages = list(fix_result.outcome.messages or [])
            messages.extend(fix_messages)
            repair_turn_metrics_list.extend(
                [dict(item) for item in (fix_result.outcome.turn_metrics or []) if isinstance(item, dict)]
            )
            raw_text = self._turn_outcome_raw_text(fix_result.outcome)
            if raw_path:
                raw_path.write_text(raw_text + "\n", encoding="utf-8")
            json_text = self._extract_json_object_text(raw_text)
            structured_error_fallback = False

        if not json_text:
            return None, messages, raw_text, repair_turn_metrics_list, {
                "source": "agent_session",
                "candidate_id": candidate.id,
                "parse_error": "knowledge_rule_repair 未返回可提取的 JSON 对象",
            }, current_session

        try:
            parsed = json.loads(json_text)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 顶层不是对象")
        except Exception as exc:
            decode_fix_prompt = (
                "【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。\n"
                "你上一条规则修复 JSON 存在解析错误。不要重新修复规则，只修复 JSON 的格式问题并重新输出一个合法 JSON 对象。\n\n"
                f"解析错误信息（供修复参考）: {exc}\n\n"
                "要求：\n"
                "1. 仅输出 JSON 对象，不要输出 Markdown/解释/代码块围栏。\n"
                "2. 仍然输出 schema_version=flowark-knowledge-rule-repair-v1。\n"
                "3. 保持原有 repairable / match_rules / entry_condition 语义，只做格式修复。"
            )
            fix_spec = self._derive_phase_spec(
                phase_spec,
                instruction=decode_fix_prompt,
                turn_name="knowledge_rule_repair_fix_parse",
                echo=echo,
            )
            fix_result = await self._continue_phase(session=current_session, phase_spec=fix_spec)
            current_session = fix_result.session
            fix_messages = list(fix_result.outcome.messages or [])
            messages.extend(fix_messages)
            repair_turn_metrics_list.extend(
                [dict(item) for item in (fix_result.outcome.turn_metrics or []) if isinstance(item, dict)]
            )
            raw_text = self._turn_outcome_raw_text(fix_result.outcome)
            if raw_path:
                raw_path.write_text(raw_text + "\n", encoding="utf-8")
            json_text = self._extract_json_object_text(raw_text)
            if not json_text:
                return None, messages, raw_text, repair_turn_metrics_list, {
                    "source": "agent_session",
                    "candidate_id": candidate.id,
                    "parse_error": f"knowledge_rule_repair JSON 解析失败，修复后仍无 JSON 对象: {exc}",
                }, current_session
            try:
                parsed = json.loads(json_text)
                if not isinstance(parsed, dict):
                    raise ValueError("JSON 顶层不是对象")
            except Exception as exc2:
                return None, messages, raw_text, repair_turn_metrics_list, {
                    "source": "agent_session",
                    "candidate_id": candidate.id,
                    "parse_error": f"knowledge_rule_repair JSON 解析失败（修复后）: {exc2}",
                }, current_session

        repaired_candidate, meta = self._normalize_auto_knowledge_rule_repair_payload(
            raw_payload=parsed,
            original_candidate=candidate,
        )
        meta = dict(meta)
        meta["structured_error_plain_text_fallback"] = bool(structured_error_fallback)
        if structured_error_fallback:
            self._mark_recovered_opencode_structured_output_error(repair_turn_metrics_list)
        return repaired_candidate, messages, raw_text, repair_turn_metrics_list, meta, current_session

    async def _run_static_rule_repair_phase(
        self,
        *,
        session: SessionHandle,
        request: AnalysisRequest,
        repair_candidates: list[tuple[KnowledgeCandidate, ValidationResult, list[str]]],
        run_dir: Path | None,
    ) -> tuple[dict[str, ValidationResult], list[str], list[dict[str, Any]], dict[str, Any], SessionHandle]:
        branch_messages: list[str] = []
        branch_turn_metrics: list[dict[str, Any]] = []
        repaired_results: dict[str, ValidationResult] = {}

        if not repair_candidates:
            return repaired_results, branch_messages, branch_turn_metrics, {
                "source": "static_rule_repair",
                "executed": False,
                "candidate_count": 0,
                "attempted_candidate_ids": [],
                "repaired_candidate_ids": [],
                "failed_candidate_ids": [],
                "repaired_count": 0,
                "failed_count": 0,
                "skipped": True,
                "reason": "no_repair_candidates",
            }, session

        sample_phase_spec = self._semantic_engine().build_phase_spec(
            Phase.KNOWLEDGE_RULE_REPAIR,
            phase_input=KnowledgeRuleRepairPhaseInput(
                request=request,
                candidate=repair_candidates[0][0],
                static_result=repair_candidates[0][1],
                issue_types=repair_candidates[0][2],
            ),
        )
        current_session = session
        per_candidate_meta: list[dict[str, Any]] = []
        for candidate, static_result, issue_types in repair_candidates:
            repair_meta: dict[str, Any]
            try:
                (
                    repaired_candidate,
                    repair_messages,
                    _,
                    repair_turn_metrics_list,
                    repair_meta,
                    current_session,
                ) = await asyncio.wait_for(
                    self._request_auto_knowledge_rule_repair(
                        session=current_session,
                        phase_spec=self._semantic_engine().build_phase_spec(
                            Phase.KNOWLEDGE_RULE_REPAIR,
                            phase_input=KnowledgeRuleRepairPhaseInput(
                                request=request,
                                candidate=candidate,
                                static_result=static_result,
                                issue_types=issue_types,
                            ),
                        ),
                        candidate=candidate,
                        run_dir=run_dir,
                        echo=False,
                    ),
                    timeout=int(sample_phase_spec.turn_contract.timeout_seconds or 120),
                )
                branch_messages.extend(self._prefix_branch_messages("phase:knowledge_rule_repair", repair_messages))
                branch_turn_metrics.extend([m for m in repair_turn_metrics_list if isinstance(m, dict)])
                repair_meta = dict(repair_meta or {})
            except asyncio.TimeoutError:
                repaired_candidate = None
                repair_meta = {
                    "source": "agent_session",
                    "candidate_id": candidate.id,
                    "parse_error": (
                        f"knowledge_rule_repair timeout after "
                        f"{int(sample_phase_spec.turn_contract.timeout_seconds or 120)}s"
                    ),
                }
            except Exception as exc:
                repaired_candidate = None
                repair_meta = {
                    "source": "agent_session",
                    "candidate_id": candidate.id,
                    "parse_error": f"knowledge_rule_repair failed: {exc}",
                }
            repair_meta["candidate_id"] = candidate.id

            if repaired_candidate is None:
                per_candidate_meta.append(repair_meta)
                continue

            revalidated = KnowledgeValidator().validate(repaired_candidate, cwd=self.config.cwd)
            if revalidated.status == "REJECT":
                repair_meta["revalidated_status"] = "REJECT"
                repair_meta["revalidated_reasons"] = list(revalidated.reasons or [])
                per_candidate_meta.append(repair_meta)
                continue

            repair_meta["revalidated_status"] = str(revalidated.status)
            repair_meta["revalidated_reasons"] = list(revalidated.reasons or [])
            per_candidate_meta.append(repair_meta)
            repaired_results[candidate.id] = revalidated

        repaired_candidate_ids = [candidate_id for candidate_id in repaired_results]
        failed_candidate_ids = [
            str(item.get("candidate_id") or "").strip()
            for item in per_candidate_meta
            if str(item.get("candidate_id") or "").strip() and str(item.get("candidate_id") or "").strip() not in repaired_results
        ]
        meta = {
            "source": "static_rule_repair",
            "executed": True,
            "candidate_count": len(repair_candidates),
            "attempted_candidate_ids": [candidate.id for candidate, _, _ in repair_candidates],
            "repaired_candidate_ids": repaired_candidate_ids,
            "failed_candidate_ids": failed_candidate_ids,
            "repaired_count": len(repaired_candidate_ids),
            "failed_count": len(failed_candidate_ids),
            "candidates": per_candidate_meta,
        }
        return repaired_results, branch_messages, branch_turn_metrics, meta, current_session

    async def _run_auto_knowledge_synth_phase(
        self,
        *,
        session: SessionHandle,
        request: AnalysisRequest,
        run_dir: Path | None,
        current_case_profile: dict[str, Any] | None = None,
    ) -> tuple[
        list[KnowledgeCandidate] | None,
        list[str],
        list[dict[str, Any]],
        dict[str, Any] | None,
        SessionHandle | None,
    ]:
        branch_messages: list[str] = []
        branch_turn_metrics: list[dict[str, Any]] = []
        candidates: list[KnowledgeCandidate] | None = None
        synth_meta: dict[str, Any] | None = None

        validated_catalog, repairable_catalog = self._status_aware_skill_catalog(request)
        historical_reuse_digest_block, historical_reuse_digest_meta = self._build_live_reuse_digest_context(
            request,
            run_dir=run_dir,
            limit=LIVE_DIGEST_TOP_K,
            validated_skill_catalog=validated_catalog,
            repairable_skill_catalog=repairable_catalog,
            current_case_profile=current_case_profile,
        )
        distillation_mode = str(
            getattr(self.config, "knowledge_distillation_mode", "with_selection_rules")
            or "with_selection_rules"
        ).strip().lower()
        prompt_validated_catalog, prompt_repairable_catalog, catalog_filter_meta = self._filter_synth_catalogs_for_prompt(
            run_dir=run_dir,
            distillation_mode=distillation_mode,
            validated_skill_catalog=validated_catalog,
            repairable_skill_catalog=repairable_catalog,
            historical_reuse_digest_meta=historical_reuse_digest_meta,
            current_case_profile=current_case_profile,
            skills_dir=self._skills_dir(),
        )
        self._write_catalog_filter_artifact(run_dir, meta=catalog_filter_meta)
        phase_spec = self._semantic_engine().build_phase_spec(
            Phase.KNOWLEDGE_SYNTH,
            phase_input=KnowledgeSynthPhaseInput(
                request=request,
                current_case_profile=current_case_profile,
                validated_skill_catalog=prompt_validated_catalog,
                repairable_skill_catalog=prompt_repairable_catalog,
                historical_reuse_digest_block=historical_reuse_digest_block,
            ),
        )
        knowledge_synth_timeout_seconds = int(phase_spec.turn_contract.timeout_seconds or 300)
        next_session: SessionHandle | None = session
        try:
            (
                candidates,
                synth_messages,
                _,
                synth_turn_metrics_list,
                synth_meta,
                next_session,
            ) = await asyncio.wait_for(
                self._request_auto_knowledge_candidates(
                    session=session,
                    phase_spec=phase_spec,
                    request=request,
                    run_dir=run_dir,
                    validated_skill_catalog=prompt_validated_catalog,
                    repairable_skill_catalog=prompt_repairable_catalog,
                    historical_reuse_digest_meta=historical_reuse_digest_meta,
                    echo=False,
                ),
                timeout=knowledge_synth_timeout_seconds,
            )
            branch_messages.extend(self._prefix_branch_messages("phase:knowledge_synth", synth_messages))
            branch_turn_metrics.extend([m for m in synth_turn_metrics_list if isinstance(m, dict)])
        except asyncio.TimeoutError:
            candidates = None
            synth_meta = {
                "source": "agent_session",
                "parse_error": f"knowledge synth timeout after {knowledge_synth_timeout_seconds}s",
            }
        except Exception as exc:
            candidates = None
            synth_meta = {
                "source": "agent_session",
                "parse_error": f"knowledge synth failed: {exc}",
            }

        if synth_meta is not None:
            synth_meta = dict(synth_meta)
            synth_meta.update(catalog_filter_meta)

        return (
            candidates,
            branch_messages,
            branch_turn_metrics,
            synth_meta,
            next_session,
        )

    def _run_static_auto_knowledge_validation(
        self,
        *,
        candidates: list[KnowledgeCandidate],
    ) -> tuple[list[ValidationResult], dict[str, Any]]:
        if not candidates:
            return [], {
                "source": "static_validator",
                "candidate_count": 0,
                "result_count": 0,
                "pass_count": 0,
                "revise_count": 0,
                "reject_count": 0,
                "skipped": True,
                "reason": "no_candidates",
            }

        validator = KnowledgeValidator()
        results = [validator.validate(candidate, cwd=self.config.cwd) for candidate in candidates]
        meta = {
            "source": "static_validator",
            "candidate_count": len(candidates),
            "result_count": len(results),
            "pass_count": sum(1 for r in results if r.status == "PASS"),
            "revise_count": sum(1 for r in results if r.status == "REVISE"),
            "reject_count": sum(1 for r in results if r.status == "REJECT"),
        }
        return results, meta

    async def _run_auto_knowledge_validation_pipeline(
        self,
        *,
        session: SessionHandle,
        request: AnalysisRequest,
        candidates: list[KnowledgeCandidate],
        run_dir: Path | None,
    ) -> tuple[list[ValidationResult] | None, list[str], list[dict[str, Any]], dict[str, Any]]:
        mode = self._auto_knowledge_validate_mode()
        branch_messages: list[str] = []
        branch_turn_metrics: list[dict[str, Any]] = []

        if not candidates:
            return [], branch_messages, branch_turn_metrics, {
                "source": "validation_pipeline",
                "validation_mode": mode,
                "candidate_count": 0,
                "result_count": 0,
                "static_validation_executed": False,
                "skipped": True,
                "reason": "no_candidates",
            }

        if mode == "off":
            return None, branch_messages, branch_turn_metrics, {
                "source": "validation_pipeline",
                "validation_mode": mode,
                "candidate_count": len(candidates),
                "result_count": 0,
                "static_validation_executed": False,
                "validation_skipped": True,
                "reason": "validation_mode_off",
            }

        static_results, static_meta = self._run_static_auto_knowledge_validation(
            candidates=candidates,
        )
        current_session = session
        static_by_id = {result.candidate_id: result for result in static_results}
        if mode == "static":
            repair_candidates: list[tuple[KnowledgeCandidate, ValidationResult, list[str]]] = []
            for candidate in candidates:
                if not str(candidate.id or "").strip() or not str(candidate.name or "").strip():
                    continue
                static_result = static_by_id.get(candidate.id)
                if static_result is None or static_result.status != "REJECT":
                    continue
                issue_types = _candidate_repairable_rule_issue_types(candidate)
                if not issue_types:
                    continue
                repair_candidates.append((candidate, static_result, issue_types))

            final_static_results = list(static_results)
            repair_meta: dict[str, Any] = {
                "source": "static_rule_repair",
                "executed": False,
                "candidate_count": 0,
                "attempted_candidate_ids": [],
                "repaired_candidate_ids": [],
                "failed_candidate_ids": [],
                "repaired_count": 0,
                "failed_count": 0,
                "skipped": True,
                "reason": "no_repair_candidates",
            }
            if repair_candidates:
                (
                    repaired_by_id,
                    repair_messages,
                    repair_turn_metrics,
                    repair_meta,
                    current_session,
                ) = await self._run_static_rule_repair_phase(
                    session=current_session,
                    request=request,
                    repair_candidates=repair_candidates,
                    run_dir=run_dir,
                )
                branch_messages.extend(repair_messages)
                branch_turn_metrics.extend(repair_turn_metrics)
                if repaired_by_id:
                    final_static_results = [repaired_by_id.get(result.candidate_id, result) for result in static_results]

            meta = {
                "source": "validation_pipeline",
                "validation_mode": mode,
                "candidate_count": len(candidates),
                "result_count": len(final_static_results),
                "static_validation_executed": True,
                "static_meta": dict(static_meta),
                "static_rule_repair_executed": bool(repair_meta.get("executed")),
                "static_rule_repair_meta": dict(repair_meta),
            }
            return final_static_results, branch_messages, branch_turn_metrics, meta

        raise ValueError(f"unsupported auto_knowledge_validate_mode: {mode}")

    def _run_auto_knowledge_cycle(
        self,
        *,
        run_dir: Path | None,
        precomputed_candidates: list[KnowledgeCandidate] | None = None,
        synth_meta: dict[str, Any] | None = None,
        precomputed_validation_results: list[ValidationResult] | None = None,
        validation_meta: dict[str, Any] | None = None,
    ) -> dict:
        validation_mode = self._auto_knowledge_validate_mode()
        if not run_dir or not self.config.auto_knowledge_cycle:
            return {
                "enabled": bool(self.config.auto_knowledge_cycle),
                "executed": False,
                "validation_mode": validation_mode,
            }

        if precomputed_candidates is None:
            apply_summary = {
                "enabled": True,
                "executed": False,
                "skipped": True,
                "reason": "no_agent_synth_candidates",
                "mode": "synth_validate_apply",
                "validation_mode": validation_mode,
                "validation_skipped": validation_mode == "off",
                "static_validation_executed": False,
                "synth_source": "agent_session_only",
                "synth_meta": dict(synth_meta or {}),
                "validation_source": str((validation_meta or {}).get("source") or "validation_pipeline"),
                "validation_meta": dict(validation_meta or {}),
                "candidate_count": 0,
                "result_count": 0,
                "validated_count": 0,
                "pass_count": 0,
                "revise_count": 0,
                "reject_count": 0,
                "final_validated_count": 0,
                "final_revise_count": 0,
                "final_reject_count": 0,
                "candidates_path": None,
                "validation_path": None,
                "applied_skill_paths": [],
            }
            self._write_json(run_dir / "knowledge_apply.json", apply_summary)
            return apply_summary

        store = KnowledgeStore(self._skills_dir())
        raw_candidates = list(precomputed_candidates)
        skipped_unsupported_candidates: list[KnowledgeCandidate] = []
        candidates = list(raw_candidates)
        artifact_candidates = [
            {
                "skill_id": str(candidate.id),
                "app_name": candidate.app_name,
            }
            for candidate in candidates
            if str(candidate.id or "").strip()
        ]
        synth_source = str((synth_meta or {}).get("source") or "agent_session")
        candidates_path = save_candidates(
            run_dir / "knowledge_candidates.json",
            candidates,
            reason=str((synth_meta or {}).get("reason") or "").strip() or None,
        )
        validation_meta_dict = dict(validation_meta or {})
        candidate_ids = {str(candidate.id) for candidate in candidates}
        validation_results = [
            result
            for result in list(precomputed_validation_results or [])
            if str(result.candidate_id) in candidate_ids
        ]
        validation_source = str(validation_meta_dict.get("source") or "validation_pipeline")
        apply_errors: list[dict[str, Any]] = []

        if validation_mode == "off":
            applied_paths: list[str] = []
            for candidate in candidates:
                self._record_run_dir_knowledge_restore_snapshot(
                    run_dir=run_dir,
                    store=store,
                    candidate=candidate,
                )
                try:
                    applied_path = store.apply_validated_candidate(candidate)
                except Exception as exc:
                    print(f"[FlowArk] 自动总结知识 validated 写入失败: {candidate.id}: {exc}")
                    apply_errors.append(
                        {
                            "candidate_id": str(candidate.id or ""),
                            "stage": "validated",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    continue
                if applied_path is not None:
                    applied_paths.append(str(applied_path))
            final_validated_count = len(applied_paths)
            apply_summary = {
                "enabled": True,
                "executed": True,
                "mode": "synth_validate_apply",
                "validation_mode": validation_mode,
                "validation_skipped": True,
                "static_validation_executed": False,
                "synth_source": synth_source,
                "synth_meta": dict(synth_meta or {}),
                "validation_source": validation_source,
                "validation_meta": validation_meta_dict,
                "candidate_count": len(candidates),
                "unsupported_candidate_count": len(skipped_unsupported_candidates),
                "result_count": len(candidates),
                "validated_count": final_validated_count,
                "pass_count": final_validated_count,
                "revise_count": 0,
                "reject_count": 0,
                "final_validated_count": final_validated_count,
                "final_revise_count": 0,
                "final_reject_count": 0,
                "candidates_path": str(candidates_path),
                "validation_path": None,
                "artifact_candidates": artifact_candidates,
                "applied_skill_paths": applied_paths,
                "apply_error_count": len(apply_errors),
                "apply_errors": apply_errors,
                "apply_failed_candidate_ids": [
                    str(item.get("candidate_id") or "")
                    for item in apply_errors
                    if str(item.get("candidate_id") or "").strip()
                ],
            }
            self._write_json(run_dir / "knowledge_apply.json", apply_summary)
            return apply_summary

        validation_path = save_validation_results(run_dir / "knowledge_validation.json", validation_results)
        candidate_index = {candidate.id: candidate for candidate in candidates}
        applied_paths: list[str] = []
        status_map = {
            "PASS": "validated",
            "REVISE": "revise",
            "REJECT": "rejected",
        }
        skipped_non_pass_candidate_ids: list[str] = []
        for result in validation_results:
            if str(result.status).upper() != "PASS":
                candidate_id = str(result.candidate_id or "").strip()
                if candidate_id:
                    skipped_non_pass_candidate_ids.append(candidate_id)
                continue
            target_candidate = result.normalized_candidate or candidate_index.get(result.candidate_id)
            if target_candidate is None:
                continue
            target_candidate = self._prepare_candidate_for_apply(
                store=store,
                candidate=target_candidate,
            )
            self._record_run_dir_knowledge_restore_snapshot(
                run_dir=run_dir,
                store=store,
                candidate=target_candidate,
            )
            try:
                applied_path = store.apply_candidate(
                    target_candidate,
                    validation_status=status_map.get(str(result.status).upper(), "auto_synth"),
                    last_validation_reasons=list(result.reasons or []),
                    last_evidence_summary=result.evidence_summary,
                    last_validation_schema_version=str(validation_meta_dict.get("schema_version") or "").strip() or None,
                )
            except Exception as exc:
                print(f"[FlowArk] 自动总结知识写入失败: {result.candidate_id}: {exc}")
                apply_errors.append(
                    {
                        "candidate_id": str(result.candidate_id or ""),
                        "stage": "validated_result",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                continue
            if applied_path is not None:
                applied_paths.append(str(applied_path))

        pass_count = sum(1 for r in validation_results if r.status == "PASS")
        revise_count = sum(1 for r in validation_results if r.status == "REVISE")
        reject_count = sum(1 for r in validation_results if r.status == "REJECT")

        apply_summary = {
            "enabled": True,
            "executed": True,
            "mode": "synth_validate_apply",
            "validation_mode": validation_mode,
            "validation_skipped": False,
            "static_validation_executed": bool(validation_meta_dict.get("static_validation_executed", validation_mode == "static")),
            "synth_source": synth_source,
            "synth_meta": dict(synth_meta or {}),
            "validation_source": validation_source,
            "validation_meta": validation_meta_dict,
            "candidate_count": len(candidates),
            "unsupported_candidate_count": len(skipped_unsupported_candidates),
            "result_count": len(validation_results),
            "validated_count": pass_count,
            "pass_count": pass_count,
            "revise_count": revise_count,
            "reject_count": reject_count,
            "final_validated_count": pass_count,
            "final_revise_count": revise_count,
            "final_reject_count": reject_count,
            "candidates_path": str(candidates_path),
            "validation_path": str(validation_path),
            "artifact_candidates": artifact_candidates,
            "applied_skill_paths": applied_paths,
            "skipped_non_pass_candidate_count": len(skipped_non_pass_candidate_ids),
            "skipped_non_pass_candidate_ids": skipped_non_pass_candidate_ids,
            "apply_error_count": len(apply_errors),
            "apply_errors": apply_errors,
            "apply_failed_candidate_ids": [
                str(item.get("candidate_id") or "")
                for item in apply_errors
                if str(item.get("candidate_id") or "").strip()
            ],
        }
        self._write_json(run_dir / "knowledge_apply.json", apply_summary)
        return apply_summary
