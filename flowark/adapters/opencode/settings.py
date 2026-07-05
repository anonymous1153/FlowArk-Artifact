"""OpenCode runtime isolation and provider configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
from typing import Any, Mapping
from uuid import uuid4

from flowark.anthropic_env import (
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)
from flowark.config import resolve_repo_env_config
from flowark.runtime.config import AnalysisRequest, RunConfig, normalize_runtime_injection_mode
from flowark.runtime.hook_context import HookRuntimeContext, hook_runtime_context_to_payload
from flowark.semantics.models import AugmentRuntimeConfig

OPENCODE_NPM_PACKAGE = "opencode-ai"
OPENCODE_NPM_VERSION = "1.14.24"
OPENCODE_SDK_NPM_PACKAGE = "@opencode-ai/sdk"
OPENCODE_SDK_NPM_VERSION = "1.14.24"
OPENAI_COMPATIBLE_PROVIDER_NPM = "@ai-sdk/openai-compatible"
OPENCODE_NPM_CACHE_DIRNAME = f"flowark-opencode-npm-cache-{OPENCODE_NPM_VERSION}"
OPENCODE_SHARED_RUNTIME_DIRNAME = f"flowark-opencode-runtime-cache-{OPENCODE_NPM_VERSION}"
OPENCODE_SHARED_CONFIG_FORBIDDEN_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "config",
    "config.json",
    "config.jsonc",
    "opencode.json",
    "opencode.jsonc",
}
OPENCODE_SHARED_CONFIG_FORBIDDEN_DIRS = {
    "agent",
    "agents",
    "command",
    "commands",
    "mode",
    "modes",
    "plugin",
    "plugins",
}
DEFAULT_OPENCODE_PROVIDER = "anthropic"
DEFAULT_OPENCODE_MODEL = "claude-sonnet-4-5"
DEFAULT_AFTER_TOOL_DELIVERY = "no_reply_context"
DEFAULT_BASH_POLICY = "read_only_guarded"
DEFAULT_POST_PHASE_MODE = "plain_json_same_surface"
DEFAULT_OPENCODE_LOG_LEVEL = "WARN"
OPENCODE_LOG_LEVEL_ENV = "FLOWARK_OPENCODE_LOG_LEVEL"
OPENCODE_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}
GLM_47_COST_PER_1M_TOKENS = {
    "input": 0.60,
    "output": 2.20,
    "cache_read": 0.11,
    "cache_write": 0.0,
}
MODEL_COSTS_PER_1M_TOKENS = {
    "glm-4.7": GLM_47_COST_PER_1M_TOKENS,
    "deepseek-v4-flash": {
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.0028,
        "cache_write": 0.0,
    },
    "deepseek-v4-pro": {
        "input": 0.435,
        "output": 0.87,
        "cache_read": 0.003625,
        "cache_write": 0.0,
    },
    "minimax-m3": {
        "input": 0.30,
        "output": 1.20,
        "cache_read": 0.06,
        "cache_write": 0.0,
    },
}
MODEL_METADATA = {
    "ark-code-latest": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "doubao-seed-2.0-code": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "doubao-seed-2.0-pro": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "doubao-seed-2.0-lite": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "doubao-seed-code": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "minimax-m2.7": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "minimax-m3": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 1_048_576, "output": 131_072},
    },
    "glm-5.2": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "glm-latest": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
    "deepseek-v4-flash": {
        "family": "deepseek",
        "reasoning": True,
        "limit": {"context": 1_048_576, "output": 393_216},
    },
    "deepseek-v4-pro": {
        "family": "deepseek",
        "reasoning": True,
        "limit": {"context": 1_048_576, "output": 393_216},
    },
    "mimo-v2.5-pro": {
        "family": "mimo",
        "reasoning": False,
        "limit": {"context": 1_048_576, "output": 131_072},
    },
    "kimi-k2.6": {
        "family": "volcengine",
        "reasoning": True,
        "limit": {"context": 262_144, "output": 32_768},
    },
}
RUNTIME_PLUGIN_ENABLED = True
RUNTIME_PLUGIN_PATH = Path(__file__).with_name("plugin") / "flowark-runtime-plugin.js"
RUNTIME_PLUGIN_SENTINEL = "flowark-opencode-runtime-plugin-v1"
MEM0_PLUGIN_PATH_ENV = "FLOWARK_OPENCODE_MEM0_PLUGIN_PATH"
MEM0_ENV_PASSTHROUGH_KEYS = (
    "MEM0_API_KEY",
    "MEM0_SELF_HOST_URL",
    "MEM0_HOST",
    "MEM0_SELF_HOST_TIMEOUT_MS",
    "MEM0_HTTP_TIMEOUT_MS",
    "MEM0_SELF_HOST_RETRY_ATTEMPTS",
    "MEM0_HTTP_RETRY_ATTEMPTS",
    "MEM0_SELF_HOST_RETRY_BACKOFF_MS",
    "MEM0_HTTP_RETRY_BACKOFF_MS",
)
MEM0_DEFAULT_SELF_HOST_TIMEOUT_MS = "120000"
MEM0_DEFAULT_SELF_HOST_RETRY_ATTEMPTS = "3"
MEM0_DEFAULT_SELF_HOST_RETRY_BACKOFF_MS = "1000"
MEM0_TOOL_NAMES = (
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "delete_all_memories",
    "delete_entities",
    "list_entities",
    "get_event_status",
)
MEM0_ALLOWED_TOOL_NAMES = (
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
)
SESSION_AFFINITY_FIXED_OUT_DIR_MARKER = "affinity-fixed"
SESSION_AFFINITY_FIXED_VALUE = "flowark-opencode-affinity-fixed"
AFTER_TOOL_DELIVERY_CHOICES = {
    "no_reply_context",
    "tool_output_append",
}
BASH_POLICY_CHOICES = {
    "read_only_guarded",
}
POST_PHASE_MODE_CHOICES = {
    DEFAULT_POST_PHASE_MODE,
}


def _shared_npm_cache_dir() -> Path:
    return (Path(tempfile.gettempdir()) / OPENCODE_NPM_CACHE_DIRNAME).expanduser().resolve()


def _shared_runtime_cache_dir() -> Path:
    return (Path(tempfile.gettempdir()) / OPENCODE_SHARED_RUNTIME_DIRNAME).expanduser().resolve()


def _shared_xdg_config_home() -> Path:
    return _shared_runtime_cache_dir() / "xdg-config"


def _shared_opencode_config_dir() -> Path:
    return _shared_xdg_config_home() / "opencode"


@dataclass(slots=True)
class OpenCodeRuntime:
    cwd: Path
    isolation_dir: Path
    run_dir: Path | None
    command: list[str]
    env: dict[str, str]
    config_content: dict[str, Any]
    auth_content: dict[str, Any]
    provider_id: str
    model_id: str
    model: str
    after_tool_delivery: str
    bash_policy: str
    post_phase_mode: str
    log_level: str
    structured_output_enabled: bool
    package: str
    package_version: str
    sdk_package: str
    sdk_package_version: str
    command_source: str
    tool_policy: dict[str, bool]
    post_phase_tool_policy: dict[str, bool]
    runtime_plugin_active: bool
    hook_context_file: Path
    hook_trace_path: Path
    transcript_path: Path | None
    hook_context_payload: dict[str, Any]
    shared_runtime_cache_dir: Path
    shared_npm_cache_dir: Path
    shared_config_dir: Path

    def ensure_directories(self) -> None:
        config_dir = self.env.get("OPENCODE_CONFIG_DIR")
        for path in (
            self.shared_runtime_cache_dir,
            self.shared_npm_cache_dir,
            Path(config_dir) if config_dir else None,
        ):
            if path is None:
                continue
            path.mkdir(parents=True, exist_ok=True)
        self._ensure_shared_config_dependency_only()
        for path in (
            self.isolation_dir,
            self.isolation_dir / "home",
            self.isolation_dir / "xdg-data",
            self.isolation_dir / "xdg-cache",
            self.isolation_dir / "xdg-state",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._ensure_shared_npm_cache_link()
        self.write_hook_context()

    def _ensure_shared_config_dependency_only(self) -> None:
        blockers: list[Path] = []
        for filename in OPENCODE_SHARED_CONFIG_FORBIDDEN_FILES:
            path = self.shared_config_dir / filename
            if path.exists():
                blockers.append(path)
        for dirname in OPENCODE_SHARED_CONFIG_FORBIDDEN_DIRS:
            path = self.shared_config_dir / dirname
            if path.exists():
                blockers.append(path)
        if not blockers:
            return
        rel = ", ".join(sorted(str(path.relative_to(self.shared_config_dir)) for path in blockers))
        raise RuntimeError(
            "OpenCode shared runtime config cache contains files that OpenCode would load as behavior config. "
            f"Refusing to start to avoid cross-run config pollution: {rel}. "
            f"Remove the contaminated shared cache directory and retry: {self.shared_config_dir}"
        )

    def _ensure_shared_npm_cache_link(self) -> None:
        npm_home = self.isolation_dir / "home" / ".npm"
        target = self.shared_npm_cache_dir
        target.mkdir(parents=True, exist_ok=True)
        if npm_home.is_symlink():
            try:
                if npm_home.resolve() == target:
                    return
            except FileNotFoundError:
                pass
            npm_home.unlink()
        elif npm_home.exists():
            if npm_home.is_dir():
                shutil.rmtree(npm_home)
            else:
                npm_home.unlink()
        npm_home.symlink_to(target, target_is_directory=True)

    def write_hook_context(
        self,
        *,
        current_phase: str | None = None,
        active: bool | None = None,
        inactive_reason: str | None = None,
    ) -> None:
        if current_phase is not None:
            phase = str(current_phase or "").strip() or "analysis"
            self.hook_context_payload["current_phase"] = phase
            self.hook_context_payload["phase"] = phase
        if active is not None:
            runtime_injection_mode = normalize_runtime_injection_mode(
                str(self.hook_context_payload.get("runtime_injection_mode") or "context_aware")
            )
            self.hook_context_payload["active"] = bool(active)
            self.hook_context_payload["allow_request_submit_augment"] = bool(active)
            self.hook_context_payload["allow_after_tool_augment"] = (
                bool(active) and runtime_injection_mode == "context_aware"
            )
        if inactive_reason is not None:
            self.hook_context_payload["inactive_reason"] = inactive_reason
        elif active is True:
            self.hook_context_payload["inactive_reason"] = None
        self.hook_context_file.parent.mkdir(parents=True, exist_ok=True)
        self.hook_context_file.write_text(
            json.dumps(self.hook_context_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @property
    def server_log_path(self) -> Path:
        return self.isolation_dir / "opencode_server.log"

    @property
    def plugin_event_log_path(self) -> Path:
        return self.isolation_dir / "opencode_plugin_events.jsonl"

    @property
    def hook_runtime_context_path(self) -> Path:
        return self.hook_context_file

    @property
    def preserved_server_log_path(self) -> Path | None:
        return (self.run_dir / "opencode_server.log") if self.run_dir is not None else None

    @property
    def preserved_plugin_event_log_path(self) -> Path | None:
        return (self.run_dir / "opencode_plugin_events.jsonl") if self.run_dir is not None else None

    @property
    def preserved_hook_runtime_context_path(self) -> Path | None:
        return (self.run_dir / "opencode_hook_runtime_context.json") if self.run_dir is not None else None

    @property
    def app_log_dir(self) -> Path:
        return self.isolation_dir / "xdg-data" / "opencode" / "log"

    @property
    def preserved_app_log_dir(self) -> Path | None:
        return (self.run_dir / "opencode_app_logs") if self.run_dir is not None else None

    @property
    def cleanup_summary_path(self) -> Path | None:
        return (self.run_dir / "opencode_runtime_cleanup.json") if self.run_dir is not None else None

    def preserve_runtime_artifacts(self, *, include_app_logs: bool = True) -> dict[str, Any]:
        """Copy small audit artifacts out of the disposable runtime directory."""

        preserved: dict[str, Any] = {}
        if self.run_dir is None:
            return preserved
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for source, target in (
            (self.server_log_path, self.preserved_server_log_path),
            (self.plugin_event_log_path, self.preserved_plugin_event_log_path),
            (self.hook_runtime_context_path, self.preserved_hook_runtime_context_path),
        ):
            if target is None or not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            preserved[source.name] = str(target)
        app_log_dir = self.app_log_dir
        target_log_dir = self.preserved_app_log_dir
        if include_app_logs and target_log_dir is not None and app_log_dir.is_dir():
            copied: list[str] = []
            target_log_dir.mkdir(parents=True, exist_ok=True)
            for source in sorted(app_log_dir.glob("*.log")):
                if not source.is_file():
                    continue
                target = target_log_dir / source.name
                shutil.copy2(source, target)
                copied.append(str(target))
            if copied:
                preserved["opencode_app_logs"] = copied
        return preserved

    def cleanup_runtime_directory(self) -> dict[str, Any]:
        """Remove the per-run OpenCode runtime cache after preserving audit files."""

        if self.run_dir is None:
            return {"cleanup_enabled": False, "reason": "missing_run_dir"}
        if os.environ.get("FLOWARK_OPENCODE_KEEP_RUNTIME") == "1":
            preserved = self.preserve_runtime_artifacts(include_app_logs=False)
            return {
                "cleanup_enabled": False,
                "reason": "FLOWARK_OPENCODE_KEEP_RUNTIME=1",
                "isolation_dir": str(self.isolation_dir),
                "preserved_artifacts": preserved,
            }
        summary_path = self.cleanup_summary_path
        try:
            preserved = self.preserve_runtime_artifacts()
            existed = self.isolation_dir.exists()
            if existed:
                shutil.rmtree(self.isolation_dir)
            payload: dict[str, Any] = {
                "cleanup_enabled": True,
                "removed": existed,
                "isolation_dir": str(self.isolation_dir),
                "preserved_artifacts": preserved,
            }
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            payload = {
                "cleanup_enabled": True,
                "removed": False,
                "isolation_dir": str(self.isolation_dir),
                "error": f"{type(exc).__name__}: {exc}",
            }
        if summary_path is not None:
            summary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return payload

    def isolation_summary(self) -> dict[str, Any]:
        return {
            "home": self.env.get("HOME"),
            "test_home": self.env.get("OPENCODE_TEST_HOME"),
            "xdg_data_home": self.env.get("XDG_DATA_HOME"),
            "xdg_config_home": self.env.get("XDG_CONFIG_HOME"),
            "xdg_cache_home": self.env.get("XDG_CACHE_HOME"),
            "xdg_state_home": self.env.get("XDG_STATE_HOME"),
            "npm_config_cache": self.env.get("NPM_CONFIG_CACHE"),
            "config_dir": self.env.get("OPENCODE_CONFIG_DIR"),
            "shared_runtime_cache_dir": str(self.shared_runtime_cache_dir),
            "shared_npm_cache_dir": str(self.shared_npm_cache_dir),
            "shared_config_dir": str(self.shared_config_dir),
            "shared_config_enabled": True,
            "log_level": self.log_level,
            "log_level_env_var": OPENCODE_LOG_LEVEL_ENV,
            "server_log_path": str(self.server_log_path),
            "preserved_server_log_path": str(self.preserved_server_log_path)
            if self.preserved_server_log_path is not None
            else None,
            "plugin_event_log_path": str(self.plugin_event_log_path),
            "preserved_plugin_event_log_path": str(self.preserved_plugin_event_log_path)
            if self.preserved_plugin_event_log_path is not None
            else None,
            "hook_context_file": str(self.hook_context_file),
            "preserved_hook_context_file": str(self.preserved_hook_runtime_context_path)
            if self.preserved_hook_runtime_context_path is not None
            else None,
            "hook_trace_path": str(self.hook_trace_path),
            "runtime_cleanup_summary_path": str(self.cleanup_summary_path)
            if self.cleanup_summary_path is not None
            else None,
        }

    def disabled_flags(self) -> dict[str, bool]:
        keys = [
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS",
            "OPENCODE_DISABLE_AUTOUPDATE",
            "OPENCODE_DISABLE_LSP_DOWNLOAD",
            "OPENCODE_DISABLE_PRUNE",
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
            "OPENCODE_DISABLE_MODELS_FETCH",
        ]
        return {key: self.env.get(key) == "1" for key in keys}

    def trace_payload(self) -> dict[str, Any]:
        return {
            "isolation_dir": str(self.isolation_dir),
            "isolation": self.isolation_summary(),
            "command": list(self.command),
            "command_source": self.command_source,
            "package": self.package,
            "package_version": self.package_version,
            "sdk_package": self.sdk_package,
            "sdk_package_version": self.sdk_package_version,
            "provider": self.provider_id,
            "model": self.model,
            "model_id": self.model_id,
            "after_tool_delivery": self.after_tool_delivery,
            "bash_policy": self.bash_policy,
            "post_phase_mode": self.post_phase_mode,
            "log_level": self.log_level,
            "json_parse_mode": "flowark_plain_json",
            "native_structured_output_enabled": False,
            "legacy_structured_output_configured": self.structured_output_enabled,
            "structured_output_enabled": False,
            "tool_policy": dict(self.tool_policy),
            "post_phase_tool_policy": dict(self.post_phase_tool_policy),
            "project_config_disabled": self.env.get("OPENCODE_DISABLE_PROJECT_CONFIG") == "1",
            "disabled_flags": self.disabled_flags(),
            "auth_configured": bool(self.auth_content.get(self.provider_id)),
            "runtime_plugin_enabled": RUNTIME_PLUGIN_ENABLED,
            "runtime_plugin_path": str(RUNTIME_PLUGIN_PATH),
            "runtime_plugin_active": self.runtime_plugin_active,
            "hook_context_file": str(self.hook_context_file),
            "hook_trace_path": str(self.hook_trace_path),
            "hook_context_active": bool(self.hook_context_payload.get("active")),
            "hook_context_inactive_reason": self.hook_context_payload.get("inactive_reason"),
            "hook_context_phase": self.hook_context_payload.get("current_phase")
            or self.hook_context_payload.get("phase"),
            "post_tool_delivery_configured": self.after_tool_delivery,
            "post_tool_delivery_active": self.runtime_plugin_active,
        }


def normalize_after_tool_delivery(value: str | None) -> str:
    text = str(value or DEFAULT_AFTER_TOOL_DELIVERY).strip().lower().replace("-", "_")
    if text not in AFTER_TOOL_DELIVERY_CHOICES:
        choices = ", ".join(sorted(AFTER_TOOL_DELIVERY_CHOICES))
        raise ValueError(f"unsupported opencode_after_tool_delivery {value!r}; expected one of: {choices}")
    return text


def normalize_bash_policy(value: str | None) -> str:
    text = str(value or DEFAULT_BASH_POLICY).strip().lower().replace("-", "_")
    if text not in BASH_POLICY_CHOICES:
        choices = ", ".join(sorted(BASH_POLICY_CHOICES))
        raise ValueError(f"unsupported opencode_bash_policy {value!r}; expected one of: {choices}")
    return text


def normalize_post_phase_mode(value: str | None) -> str:
    text = str(value or DEFAULT_POST_PHASE_MODE).strip().lower().replace("-", "_")
    if text not in POST_PHASE_MODE_CHOICES:
        choices = ", ".join(sorted(POST_PHASE_MODE_CHOICES))
        raise ValueError(f"unsupported opencode_post_phase_mode {value!r}; expected one of: {choices}")
    return text


def _normalized_opencode_log_level(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if text == "WARNING":
        text = "WARN"
    if text in OPENCODE_LOG_LEVELS:
        return text
    return None


def normalize_opencode_log_level(value: str | None) -> str:
    text = _normalized_opencode_log_level(value)
    if text is not None:
        return text
    return DEFAULT_OPENCODE_LOG_LEVEL


def _opencode_log_level_from_command(command: list[str]) -> str | None:
    for index, arg in enumerate(command):
        if arg == "--log-level" and index + 1 < len(command):
            return _normalized_opencode_log_level(command[index + 1])
        if arg.startswith("--log-level="):
            return _normalized_opencode_log_level(arg.split("=", 1)[1])
    return None


def _opencode_log_level(command: list[str], environ: Mapping[str, str] | None = None) -> str:
    command_level = _opencode_log_level_from_command(command)
    if command_level is not None:
        return command_level
    source = environ if environ is not None else os.environ
    return normalize_opencode_log_level(source.get(OPENCODE_LOG_LEVEL_ENV))


def _first_text(*values: str | None) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _runtime_backend(
    workspace_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    env = dict(os.environ if environ is None else environ)
    selected_profile = str(env.get(STUDIO_BACKEND_PROFILE_ENV) or "").strip()
    repo_env = resolve_repo_env_config(workspace_root, environ=env)
    if selected_profile:
        return (
            _first_text(
                env.get(STUDIO_RUNTIME_BASE_URL_ENV),
                repo_env.anthropic_base_url,
            ),
            _first_text(
                env.get(STUDIO_RUNTIME_AUTH_TOKEN_ENV),
                repo_env.anthropic_auth_token,
            ),
            _first_text(
                env.get(STUDIO_RUNTIME_MODEL_ENV),
                repo_env.anthropic_model,
            ),
        )
    return (
        _first_text(
            env.get("ANTHROPIC_BASE_URL"),
            env.get("OPENAI_BASE_URL"),
            repo_env.anthropic_base_url,
        ),
        _first_text(
            env.get("ANTHROPIC_AUTH_TOKEN"),
            env.get("OPENAI_API_KEY"),
            repo_env.anthropic_auth_token,
        ),
        _first_text(
            env.get("ANTHROPIC_MODEL"),
            env.get("OPENAI_MODEL"),
            repo_env.anthropic_model,
        ),
    )


def _anthropic_ai_sdk_base_url(base_url: str) -> str:
    text = str(base_url or "").strip().rstrip("/")
    if text.endswith("/v1/messages"):
        return text[: -len("/messages")]
    if text.endswith("/messages"):
        return text[: -len("/messages")]
    if text.endswith("/v1"):
        return text
    return f"{text}/v1"


def _provider_base_url(*, provider_id: str, base_url: str) -> str:
    if provider_id == DEFAULT_OPENCODE_PROVIDER:
        return _anthropic_ai_sdk_base_url(base_url)
    return str(base_url).strip()


def _command(config: RunConfig, *, environ: Mapping[str, str] | None = None) -> tuple[list[str], str]:
    explicit = str(config.opencode_binary or "").strip()
    if explicit:
        return shlex.split(explicit), "config"
    env_binary = str((environ if environ is not None else os.environ).get("OPENCODE_BINARY") or "").strip()
    if env_binary:
        return shlex.split(env_binary), "env"
    search_env = environ if environ is not None else os.environ
    found = shutil.which("opencode", path=search_env.get("PATH"))
    if found:
        return [found], "path"
    return ["npx", "-y", f"{OPENCODE_NPM_PACKAGE}@{OPENCODE_NPM_VERSION}"], "npx"


def _tool_policy(*, mem0_enabled: bool = False) -> dict[str, bool]:
    policy = {
        "read": True,
        "grep": True,
        "glob": True,
        "bash": True,
        "task": False,
        "edit": False,
        "write": False,
        "patch": False,
        "apply_patch": False,
        "todowrite": True,
        "todo": True,
        "webfetch": False,
        "fetch": False,
        "websearch": False,
        "search": False,
        "codesearch": False,
        "code": False,
        "question": False,
        "lsp": False,
        "skill": False,
        "external_directory": False,
        "external-directory": False,
    }
    if mem0_enabled:
        policy.update({name: False for name in MEM0_TOOL_NAMES})
        policy.update({name: True for name in MEM0_ALLOWED_TOOL_NAMES})
    return policy


def _post_phase_tool_policy(*, mem0_enabled: bool = False) -> dict[str, bool]:
    policy = {tool: False for tool in _tool_policy(mem0_enabled=mem0_enabled)}
    policy.update(
        {
            "list": False,
            "external_directory": False,
            "external-directory": False,
            "question": False,
            "codesearch": False,
            "code": False,
            "webfetch": False,
            "fetch": False,
            "websearch": False,
            "search": False,
            "todowrite": False,
            "todo": False,
        }
    )
    return policy


def _permission_config(*, mem0_enabled: bool = False) -> dict[str, str]:
    permission = {
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "bash": "allow",
        "task": "deny",
        "edit": "deny",
        "write": "deny",
        "patch": "deny",
        "apply_patch": "deny",
        "list": "deny",
        "external_directory": "deny",
        "external-directory": "deny",
        "todowrite": "allow",
        "todo": "allow",
        "question": "deny",
        "webfetch": "deny",
        "fetch": "deny",
        "websearch": "deny",
        "search": "deny",
        "codesearch": "deny",
        "code": "deny",
        "lsp": "deny",
        "skill": "deny",
    }
    if mem0_enabled:
        permission.update({name: "deny" for name in MEM0_TOOL_NAMES})
        permission.update({name: "allow" for name in MEM0_ALLOWED_TOOL_NAMES})
    return permission


def _mem0_plugin_path(environ: Mapping[str, str] | None) -> str:
    source_env = environ if environ is not None else os.environ
    return str(source_env.get(MEM0_PLUGIN_PATH_ENV) or "").strip()


def _mem0_plugin_enabled(environ: Mapping[str, str] | None) -> bool:
    return bool(_mem0_plugin_path(environ))


def _safe_mem0_slug(value: str | None, *, default: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("._-")
    return text or default


def _default_mem0_app_id(*, request: AnalysisRequest | None, workspace_root: Path) -> str:
    if request is not None and request.app_name:
        return _safe_mem0_slug(str(request.app_name), default="unknown-app")
    return _safe_mem0_slug(workspace_root.name, default="unknown-app")


def _default_mem0_eval_slug(run_dir: Path | None) -> str:
    if run_dir is None:
        return "adhoc"
    resolved = run_dir.expanduser().resolve()
    if (
        resolved.parent.name == "runs"
        and len(resolved.parents) > 4
        and resolved.parents[1].name.startswith("repeat-")
    ):
        return _safe_mem0_slug(resolved.parents[4].name, default="eval")
    return _safe_mem0_slug(resolved.parent.name, default="eval")


def _default_mem0_case_slug(*, run_dir: Path | None, request: AnalysisRequest | None) -> str:
    if run_dir is not None:
        resolved = run_dir.expanduser().resolve()
        if (
            resolved.parent.name == "runs"
            and len(resolved.parents) > 2
            and resolved.parents[1].name.startswith("repeat-")
        ):
            return _safe_mem0_slug(resolved.parents[2].name, default="case")
    if request is not None and request.source:
        return _safe_mem0_slug(str(request.source), default="case")
    return "adhoc"


def _default_mem0_user_id(*, run_dir: Path | None, app_id: str) -> str:
    return f"flowark-mem0-{_default_mem0_eval_slug(run_dir)}-{_safe_mem0_slug(app_id, default='unknown-app')}"


def _mem0_run_dir_hash(run_dir: Path | None) -> str | None:
    if run_dir is None:
        return None
    try:
        text = str(run_dir.expanduser().resolve())
    except Exception:
        text = str(run_dir)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mem0_metering_run_id(run_dir: Path | None) -> str:
    if run_dir is not None:
        snapshot_path = run_dir.expanduser().resolve() / "mem0_runtime_snapshot.json"
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            existing = str(payload.get("metering_run_id") or "").strip()
            if existing:
                return existing
    return uuid4().hex


def _file_uri_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text.startswith("file://"):
        return None
    return Path(text.removeprefix("file://")).expanduser()


def _file_sha256_16(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _mem0_plugin_package_version(plugin_file: Path | None) -> str | None:
    if plugin_file is None:
        return None
    for directory in (plugin_file.parent, plugin_file.parent.parent):
        package_json = directory / "package.json"
        if not package_json.is_file():
            continue
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        version = str(payload.get("version") or "").strip()
        if version:
            return version
    return None


def _write_mem0_runtime_snapshot(
    *,
    run_dir: Path | None,
    mem0_plugin_path: str,
    env: Mapping[str, str],
) -> None:
    if run_dir is None:
        return
    api_key = env.get("MEM0_API_KEY") or ""
    plugin_file = _file_uri_path(mem0_plugin_path)
    payload = {
        "schema_version": 1,
        "enabled": True,
        "self_host_url": env.get("MEM0_SELF_HOST_URL") or env.get("MEM0_HOST"),
        "self_host_timeout_ms": env.get("MEM0_SELF_HOST_TIMEOUT_MS"),
        "http_timeout_ms": env.get("MEM0_HTTP_TIMEOUT_MS"),
        "self_host_retry_attempts": env.get("MEM0_SELF_HOST_RETRY_ATTEMPTS"),
        "http_retry_attempts": env.get("MEM0_HTTP_RETRY_ATTEMPTS"),
        "self_host_retry_backoff_ms": env.get("MEM0_SELF_HOST_RETRY_BACKOFF_MS"),
        "http_retry_backoff_ms": env.get("MEM0_HTTP_RETRY_BACKOFF_MS"),
        "api_key_present": bool(api_key),
        "api_key_sha256_16": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        if api_key
        else None,
        "user_id": env.get("MEM0_USER_ID"),
        "mem0_app_id": env.get("MEM0_APP_ID"),
        "global_search": env.get("MEM0_GLOBAL_SEARCH"),
        "dream": env.get("MEM0_DREAM"),
        "telemetry": env.get("MEM0_TELEMETRY"),
        "lock_scope": env.get("MEM0_LOCK_SCOPE"),
        "metering_run_id": env.get("FLOWARK_MEM0_METERING_RUN_ID"),
        "eval_id": env.get("FLOWARK_MEM0_EVAL_ID"),
        "case_id": env.get("FLOWARK_MEM0_CASE_ID"),
        "flowark_app_id": env.get("FLOWARK_MEM0_APP_ID"),
        "task_mode": env.get("FLOWARK_MEM0_TASK_MODE"),
        "run_dir_hash": env.get("FLOWARK_MEM0_RUN_DIR_HASH"),
        "plugin_path": mem0_plugin_path,
        "plugin_dist_sha256_16": _file_sha256_16(plugin_file),
        "plugin_package_version": _mem0_plugin_package_version(plugin_file),
    }
    path = run_dir.expanduser().resolve() / "mem0_runtime_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _model_cost_config(model_id: str) -> dict[str, float]:
    key = str(model_id or "").strip().lower()
    return dict(
        MODEL_COSTS_PER_1M_TOKENS.get(
            key,
            {
                "input": 0.0,
                "output": 0.0,
                "cache_read": 0.0,
                "cache_write": 0.0,
            },
        )
    )


def _model_config(model_id: str) -> dict[str, Any]:
    metadata = dict(MODEL_METADATA.get(str(model_id or "").strip().lower()) or {})
    limit = dict(metadata.get("limit") or {"context": 128000, "output": 8192})
    return {
        "id": model_id,
        "name": model_id,
        "family": str(metadata.get("family") or "anthropic"),
        "tool_call": True,
        "temperature": True,
        "reasoning": bool(metadata.get("reasoning", False)),
        "cost": _model_cost_config(model_id),
        "limit": limit,
        "modalities": {
            "input": ["text"],
            "output": ["text"],
        },
    }


def _runtime_config_for_context(
    *,
    config: RunConfig,
    run_dir: Path | None,
    augment_runtime_config: AugmentRuntimeConfig | None,
) -> AugmentRuntimeConfig:
    injection_log_path = (run_dir / "knowledge_injection.jsonl") if run_dir is not None else None
    if augment_runtime_config is not None:
        return replace(
            augment_runtime_config,
            skills_dir=augment_runtime_config.skills_dir or config.skills_dir,
            knowledge_injection_log_path=(
                augment_runtime_config.knowledge_injection_log_path or injection_log_path
            ),
        )
    return AugmentRuntimeConfig(
        knowledge_mode=str(config.knowledge_mode or "warm"),
        runtime_injection_mode=normalize_runtime_injection_mode(
            str(getattr(config, "runtime_injection_mode", "context_aware") or "context_aware")
        ),
        skills_dir=config.skills_dir,
        knowledge_packaging_mode=str(
            getattr(config, "knowledge_packaging_mode", "dsl_rule") or "dsl_rule"
        ),
        knowledge_top_k=max(1, int(config.knowledge_top_k or 3)),
        knowledge_min_score=float(config.knowledge_min_score or 1.0),
        knowledge_injection_char_budget=max(256, int(config.knowledge_injection_char_budget or 4000)),
        knowledge_delta_char_budget=(
            max(256, int(config.knowledge_delta_char_budget))
            if config.knowledge_delta_char_budget is not None
            else None
        ),
        knowledge_allow_repeat_within_session=bool(config.knowledge_allow_repeat_injection_within_session),
        knowledge_repeat_summary_hook_gap=max(1, int(config.knowledge_repeat_summary_hook_gap or 3)),
        knowledge_repeat_full_hook_gap=max(2, int(config.knowledge_repeat_full_hook_gap or 10)),
        knowledge_repeat_summary_react_gap=max(
            0,
            int(getattr(config, "knowledge_repeat_summary_react_gap", 0) or 0),
        ),
        knowledge_repeat_full_react_gap=max(
            0,
            int(getattr(config, "knowledge_repeat_full_react_gap", 1) or 1),
        ),
        knowledge_realtime_min_interval_ms=max(0, int(config.knowledge_realtime_min_interval_ms or 1500)),
        knowledge_injection_log_path=injection_log_path,
        knowledge_recall_top_m=max(1, int(config.knowledge_recall_top_m or 8)),
        reuse_embed_base_url=getattr(config, "reuse_embed_base_url", None),
        reuse_embed_api_key=getattr(config, "reuse_embed_api_key", None),
        reuse_embed_model=getattr(config, "reuse_embed_model", None),
        reuse_embed_verify_ssl=bool(getattr(config, "reuse_embed_verify_ssl", False)),
    )


def _runtime_plugin_active(*, config: RunConfig, request: AnalysisRequest | None) -> bool:
    if not RUNTIME_PLUGIN_ENABLED or request is None:
        return False
    if str(config.agent_mode or "flowark").strip().lower() == "naive":
        return False
    return str(config.knowledge_mode or "warm").strip().lower() == "warm"


def _hook_context_payload(
    *,
    config: RunConfig,
    request: AnalysisRequest | None,
    run_dir: Path | None,
    runtime_config: AugmentRuntimeConfig,
    active: bool,
    delivery: str,
    bash_policy: str,
    provider_id: str,
    model_id: str,
    hook_trace_path: Path,
    transcript_path: Path | None,
) -> dict[str, Any]:
    hook_context = HookRuntimeContext(
        knowledge_mode=runtime_config.knowledge_mode,
        skills_dir=runtime_config.skills_dir,
        runtime_injection_mode=normalize_runtime_injection_mode(
            runtime_config.runtime_injection_mode
        ),
        analysis_cwd=Path(config.cwd).expanduser().resolve(),
        analysis_app_name=(request.app_name if request is not None else None),
        analysis_source=(request.source if request is not None else None),
        analysis_sink_types=list(request.sink_types if request is not None else []),
        knowledge_packaging_mode=runtime_config.knowledge_packaging_mode,
        knowledge_top_k=runtime_config.knowledge_top_k,
        knowledge_min_score=runtime_config.knowledge_min_score,
        knowledge_injection_char_budget=runtime_config.knowledge_injection_char_budget,
        knowledge_delta_char_budget=runtime_config.knowledge_delta_char_budget,
        knowledge_allow_repeat_within_session=runtime_config.knowledge_allow_repeat_within_session,
        knowledge_repeat_summary_hook_gap=runtime_config.knowledge_repeat_summary_hook_gap,
        knowledge_repeat_full_hook_gap=runtime_config.knowledge_repeat_full_hook_gap,
        knowledge_repeat_summary_react_gap=runtime_config.knowledge_repeat_summary_react_gap,
        knowledge_repeat_full_react_gap=runtime_config.knowledge_repeat_full_react_gap,
        knowledge_realtime_min_interval_ms=runtime_config.knowledge_realtime_min_interval_ms,
        knowledge_injection_log_path=runtime_config.knowledge_injection_log_path,
        knowledge_recall_top_m=runtime_config.knowledge_recall_top_m,
        command_hook_trace_path=hook_trace_path,
        augment_runtime_config=runtime_config,
        reuse_embed_base_url=runtime_config.reuse_embed_base_url,
        reuse_embed_api_key=runtime_config.reuse_embed_api_key,
        reuse_embed_model=runtime_config.reuse_embed_model,
        reuse_embed_verify_ssl=runtime_config.reuse_embed_verify_ssl,
    )
    return {
        "schema_version": 1,
        "agent_adapter": "opencode",
        "runtime_injection_mode": normalize_runtime_injection_mode(
            runtime_config.runtime_injection_mode
        ),
        "active": bool(active),
        "inactive_reason": None if active else _inactive_reason(config=config, request=request),
        "phase": "analysis",
        "current_phase": "analysis",
        "allow_request_submit_augment": bool(active),
        "allow_after_tool_augment": (
            bool(active)
            and normalize_runtime_injection_mode(runtime_config.runtime_injection_mode)
            == "context_aware"
        ),
        "delivery": delivery,
        "bash_policy": bash_policy,
        "provider_id": provider_id,
        "model_id": model_id,
        "agent": "build",
        "transcript_path": str(transcript_path) if transcript_path is not None else None,
        "hook_trace_path": str(hook_trace_path),
        "hook_runtime_context": hook_runtime_context_to_payload(hook_context),
    }


def _inactive_reason(*, config: RunConfig, request: AnalysisRequest | None) -> str:
    if not RUNTIME_PLUGIN_ENABLED:
        return "runtime_plugin_disabled"
    if request is None:
        return "missing_request_context"
    if str(config.agent_mode or "flowark").strip().lower() == "naive":
        return "naive_agent_mode"
    if str(config.knowledge_mode or "warm").strip().lower() != "warm":
        return "knowledge_mode_disabled"
    return "inactive"


def _session_affinity_experiment_env(
    *,
    run_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    source_env = environ if environ is not None else os.environ
    explicit_mode = str(source_env.get("FLOWARK_OPENCODE_SESSION_AFFINITY_MODE") or "").strip()
    if explicit_mode:
        env = {"FLOWARK_OPENCODE_SESSION_AFFINITY_MODE": explicit_mode}
        explicit_value = str(source_env.get("FLOWARK_OPENCODE_SESSION_AFFINITY_VALUE") or "").strip()
        if explicit_value:
            env["FLOWARK_OPENCODE_SESSION_AFFINITY_VALUE"] = explicit_value
        return env
    if run_dir is not None and SESSION_AFFINITY_FIXED_OUT_DIR_MARKER in str(run_dir).lower():
        return {
            "FLOWARK_OPENCODE_SESSION_AFFINITY_MODE": "fixed",
            "FLOWARK_OPENCODE_SESSION_AFFINITY_VALUE": SESSION_AFFINITY_FIXED_VALUE,
        }
    return {}


def build_opencode_runtime(
    *,
    config: RunConfig,
    workspace_root: Path,
    run_dir: Path | None,
    request: AnalysisRequest | None = None,
    augment_runtime_config: AugmentRuntimeConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> OpenCodeRuntime:
    source_env = environ if environ is not None else os.environ
    provider_id = str(config.opencode_provider or DEFAULT_OPENCODE_PROVIDER).strip() or DEFAULT_OPENCODE_PROVIDER
    base_url, auth_token, env_model = _runtime_backend(
        workspace_root,
        environ=environ,
    )
    requested_model_id = str(config.opencode_model or "").strip()
    model_id = requested_model_id or str(env_model or "").strip() or DEFAULT_OPENCODE_MODEL
    model = f"{provider_id}/{model_id}"
    if not base_url:
        raise ValueError(
            "OpenCode adapter requires ANTHROPIC_BASE_URL or a Studio runtime backend base URL"
        )
    if not auth_token:
        raise ValueError(
            "OpenCode adapter requires ANTHROPIC_AUTH_TOKEN or a Studio runtime backend auth token"
        )

    isolation_dir = (
        run_dir / "opencode_runtime"
        if run_dir is not None
        else Path(tempfile.mkdtemp(prefix="flowark-opencode-"))
    ).expanduser().resolve()
    command, command_source = _command(config, environ=environ)
    shared_runtime_cache_dir = _shared_runtime_cache_dir()
    shared_npm_cache_dir = _shared_npm_cache_dir()
    shared_config_dir = _shared_opencode_config_dir()
    hook_context_file = isolation_dir / "opencode_hook_runtime_context.json"
    hook_trace_path = (run_dir / "opencode_hook_trace.jsonl") if run_dir is not None else (isolation_dir / "opencode_hook_trace.jsonl")
    transcript_path = (run_dir / "raw_transcript.txt") if run_dir is not None else None
    delivery = normalize_after_tool_delivery(config.opencode_after_tool_delivery)
    bash_policy = normalize_bash_policy(getattr(config, "opencode_bash_policy", DEFAULT_BASH_POLICY))
    post_phase_mode = normalize_post_phase_mode(
        getattr(config, "opencode_post_phase_mode", DEFAULT_POST_PHASE_MODE)
    )
    log_level = _opencode_log_level(command, environ=environ)
    mem0_plugin_path = _mem0_plugin_path(environ)
    mem0_enabled = bool(mem0_plugin_path)
    runtime_config = _runtime_config_for_context(
        config=config,
        run_dir=run_dir,
        augment_runtime_config=augment_runtime_config,
    )
    runtime_plugin_active = _runtime_plugin_active(config=config, request=request)
    permission = _permission_config(mem0_enabled=mem0_enabled)
    provider_base_url = _provider_base_url(provider_id=provider_id, base_url=base_url)
    provider_config = {
        "name": provider_id,
        "api": provider_base_url,
        "options": {
            "apiKey": str(auth_token).strip(),
            "baseURL": provider_base_url,
            "timeout": 300000,
        },
        "models": {
            model_id: _model_config(model_id),
        },
    }
    if provider_id != DEFAULT_OPENCODE_PROVIDER:
        provider_config["npm"] = OPENAI_COMPATIBLE_PROVIDER_NPM
    config_content: dict[str, Any] = {
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "logLevel": log_level,
        "model": model,
        "small_model": model,
        "default_agent": "build",
        "enabled_providers": [provider_id],
        "provider": {
            provider_id: provider_config,
        },
        "permission": permission,
        "agent": {
            "build": {
                "model": model,
                "permission": permission,
            },
            "plan": {
                "disable": True,
            },
        },
        "mcp": {},
        "plugin": [],
    }
    if RUNTIME_PLUGIN_ENABLED:
        config_content["plugin"].append(
            [
                str(RUNTIME_PLUGIN_PATH),
                {
                    "contextFile": str(hook_context_file),
                    "delivery": delivery,
                    "bashPolicy": bash_policy,
                    "sentinel": RUNTIME_PLUGIN_SENTINEL,
                    "providerID": provider_id,
                    "modelID": model_id,
                    "agent": "build",
                },
            ]
        )
    if mem0_enabled:
        config_content["plugin"].append(mem0_plugin_path)
    auth_content = {
        provider_id: {
            "type": "api",
            "key": str(auth_token).strip(),
        }
    }
    env: dict[str, str] = {
        "HOME": str(isolation_dir / "home"),
        "OPENCODE_TEST_HOME": str(isolation_dir / "home"),
        "XDG_DATA_HOME": str(isolation_dir / "xdg-data"),
        "XDG_CONFIG_HOME": str(_shared_xdg_config_home()),
        "XDG_CACHE_HOME": str(isolation_dir / "xdg-cache"),
        "XDG_STATE_HOME": str(isolation_dir / "xdg-state"),
        "OPENCODE_CONFIG_DIR": str(shared_config_dir),
        "OPENCODE_CONFIG_CONTENT": json.dumps(config_content, ensure_ascii=False),
        "OPENCODE_AUTH_CONTENT": json.dumps(auth_content, ensure_ascii=False),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_PRUNE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_CLIENT": "flowark",
        "NPM_CONFIG_CACHE": str(shared_npm_cache_dir),
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_AUDIT": "false",
        "npm_config_cache": str(shared_npm_cache_dir),
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "NO_COLOR": "1",
    }
    for key in MEM0_ENV_PASSTHROUGH_KEYS:
        value = str(source_env.get(key) or "").strip()
        if value:
            env[key] = value
    if mem0_enabled:
        mem0_app_id = _default_mem0_app_id(
            request=request,
            workspace_root=workspace_root,
        )
        env["MEM0_APP_ID"] = mem0_app_id
        env["FLOWARK_MEM0_APP_ID"] = mem0_app_id
        env["FLOWARK_MEM0_EVAL_ID"] = _default_mem0_eval_slug(run_dir)
        env["FLOWARK_MEM0_CASE_ID"] = _default_mem0_case_slug(run_dir=run_dir, request=request)
        env["FLOWARK_MEM0_TASK_MODE"] = "naive-mem0"
        run_dir_hash = _mem0_run_dir_hash(run_dir)
        if run_dir_hash:
            env["FLOWARK_MEM0_RUN_DIR_HASH"] = run_dir_hash
        env["FLOWARK_MEM0_METERING_RUN_ID"] = _mem0_metering_run_id(run_dir)
        env["MEM0_USER_ID"] = _default_mem0_user_id(run_dir=run_dir, app_id=mem0_app_id)
        env["MEM0_GLOBAL_SEARCH"] = "false"
        env["MEM0_DREAM"] = "false"
        env["MEM0_TELEMETRY"] = "false"
        env["MEM0_LOCK_SCOPE"] = "true"
        if not env.get("MEM0_SELF_HOST_TIMEOUT_MS") and not env.get("MEM0_HTTP_TIMEOUT_MS"):
            env["MEM0_SELF_HOST_TIMEOUT_MS"] = MEM0_DEFAULT_SELF_HOST_TIMEOUT_MS
        if not env.get("MEM0_SELF_HOST_RETRY_ATTEMPTS") and not env.get("MEM0_HTTP_RETRY_ATTEMPTS"):
            env["MEM0_SELF_HOST_RETRY_ATTEMPTS"] = MEM0_DEFAULT_SELF_HOST_RETRY_ATTEMPTS
        if not env.get("MEM0_SELF_HOST_RETRY_BACKOFF_MS") and not env.get("MEM0_HTTP_RETRY_BACKOFF_MS"):
            env["MEM0_SELF_HOST_RETRY_BACKOFF_MS"] = MEM0_DEFAULT_SELF_HOST_RETRY_BACKOFF_MS
        _write_mem0_runtime_snapshot(
            run_dir=run_dir,
            mem0_plugin_path=mem0_plugin_path,
            env=env,
        )
    if RUNTIME_PLUGIN_ENABLED:
        env["FLOWARK_OPENCODE_PLUGIN_EVENTS"] = str(isolation_dir / "opencode_plugin_events.jsonl")
        env["FLOWARK_OPENCODE_HOOK_CONTEXT_FILE"] = str(hook_context_file)
        env["FLOWARK_OPENCODE_BRIDGE_PYTHON"] = sys.executable
        env["FLOWARK_OPENCODE_BRIDGE_MODULE"] = "flowark.adapters.opencode.bridge"
        env.update(_session_affinity_experiment_env(run_dir=run_dir, environ=environ))
        existing_pythonpath = str((environ if environ is not None else os.environ).get("PYTHONPATH") or "").strip()
        env["PYTHONPATH"] = (
            f"{workspace_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(workspace_root)
        )
    hook_context_payload = _hook_context_payload(
        config=config,
        request=request,
        run_dir=run_dir,
        runtime_config=runtime_config,
        active=runtime_plugin_active,
        delivery=delivery,
        bash_policy=bash_policy,
        provider_id=provider_id,
        model_id=model_id,
        hook_trace_path=hook_trace_path,
        transcript_path=transcript_path,
    )
    return OpenCodeRuntime(
        cwd=Path(config.cwd).expanduser().resolve(),
        isolation_dir=isolation_dir,
        run_dir=run_dir.expanduser().resolve() if run_dir is not None else None,
        command=command,
        env=env,
        config_content=config_content,
        auth_content=auth_content,
        provider_id=provider_id,
        model_id=model_id,
        model=model,
        after_tool_delivery=delivery,
        bash_policy=bash_policy,
        post_phase_mode=post_phase_mode,
        log_level=log_level,
        structured_output_enabled=bool(config.opencode_structured_output),
        package=OPENCODE_NPM_PACKAGE,
        package_version=OPENCODE_NPM_VERSION,
        sdk_package=OPENCODE_SDK_NPM_PACKAGE,
        sdk_package_version=OPENCODE_SDK_NPM_VERSION,
        command_source=command_source,
        tool_policy=_tool_policy(mem0_enabled=mem0_enabled),
        post_phase_tool_policy=_post_phase_tool_policy(mem0_enabled=mem0_enabled),
        runtime_plugin_active=runtime_plugin_active,
        hook_context_file=hook_context_file,
        hook_trace_path=hook_trace_path,
        transcript_path=transcript_path,
        hook_context_payload=hook_context_payload,
        shared_runtime_cache_dir=shared_runtime_cache_dir,
        shared_npm_cache_dir=shared_npm_cache_dir,
        shared_config_dir=shared_config_dir,
    )


__all__ = [
    "AFTER_TOOL_DELIVERY_CHOICES",
    "BASH_POLICY_CHOICES",
    "POST_PHASE_MODE_CHOICES",
    "DEFAULT_AFTER_TOOL_DELIVERY",
    "DEFAULT_BASH_POLICY",
    "DEFAULT_OPENCODE_LOG_LEVEL",
    "DEFAULT_OPENCODE_MODEL",
    "DEFAULT_OPENCODE_PROVIDER",
    "DEFAULT_POST_PHASE_MODE",
    "OPENCODE_NPM_PACKAGE",
    "OPENCODE_NPM_VERSION",
    "OPENCODE_LOG_LEVEL_ENV",
    "OPENCODE_SDK_NPM_PACKAGE",
    "OPENCODE_SDK_NPM_VERSION",
    "RUNTIME_PLUGIN_ENABLED",
    "RUNTIME_PLUGIN_PATH",
    "RUNTIME_PLUGIN_SENTINEL",
    "OpenCodeRuntime",
    "build_opencode_runtime",
    "normalize_bash_policy",
    "normalize_after_tool_delivery",
    "normalize_post_phase_mode",
]
