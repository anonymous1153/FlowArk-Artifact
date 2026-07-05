"""Anthropic-compatible runtime environment helpers."""

from __future__ import annotations

import hashlib
import os
from typing import Mapping

ANTHROPIC_AUTH_ENV_KEYS: tuple[str, ...] = ("ANTHROPIC_AUTH_TOKEN",)
ANTHROPIC_BASE_URL_ENV_KEYS: tuple[str, ...] = ("ANTHROPIC_BASE_URL",)
ANTHROPIC_MODEL_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_REASONING_MODEL",
)
LEGACY_AUTH_ENV_KEYS: tuple[str, ...] = ("FLOWARK_API_KEY",)
LEGACY_BASE_URL_ENV_KEYS: tuple[str, ...] = ("FLOWARK_BASE_URL",)
STUDIO_BACKEND_PROFILE_ENV: str = "FLOWARK_STUDIO_BACKEND_PROFILE"
STUDIO_RUNTIME_BASE_URL_ENV: str = "FLOWARK_STUDIO_RUNTIME_BASE_URL"
STUDIO_RUNTIME_AUTH_TOKEN_ENV: str = "FLOWARK_STUDIO_RUNTIME_AUTH_TOKEN"
STUDIO_RUNTIME_MODEL_ENV: str = "FLOWARK_STUDIO_RUNTIME_MODEL"
STUDIO_RUNTIME_OVERRIDE_ENV_KEYS: tuple[str, ...] = (
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)


def clear_anthropic_api_key_env() -> None:
    """清理会干扰 OpenCode 后端认证优先级的 API key 变量。"""
    for key in ("ANTHROPIC_API_KEY", "FLOWARK_API_KEY"):
        os.environ.pop(key, None)


def resolve_anthropic_auth_token(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or "").strip()
    if value:
        return value
    for key in ANTHROPIC_AUTH_ENV_KEYS:
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return None


def build_anthropic_auth_env(explicit_value: str | None = None) -> dict[str, str]:
    value = resolve_anthropic_auth_token(explicit_value)
    if not value:
        return {}
    return {key: value for key in ANTHROPIC_AUTH_ENV_KEYS}


def apply_anthropic_auth_env(explicit_value: str | None = None) -> dict[str, str]:
    clear_anthropic_api_key_env()
    env = build_anthropic_auth_env(explicit_value)
    if env:
        os.environ.update(env)
    return env


def resolve_anthropic_model_value(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or "").strip()
    if value:
        return value

    for key in ("ANTHROPIC_MODEL", *ANTHROPIC_MODEL_ENV_KEYS):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return None


def build_anthropic_model_env(explicit_value: str | None = None) -> dict[str, str]:
    value = resolve_anthropic_model_value(explicit_value)
    if not value:
        return {}
    return {key: value for key in ANTHROPIC_MODEL_ENV_KEYS}


def apply_anthropic_model_env(explicit_value: str | None = None) -> dict[str, str]:
    env = build_anthropic_model_env(explicit_value)
    if env:
        os.environ.update(env)
    return env


def describe_anthropic_env_conflicts(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = environ or os.environ
    runtime_token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    runtime_base_url = str(env.get("ANTHROPIC_BASE_URL") or "").strip()

    legacy_token = ""
    for key in LEGACY_AUTH_ENV_KEYS:
        value = str(env.get(key) or "").strip()
        if value:
            legacy_token = value
            break

    legacy_base_url = ""
    for key in LEGACY_BASE_URL_ENV_KEYS:
        value = str(env.get(key) or "").strip()
        if value:
            legacy_base_url = value
            break

    diagnostics: list[str] = []

    if legacy_token and not runtime_token:
        diagnostics.append(
            "检测到 legacy 鉴权变量 `FLOWARK_API_KEY`，"
            "但当前 runtime 只使用 `ANTHROPIC_AUTH_TOKEN`；当前进程未设置 `ANTHROPIC_AUTH_TOKEN`。"
        )
    elif legacy_token and runtime_token and legacy_token != runtime_token:
        diagnostics.append(
            "检测到混用鉴权 env：runtime 使用 "
            f"`ANTHROPIC_AUTH_TOKEN`(sha256={_value_fingerprint(runtime_token)})，同时存在 "
            f"legacy key (sha256={_value_fingerprint(legacy_token)})；runtime 不会回退到 legacy key。"
        )

    if legacy_base_url and runtime_base_url and legacy_base_url != runtime_base_url:
        diagnostics.append(
            "检测到冲突的 base URL：runtime 使用 "
            f"`ANTHROPIC_BASE_URL`={runtime_base_url}，同时存在 "
            f"legacy `FLOWARK_BASE_URL`={legacy_base_url}。"
        )

    return diagnostics


def _value_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
