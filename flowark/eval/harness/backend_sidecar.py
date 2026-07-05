from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .common import _json_dump, _json_load
from .models import EvaluationConfig

BACKEND_SNAPSHOT_FILE = "backend_snapshot.json"
BACKEND_SECRETS_FILE = "backend_secrets.json"
EPHEMERAL_BACKEND_SECRETS_ENV = "FLOWARK_EPHEMERAL_BACKEND_SECRETS"


def backend_snapshot_payload(config: EvaluationConfig) -> dict[str, Any]:
    pool_candidates = [
        {
            "profile_name": str(item.get("profile_name") or item.get("name") or ""),
            "name": str(item.get("name") or item.get("profile_name") or ""),
            "base_url": item.get("base_url"),
            "model": item.get("model"),
        }
        for item in list(config.runtime_backend_pool_candidates or [])
        if isinstance(item, dict)
    ]
    return {
        "schema_version": 1,
        "runtime_backend": {
            "profile_name": config.runtime_backend_profile,
            "mode": config.runtime_backend_mode,
            "pool": config.runtime_backend_pool,
            "base_url": config.runtime_backend_base_url,
            "model": config.runtime_backend_model,
            "pool_candidates": pool_candidates,
        },
        "llm_judge_backend": {
            "base_url": config.llm_judge_base_url,
            "model": config.llm_judge_model,
            "timeout_seconds": config.llm_judge_timeout_seconds,
            "max_retries": config.llm_judge_max_retries,
        },
        "reuse_embed_backend": {
            "base_url": config.reuse_embed_base_url,
            "model": config.reuse_embed_model,
            "verify_ssl": bool(config.reuse_embed_verify_ssl),
        },
        "reuse_rerank_backend": {
            "base_url": config.reuse_rerank_base_url,
            "model": config.reuse_rerank_model,
            "timeout_seconds": config.reuse_rerank_timeout_seconds,
        },
    }


def backend_secrets_payload(config: EvaluationConfig) -> dict[str, Any]:
    persist_secrets = str(os.environ.get(EPHEMERAL_BACKEND_SECRETS_ENV) or "").strip().lower() not in {"1", "true", "yes", "on"}

    def _secret(value: Any) -> Any:
        return value if persist_secrets else None

    pool_candidates = [
        {
            "profile_name": str(item.get("profile_name") or item.get("name") or ""),
            "name": str(item.get("name") or item.get("profile_name") or ""),
            "auth_token": _secret(item.get("auth_token")),
        }
        for item in list(config.runtime_backend_pool_candidates or [])
        if isinstance(item, dict)
    ]
    return {
        "schema_version": 1,
        "runtime_backend": {
            "auth_token": _secret(config.runtime_backend_auth_token),
            "pool_candidates": pool_candidates,
        },
        "llm_judge_backend": {
            "api_key": _secret(config.llm_judge_api_key),
        },
        "reuse_embed_backend": {
            "api_key": _secret(config.reuse_embed_api_key),
        },
        "reuse_rerank_backend": {
            "api_key": _secret(config.reuse_rerank_api_key),
        },
    }


def write_backend_sidecars(eval_root: Path, config: EvaluationConfig) -> tuple[Path, Path]:
    snapshot_path = eval_root / BACKEND_SNAPSHOT_FILE
    secrets_path = eval_root / BACKEND_SECRETS_FILE
    _json_dump(snapshot_path, backend_snapshot_payload(config))
    _json_dump(secrets_path, backend_secrets_payload(config))
    return snapshot_path, secrets_path


def load_backend_sidecars(eval_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_path = eval_root / BACKEND_SNAPSHOT_FILE
    secrets_path = eval_root / BACKEND_SECRETS_FILE
    if not snapshot_path.exists():
        raise ValueError(f"缺少 {BACKEND_SNAPSHOT_FILE}: {snapshot_path}")
    if not secrets_path.exists():
        raise ValueError(f"缺少 {BACKEND_SECRETS_FILE}: {secrets_path}")
    snapshot = _json_load(snapshot_path)
    secrets = _json_load(secrets_path)
    if not isinstance(snapshot, dict):
        raise ValueError(f"无效 {BACKEND_SNAPSHOT_FILE}: {snapshot_path}")
    if not isinstance(secrets, dict):
        raise ValueError(f"无效 {BACKEND_SECRETS_FILE}: {secrets_path}")
    return snapshot, secrets


def _section_text(payload: dict[str, Any], section: str, key: str) -> str:
    section_payload = payload.get(section)
    if not isinstance(section_payload, dict):
        return ""
    if key not in section_payload:
        return ""
    value = section_payload.get(key)
    if value is None:
        return ""
    return str(value).strip()


def apply_backend_sidecars_to_config(
    config: EvaluationConfig,
    *,
    snapshot: dict[str, Any],
    secrets: dict[str, Any],
) -> EvaluationConfig:
    runtime_snapshot = snapshot.get("runtime_backend") if isinstance(snapshot.get("runtime_backend"), dict) else {}
    runtime_secrets = secrets.get("runtime_backend") if isinstance(secrets.get("runtime_backend"), dict) else {}
    config.runtime_backend_profile = _section_text(snapshot, "runtime_backend", "profile_name") or None
    config.runtime_backend_mode = "single"
    config.runtime_backend_pool = None
    config.runtime_backend_base_url = _section_text(snapshot, "runtime_backend", "base_url") or None
    config.runtime_backend_model = _section_text(snapshot, "runtime_backend", "model") or None
    config.runtime_backend_auth_token = _section_text(secrets, "runtime_backend", "auth_token") or None
    snapshot_candidates = runtime_snapshot.get("pool_candidates") if isinstance(runtime_snapshot, dict) else None
    secret_candidates = runtime_secrets.get("pool_candidates") if isinstance(runtime_secrets, dict) else None
    if isinstance(snapshot_candidates, list):
        secret_by_name: dict[str, dict[str, Any]] = {}
        if isinstance(secret_candidates, list):
            for item in secret_candidates:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("profile_name") or item.get("name") or "").strip()
                if name:
                    secret_by_name[name] = item
        candidates: list[dict[str, Any]] = []
        for item in snapshot_candidates:
            if not isinstance(item, dict):
                continue
            name = str(item.get("profile_name") or item.get("name") or "").strip()
            if not name:
                continue
            candidate = {
                "profile_name": name,
                "name": name,
                "base_url": item.get("base_url"),
                "model": item.get("model"),
                "auth_token": (secret_by_name.get(name) or {}).get("auth_token"),
            }
            candidates.append(candidate)
        config.runtime_backend_pool_candidates = []

    config.llm_judge_base_url = _section_text(snapshot, "llm_judge_backend", "base_url")
    config.llm_judge_model = _section_text(snapshot, "llm_judge_backend", "model") or config.llm_judge_model
    llm_timeout = _section_text(snapshot, "llm_judge_backend", "timeout_seconds")
    if llm_timeout:
        config.llm_judge_timeout_seconds = max(1, int(llm_timeout))
    llm_retries = _section_text(snapshot, "llm_judge_backend", "max_retries")
    if llm_retries:
        config.llm_judge_max_retries = max(0, int(llm_retries))
    config.llm_judge_api_key = _section_text(secrets, "llm_judge_backend", "api_key")

    config.reuse_embed_base_url = _section_text(snapshot, "reuse_embed_backend", "base_url") or None
    config.reuse_embed_model = _section_text(snapshot, "reuse_embed_backend", "model") or None
    reuse_embed_verify = _section_text(snapshot, "reuse_embed_backend", "verify_ssl").lower()
    if reuse_embed_verify:
        config.reuse_embed_verify_ssl = reuse_embed_verify in {"1", "true", "yes", "on"}
    config.reuse_embed_api_key = _section_text(secrets, "reuse_embed_backend", "api_key") or None

    config.reuse_rerank_base_url = _section_text(snapshot, "reuse_rerank_backend", "base_url") or None
    config.reuse_rerank_model = _section_text(snapshot, "reuse_rerank_backend", "model") or None
    reuse_timeout = _section_text(snapshot, "reuse_rerank_backend", "timeout_seconds")
    if reuse_timeout:
        config.reuse_rerank_timeout_seconds = max(1, int(reuse_timeout))
    config.reuse_rerank_api_key = _section_text(secrets, "reuse_rerank_backend", "api_key") or None
    return config
