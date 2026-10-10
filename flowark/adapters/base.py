"""Base adapter interfaces for session-based agent integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncContextManager, Protocol

from flowark.semantics.engine import SemanticEngine
from flowark.semantics.models import (
    AnalysisRunContext,
    AnalysisRunResult,
    PhaseRunResult,
    PhaseSpec,
    SessionHandle,
)
from flowark.types import AnalysisRequest


@dataclass(slots=True)
class AdapterCapabilities:
    supports_same_session_continuation: bool
    supports_request_submit_augment: bool
    supports_after_tool_augment: bool
    supports_augmentation_delivery: bool
    supports_delta_context: bool = False
    supports_continuous_run_session: bool = False
    supports_native_structured_output: bool = False


class AgentAdapter(Protocol):
    name: str
    capabilities: AdapterCapabilities

    def open_run_session(
        self,
        *,
        request: AnalysisRequest,
        run_context: AnalysisRunContext,
        semantics: SemanticEngine,
    ) -> AsyncContextManager["AgentRunSession"]: ...

    async def run_analysis(
        self,
        *,
        request: AnalysisRequest,
        run_context: AnalysisRunContext,
        semantics: SemanticEngine,
    ) -> AnalysisRunResult: ...

    async def continue_phase(
        self,
        *,
        session: SessionHandle,
        phase_spec: PhaseSpec,
    ) -> PhaseRunResult: ...

    def reset_ephemeral_state(self) -> None: ...


class AgentRunSession(Protocol):
    async def run_analysis(self) -> AnalysisRunResult: ...

    async def continue_phase(
        self,
        *,
        phase_spec: PhaseSpec,
    ) -> PhaseRunResult: ...
