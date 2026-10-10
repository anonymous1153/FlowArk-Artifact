"""知识规则路由。"""

from __future__ import annotations

from pathlib import Path

from flowark.knowledge.manager import KnowledgeManager, SKILL_SCHEMA_V4, SKILL_SCHEMA_V5
from flowark.knowledge.rule_matcher import rank_rule_candidates
from flowark.types import KnowledgeMatch


class RuleKnowledgeRouter:
    """基于 match_rules 的知识路由器。"""

    def __init__(
        self,
        skills_dir: Path | None = None,
        manager: KnowledgeManager | None = None,
        *,
        validated_only: bool = False,
        disable_legacy_task_specific: bool = False,
    ) -> None:
        if manager is None:
            if skills_dir is None:
                raise ValueError("RuleKnowledgeRouter 需要显式提供知识 scope 的 skills_dir")
            manager = KnowledgeManager(
                Path(skills_dir).expanduser().resolve(),
                accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5},
            )
        self.manager = manager
        self.validated_only = validated_only
        self.disable_legacy_task_specific = disable_legacy_task_specific

    @staticmethod
    def _reasons_from_match_result(result) -> list[str]:
        reasons: list[str] = []
        for matched in list(result.matched_require_all or [])[:4]:
            reasons.append(f"require_all 命中: {matched}")
        for matched in list(result.matched_require_any or [])[:4]:
            reasons.append(f"require_any 命中: {matched}")
        return reasons or ["match_rules 命中"]

    def _runtime_skills(self, *, current_app_name: str | None) -> list:
        if self.validated_only:
            skills = self.manager.get_validated_skills(current_app_name=current_app_name)
        else:
            skills = self.manager.get_runtime_eligible_skills(current_app_name=current_app_name)
        current_app = str(current_app_name or "").strip()
        filtered: list = []
        for skill in skills:
            skill_app = (skill.get_app_name() or "").strip()
            if skill_app and (not current_app or skill_app.casefold() != current_app.casefold()):
                continue
            if not skill.is_dsl_rule_packaged():
                continue
            if self.disable_legacy_task_specific and skill.is_legacy_task_specific():
                continue
            filtered.append(skill)
        return filtered

    def recall(
        self,
        *,
        text: str,
        limit: int = 8,
        current_app_name: str | None = None,
    ) -> list[KnowledgeMatch]:
        if limit <= 0 or not str(text or "").strip():
            return []
        skills = self._runtime_skills(current_app_name=current_app_name)
        if not skills:
            return []

        candidate_rules = [
            (skill.scoped_id, skill.get_match_rules())
            for skill in skills
            if skill.get_match_rules() is not None
        ]
        ranked = rank_rule_candidates(
            [
                (candidate_id, rules)
                for candidate_id, rules in candidate_rules
                if rules is not None
            ],
            text,
        )
        skill_by_candidate_id = {skill.scoped_id: skill for skill in skills}

        matches: list[KnowledgeMatch] = []
        for item in ranked:
            if not item.matched:
                continue
            skill = skill_by_candidate_id.get(item.candidate_id)
            if skill is None:
                continue
            matches.append(
                KnowledgeMatch(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    score=float(item.score),
                    validation_status=skill.get_validation_status(),
                    reasons=self._reasons_from_match_result(item),
                    match_fields=["match_rules"],
                    summary=skill.get_summary(),
                    content=skill.content,
                    metadata=dict(skill.metadata),
                    file_path=str(skill.file_path),
                    match_stage="rule_route",
                    legacy_task_specific=skill.is_legacy_task_specific(),
                )
            )
            if len(matches) >= limit:
                break
        return matches


KnowledgeRouter = RuleKnowledgeRouter
