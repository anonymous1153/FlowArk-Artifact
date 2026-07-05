"""Concrete FlowArk semantic engine used by the current runtime."""

from __future__ import annotations

import json
import re

from flowark.prompt_loader import render_prompt
from flowark.knowledge_packaging import (
    KNOWLEDGE_PACKAGING_EMBEDDING,
    normalize_knowledge_packaging_mode,
)
from flowark.runtime.config import (
    KNOWLEDGE_DISTILLATION_GENERIC,
    normalize_knowledge_distillation_mode,
)
from flowark.semantics.knowledge_augmentation import KnowledgeAugmentationSemantics
from flowark.semantics.models import (
    AfterToolContext,
    AugmentDecision,
    AugmentRuntimeConfig,
    FinalReportPhaseInput,
    KnowledgeRuleRepairPhaseInput,
    KnowledgeSynthPhaseInput,
    Phase,
    PhaseInput,
    PhasePolicy,
    PhaseSpec,
    RequestSubmitContext,
    TurnContract,
)
from flowark.semantics.phases import default_phase_policy
from flowark.types import to_jsonable


class FlowArkSemanticEngine:
    """Runtime-facing semantic engine for augmentation and phase spec construction."""

    def __init__(
        self,
        *,
        runtime_config: AugmentRuntimeConfig,
        knowledge_distillation_mode: str = "with_selection_rules",
        knowledge_reuse_digest_mode: str = "off",
    ) -> None:
        self._augmentation = KnowledgeAugmentationSemantics(runtime_config)
        self._knowledge_distillation_mode = normalize_knowledge_distillation_mode(
            knowledge_distillation_mode
        )
        self._knowledge_reuse_digest_mode = str(knowledge_reuse_digest_mode or "off").strip().lower()
        self._knowledge_packaging_mode = normalize_knowledge_packaging_mode(
            runtime_config.knowledge_packaging_mode
        )

    async def request_submit_augment(
        self,
        ctx: RequestSubmitContext,
    ) -> AugmentDecision:
        return await self._augmentation.request_submit_augment(ctx)

    async def after_tool_augment(
        self,
        ctx: AfterToolContext,
    ) -> AugmentDecision:
        return await self._augmentation.after_tool_augment(ctx)

    def augment_runtime_config(self) -> AugmentRuntimeConfig:
        return self._augmentation.augment_runtime_config()

    def phase_policy(self, phase: Phase) -> PhasePolicy:
        return default_phase_policy(phase)

    def build_phase_spec(
        self,
        phase: Phase,
        *,
        phase_input: PhaseInput,
    ) -> PhaseSpec:
        if phase is Phase.FINAL_REPORT:
            if not isinstance(phase_input, FinalReportPhaseInput):
                raise TypeError("final_report phase_input 必须是 FinalReportPhaseInput")
            return PhaseSpec(
                phase=phase,
                phase_input=phase_input,
                instruction=self._build_final_report_request(phase_input),
                policy=self.phase_policy(phase),
                turn_contract=TurnContract(
                    turn_name="final_report",
                    transcript_prefix="phase:final_report",
                    echo=False,
                    expect_json=True,
                    timeout_seconds=180,
                ),
            )

        if phase is Phase.KNOWLEDGE_SYNTH:
            if not isinstance(phase_input, KnowledgeSynthPhaseInput):
                raise TypeError("knowledge_synth phase_input 必须是 KnowledgeSynthPhaseInput")
            return PhaseSpec(
                phase=phase,
                phase_input=phase_input,
                instruction=self._build_knowledge_synth_request(phase_input),
                policy=self.phase_policy(phase),
                turn_contract=TurnContract(
                    turn_name="knowledge_synth",
                    transcript_prefix="phase:knowledge_synth",
                    echo=False,
                    expect_json=True,
                    timeout_seconds=300,
                ),
            )

        if phase is Phase.KNOWLEDGE_RULE_REPAIR:
            if not isinstance(phase_input, KnowledgeRuleRepairPhaseInput):
                raise TypeError("knowledge_rule_repair phase_input 必须是 KnowledgeRuleRepairPhaseInput")
            return PhaseSpec(
                phase=phase,
                phase_input=phase_input,
                instruction=self._build_knowledge_rule_repair_request(phase_input),
                policy=self.phase_policy(phase),
                turn_contract=TurnContract(
                    turn_name="knowledge_rule_repair",
                    transcript_prefix="phase:knowledge_rule_repair",
                    echo=False,
                    expect_json=True,
                    timeout_seconds=120,
                ),
            )

        raise ValueError(f"unsupported phase spec construction: {phase!r}")

    @staticmethod
    def _build_final_report_request(phase_input: FinalReportPhaseInput) -> str:
        request = phase_input.request
        sink_types_json = json.dumps(request.sink_types or ["unknown"], ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        return render_prompt(
            "final_report",
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
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

    def _build_knowledge_synth_request(self, phase_input: KnowledgeSynthPhaseInput) -> str:
        request = phase_input.request
        sink_types_json = json.dumps(list(request.sink_types or []), ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        validated_catalog_block = self._render_skill_catalog_block(
            title="当前已验证的持久化知识目录（只有这里的知识可视为真正已覆盖）",
            items=phase_input.validated_skill_catalog,
            include_repair_details=False,
        )
        repairable_catalog_block = self._render_skill_catalog_block(
            title="当前待修复知识目录（仅作修复参考，不算已覆盖）",
            items=phase_input.repairable_skill_catalog,
            include_repair_details=True,
        )
        historical_reuse_digest_guidance_block = ""
        if self._knowledge_distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
            return render_prompt(
                "knowledge_synth_generic",
                validated_catalog_block=validated_catalog_block,
                repairable_catalog_block=repairable_catalog_block,
                source_desc_json=source_desc_json,
                sink_types_json=sink_types_json,
            )
        if str(phase_input.historical_reuse_digest_block or "").strip():
            if self._knowledge_reuse_digest_mode == "live_corridor_v2":
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
            if self._knowledge_packaging_mode == KNOWLEDGE_PACKAGING_EMBEDDING
            else "knowledge_synth"
        )
        return render_prompt(
            prompt_name,
            validated_catalog_block=validated_catalog_block,
            repairable_catalog_block=repairable_catalog_block,
            historical_reuse_digest_guidance_block=historical_reuse_digest_guidance_block,
            historical_reuse_digest_block=str(phase_input.historical_reuse_digest_block or ""),
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
        )

    @staticmethod
    def _build_knowledge_rule_repair_request(phase_input: KnowledgeRuleRepairPhaseInput) -> str:
        request = phase_input.request
        sink_types_json = json.dumps(list(request.sink_types or []), ensure_ascii=False)
        source_desc_json = json.dumps(request.source or "", ensure_ascii=False)
        return render_prompt(
            "knowledge_rule_repair",
            source_desc_json=source_desc_json,
            sink_types_json=sink_types_json,
            candidate_json=json.dumps(to_jsonable(phase_input.candidate), ensure_ascii=False, indent=2),
            static_reasons_json=json.dumps(
                list(phase_input.static_result.reasons or []),
                ensure_ascii=False,
                indent=2,
            ),
            issue_types_json=json.dumps(list(phase_input.issue_types or []), ensure_ascii=False, indent=2),
        )


__all__ = ["FlowArkSemanticEngine"]
