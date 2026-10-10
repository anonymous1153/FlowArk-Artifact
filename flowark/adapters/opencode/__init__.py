"""OpenCode adapter integration."""

from flowark.adapters.opencode.adapter import OpenCodeAdapter, OpenCodeRunSession
from flowark.adapters.opencode.smoke import (
    DEFAULT_REAL_SMOKE_DELIVERIES,
    run_opencode_delivery_real_smoke,
    run_opencode_server_smoke,
)
from flowark.adapters.opencode.settings import (
    DEFAULT_AFTER_TOOL_DELIVERY,
    DEFAULT_BASH_POLICY,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_OPENCODE_PROVIDER,
    DEFAULT_POST_PHASE_MODE,
    OPENCODE_NPM_PACKAGE,
    OPENCODE_NPM_VERSION,
    OPENCODE_SDK_NPM_PACKAGE,
    OPENCODE_SDK_NPM_VERSION,
    OpenCodeRuntime,
    build_opencode_runtime,
    normalize_bash_policy,
    normalize_post_phase_mode,
    normalize_after_tool_delivery,
)

__all__ = [
    "DEFAULT_AFTER_TOOL_DELIVERY",
    "DEFAULT_BASH_POLICY",
    "DEFAULT_OPENCODE_MODEL",
    "DEFAULT_OPENCODE_PROVIDER",
    "DEFAULT_POST_PHASE_MODE",
    "DEFAULT_REAL_SMOKE_DELIVERIES",
    "OPENCODE_NPM_PACKAGE",
    "OPENCODE_NPM_VERSION",
    "OPENCODE_SDK_NPM_PACKAGE",
    "OPENCODE_SDK_NPM_VERSION",
    "OpenCodeAdapter",
    "OpenCodeRunSession",
    "OpenCodeRuntime",
    "build_opencode_runtime",
    "normalize_bash_policy",
    "normalize_post_phase_mode",
    "normalize_after_tool_delivery",
    "run_opencode_delivery_real_smoke",
    "run_opencode_server_smoke",
]
