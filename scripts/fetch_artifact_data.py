#!/usr/bin/env python3
"""Download, verify, and extract FlowArk artifact data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

from verify_eval_data import load_allowed_apps, verify_public_eval_root

REPO = "anonymous1153/FlowArk-Artifact"
EVAL_RELEASE_TAG = "flowark-evaluation-logs-v1"
SOURCE_RELEASE_TAG = "flowark-source-archives-v1"
MANUAL_AUDIT_RELEASE_TAG = "flowark-manual-audit-v1"
_VERIFIED_FILES: set[tuple] = set()
_ARCHIVE_CACHE_DIRS = {
    "source": "source-code-archives",
    "evaluation": "downloads/evaluation-logs",
    "reproduction": "downloads/reproduction",
}


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
    signature = _file_signature(path, expected)
    if signature in _VERIFIED_FILES:
        return
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"checksum mismatch for {path.name}: expected {expected}, got {actual}")
    if _file_signature(path, expected) != signature:
        raise ValueError(f"file changed during checksum verification: {path.name}")
    _VERIFIED_FILES.add(signature)


def _file_signature(path: Path, expected: str) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), expected, stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _log(message: str) -> None:
    print(message, flush=True)


def retire_path(path: Path) -> Path:
    """Move replaced data outside the artifact so it remains recoverable."""
    configured = str(os.getenv("FLOWARK_RETIRED_DATA_ROOT") or "").strip()
    if configured:
        retired = Path(configured).expanduser().resolve()
    elif sys.platform == "darwin":
        retired = Path.home() / ".Trash" / "flowark-artifact-retired"
    else:
        retired = Path.home() / ".local/share/Trash/files/flowark-artifact-retired"
    artifact = repo_root().resolve()
    if retired == artifact or artifact in retired.parents:
        raise ValueError("Retired data must be stored outside the artifact checkout")
    retired.mkdir(parents=True, exist_ok=True)
    destination = retired / f"{uuid.uuid4().hex[:12]}-{path.name}"
    shutil.move(str(path), str(destination))
    _log(f"moved replaced data outside the artifact: {destination}")
    return destination


def verify_eval_apps(eval_root: Path, allowed_apps: set[str]) -> None:
    verify_public_eval_root(eval_root, allowed_apps)


def release_url(tag: str, filename: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def _local_assets(filename: str, directory: Path | None) -> list[Path]:
    if directory is None:
        return []
    return [directory / filename, *sorted(directory.glob(f"*/{filename}"))]


def _asset_filename(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\\0"):
        raise ValueError("invalid release archive filename")
    return value


def _find_bundle(root: Path, filename: str, expected_sha256: str, tag: str) -> dict | None:
    path = root / "data/release-bundles.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "flowark-release-bundles-v1" or not isinstance(manifest.get("bundles"), list):
        raise ValueError("invalid release bundle manifest")
    matches = [(bundle, member) for bundle in manifest["bundles"] for member in bundle["members"]
               if member["filename"] == filename]
    if len(matches) > 1:
        raise ValueError(f"archive belongs to multiple release bundles: {filename}")
    if not matches:
        return None
    bundle, member = matches[0]
    if member["sha256"] != expected_sha256 or bundle["release_tag"] != tag:
        raise ValueError(f"release bundle differs from the archive manifest: {filename}")
    return bundle


def _install_bundle(bundle: dict, root: Path, local_assets_dir: Path | None) -> None:
    filename = _asset_filename(bundle["filename"])
    expected = {}
    for row in bundle["members"]:
        name = _asset_filename(row["filename"])
        if name in expected or row["kind"] not in _ARCHIVE_CACHE_DIRS or int(row["size_bytes"]) < 0:
            raise ValueError("invalid or duplicate release bundle member")
        expected[name] = row
    if not expected:
        raise ValueError("release bundle is empty")
    downloads = root / "artifact-data/downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flowark-bundle-", dir=downloads) as staging:
        local = next((p for p in _local_assets(filename, local_assets_dir) if p.is_file()), None)
        if local is not None:
            verify(local, bundle["sha256"])
            archive = local
        else:
            archive = download_asset(filename=filename, tag=bundle["release_tag"],
                                     expected_sha256=bundle["sha256"], dest_dir=Path(staging),
                                     local_assets_dir=None)
        if archive.stat().st_size != int(bundle["size_bytes"]):
            raise ValueError(f"release bundle has an unexpected size: {filename}")
        with tarfile.open(archive, "r:*") as package:
            members = package.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise ValueError("release bundle members differ from its manifest")
            for member in members:
                if not member.isfile() or member.size != int(expected[member.name]["size_bytes"]):
                    raise ValueError("release bundle member type or size differs from its manifest")
            for member in members:
                row = expected[member.name]
                target = root / "artifact-data" / _ARCHIVE_CACHE_DIRS[row["kind"]] / member.name
                if target.is_symlink():
                    raise ValueError("release archive cache must not be a symlink")
                if target.exists():
                    try:
                        verify(target, row["sha256"])
                        continue
                    except ValueError:
                        retire_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{uuid.uuid4().hex}-{target.name}.tmp")
                digest = hashlib.sha256()
                try:
                    with package.extractfile(member) as incoming, temporary.open("xb") as outgoing:
                        for chunk in iter(lambda: incoming.read(4 * 1024 * 1024), b""):
                            outgoing.write(chunk)
                            digest.update(chunk)
                    if digest.hexdigest() != row["sha256"]:
                        raise ValueError(f"checksum mismatch for bundled archive: {member.name}")
                    if target.exists():
                        raise FileExistsError(f"archive appeared while installing a bundle: {member.name}")
                    temporary.replace(target)
                    _VERIFIED_FILES.add(_file_signature(target, row["sha256"]))
                finally:
                    temporary.unlink(missing_ok=True)
        _log(f"release bundle available: {filename} ({len(members)} archives)")


def download_asset(
    *,
    filename: str,
    tag: str,
    expected_sha256: str,
    dest_dir: Path,
    local_assets_dir: Path | None,
    artifact_root: Path | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.exists():
        try:
            verify(dest, expected_sha256)
            _log(f"using cached asset: {filename}")
            return dest
        except ValueError:
            retire_path(dest)

    if local_assets_dir is not None:
        for local in _local_assets(filename, local_assets_dir):
            if not local.exists():
                continue
            _log(f"copying local asset: {filename}")
            shutil.copy2(local, dest)
            verify(dest, expected_sha256)
            return dest

    if artifact_root is not None:
        bundle = _find_bundle(artifact_root, filename, expected_sha256, tag)
        if bundle is not None:
            _install_bundle(bundle, artifact_root, local_assets_dir)
            verify(dest, expected_sha256)
            return dest

    url = release_url(tag, filename)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if tmp.exists():
        retire_path(tmp)
    _log(f"downloading {url}")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as incoming, tmp.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(4 * 1024 * 1024), b""):
            outgoing.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError(f"checksum mismatch for downloaded asset: {filename}")
    tmp.replace(dest)
    _VERIFIED_FILES.add(_file_signature(dest, expected_sha256))
    return dest


def safe_extract_tar_gz(archive: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    # Source assets can be plain tar files despite their historical .tar.gz names.
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            target = (dest_dir / member.name).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe archive member in {archive.name}: {member.name}")
        try:
            tar.extractall(dest_dir, filter="data")
        except TypeError:
            tar.extractall(dest_dir)


SOURCE_EXTRACTION_SCHEMA = "flowark-portable-source-v1"
SOURCE_EXTRACTION_MARKER = ".flowark_source_archive.json"


def prepare_source_archive(archive: Path, source_root: Path, filename: str, sha256: str) -> None:
    """Install portable source members without changing an existing source directory."""
    filename = _asset_filename(filename)
    target = source_root / filename
    marker = target / SOURCE_EXTRACTION_MARKER
    if target.exists() or target.is_symlink():
        identity = None
        if target.is_dir() and not target.is_symlink() and marker.is_file() and not marker.is_symlink():
            try:
                identity = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if (isinstance(identity, dict) and identity.get("schema_version") == SOURCE_EXTRACTION_SCHEMA
                and identity.get("filename") == filename and identity.get("sha256") == sha256):
            _log(f"source directory available: {filename}")
            return
        raise ValueError(
            f"existing source directory has a different or unverified archive identity: {filename}; "
            "it was left unchanged; prepare sources in a fresh artifact checkout"
        )

    source_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".flowark-source-", dir=source_root) as temporary:
        staging = Path(temporary)
        staged_root = staging / filename
        staged_root.mkdir()
        skipped = []
        with tarfile.open(archive, "r:*") as package:
            members = package.getmembers()
            # Validate every original name before creating any archive member.
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe source archive member in {filename}: {member.name}")
            top = {PurePosixPath(member.name).parts[0] for member in members
                   if PurePosixPath(member.name).parts}
            archive_root = next(iter(top)) if len(top) == 1 else None
            if not any(member.isdir() and PurePosixPath(member.name).parts == (archive_root,)
                       for member in members):
                archive_root = None

            for original in members:
                parts = PurePosixPath(original.name).parts
                if not parts:
                    continue
                tail = parts[1:] if archive_root else parts
                relative = PurePosixPath(filename, *tail)
                if relative == PurePosixPath(filename, SOURCE_EXTRACTION_MARKER):
                    raise ValueError(f"source archive contains a reserved completion marker: {filename}")
                member = original.replace(name=relative.as_posix(), deep=False)
                if (staging / member.name).resolve() == staged_root / SOURCE_EXTRACTION_MARKER:
                    raise ValueError(f"source archive contains a reserved completion marker: {filename}")
                if member.issym() or member.islnk():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute():
                        skipped.append({"member": original.name, "reason": "absolute link"})
                        continue
                    if member.islnk():
                        if archive_root:
                            if not link.parts or link.parts[0] != archive_root:
                                skipped.append({"member": original.name, "reason": "external link"})
                                continue
                            link = PurePosixPath(filename, *link.parts[1:])
                        else:
                            link = PurePosixPath(filename, *link.parts)
                        member = member.replace(linkname=link.as_posix(), deep=False)
                    link_target = ((staging / member.name).parent / link if member.issym()
                                   else staging / link)
                    if not link_target.resolve().is_relative_to(staged_root.resolve()):
                        skipped.append({"member": original.name, "reason": "external link"})
                        continue
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    skipped.append({"member": original.name, "reason": "nonportable special file"})
                    continue
                # The strict data filter also checks traversal through earlier internal links.
                package.extract(member, path=staging, filter="data")

        (staged_root / SOURCE_EXTRACTION_MARKER).write_text(json.dumps({
            "schema_version": SOURCE_EXTRACTION_SCHEMA, "filename": filename, "sha256": sha256,
            "skipped_members": skipped,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"source directory appeared during extraction: {filename}")
        staged_root.rename(target)
    _log(f"source extracted: {filename}; skipped {len(skipped)} nonportable members "
         f"(details: {filename}/{SOURCE_EXTRACTION_MARKER})")


def materialize_benchmark(template: Path, output: Path, source_root: Path) -> None:
    data = json.loads(template.read_text(encoding="utf-8"))
    allowed_apps = load_allowed_apps(repo_root() / "data/source-archives-manifest.csv")
    for case in data.get("cases", []):
        if not isinstance(case, dict):
            continue
        if case.get("app_name") not in allowed_apps:
            raise ValueError("Benchmark contains an app outside the published source corpus")
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
    allowed_apps = load_allowed_apps(root / "data/source-archives-manifest.csv")
    downloads = root / "artifact-data" / "downloads" / "evaluation-logs"
    evals_dir = studio_data_root(root) / workspace_id(root) / "evals" / "evals"
    total = len(manifest)
    for index, row in enumerate(manifest, start=1):
        eval_root = evals_dir / row["eval_root"]
        marker = eval_root / ".flowark_archive_sha256"
        if eval_root.exists():
            verify_eval_apps(eval_root, allowed_apps)
            if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != row["sha256"]:
                raise ValueError(f"existing evaluation has a different or unverified archive identity: {row['eval_root']}; use a separate FLOWARK_DATA_ROOT")
            _log(f"[{index}/{total}] evaluation records available: {row['eval_root']}")
            continue
        asset = download_asset(
            filename=row["filename"],
            tag=row.get("release_tag") or EVAL_RELEASE_TAG,
            expected_sha256=row["sha256"],
            dest_dir=downloads,
            local_assets_dir=local_assets_dir,
            artifact_root=root,
        )
        _log(f"[{index}/{total}] extracting evaluation root: {row['eval_root']}")
        with tempfile.TemporaryDirectory(prefix="flowark-eval-fetch-") as staging:
            safe_extract_tar_gz(asset, Path(staging))
            staged_root = Path(staging) / row["eval_root"]
            verify_eval_apps(staged_root, allowed_apps)
            if eval_root.exists():
                raise FileExistsError(f"evaluation appeared during download: {eval_root}")
            evals_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_root), str(eval_root))
        marker.write_text(str(row["sha256"]) + "\n", encoding="utf-8")
    _log(f"evaluation logs ready at {evals_dir}")


def verify_reproduction_data(root: Path, directory: Path) -> None:
    manifest = json.loads((root / "data/reproduction-files-manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("files")
    if manifest.get("schema_version") != "flowark-reproduction-files-v1" or not isinstance(entries, list) or not entries:
        raise ValueError("invalid reproduction file manifest")
    names = set()
    directory = directory.resolve()
    for row in entries:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.as_posix() in names:
            raise ValueError("invalid or duplicate reproduction file path")
        names.add(relative.as_posix())
        path = directory / relative
        if not path.resolve().is_relative_to(directory) or path.is_symlink():
            raise ValueError("reproduction file path escapes its directory")
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise ValueError(f"reproduction file is missing or incomplete: {relative}")
        verify(path, row["sha256"])
    if "catalog.json" not in names or "recompute.py" not in names:
        raise ValueError("reproduction bundle requires its catalog and computation entry point")
    catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
    experiments = catalog.get("experiments", [])
    actual = [row["eval_root"] for row in experiments]
    expected = [row["eval_root"] for row in read_manifest(root / "data/evaluation-archives-manifest.csv")]
    if catalog.get("schema_version") != "flowark-evidence-catalog-v1" or len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("reproduction catalog differs from the evaluation archive manifest")


def fetch_reproduction_data(root: Path, local_assets_dir: Path | None) -> None:
    rows = read_manifest(root / "data/reproduction-archives-manifest.csv")
    if len(rows) != 1 or rows[0].get("archive_root") != "reproduction":
        raise ValueError("expected one reproduction archive")
    row = rows[0]
    destination = root / "artifact-data/reproduction"
    if destination.exists():
        verify_reproduction_data(root, destination)
        _log("reproduction data available")
        return
    asset = download_asset(filename=row["filename"], tag=row.get("release_tag") or EVAL_RELEASE_TAG,
                           expected_sha256=row["sha256"], dest_dir=root / "artifact-data/downloads/reproduction",
                           local_assets_dir=local_assets_dir, artifact_root=root)
    with tempfile.TemporaryDirectory(prefix="flowark-reproduction-fetch-") as staging:
        safe_extract_tar_gz(asset, Path(staging))
        staged = Path(staging) / "reproduction"
        verify_reproduction_data(root, staged)
        if destination.exists():
            raise FileExistsError("reproduction data appeared during download")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(destination))
    _log("reproduction data extracted")


def fetch_manual_audit_logs(root: Path, local_assets_dir: Path | None) -> None:
    manifest = read_manifest(root / "data" / "manual-audit-archives-manifest.csv")
    downloads = root / "artifact-data" / "downloads" / "manual-audit"
    audit_dir = root / "artifact-data" / "manual-audit"
    total = len(manifest)
    for index, row in enumerate(manifest, start=1):
        asset = download_asset(
            filename=row["filename"],
            tag=row.get("release_tag") or MANUAL_AUDIT_RELEASE_TAG,
            expected_sha256=row["sha256"],
            dest_dir=downloads,
            local_assets_dir=local_assets_dir,
            artifact_root=root,
        )
        audit_root = audit_dir / row["archive_root"]
        marker = audit_root / ".flowark_archive_sha256"
        required_files = [audit_root / "MANIFEST.json", audit_root / "summary" / "mode_metrics.csv"]
        if all(path.exists() for path in required_files):
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == row["sha256"]:
                _log(f"[{index}/{total}] already extracted manual audit records: {row['archive_root']}")
                continue
            if not marker.exists():
                marker.write_text(str(row["sha256"]) + "\n", encoding="utf-8")
                _log(f"[{index}/{total}] found existing manual audit records: {row['archive_root']}")
                continue
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != row["sha256"] and audit_root.exists():
            old_digest = marker.read_text(encoding="utf-8").strip()[:12] or "unknown"
            stale_root = audit_root.parent / f".stale-{audit_root.name}-{old_digest}"
            suffix = 1
            while stale_root.exists():
                suffix += 1
                stale_root = audit_root.parent / f".stale-{audit_root.name}-{old_digest}-{suffix}"
            shutil.move(str(audit_root), str(stale_root))
            _log(f"[{index}/{total}] moved stale manual audit records aside: {stale_root.name}")
        _log(f"[{index}/{total}] extracting manual audit records: {row['archive_root']}")
        safe_extract_tar_gz(asset, audit_dir)
        marker.write_text(str(row["sha256"]) + "\n", encoding="utf-8")
    _log(f"manual audit records ready at {audit_dir}")


def fetch_source_archives(root: Path, local_assets_dir: Path | None, *, extract: bool) -> None:
    manifest = read_manifest(root / "data" / "source-archives-manifest.csv")
    load_allowed_apps(root / "data/source-archives-manifest.csv")
    downloads = root / "artifact-data" / "source-code-archives"
    source_root = root / "artifact-data" / "source-code"
    allowed_filenames = {row["filename"] for row in manifest}
    for path in downloads.glob("*_src.tar.gz"):
        if path.name not in allowed_filenames:
            retire_path(path)
    total = len(manifest)
    for index, row in enumerate(manifest, start=1):
        asset = download_asset(
            filename=row["filename"],
            tag=SOURCE_RELEASE_TAG,
            expected_sha256=row["sha256"],
            dest_dir=downloads,
            local_assets_dir=local_assets_dir,
            artifact_root=root,
        )
        if extract:
            _log(f"[{index}/{total}] preparing Android app source code: {row['filename']}")
            prepare_source_archive(asset, source_root, row["filename"], row["sha256"])
    if extract:
        _log(f"Android apps source code archives (the 171-app corpus for Main49/Strat15) extracted at {source_root}")
    else:
        _log(f"Android apps source code archives (the 171-app corpus for Main49/Strat15) ready at {downloads}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-logs", dest="eval_logs", action="store_true", help="Fetch Studio evaluation logs and their reproduction data.")
    parser.add_argument("--reproduction", action="store_true", help="Fetch the experiment catalog and offline reproduction data.")
    parser.add_argument("--eval-logs", dest="eval_logs", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--manual-audit-logs",
        dest="manual_audit_logs",
        action="store_true",
        help="Fetch and extract manual audit records for the reported Relative F1 values.",
    )
    parser.add_argument(
        "--source-code-archives",
        dest="source_archives",
        action="store_true",
        help="Fetch Android apps source code archives (the 171-app corpus for Main49/Strat15).",
    )
    parser.add_argument("--source-archives", dest="source_archives", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--extract-source", action="store_true", help="Extract Android apps source code archives after fetching.")
    parser.add_argument("--benchmarks", action="store_true", help="Materialize local benchmark JSON files.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch evaluation logs, manual audit records, Android apps source code archives, and materialize benchmarks.",
    )
    parser.add_argument(
        "--local-assets-dir",
        type=Path,
        default=None,
        help="Use local assets from this directory (default: release-assets/) before downloading from GitHub.",
    )
    args = parser.parse_args()

    root = repo_root()
    local_assets_dir = (args.local_assets_dir.expanduser().resolve()
                        if args.local_assets_dir else root / "release-assets")
    do_eval = args.all or args.eval_logs
    do_reproduction = args.all or args.eval_logs or args.reproduction
    do_manual_audit = args.all or args.manual_audit_logs
    do_sources = args.all or args.source_archives
    do_benchmarks = args.all or args.benchmarks
    extract_source = args.all or args.extract_source
    if not any([do_eval, do_reproduction, do_manual_audit, do_sources, do_benchmarks]):
        do_eval = True
        do_reproduction = True
        do_manual_audit = True
        do_benchmarks = True

    if do_reproduction:
        fetch_reproduction_data(root, local_assets_dir)
    if do_eval:
        fetch_eval_logs(root, local_assets_dir)
    if do_manual_audit:
        fetch_manual_audit_logs(root, local_assets_dir)
    if do_sources:
        fetch_source_archives(root, local_assets_dir, extract=extract_source)
    if do_benchmarks:
        prepare_benchmarks(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
