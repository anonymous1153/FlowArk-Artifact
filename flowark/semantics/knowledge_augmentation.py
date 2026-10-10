"""Executable augmentation semantics for runtime knowledge injection."""

from __future__ import annotations

from typing import Callable

from flowark.knowledge.runtime_augment import compute_after_tool_augment, compute_request_submit_augment
from flowark.semantics.models import (
    AfterToolContext,
    AugmentDecision,
    AugmentRuntimeConfig,
    AugmentationPayload,
    RequestSubmitContext,
)

WrapPayloadFn = Callable[[AugmentationPayload], str]


class KnowledgeAugmentationSemantics:
    """Platform-agnostic augmentation semantics backed by the current knowledge runtime."""

    def __init__(
        self,
        runtime_config: AugmentRuntimeConfig,
        *,
        request_submit_wrap_payload_fn: WrapPayloadFn | None = None,
        after_tool_wrap_payload_fn: WrapPayloadFn | None = None,
    ) -> None:
        self._runtime_config = runtime_config
        self._request_submit_wrap_payload_fn = request_submit_wrap_payload_fn
        self._after_tool_wrap_payload_fn = after_tool_wrap_payload_fn

    def augment_runtime_config(self) -> AugmentRuntimeConfig:
        return self._runtime_config

    async def request_submit_augment(
        self,
        ctx: RequestSubmitContext,
    ) -> AugmentDecision:
        return compute_request_submit_augment(
            runtime_config=self._runtime_config,
            ctx=ctx,
            wrap_payload_fn=self._request_submit_wrap_payload_fn,
        )

    async def after_tool_augment(
        self,
        ctx: AfterToolContext,
    ) -> AugmentDecision:
        return compute_after_tool_augment(
            runtime_config=self._runtime_config,
            ctx=ctx,
            wrap_payload_fn=self._after_tool_wrap_payload_fn,
        )
