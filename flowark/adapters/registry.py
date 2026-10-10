"""Adapter selection for runtime-facing agent integrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flowark.adapters.base import AgentAdapter
from flowark.adapters.opencode import OpenCodeAdapter
from flowark.runtime.config import RunConfig

CaptureProxyFactory = Callable[..., Any]
TranscriptAppendFn = Callable[..., None]

DEFAULT_AGENT_ADAPTER = "opencode"

_ADAPTER_ALIASES = {
    "open_code": "opencode",
    "opencode": "opencode",
}


def normalize_agent_adapter_name(name: str | None) -> str:
    raw = str(name or DEFAULT_AGENT_ADAPTER).strip().lower().replace("-", "_")
    return _ADAPTER_ALIASES.get(raw, raw)


def build_agent_adapter(
    *,
    name: str | None,
    config: RunConfig,
    workspace_root: Path,
    transcript_append_fn: TranscriptAppendFn,
    capture_proxy_factory: CaptureProxyFactory,
) -> AgentAdapter:
    adapter_name = normalize_agent_adapter_name(name)
    if adapter_name == "opencode":
        return OpenCodeAdapter(
            config=config,
            workspace_root=workspace_root,
            transcript_append_fn=transcript_append_fn,
            capture_proxy_factory=capture_proxy_factory,
        )
    raise ValueError(
        f"unsupported agent adapter {name!r}; public runtime supports only opencode"
    )


__all__ = [
    "DEFAULT_AGENT_ADAPTER",
    "build_agent_adapter",
    "normalize_agent_adapter_name",
]
