from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from flowark_studio.api.deps import get_manager


def create_tag_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/tasks/{task_id}/tags")
    async def api_set_task_tags(
        request: Request,
        task_id: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.set_task_tags(task_id, payload.get("tags") if isinstance(payload, dict) else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tags")
    async def api_list_tags(request: Request, query: str | None = Query(None)) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.list_tags(query=query)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tags/lookup")
    async def api_lookup_tag(request: Request, tag: str = Query(...)) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.lookup_public_tag(tag)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
