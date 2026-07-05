from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from flowark_studio.api.app import create_app

FLOWARK_STUDIO_RELOAD_WORKSPACE_ROOT_ENV = "FLOWARK_STUDIO_RELOAD_WORKSPACE_ROOT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlowArk Studio local web UI")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address (default: 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    parser.add_argument(
        "--workspace-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (default: current flowark-codex-master root)",
    )
    parser.add_argument("--reload", action="store_true", help="Enable development reload")
    return parser


def create_reload_app():
    workspace_root = Path(
        os.environ.get(FLOWARK_STUDIO_RELOAD_WORKSPACE_ROOT_ENV) or Path(__file__).resolve().parents[1]
    ).expanduser().resolve()
    return create_app(workspace_root=workspace_root)


def main() -> int:
    args = build_parser().parse_args()
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(args.port)
    if bool(args.reload):
        os.environ[FLOWARK_STUDIO_RELOAD_WORKSPACE_ROOT_ENV] = str(workspace_root)
        uvicorn.run(
            "flowark_studio.server:create_reload_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
        )
        return 0

    uvicorn.run(create_app(workspace_root=workspace_root), host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
