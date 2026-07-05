#!/usr/bin/env python3
"""Download, verify, and extract FlowArk artifact data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path


REPO = "anonymous1153/FlowArk-Artifact"
EVAL_RELEASE_TAG = "flowark-evaluation-logs-v1"
SOURCE_RELEASE_TAG = "flowark-source-archives-v1"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def workspace_id(root: Path) -> str:
    import re

    name = re.sub(r"[^A-Za-z0-9._-]+", "-", root.resolve().name).strip("._-") or "workspace"
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def studio_data_root(root: Path) -> Path:
    raw = str(os.getenv("FLOWARK_DATA_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return root / "artifact-data" / "studio-state"


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"checksum mismatch for {path.name}: expected {expected}, got {actual}")


def _log(message: str) -> None:
    print(message, flush=True)


def release_url(tag: str, filename: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def download_asset(
    *,
    filename: str,
    tag: str,
    expected_sha256: str,
    dest_dir: Path,
    local_assets_dir: Path | None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.exists():
        try:
            verify(dest, expected_sha256)
            _log(f"using cached asset: {filename}")
            return dest
        except ValueError:
            _log(f"discarding stale cached asset: {filename}")
            dest.unlink()

    if local_assets_dir is not None:
        local = local_assets_dir / filename
        if local.exists():
            _log(f"copying local asset: {filename}")
            shutil.copy2(local, dest)
            verify(dest, expected_sha256)
            return dest

    url = release_url(tag, filename)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    _log(f"downloading {url}")
    urllib.request.urlretrieve(url, tmp)
    verify(tmp, expected_sha256)
    tmp.replace(dest)
    return dest


def safe_extract_tar_gz(archive: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest_dir / member.name).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe archive member in {archive.name}: {member.name}")
        try:
            tar.extractall(dest_dir, filter="data")
        except TypeError:
            tar.extractall(dest_dir)


def materialize_benchmark(template: Path, output: Path, source_root: Path) -> None:
    data = json.loads(template.read_text(encoding="utf-8"))
    for case in data.get("cases", []):
        if not isinstance(case, dict):
            continue
        archive = str(case.get("source_archive") or "").strip()
        if archive:
            case["source_dir"] = str((source_root / archive).resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_benchmarks(root: Path) -> None:
    source_root = root / "artifact-data" / "source-code"
    out_dir = root / "artifact-data" / "benchmarks"
    for template in sorted((root / "data" / "benchmarks").glob("*.template.json")):
        output = out_dir / template.name.replace(".template.json", ".json")
        materialize_benchmark(template, output, source_root)
        print(f"wrote {output}")


def fetch_eval_logs(root: Path, local_assets_dir: Path | None) -> None:
    manifest = read_manifest(root / "data" / "evaluation-archives-manifest.csv")
    downloads = root / "artifact-data" / "downloads" / "evaluation-logs"
    evals_dir = studio_data_root(root) / workspace_id(root) / "evals" / "evals"
    total = len(manifest)
    for index, row in enumerate(manifest, start=1):
        asset = download_asset(
            filename=row["filename"],
            tag=row.get("release_tag") or EVAL_RELEASE_TAG,
            expected_sha256=row["sha256"],
            dest_dir=downloads,
            local_assets_dir=local_assets_dir,
        )
        eval_root = evals_dir / row["eval_root"]
        marker = eval_root / ".flowark_archive_sha256"
        required_files = [
            eval_root / "config.json",
            eval_root / "results.jsonl",
            eval_root / ".flowark_studio_task.json",
        ]
        if all(path.exists() for path in required_files):
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == row["sha256"]:
                _log(f"[{index}/{total}] already extracted evaluation root: {row['eval_root']}")
                continue
            if not marker.exists():
                marker.write_text(str(row["sha256"]) + "\n", encoding="utf-8")
                _log(f"[{index}/{total}] found existing evaluation root: {row['eval_root']}")
                continue
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != row["sha256"] and eval_root.exists():
            old_digest = marker.read_text(encoding="utf-8").strip()[:12] or "unknown"
            stale_root = eval_root.parent / f".stale-{eval_root.name}-{old_digest}"
            suffix = 1
            while stale_root.exists():
                suffix += 1
                stale_root = eval_root.parent / f".stale-{eval_root.name}-{old_digest}-{suffix}"
            shutil.move(str(eval_root), str(stale_root))
            _log(f"[{index}/{total}] moved stale evaluation root aside: {stale_root.name}")
        _log(f"[{index}/{total}] extracting evaluation root: {row['eval_root']}")
        safe_extract_tar_gz(asset, evals_dir)
        marker.write_text(str(row["sha256"]) + "\n", encoding="utf-8")
    _log(f"evaluation logs ready at {evals_dir}")


def fetch_source_archives(root: Path, local_assets_dir: Path | None, *, extract: bool) -> None:
    manifest = read_manifest(root / "data" / "source-archives-manifest.csv")
    downloads = root / "artifact-data" / "source-code-archives"
    source_root = root / "artifact-data" / "source-code"
    total = len(manifest)
    for index, row in enumerate(manifest, start=1):
        asset = download_asset(
            filename=row["filename"],
            tag=SOURCE_RELEASE_TAG,
            expected_sha256=row["sha256"],
            dest_dir=downloads,
            local_assets_dir=local_assets_dir,
        )
        if extract:
            target = source_root / row["filename"]
            if target.exists():
                _log(f"[{index}/{total}] already extracted Android app source code: {row['filename']}")
                continue
            _log(f"[{index}/{total}] extracting Android app source code: {row['filename']}")
            safe_extract_tar_gz(asset, source_root)
    if extract:
        _log(f"Android apps source code archives (the Main50/Strat15 benchmark dataset) extracted at {source_root}")
    else:
        _log(f"Android apps source code archives (the Main50/Strat15 benchmark dataset) ready at {downloads}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-logs", dest="eval_logs", action="store_true", help="Fetch and extract public Studio evaluation logs.")
    parser.add_argument("--eval-logs", dest="eval_logs", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--source-code-archives",
        dest="source_archives",
        action="store_true",
        help="Fetch Android apps source code archives (the Main50/Strat15 benchmark dataset).",
    )
    parser.add_argument("--source-archives", dest="source_archives", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--extract-source", action="store_true", help="Extract Android apps source code archives after fetching.")
    parser.add_argument("--benchmarks", action="store_true", help="Materialize local benchmark JSON files.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch evaluation logs, Android apps source code archives, and materialize benchmarks.",
    )
    parser.add_argument(
        "--local-assets-dir",
        type=Path,
        default=None,
        help="Use local release assets from this directory when present, otherwise download from GitHub.",
    )
    args = parser.parse_args()

    root = repo_root()
    local_assets_dir = args.local_assets_dir.expanduser().resolve() if args.local_assets_dir else None
    do_eval = args.all or args.eval_logs
    do_sources = args.all or args.source_archives
    do_benchmarks = args.all or args.benchmarks
    extract_source = args.all or args.extract_source
    if not any([do_eval, do_sources, do_benchmarks]):
        do_eval = True
        do_benchmarks = True

    if do_eval:
        fetch_eval_logs(root, local_assets_dir)
    if do_sources:
        fetch_source_archives(root, local_assets_dir, extract=extract_source)
    if do_benchmarks:
        prepare_benchmarks(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
