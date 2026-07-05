from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from flowark_studio.api.deps import get_manager, split_task_start_payload, sse_event_message

HIDDEN_PUBLIC_EVENT_TYPES = {"subprocess_stdout", "subprocess_stderr"}


def is_public_stream_event(event_type: str) -> bool:
    return str(event_type or "") not in HIDDEN_PUBLIC_EVENT_TYPES


def _task_start_response(task_id: str, task: dict[str, Any] | None) -> dict[str, Any]:
    response: dict[str, Any] = {"task_id": task_id}
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    warnings = metadata.get("normalization_warnings")
    if not isinstance(warnings, list):
        warnings = metadata.get("parameter_warnings")
    if not isinstance(warnings, list):
        warnings = []
    response["normalization_warnings"] = [str(item) for item in warnings if str(item).strip()]
    response["parameter_warnings"] = list(response["normalization_warnings"])
    return response


def create_task_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/eval")
    async def api_start_eval(request: Request, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        manager = get_manager(request)
        params, dispatch_mode = split_task_start_payload(payload)
        try:
            task_id = await manager.start_eval(params, dispatch_mode=dispatch_mode)
            task = await manager.get_task(task_id)
            return _task_start_response(task_id, task)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tasks")
    async def api_list_tasks(request: Request, view: str = Query(default="full")) -> dict[str, Any]:
        manager = get_manager(request)
        summary = str(view or "").strip().lower() in {"summary", "list"}
        return {"tasks": await manager.list_public_tasks(summary=summary)}

    @router.get("/api/tasks/{task_id}")
    async def api_get_task(request: Request, task_id: str) -> dict[str, Any]:
        manager = get_manager(request)
        task = await manager.get_public_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @router.post("/api/tasks/{task_id}/stop")
    async def api_stop_task(request: Request, task_id: str) -> dict[str, Any]:
        manager = get_manager(request)
        ok = await manager.stop_task(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="task not running or not found")
        return {"ok": True}

    @router.post("/api/tasks/{task_id}/eval/pause")
    async def api_pause_eval(request: Request, task_id: str) -> dict[str, Any]:
        manager = get_manager(request)
        ok = await manager.pause_eval(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="eval task not pausable")
        return {"ok": True}

    @router.post("/api/tasks/{task_id}/eval/resume")
    async def api_resume_eval(request: Request, task_id: str) -> dict[str, Any]:
        manager = get_manager(request)
        ok = await manager.resume_eval(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="eval task not resumable")
        return {"ok": True}

    @router.get("/api/tasks/{task_id}/events")
    async def api_task_events(
        request: Request,
        task_id: str,
        replay_last: int = Query(200, ge=0, le=5000),
    ):
        manager = get_manager(request)
        task = await manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def _stream():
            yield b": connected\n\n"
            agen = await manager.subscribe_task_events(task_id, replay_last=replay_last)
            async for event in agen:
                if not is_public_stream_event(event.type):
                    continue
                yield sse_event_message(event.type, await manager.public_event_payload(event))
                await asyncio.sleep(0)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @router.get("/api/events")
    async def api_all_events(request: Request, replay_last: int = Query(100, ge=0, le=2000)):
        manager = get_manager(request)

        async def _stream():
            yield b": connected\n\n"
            agen = await manager.subscribe_all_events(replay_last=replay_last)
            async for event in agen:
                if not is_public_stream_event(event.type):
                    continue
                yield sse_event_message(event.type, await manager.public_event_payload(event))
                await asyncio.sleep(0)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @router.get("/api/tasks/{task_id}/artifacts")
    async def api_task_artifacts(
        request: Request,
        task_id: str,
        selected_run_dir: str | None = Query(None),
        include_all_eval_runs: bool = Query(False),
    ) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.list_task_artifacts(
                task_id,
                selected_run_dir=selected_run_dir,
                include_all_eval_runs=include_all_eval_runs,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tasks/{task_id}/eval-runs")
    async def api_task_eval_runs(
        request: Request,
        task_id: str,
        detail: str = Query("summary", pattern="^(summary|full)$"),
    ) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.list_public_task_eval_runs(task_id, detail=detail)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tasks/{task_id}/artifact")
    async def api_task_artifact(
        request: Request,
        task_id: str,
        path: str = Query(...),
        max_bytes: int = Query(2_000_000, ge=1024, le=20_000_000),
    ):
        manager = get_manager(request)
        try:
            payload = await manager.read_task_artifact(task_id, path, max_bytes=max_bytes)
            return JSONResponse(payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/tasks/{task_id}/tail")
    async def api_task_tail(
        request: Request,
        task_id: str,
        path: str = Query(...),
        offset: int = Query(0, ge=0),
        max_bytes: int = Query(512_000, ge=1024, le=5_000_000),
        from_end: bool = Query(False),
    ) -> dict[str, Any]:
        manager = get_manager(request)
        try:
            return await manager.tail_task_artifact(task_id, path, offset=offset, max_bytes=max_bytes, from_end=from_end)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
