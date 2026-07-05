"""Internal transport env keys for project-owned backend configuration."""

from __future__ import annotations

from typing import Any, Mapping

INTERNAL_LLM_JUDGE_BASE_URL_ENV = "FLOWARK_INTERNAL_LLM_JUDGE_BASE_URL"
INTERNAL_LLM_JUDGE_API_KEY_ENV = "FLOWARK_INTERNAL_LLM_JUDGE_API_KEY"
INTERNAL_LLM_JUDGE_MODEL_ENV = "FLOWARK_INTERNAL_LLM_JUDGE_MODEL"
INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS_ENV = "FLOWARK_INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS"
INTERNAL_LLM_JUDGE_MAX_RETRIES_ENV = "FLOWARK_INTERNAL_LLM_JUDGE_MAX_RETRIES"

INTERNAL_REUSE_EMBED_BASE_URL_ENV = "FLOWARK_INTERNAL_REUSE_EMBED_BASE_URL"
INTERNAL_REUSE_EMBED_API_KEY_ENV = "FLOWARK_INTERNAL_REUSE_EMBED_API_KEY"
INTERNAL_REUSE_EMBED_MODEL_ENV = "FLOWARK_INTERNAL_REUSE_EMBED_MODEL"
INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV = "FLOWARK_INTERNAL_REUSE_EMBED_VERIFY_SSL"

INTERNAL_REUSE_RERANK_BASE_URL_ENV = "FLOWARK_INTERNAL_REUSE_RERANK_BASE_URL"
INTERNAL_REUSE_RERANK_API_KEY_ENV = "FLOWARK_INTERNAL_REUSE_RERANK_API_KEY"
INTERNAL_REUSE_RERANK_MODEL_ENV = "FLOWARK_INTERNAL_REUSE_RERANK_MODEL"
INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV = "FLOWARK_INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS"
INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV = "FLOWARK_INTERNAL_BACKEND_TRANSPORT_ACTIVE"

INTERNAL_BACKEND_TRANSPORT_ENV_KEYS = (
    INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV,
    INTERNAL_LLM_JUDGE_BASE_URL_ENV,
    INTERNAL_LLM_JUDGE_API_KEY_ENV,
    INTERNAL_LLM_JUDGE_MODEL_ENV,
    INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS_ENV,
    INTERNAL_LLM_JUDGE_MAX_RETRIES_ENV,
    INTERNAL_REUSE_EMBED_BASE_URL_ENV,
    INTERNAL_REUSE_EMBED_API_KEY_ENV,
    INTERNAL_REUSE_EMBED_MODEL_ENV,
    INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV,
    INTERNAL_REUSE_RERANK_BASE_URL_ENV,
    INTERNAL_REUSE_RERANK_API_KEY_ENV,
    INTERNAL_REUSE_RERANK_MODEL_ENV,
    INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV,
)


def _nonempty_text(mapping: Mapping[str, Any], key: str) -> str | None:
    value = str(mapping.get(key) or "").strip()
    return value or None


def _bool_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on"}:
        return "true"
    if text in {"0", "false", "no", "off"}:
        return "false"
    return None


def build_internal_backend_transport_env(
    *,
    llm_judge_base_url: str | None = None,
    llm_judge_api_key: str | None = None,
    llm_judge_model: str | None = None,
    llm_judge_timeout_seconds: int | str | None = None,
    llm_judge_max_retries: int | str | None = None,
    reuse_embed_base_url: str | None = None,
    reuse_embed_api_key: str | None = None,
    reuse_embed_model: str | None = None,
    reuse_embed_verify_ssl: bool | str | None = None,
    reuse_rerank_base_url: str | None = None,
    reuse_rerank_api_key: str | None = None,
    reuse_rerank_model: str | None = None,
    reuse_rerank_timeout_seconds: int | str | None = None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}

    def _put(key: str, value: Any) -> None:
        if value is None:
            text = ""
        else:
            text = str(value).strip()
        if text:
            overrides[key] = text

    _put(INTERNAL_LLM_JUDGE_BASE_URL_ENV, llm_judge_base_url)
    _put(INTERNAL_LLM_JUDGE_API_KEY_ENV, llm_judge_api_key)
    _put(INTERNAL_LLM_JUDGE_MODEL_ENV, llm_judge_model)
    _put(INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS_ENV, llm_judge_timeout_seconds)
    _put(INTERNAL_LLM_JUDGE_MAX_RETRIES_ENV, llm_judge_max_retries)

    _put(INTERNAL_REUSE_EMBED_BASE_URL_ENV, reuse_embed_base_url)
    _put(INTERNAL_REUSE_EMBED_API_KEY_ENV, reuse_embed_api_key)
    _put(INTERNAL_REUSE_EMBED_MODEL_ENV, reuse_embed_model)
    verify_text = _bool_text(reuse_embed_verify_ssl)
    if verify_text:
        overrides[INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV] = verify_text

    _put(INTERNAL_REUSE_RERANK_BASE_URL_ENV, reuse_rerank_base_url)
    _put(INTERNAL_REUSE_RERANK_API_KEY_ENV, reuse_rerank_api_key)
    _put(INTERNAL_REUSE_RERANK_MODEL_ENV, reuse_rerank_model)
    _put(INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV, reuse_rerank_timeout_seconds)
    if overrides:
        overrides[INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV] = "1"
    return overrides


def read_internal_backend_transport_env(environ: Mapping[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key in INTERNAL_BACKEND_TRANSPORT_ENV_KEYS:
        value = _nonempty_text(environ, key)
        if value is not None:
            payload[key] = value
    return payload
