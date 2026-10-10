from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter


def create_system_router(*, workspace: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @router.get("/api/schema/eval")
    async def api_schema_eval() -> dict[str, Any]:
        from flowark_studio.common.config_presets import get_eval_schema

        return get_eval_schema(workspace_root=workspace)

    return router
