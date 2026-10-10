from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from flowark.config import load_repo_dotenv
from flowark.state_paths import format_legacy_state_dirs_warning
from flowark_studio.api.routes.system import create_system_router
from flowark_studio.api.routes.tags import create_tag_router
from flowark_studio.api.routes.tasks import create_task_router
from flowark_studio.process.manager import ProcessManager


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def create_app(
    *,
    workspace_root: Path | None = None,
) -> FastAPI:
    workspace = (workspace_root or _repo_root()).expanduser().resolve()
    load_repo_dotenv(workspace)
    static_dir = workspace / "flowark_studio" / "static"
    manager = ProcessManager(workspace_root=workspace)
    static_version = _compute_static_version(static_dir)

    app = FastAPI(
        title="FlowArk Studio",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.workspace_root = workspace
    app.state.manager = manager
    app.state.static_version = static_version

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def add_no_cache_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path or ""
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.on_event("startup")
    async def on_startup() -> None:
        import logging

        warning = format_legacy_state_dirs_warning(workspace)
        if warning:
            logging.getLogger("flowark_studio").warning(warning)
        await manager.ensure_studio_state_initialized()
        loaded = await manager.load_historical_tasks()
        await manager.load_runtime_state()
        await manager.start_background_tasks()
        if loaded > 0:
            logging.getLogger("flowark_studio").info(f"Loaded {loaded} historical tasks")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await manager.shutdown()

    @app.get("/")
    async def index() -> HTMLResponse:
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        html = html.replace("__STATIC_VERSION__", static_version)
        return HTMLResponse(html)

    app.include_router(create_task_router())
    app.include_router(create_tag_router())
    app.include_router(create_system_router(workspace=workspace))
    return app


def _compute_static_version(static_dir: Path) -> str:
    latest_ns = 0
    for pattern in ("*.js", "*.css", "*.html"):
        for path in static_dir.rglob(pattern):
            if not path.is_file():
                continue
            try:
                latest_ns = max(latest_ns, int(path.stat().st_mtime_ns))
            except OSError:
                continue
    return str(latest_ns or 1)
