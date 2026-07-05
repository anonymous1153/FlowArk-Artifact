from __future__ import annotations

import json
from typing import Any, cast

from fastapi import Request

from flowark_studio.process.manager import ProcessManager


def get_manager(request: Request) -> ProcessManager:
    return cast(ProcessManager, request.app.state.manager)


def split_task_start_payload(payload: Any) -> tuple[dict[str, Any], Any]:
    params = payload.get("params") if isinstance(payload, dict) else None
    if not isinstance(params, dict):
        params = payload if isinstance(payload, dict) else {}
    return params, None


def sse_event_message(event_type: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {body}\n\n".encode("utf-8")
