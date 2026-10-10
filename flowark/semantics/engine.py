"""Semantic engine protocol for platform-agnostic FlowArk orchestration."""

from __future__ import annotations

from typing import Protocol

from flowark.semantics.models import (
    AfterToolContext,
    AugmentDecision,
    AugmentRuntimeConfig,
    Phase,
    PhaseInput,
    PhasePolicy,
    PhaseSpec,
    RequestSubmitContext,
)


class SemanticEngine(Protocol):
    async def request_submit_augment(
        self,
        ctx: RequestSubmitContext,
    ) -> AugmentDecision: ...

    async def after_tool_augment(
        self,
        ctx: AfterToolContext,
    ) -> AugmentDecision: ...

    def augment_runtime_config(self) -> AugmentRuntimeConfig: ...

    def phase_policy(self, phase: Phase) -> PhasePolicy: ...

    def build_phase_spec(
        self,
        phase: Phase,
        *,
        phase_input: PhaseInput,
    ) -> PhaseSpec: ...
