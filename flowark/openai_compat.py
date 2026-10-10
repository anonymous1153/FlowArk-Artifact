"""Helpers for OpenAI-compatible chat completions endpoints."""

from __future__ import annotations

import re

_VERSION_SUFFIX_RE = re.compile(r"/v\d+$")


def normalize_chat_completions_base_url(
    base_url: str | None,
    *,
    default_base_url: str = "http://localhost:4000",
) -> str:
    """Return a base URL suitable for OpenAI-compatible clients.

    Supports both classic `/v1/chat/completions` endpoints and vendor-specific
    variants such as BigModel `/api/paas/v4/chat/completions`.
    """

    base = str(base_url or "").strip()
    if not base:
        base = default_base_url
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    base = base.rstrip("/")
    if _VERSION_SUFFIX_RE.search(base):
        return base
    return f"{base}/v1"


def build_chat_completions_endpoint(
    base_url: str | None,
    *,
    default_base_url: str = "http://localhost:4000",
) -> str:
    """Return the full chat completions endpoint for an OpenAI-compatible API."""

    raw = str(base_url or "").strip()
    if raw.endswith("/chat/completions"):
        return raw.rstrip("/")
    base = normalize_chat_completions_base_url(
        raw,
        default_base_url=default_base_url,
    )
    return f"{base.rstrip('/')}/chat/completions"


def disabled_thinking_extra_body() -> dict[str, dict[str, str]]:
    """Return the OpenAI-compatible vendor extension for disabling thinking."""

    return {"thinking": {"type": "disabled"}}
