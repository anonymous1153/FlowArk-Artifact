from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("fetch_artifact_data")


@pytest.mark.parametrize("archive_mode", ["w:gz", "w"])
def test_source_upgrade_preserves_existing_source_directories(tmp_path, monkeypatch, fetch, archive_mode):
    root = tmp_path / "artifact"
    assets = tmp_path / "assets"
    retired = tmp_path / "DataSet-retired"
    filename = "example.retained_1_src.tar.gz"
    source = tmp_path / "source" / filename
    source.mkdir(parents=True)
    (source / "README").write_text("retained source")
    assets.mkdir()
    with tarfile.open(assets / filename, archive_mode) as archive:
        archive.add(source, arcname=filename)
    payload = (assets / filename).read_bytes()
    (root / "data").mkdir(parents=True)
    with (root / "data/source-archives-manifest.csv").open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["filename", "package_name", "version_code", "sha256"])
        writer.writeheader()
        writer.writerow({"filename": filename, "package_name": "example.retained", "version_code": 1,
                         "sha256": hashlib.sha256(payload).hexdigest()})
    downloads = root / "artifact-data/source-code-archives"
    sources = root / "artifact-data/source-code"
    downloads.mkdir(parents=True)
    sources.mkdir(parents=True)
    (downloads / filename).write_bytes(b"stale retained archive")
    (downloads / "example.removed_1_src.tar.gz").write_bytes(b"removed archive")
    old_source = sources / "example.removed_1_src.tar.gz"
    old_source.mkdir()
    (old_source / "README").write_text("removed source")
    (sources / "unrelated.txt").write_text("leave unchanged")
    monkeypatch.setattr(fetch, "repo_root", lambda: root)
    monkeypatch.setenv("FLOWARK_RETIRED_DATA_ROOT", str(retired))

    fetch.fetch_source_archives(root, assets, extract=True)

    assert (sources / filename / "README").read_text() == "retained source"
    assert (downloads / filename).read_bytes() == payload
    assert (old_source / "README").read_text() == "removed source"
    assert not (downloads / "example.removed_1_src.tar.gz").exists()
    assert (sources / "unrelated.txt").read_text() == "leave unchanged"
    saved = {p.read_bytes() for p in retired.rglob("*") if p.is_file()}
    assert saved == {b"stale retained archive", b"removed archive"}


def source_package(tmp_path, entries):
    archive_path = tmp_path / "source.tar"
    with tarfile.open(archive_path, "w") as package:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                data = content.encode()
                member.size = len(data)
                package.addfile(member, io.BytesIO(data))
            else:
                if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    member.linkname = content
                package.addfile(member)
    return archive_path


def test_source_normalizes_root_and_preserves_portable_links(tmp_path, fetch, capsys):
    filename = "example_1_src.tar.gz"
    archive = source_package(tmp_path, [
        ("old-root", tarfile.DIRTYPE, ""),
        ("old-root/src", tarfile.DIRTYPE, ""),
        ("old-root/src/Main.java", tarfile.REGTYPE, "source"),
        ("old-root/current", tarfile.SYMTYPE, "src"),
        ("old-root/Main.java", tarfile.LNKTYPE, "old-root/src/Main.java"),
        ("old-root/cache", tarfile.SYMTYPE, "/home/build/cache"),
        ("old-root/external", tarfile.SYMTYPE, "../../outside"),
        ("old-root/pipe", tarfile.FIFOTYPE, ""),
    ])
    source_root = tmp_path / "sources"
    fetch.prepare_source_archive(archive, source_root, filename, "expected-sha")
    target = source_root / filename
    assert not (source_root / "old-root").exists()
    assert (target / "current").is_symlink()
    assert (target / "current/Main.java").read_text() == "source"
    assert (target / "Main.java").stat().st_ino == (target / "src/Main.java").stat().st_ino
    assert all(not (target / name).exists() and not (target / name).is_symlink()
               for name in ("cache", "external", "pipe"))
    marker = target / fetch.SOURCE_EXTRACTION_MARKER
    record = json.loads(marker.read_text())
    assert len(record["skipped_members"]) == 3
    assert "/home/build/cache" not in marker.read_text()
    assert "skipped 3 nonportable members" in capsys.readouterr().out
    # A matching completed extraction reuses the directory without unpacking again.
    (target / "local-notes").write_text("keep")
    fetch.prepare_source_archive(tmp_path / "missing.tar", source_root, filename, "expected-sha")
    assert (target / "local-notes").read_text() == "keep"


def test_source_without_wrapper_directory_is_contained(tmp_path, fetch):
    archive = source_package(tmp_path, [
        ("README", tarfile.REGTYPE, "readme"),
        ("src/Main.java", tarfile.REGTYPE, "source"),
    ])
    fetch.prepare_source_archive(archive, tmp_path / "sources", "canonical", "sha")
    assert (tmp_path / "sources/canonical/README").read_text() == "readme"
    assert (tmp_path / "sources/canonical/src/Main.java").read_text() == "source"


@pytest.mark.parametrize("name", ["../outside", "/outside", "old-root/../../outside"])
def test_source_rejects_unsafe_names_without_installing_partial_directory(tmp_path, fetch, name):
    archive = source_package(tmp_path, [
        ("old-root", tarfile.DIRTYPE, ""),
        ("old-root/README", tarfile.REGTYPE, "source"),
        (name, tarfile.REGTYPE, "unsafe"),
    ])
    sources = tmp_path / "sources"
    with pytest.raises(ValueError, match="unsafe source archive member"):
        fetch.prepare_source_archive(archive, sources, "canonical", "sha")
    assert not (sources / "canonical").exists()
    assert list(sources.iterdir()) == []


def test_source_failed_extraction_does_not_leave_completed_directory(tmp_path, fetch):
    archive = source_package(tmp_path, [
        ("old-root", tarfile.DIRTYPE, ""),
        ("old-root/README", tarfile.REGTYPE, "source"),
        ("old-root/README/child", tarfile.REGTYPE, "conflict"),
    ])
    sources = tmp_path / "sources"
    with pytest.raises(OSError):
        fetch.prepare_source_archive(archive, sources, "canonical", "sha")
    assert list(sources.iterdir()) == []


@pytest.mark.parametrize("identity", [None, {"schema_version": "old"}, {"schema_version": "flowark-portable-source-v1", "filename": "canonical", "sha256": "other-sha"}])
def test_source_preserves_unverified_existing_directory(tmp_path, fetch, identity):
    target = tmp_path / "sources/canonical"
    target.mkdir(parents=True)
    (target / "README").write_text("user data")
    if identity is not None:
        (target / fetch.SOURCE_EXTRACTION_MARKER).write_text(json.dumps(identity))
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(ValueError, match="left unchanged"):
        fetch.prepare_source_archive(tmp_path / "missing.tar", target.parent, target.name, "sha")
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_source_does_not_trust_symlinked_cache_directory(tmp_path, fetch):
    sources = tmp_path / "sources"
    sources.mkdir()
    outside = tmp_path / "user-source"
    outside.mkdir()
    (outside / fetch.SOURCE_EXTRACTION_MARKER).write_text(json.dumps({
        "schema_version": fetch.SOURCE_EXTRACTION_SCHEMA, "filename": "canonical", "sha256": "sha",
    }))
    (sources / "canonical").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="left unchanged"):
        fetch.prepare_source_archive(tmp_path / "missing.tar", sources, "canonical", "sha")
    assert (sources / "canonical").is_symlink()


def test_source_rejects_completion_marker_through_internal_alias(tmp_path, fetch):
    archive = source_package(tmp_path, [
        ("old-root", tarfile.DIRTYPE, ""),
        ("old-root/alias", tarfile.SYMTYPE, "."),
        (f"old-root/alias/{fetch.SOURCE_EXTRACTION_MARKER}", tarfile.REGTYPE, "forged"),
    ])
    with pytest.raises(ValueError, match="reserved completion marker"):
        fetch.prepare_source_archive(archive, tmp_path / "sources", "canonical", "sha")
    assert not (tmp_path / "sources/canonical").exists()


def test_strict_evidence_extraction_still_rejects_absolute_links(tmp_path, fetch):
    archive = source_package(tmp_path, [("cache", tarfile.SYMTYPE, "/home/build/cache")])
    with pytest.raises(tarfile.AbsoluteLinkError):
        fetch.safe_extract_tar_gz(archive, tmp_path / "evidence")


def test_eval_verifier_rejects_unlisted_case_directory(tmp_path, fetch):
    repeat = tmp_path / "mode/case/repeat-01"
    repeat.mkdir(parents=True)
    (repeat / "case_input.json").write_text(json.dumps({"app_name": "example.retained_1", "source_id": "case"}))
    row = {"entry_id": "one", "app_name": "example.retained_1", "source_id": "case", "repeat_dir": "mode/case/repeat-01",
           "paths": {"case_input": "mode/case/repeat-01/case_input.json"}}
    (tmp_path / "results.jsonl").write_text(json.dumps(row) + "\n")
    for name in ("config.json", "manifest.json", "planned_runs.json"):
        (tmp_path / name).write_text(json.dumps({"app_names": ["example.retained_1"]}))
    extra = tmp_path / "orphan/repeat-01"
    extra.mkdir(parents=True)
    (extra / "case_input.json").write_text(json.dumps({"app_name": "example.removed_1"}))
    with pytest.raises(ValueError, match="case files"):
        fetch.verify_eval_apps(tmp_path, {"example.retained_1"})
