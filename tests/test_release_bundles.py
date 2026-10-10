from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def grouped(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    fetch = importlib.import_module("fetch_artifact_data")
    root = tmp_path / "artifact"
    (root / "data").mkdir(parents=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    bodies = {"app_1_src.tar.gz": b"source archive", "paper-example.tar.gz": b"evaluation archive",
              "paper-reproduction.tar.gz": b"reproduction archive"}
    kinds = ["source", "evaluation", "reproduction"]
    members = [{"filename": name, "kind": kind, "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
               for (name, data), kind in zip(bodies.items(), kinds)]
    bundle = {"filename": "example-bundle.tar", "release_tag": "example-v1", "members": members}

    def build(*, corruption=None):
        path = assets / bundle["filename"]
        with tarfile.open(path, "w") as archive:
            for index, (name, data) in enumerate(bodies.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                if index == 0 and corruption == "content":
                    data = b"x" * len(data)
                if index == 0 and corruption == "symlink":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../../outside"
                    entry.size = 0
                if index == 0 and corruption == "path":
                    entry.name = "../outside"
                archive.addfile(entry, io.BytesIO(data))
                if index == 0 and corruption == "duplicate":
                    archive.addfile(entry, io.BytesIO(data))
        bundle.update(size_bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        (root / "data/release-bundles.json").write_text(json.dumps({
            "schema_version": "flowark-release-bundles-v1", "bundles": [bundle]}))
        return path

    build()
    monkeypatch.setattr(fetch, "repo_root", lambda: root)
    monkeypatch.setenv("FLOWARK_RETIRED_DATA_ROOT", str(tmp_path / "retired"))
    return fetch, root, assets, bodies, bundle, build


def request(fetch, root, assets, bundle, index):
    member = bundle["members"][index]
    return fetch.download_asset(filename=member["filename"], tag=bundle["release_tag"],
                                expected_sha256=member["sha256"],
                                dest_dir=root / "artifact-data" / fetch._ARCHIVE_CACHE_DIRS[member["kind"]],
                                local_assets_dir=assets, artifact_root=root)


def test_one_download_populates_all_archive_caches_without_retaining_the_outer_bundle(grouped, monkeypatch):
    fetch, root, assets, bodies, bundle, _ = grouped
    payload = (assets / bundle["filename"]).read_bytes()
    calls = []

    def open_url(url, **kwargs):
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", open_url)
    for index, body in enumerate(bodies.values()):
        assert request(fetch, root, None, bundle, index).read_bytes() == body
    assert calls == [fetch.release_url(bundle["release_tag"], bundle["filename"])]
    assert not list((root / "artifact-data").rglob(bundle["filename"]))
    assert not list((root / "artifact-data/downloads").glob("flowark-bundle-*"))


def test_local_bundle_preserves_cached_archives_and_recovers_a_changed_cache(grouped, monkeypatch):
    fetch, root, assets, bodies, bundle, _ = grouped
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: pytest.fail("local bundle must stay offline"))
    target = request(fetch, root, assets, bundle, 0)
    kept = request(fetch, root, assets, bundle, 1)
    kept_stat = kept.stat()
    target.write_bytes(b"modified cache")
    assert request(fetch, root, assets, bundle, 0).read_bytes() == bodies[target.name]
    assert (kept.stat().st_ino, kept.stat().st_mtime_ns) == (kept_stat.st_ino, kept_stat.st_mtime_ns)
    retired = root.parent / "retired"
    assert [p.read_bytes() for p in retired.iterdir()] == [b"modified cache"]


@pytest.mark.parametrize("corruption", ["content", "symlink", "path", "duplicate"])
def test_invalid_bundle_does_not_install_the_requested_archive(grouped, corruption):
    fetch, root, assets, _, bundle, build = grouped
    build(corruption=corruption)
    with pytest.raises(ValueError, match="checksum|member"):
        request(fetch, root, assets, bundle, 0)
    assert not (root / "artifact-data/source-code-archives/app_1_src.tar.gz").exists()
    assert not (root / "outside").exists()
    assert not list((root / "artifact-data").rglob("*.tmp"))


def test_download_checksum_failure_keeps_the_archive_cache_empty(grouped, monkeypatch):
    fetch, root, _, _, bundle, _ = grouped
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"truncated download"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        request(fetch, root, None, bundle, 0)
    assert not list((root / "artifact-data/source-code-archives").iterdir())
    assert not list((root / "artifact-data/downloads").iterdir())
