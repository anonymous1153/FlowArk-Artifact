#!/usr/bin/env python3
"""Start the public FlowArk Studio with repository-local artifact data."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8999")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.setdefault("FLOWARK_DATA_ROOT", str(root / "artifact-data" / "studio-state"))
    cmd = [
        sys.executable,
        "-m",
        "flowark_studio",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--workspace-root",
        str(root),
    ]
    try:
        return subprocess.call(cmd, cwd=root, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
