"""Embedding-based knowledge router for packaging ablations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

from flowark.knowledge.manager import KnowledgeManager, SKILL_SCHEMA_V4, SKILL_SCHEMA_V5, SkillRecord
from flowark.knowledge.reuse_recall import ReuseEmbeddingConfig, embed_texts
from flowark.types import KnowledgeMatch

_QUERY_INSTRUCTION = "Retrieve knowledge relevant to the current code/data-flow analysis context."


def build_knowledge_embedding_query_text(match_text: str) -> str:
    body = str(match_text or "").strip()
    if not body:
        return ""
    return f"Instruct: {_QUERY_INSTRUCTION}\nQuery:\n{body}"


def build_skill_embedding_text(skill: SkillRecord) -> str:
    return "\n".join(
        part
        for part in [
            skill.get_entry_condition(),
            skill.content,
        ]
        if str(part or "").strip()
    ).strip()


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _slug(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip()).strip("._-")
    return text or "default"


def _backend_fingerprint(config: ReuseEmbeddingConfig) -> str:
    payload = {
        "base_url": str(config.base_url or "").strip().rstrip("/"),
        "model": str(config.model or "").strip(),
        "verify_ssl": bool(config.verify_ssl),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


class EmbeddingKnowledgeRouter:
    """基于知识正文 embedding 的召回器。"""

    def __init__(
        self,
        *,
        skills_dir: Path,
        embedding_config: ReuseEmbeddingConfig,
        manager: KnowledgeManager | None = None,
        validated_only: bool = False,
        disable_legacy_task_specific: bool = False,
    ) -> None:
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        self.manager = manager or KnowledgeManager(
            self.skills_dir,
            accepted_schema_versions={SKILL_SCHEMA_V4, SKILL_SCHEMA_V5},
        )
        self.embedding_config = embedding_config
        self.validated_only = validated_only
        self.disable_legacy_task_specific = disable_legacy_task_specific
        self.backend_fingerprint = _backend_fingerprint(self.embedding_config)
        self.cache_dir = (
            self.skills_dir.parent
            / "embedding_cache"
            / _slug(str(self.embedding_config.model or "default"))
            / self.backend_fingerprint
        )

    def _runtime_skills(self, *, current_app_name: str | None) -> list[SkillRecord]:
        if self.validated_only:
            skills = self.manager.get_validated_skills(current_app_name=current_app_name)
        else:
            skills = self.manager.get_runtime_eligible_skills(current_app_name=current_app_name)
        current_app = str(current_app_name or "").strip()
        filtered: list[SkillRecord] = []
        for skill in skills:
            if not skill.is_embedding_packaged():
                continue
            skill_app = (skill.get_app_name() or "").strip()
            if skill_app and (not current_app or skill_app.casefold() != current_app.casefold()):
                continue
            if self.disable_legacy_task_specific and skill.is_legacy_task_specific():
                continue
            if not build_skill_embedding_text(skill):
                continue
            filtered.append(skill)
        return filtered

    def _cache_path(self, skill: SkillRecord, *, input_hash: str) -> Path:
        key = "\0".join(
            [
                str(self.embedding_config.model or ""),
                self.backend_fingerprint,
                str(skill.scoped_id or skill.id),
                str(skill.file_path),
                input_hash,
            ]
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cached_embedding(self, skill: SkillRecord, *, input_hash: str) -> list[float] | None:
        path = self._cache_path(skill, input_hash=input_hash)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if str(payload.get("model") or "") != str(self.embedding_config.model or ""):
            return None
        if str(payload.get("backend_fingerprint") or "") != self.backend_fingerprint:
            return None
        if str(payload.get("input_hash") or "") != input_hash:
            return None
        embedding = payload.get("embedding")
        if not isinstance(embedding, list):
            return None
        try:
            return [float(value) for value in embedding]
        except Exception:
            return None

    def _write_cached_embedding(
        self,
        skill: SkillRecord,
        *,
        input_text: str,
        input_hash: str,
        embedding: Sequence[float],
    ) -> None:
        path = self._cache_path(skill, input_hash=input_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": "flowark-knowledge-embedding-cache-v1",
            "model": str(self.embedding_config.model or ""),
            "backend_fingerprint": self.backend_fingerprint,
            "base_url": str(self.embedding_config.base_url or "").strip(),
            "verify_ssl": bool(self.embedding_config.verify_ssl),
            "skill_id": skill.id,
            "scoped_id": skill.scoped_id,
            "skill_file": str(skill.file_path),
            "input_hash": input_hash,
            "input_chars": len(input_text),
            "embedding": [float(value) for value in embedding],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _skill_embeddings(self, skills: list[SkillRecord]) -> list[tuple[SkillRecord, list[float]]]:
        resolved: list[tuple[SkillRecord, list[float]]] = []
        missing: list[tuple[SkillRecord, str, str]] = []
        for skill in skills:
            input_text = build_skill_embedding_text(skill)
            input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
            cached = self._read_cached_embedding(skill, input_hash=input_hash)
            if cached is not None:
                resolved.append((skill, cached))
            else:
                missing.append((skill, input_text, input_hash))
        if missing:
            vectors = embed_texts([input_text for _skill, input_text, _hash in missing], self.embedding_config)
            if len(vectors) != len(missing):
                raise RuntimeError("embedding 后端返回数量与请求数量不一致")
            for (skill, input_text, input_hash), vector in zip(missing, vectors, strict=False):
                self._write_cached_embedding(skill, input_text=input_text, input_hash=input_hash, embedding=vector)
                resolved.append((skill, [float(value) for value in vector]))
        return resolved

    def recall(
        self,
        *,
        text: str,
        limit: int = 3,
        current_app_name: str | None = None,
    ) -> list[KnowledgeMatch]:
        query_text = build_knowledge_embedding_query_text(text)
        if limit <= 0 or not query_text:
            return []
        skills = self._runtime_skills(current_app_name=current_app_name)
        if not skills:
            return []
        query_vectors = embed_texts([query_text], self.embedding_config)
        if len(query_vectors) != 1:
            raise RuntimeError("embedding 后端未返回 query embedding")
        query_vector = query_vectors[0]
        scored: list[tuple[float, SkillRecord]] = []
        for skill, vector in self._skill_embeddings(skills):
            scored.append((_cosine_similarity(query_vector, vector), skill))
        scored.sort(key=lambda item: item[0], reverse=True)

        matches: list[KnowledgeMatch] = []
        for score, skill in scored[: max(1, int(limit or 3))]:
            matches.append(
                KnowledgeMatch(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    score=float(score),
                    validation_status=skill.get_validation_status(),
                    reasons=[f"embedding_similarity={score:.4f}"],
                    match_fields=["embedding"],
                    summary=skill.get_summary(),
                    content=skill.content,
                    metadata=dict(skill.metadata),
                    file_path=str(skill.file_path),
                    match_stage="embedding_route",
                    legacy_task_specific=skill.is_legacy_task_specific(),
                )
            )
        return matches
