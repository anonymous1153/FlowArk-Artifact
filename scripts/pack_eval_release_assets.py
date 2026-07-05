#!/usr/bin/env python3
"""Pack public paper evaluation roots as GitHub Release assets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path


PAPER_EVAL_ROOTS = [
    "paper-main50-standard-opencode",
    "paper-main50-flowark-enabled-opencode",
    "paper-strat15-glm-4-7-standard-opencode",
    "paper-strat15-glm-4-7-flowark-enabled-opencode",
    "paper-strat15-deepseek-v4-flash-standard-opencode",
    "paper-strat15-deepseek-v4-flash-flowark-enabled-opencode",
    "paper-strat15-minimax-m3-standard-opencode",
    "paper-strat15-minimax-m3-flowark-enabled-opencode",
    "paper-strat15-mem0-enabled-opencode",
    "paper-strat15-analysis-log-rag-baseline",
    "paper-strat15-ablation-m1-generic",
    "paper-strat15-ablation-m2-embedding",
    "paper-strat15-ablation-m3-start-only",
]

_LEGACY_UPPER = b"L" + b"DFC"
_LEGACY_LOWER = b"l" + b"dfc"
BYTE_REPLACEMENTS = (
    (_LEGACY_UPPER + b"_STUDIO", b"FLOWARK_STUDIO"),
    (_LEGACY_UPPER + b"_", b"FLOWARK_"),
    (_LEGACY_UPPER, b"FlowArk"),
    (b"L" + b"dfc", b"FlowArk"),
    (_LEGACY_LOWER + b"_studio", b"flowark_studio"),
    (_LEGACY_LOWER, b"flowark"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_files(path: Path) -> int:
    total = 0
    for _root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if _keep_public_path_name(name)]
        total += sum(1 for name in files if _keep_public_path_name(name))
    return total


def _keep_public_path_name(name: str) -> bool:
    return not name.startswith("._") and name != ".DS_Store"


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if not _keep_public_path_name(name)}


def normalize_public_eval_root(src: Path, work_dir: Path) -> Path:
    """Copy one evaluation root and rewrite legacy project names for the public artifact."""
    dest = work_dir / src.name
    shutil.copytree(src, dest, ignore=_copy_ignore)
    for path in sorted(dest.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        next_name = path.name
        for old, new in BYTE_REPLACEMENTS:
            next_name = next_name.replace(old.decode("ascii"), new.decode("ascii"))
        if next_name != path.name:
            path.rename(path.with_name(next_name))
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        next_data = data
        for old, new in BYTE_REPLACEMENTS:
            next_data = next_data.replace(old, new)
        if next_data != data:
            path.write_bytes(next_data)
    return dest


def pack_with_tarfile(src: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        paths = [src, *sorted(src.rglob("*"), key=lambda path: str(path.relative_to(src.parent)))]
        for path in paths:
            if any(not _keep_public_path_name(part) for part in path.relative_to(src.parent).parts):
                continue
            tar.add(path, arcname=path.relative_to(src.parent), recursive=False)


def pack_one(src: Path, dest: Path, *, force: bool) -> None:
    if dest.exists() and not force:
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    pack_with_tarfile(src, tmp)
    tmp.replace(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-evals-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("release-assets/evaluation-logs"))
    parser.add_argument("--manifest", type=Path, default=Path("data/evaluation-archives-manifest.csv"))
    parser.add_argument("--sha256", type=Path, default=Path("data/evaluation-archives-sha256.txt"))
    parser.add_argument("--release-tag", default="flowark-evaluation-logs-v1")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    source_dir = args.paper_evals_dir.expanduser().resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str | int]] = []
    sha_lines: list[str] = []

    with tempfile.TemporaryDirectory(prefix="flowark-evaluation-pack-") as tmp:
        tmp_dir = Path(tmp)
        for root_name in PAPER_EVAL_ROOTS:
            src = source_dir / root_name
            if not src.is_dir():
                raise FileNotFoundError(f"missing evaluation root: {src}")
            public_src = normalize_public_eval_root(src, tmp_dir)
            asset = args.out_dir / f"{root_name}.tar.gz"
            pack_one(public_src, asset, force=args.force)
            digest = sha256_file(asset)
            size = asset.stat().st_size
            rows.append(
                {
                    "filename": asset.name,
                    "eval_root": root_name,
                    "release_tag": args.release_tag,
                    "size_bytes": size,
                    "sha256": digest,
                    "source_file_count": count_files(public_src),
                }
            )
            sha_lines.append(f"{digest}  {asset.name}\n")
            print(f"{asset.name}\t{size}\t{digest}")

    with args.manifest.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "filename",
                "eval_root",
                "release_tag",
                "size_bytes",
                "sha256",
                "source_file_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    args.sha256.write_text("".join(sha_lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
