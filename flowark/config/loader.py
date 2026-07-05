"""Unified config/env loader for runtime, eval, and Studio."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values, load_dotenv
import yaml

from flowark.backend_transport import build_internal_backend_transport_env

RUNTIME_CONFIG_RELATIVE_PATH = Path("flowark/config/runtime.yaml")
EVAL_CONFIG_RELATIVE_PATH = Path("flowark/config/eval.yaml")
CAPTURE_PROXY_CONFIG_RELATIVE_PATH = Path("flowark/config/capture_proxy.yaml")

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "out_dir": "runs",
    "agent_adapter": "opencode",
    "opencode_binary": None,
    "opencode_model": "",
    "opencode_provider": "anthropic",
    "opencode_after_tool_delivery": "no_reply_context",
    "opencode_bash_policy": "read_only_guarded",
    "opencode_post_phase_mode": "plain_json_same_surface",
    "opencode_structured_output": True,
    "agent_mode": "flowark",
    "knowledge_mode": "warm",
    "knowledge_allow_repeat_injection_within_session": True,
    "auto_knowledge_cycle": True,
    "runtime_injection_mode": "context_aware",
    "knowledge_distillation_mode": "with_selection_rules",
    "knowledge_packaging_mode": "dsl_rule",
    "auto_knowledge_validate_mode": "static",
    "knowledge_reuse_digest_mode": "off",
    "knowledge_top_k": 3,
    "knowledge_recall_top_m": 8,
    "knowledge_min_score": 1.0,
    "knowledge_injection_char_budget": 4000,
    "knowledge_delta_char_budget": None,
    "knowledge_realtime_min_interval_ms": 1500,
    "knowledge_repeat_summary_hook_gap": 3,
    "knowledge_repeat_full_hook_gap": 10,
    "knowledge_repeat_summary_react_gap": 0,
    "knowledge_repeat_full_react_gap": 1,
}

DEFAULT_EVAL_CONFIG: dict[str, Any] = {
    "input_path": "",
    "out_dir": "evals",
    "agent_adapter": "opencode",
    "opencode_binary": None,
    "opencode_model": "",
    "opencode_provider": "anthropic",
    "opencode_after_tool_delivery": "no_reply_context",
    "opencode_bash_policy": "read_only_guarded",
    "opencode_post_phase_mode": "plain_json_same_surface",
    "opencode_structured_output": True,
    "modes": ["naive", "flowark"],
    "parallel": 2,
    "serialize_within_app": True,
    "repeats": 1,
    "classification_filter": "all",
    "max_apps": None,
    "max_sources": None,
    "app_names": [],
    "dummy_run": False,
    "knowledge_mode": "warm",
    "knowledge_allow_repeat_injection_within_session": True,
    "auto_knowledge_cycle": True,
    "runtime_injection_mode": "context_aware",
    "knowledge_distillation_mode": "with_selection_rules",
    "knowledge_packaging_mode": "dsl_rule",
    "auto_knowledge_validate_mode": "static",
    "knowledge_reuse_digest_mode": "off",
    "knowledge_top_k": 3,
    "knowledge_recall_top_m": 8,
    "knowledge_repeat_summary_react_gap": 0,
    "knowledge_repeat_full_react_gap": 1,
    "timeout_seconds": 1800,
    "llm_judge_enabled": True,
    "llm_judge_model": "",
    "llm_judge_timeout_seconds": 120,
    "llm_judge_max_retries": 2,
}

DEFAULT_CAPTURE_PROXY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "listen_host": "127.0.0.1",
    "port": 0,
    "strip_prefix": "/api/anthropic",
    "output_root": None,
}

@dataclass(frozen=True)
class RepoEnvConfig:
    anthropic_base_url: str | None = None
    anthropic_auth_token: str | None = None
    anthropic_model: str | None = None
    judge_base_url: str | None = None
    judge_api_key: str | None = None
    judge_model: str | None = None
    api_timeout_ms: str | None = None
    reuse_embed_base_url: str | None = None
    reuse_embed_api_key: str | None = None
    reuse_embed_model: str | None = None
    reuse_embed_verify_ssl: str | None = None
    reuse_rerank_base_url: str | None = None
    reuse_rerank_api_key: str | None = None
    reuse_rerank_model: str | None = None
    reuse_rerank_timeout_seconds: str | None = None
    flowark_data_root: str | None = None


def load_repo_dotenv(workspace_root: Path | str, *, override: bool = False) -> bool:
    env_path = Path(workspace_root).expanduser().resolve() / ".env"
    if not env_path.exists():
        return False
    return bool(load_dotenv(dotenv_path=env_path, override=override))


def load_runtime_config_defaults(workspace_root: Path | str) -> dict[str, Any]:
    return _load_config_defaults(
        workspace_root=workspace_root,
        relative_path=RUNTIME_CONFIG_RELATIVE_PATH,
        default_values=DEFAULT_RUNTIME_CONFIG,
    )


def load_eval_config_defaults(workspace_root: Path | str) -> dict[str, Any]:
    return _load_config_defaults(
        workspace_root=workspace_root,
        relative_path=EVAL_CONFIG_RELATIVE_PATH,
        default_values=DEFAULT_EVAL_CONFIG,
    )


def load_capture_proxy_config_defaults(workspace_root: Path | str) -> dict[str, Any]:
    return _load_config_defaults(
        workspace_root=workspace_root,
        relative_path=CAPTURE_PROXY_CONFIG_RELATIVE_PATH,
        default_values=DEFAULT_CAPTURE_PROXY_CONFIG,
    )


def resolve_repo_env_config(
    workspace_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> RepoEnvConfig:
    file_values = _load_dotenv_values(workspace_root)
    return RepoEnvConfig(
        anthropic_base_url=_resolve_env_value(
            file_values,
            primary_keys=("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"),
        ),
        anthropic_auth_token=_resolve_env_value(
            file_values,
            primary_keys=("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"),
        ),
        anthropic_model=_resolve_env_value(
            file_values,
            primary_keys=(
                "ANTHROPIC_MODEL",
                "OPENAI_MODEL",
                "ANTHROPIC_REASONING_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            ),
        ),
        judge_base_url=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_LLM_JUDGE_BASE_URL",),
        ),
        judge_api_key=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_LLM_JUDGE_API_KEY",),
        ),
        judge_model=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_LLM_JUDGE_MODEL",),
        ),
        api_timeout_ms=_resolve_env_value(
            file_values,
            primary_keys=("API_TIMEOUT_MS",),
        ),
        reuse_embed_base_url=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_EMBED_BASE_URL",),
        ),
        reuse_embed_api_key=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_EMBED_API_KEY",),
        ),
        reuse_embed_model=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_EMBED_MODEL",),
        ),
        reuse_embed_verify_ssl=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_EMBED_VERIFY_SSL",),
        ),
        reuse_rerank_base_url=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_RERANK_BASE_URL",),
        ),
        reuse_rerank_api_key=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_RERANK_API_KEY",),
        ),
        reuse_rerank_model=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_RERANK_MODEL",),
        ),
        reuse_rerank_timeout_seconds=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_REUSE_RERANK_TIMEOUT_SECONDS",),
        ),
        flowark_data_root=_resolve_env_value(
            file_values,
            primary_keys=("FLOWARK_DATA_ROOT",),
        ),
    )


def resolve_run_env_config(
    workspace_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    repo_env = resolve_repo_env_config(workspace_root, environ=environ)
    return {
        "reuse_embed_base_url": repo_env.reuse_embed_base_url,
        "reuse_embed_api_key": repo_env.reuse_embed_api_key,
        "reuse_embed_model": repo_env.reuse_embed_model,
        "reuse_embed_verify_ssl": repo_env.reuse_embed_verify_ssl,
        "reuse_rerank_base_url": repo_env.reuse_rerank_base_url,
        "reuse_rerank_api_key": repo_env.reuse_rerank_api_key,
        "reuse_rerank_model": repo_env.reuse_rerank_model,
        "reuse_rerank_timeout_seconds": repo_env.reuse_rerank_timeout_seconds,
    }


def resolve_eval_env_config(
    workspace_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    repo_env = resolve_repo_env_config(workspace_root, environ=environ)
    return {
        "llm_judge_base_url": repo_env.judge_base_url,
        "llm_judge_api_key": repo_env.judge_api_key,
        "llm_judge_model": repo_env.judge_model,
        "reuse_embed_base_url": repo_env.reuse_embed_base_url,
        "reuse_embed_api_key": repo_env.reuse_embed_api_key,
        "reuse_embed_model": repo_env.reuse_embed_model,
        "reuse_embed_verify_ssl": repo_env.reuse_embed_verify_ssl,
        "reuse_rerank_base_url": repo_env.reuse_rerank_base_url,
        "reuse_rerank_api_key": repo_env.reuse_rerank_api_key,
        "reuse_rerank_model": repo_env.reuse_rerank_model,
        "reuse_rerank_timeout_seconds": repo_env.reuse_rerank_timeout_seconds,
    }


def build_workspace_child_env_overrides(
    workspace_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    repo_env = resolve_repo_env_config(workspace_root, environ=environ)
    overrides = build_internal_backend_transport_env(
        llm_judge_base_url=repo_env.judge_base_url,
        llm_judge_api_key=repo_env.judge_api_key,
        llm_judge_model=repo_env.judge_model,
        reuse_embed_base_url=repo_env.reuse_embed_base_url,
        reuse_embed_api_key=repo_env.reuse_embed_api_key,
        reuse_embed_model=repo_env.reuse_embed_model,
        reuse_embed_verify_ssl=repo_env.reuse_embed_verify_ssl,
        reuse_rerank_base_url=repo_env.reuse_rerank_base_url,
        reuse_rerank_api_key=repo_env.reuse_rerank_api_key,
        reuse_rerank_model=repo_env.reuse_rerank_model,
        reuse_rerank_timeout_seconds=repo_env.reuse_rerank_timeout_seconds,
    )
    if repo_env.flowark_data_root:
        overrides["FLOWARK_DATA_ROOT"] = repo_env.flowark_data_root
    return overrides


def _load_config_defaults(
    *,
    workspace_root: Path | str,
    relative_path: Path,
    default_values: dict[str, Any],
) -> dict[str, Any]:
    path = Path(workspace_root).expanduser().resolve() / relative_path
    merged = dict(default_values)
    if not path.exists():
        return merged
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件必须是 YAML 对象: {path}")
    for key, value in raw.items():
        merged[key] = value
    return merged


def _load_dotenv_values(workspace_root: Path | str) -> dict[str, str]:
    env_path = Path(workspace_root).expanduser().resolve() / ".env"
    if not env_path.is_file():
        return {}
    return _normalize_env_mapping(dotenv_values(env_path))


def _normalize_env_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized[str(key)] = text
    return normalized


def _resolve_env_value(
    file_values: Mapping[str, str],
    *,
    primary_keys: tuple[str, ...],
) -> str | None:
    for key in primary_keys:
        value = str(file_values.get(key) or "").strip()
        if value:
            return value
    return None
