"""Adapter protocols for agent-specific integrations."""

from flowark.adapters.base import AdapterCapabilities, AgentAdapter
from flowark.adapters.registry import (
    DEFAULT_AGENT_ADAPTER,
    build_agent_adapter,
    normalize_agent_adapter_name,
)

__all__ = [
    "AdapterCapabilities",
    "AgentAdapter",
    "DEFAULT_AGENT_ADAPTER",
    "build_agent_adapter",
    "normalize_agent_adapter_name",
]
