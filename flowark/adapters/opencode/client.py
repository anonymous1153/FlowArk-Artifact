"""Small HTTP client for the OpenCode server API."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_OPENCODE_HTTP_TIMEOUT_SECONDS = 1800


class OpenCodeHttpError(RuntimeError):
    pass


class OpenCodeHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        directory: str,
        timeout_seconds: int = DEFAULT_OPENCODE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.directory = directory
        self.timeout_seconds = max(1, int(timeout_seconds))

    async def health(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/global/health")
        return payload if isinstance(payload, dict) else {}

    async def paths(self) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "/path",
            query={"directory": self.directory},
        )
        return payload if isinstance(payload, dict) else {}

    async def create_session(
        self,
        *,
        title: str,
    ) -> dict[str, Any]:
        last_timeout: BaseException | None = None
        for attempt in range(3):
            try:
                return await self._request_json(
                    "POST",
                    "/session",
                    query={"directory": self.directory},
                    body={"title": title},
                    timeout_seconds=min(int(self.timeout_seconds), 30),
                )
            except (TimeoutError, OpenCodeHttpError) as exc:
                if isinstance(exc, OpenCodeHttpError) and "timed out" not in str(exc):
                    raise
                last_timeout = exc
                if attempt >= 2:
                    break
                await asyncio.sleep(2 * (attempt + 1))
        raise OpenCodeHttpError("OpenCode create_session timed out after 3 attempts") from last_timeout

    async def prompt(
        self,
        *,
        session_id: str,
        text: str,
        provider_id: str,
        model_id: str,
        tools: dict[str, bool],
        agent: str = "build",
        no_reply: bool = False,
        format_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": {
                "providerID": provider_id,
                "modelID": model_id,
            },
            "agent": agent,
            "noReply": no_reply,
            "tools": dict(tools),
            "parts": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        }
        if format_payload is not None:
            body["format"] = format_payload
        return await self._request_json(
            "POST",
            f"/session/{session_id}/message",
            query={"directory": self.directory},
            body=body,
        )

    async def messages(self, *, session_id: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/session/{session_id}/message",
            query={"directory": self.directory},
        )
        return payload if isinstance(payload, list) else []

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._request_json_sync,
            method,
            path,
            query or {},
            body,
            timeout_seconds,
        )

    def _request_json_sync(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any] | None,
        timeout_seconds: int | None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        timeout = timeout_seconds or self.timeout_seconds
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            raise OpenCodeHttpError(f"OpenCode HTTP {exc.code} for {method} {path}: {raw_error}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise OpenCodeHttpError(f"OpenCode request timed out after {timeout}s for {method} {path}") from exc
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenCodeHttpError(f"OpenCode returned non-JSON response for {method} {path}: {text[:500]}") from exc


__all__ = ["OpenCodeHttpClient", "OpenCodeHttpError"]
