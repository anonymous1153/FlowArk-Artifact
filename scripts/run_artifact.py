#!/usr/bin/env python3
"""Fetch public artifact data and start FlowArk Studio."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8999")
    parser.add_argument(
        "--with-source-code-archives",
        dest="with_source_archives",
        action="store_true",
        help="Also fetch Android apps source code archives (the Main50/Strat15 benchmark dataset). This downloads a much larger dataset.",
    )
    parser.add_argument("--with-source-archives", dest="with_source_archives", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--extract-source",
        action="store_true",
        help="Extract Android apps source code archives after fetching them.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Start Studio without fetching or verifying data first.",
    )
    parser.add_argument(
        "--local-assets-dir",
        type=Path,
        default=None,
        help="Use local release assets from this directory when present.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    if not args.skip_fetch:
        print(
            "Preparing artifact data before starting Studio. "
            "The web page becomes available after evaluation logs are verified and extracted.",
            flush=True,
        )
        fetch_cmd = [
            sys.executable,
            str(root / "scripts" / "fetch_artifact_data.py"),
            "--evaluation-logs",
            "--benchmarks",
        ]
        if args.with_source_archives:
            fetch_cmd.append("--source-code-archives")
        if args.extract_source:
            fetch_cmd.append("--extract-source")
        if args.local_assets_dir is not None:
            fetch_cmd.extend(["--local-assets-dir", str(args.local_assets_dir)])
        subprocess.run(fetch_cmd, cwd=root, check=True)

    start_cmd = [
        sys.executable,
        str(root / "scripts" / "start_studio.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    print(f"Starting FlowArk Studio at http://{args.host}:{args.port}", flush=True)
    try:
        return subprocess.call(start_cmd, cwd=root)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
