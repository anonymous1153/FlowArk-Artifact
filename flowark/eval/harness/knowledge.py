"""Knowledge scope helpers for the evaluation harness."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from flowark.types import KnowledgeCandidate, MatchRule, MatchRules

from .common import _json_dump, _json_load, _now_utc_iso

if TYPE_CHECKING:
    from .orchestrator import EvaluationHarness


def _knowledge_restore_snapshot_path(run_dir: Path) -> Path:
    return run_dir / "knowledge_restore_snapshot.json"


def _knowledge_dirs(self: EvaluationHarness) -> tuple[Path, Path, Path]:
    if self._knowledge_scope_root is None:
        raise ValueError("evaluation knowledge scope 尚未初始化")
    knowledge_root = self._knowledge_scope_root
    skills_dir = knowledge_root / "skills"
    egress_dir = knowledge_root / "egress"
    provenance_dir = knowledge_root / "provenance"
    return skills_dir, egress_dir, provenance_dir


def _extract_candidate_artifacts_from_run_dir(run_dir: Path) -> list[dict[str, Any]]:
    apply_path = run_dir / "knowledge_apply.json"
    if apply_path.exists():
        try:
            apply_payload = _json_load(apply_path)
        except Exception:
            apply_payload = None
        artifacts = apply_payload.get("artifact_candidates") if isinstance(apply_payload, dict) else None
        if isinstance(artifacts, list):
            out = []
            for item in artifacts:
                if not isinstance(item, dict):
                    continue
                skill_id = str(item.get("skill_id") or "").strip()
                if not skill_id:
                    continue
                out.append(
                    {
                        "skill_id": skill_id,
                        "app_name": item.get("app_name"),
                    }
                )
            if out:
                return out
    candidates_path = run_dir / "knowledge_candidates.json"
    if not candidates_path.exists():
        return []
    try:
        payload = _json_load(candidates_path)
    except Exception:
        return []
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        return []
    out: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("id") or "").strip()
        if not skill_id:
            continue
        out.append(
            {
                "skill_id": skill_id,
                "app_name": item.get("app_name"),
            }
        )
    return out


def _rollback_run_dir_knowledge_side_effects(self: EvaluationHarness, run_dir: Path) -> None:
    from flowark.knowledge.pipeline import KnowledgeStore

    skills_dir, _, _ = self._knowledge_dirs()
    store = KnowledgeStore(skills_dir)
    snapshot_path = _knowledge_restore_snapshot_path(run_dir)

    if snapshot_path.exists():
        try:
            snapshot_payload = _json_load(snapshot_path)
        except Exception:
            snapshot_payload = None
        records = snapshot_payload.get("records") if isinstance(snapshot_payload, dict) else None
        if isinstance(records, dict):
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                files = record.get("files")
                if not isinstance(files, list):
                    continue
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    path_text = str(item.get("path") or "").strip()
                    if not path_text:
                        continue
                    target = Path(path_text)
                    existed = bool(item.get("existed"))
                    content = item.get("content")
                    try:
                        if existed:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(str(content or ""), encoding="utf-8")
                        elif target.exists():
                            target.unlink()
                    except Exception:
                        continue
            return

    def _candidate_for(skill_id: str, *, app_name: str | None) -> KnowledgeCandidate:
        return KnowledgeCandidate(
            id=skill_id,
            name=skill_id,
            match_rules=MatchRules(require_any=[MatchRule(kind="symbol_tail", value=skill_id)]),
            entry_condition="",
            schema_version="flowark-skill-v5",
            app_name=app_name,
            content="",
        )

    for item in self._extract_candidate_artifacts_from_run_dir(run_dir):
        skill_id = str(item.get("skill_id") or "").strip()
        if not skill_id:
            continue
        app_name = str(item.get("app_name") or "").strip() or None
        candidate = _candidate_for(skill_id, app_name=app_name)
        targets = [
            store._skill_path_for(skill_id=skill_id, app_name=app_name),
            store._legacy_skill_path_for(skill_id=skill_id),
            store._provenance_path_for(candidate=candidate),
            store._legacy_provenance_path_for(candidate=candidate),
            store._sidecar_path_for(skill_id, app_name=app_name),
            store._legacy_sidecar_path_for(skill_id),
        ]
        for target in targets:
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                continue


def _prepare_knowledge_scope(
    self: EvaluationHarness,
    *,
    eval_root: Path,
    cases: list[Any],
    modes: list[str],
) -> dict[str, Any]:
    """为当前 eval 任务创建独立知识作用域。"""
    scope_root = (eval_root / "knowledge_scope").resolve()
    skills_dir = scope_root / "skills"
    egress_dir = scope_root / "egress"
    provenance_dir = scope_root / "provenance"
    skills_dir.mkdir(parents=True, exist_ok=True)
    egress_dir.mkdir(parents=True, exist_ok=True)
    provenance_dir.mkdir(parents=True, exist_ok=True)

    self._knowledge_scope_root = scope_root
    manifest = {
        "created_at": _now_utc_iso(),
        "scope_mode": "isolated_per_eval_task",
        "eval_root": str(eval_root),
        "dataset": self._dataset_slug(cases),
        "modes": list(modes),
        "knowledge_root": str(scope_root),
        "skills_dir": str(skills_dir),
        "egress_dir": str(egress_dir),
        "provenance_dir": str(provenance_dir),
    }
    _json_dump(scope_root / "scope_manifest.json", manifest)
    return manifest
