from __future__ import annotations

import csv
import hashlib
import importlib
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    fetch = importlib.import_module("fetch_artifact_data")
    root = tmp_path / "artifact"
    data = root / "data"
    data.mkdir(parents=True)
    source = tmp_path / "source/reproduction"
    source.mkdir(parents=True)
    (source / "catalog.json").write_text(json.dumps({"schema_version": "flowark-evidence-catalog-v1", "experiments": [{"eval_root": "paper-example"}]}))
    (source / "recompute.py").write_text("print('ready')\n")
    files = [{"path": path.name, "size_bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(source.iterdir())]
    (data / "reproduction-files-manifest.json").write_text(json.dumps({"schema_version": "flowark-reproduction-files-v1", "files": files}))
    (data / "evaluation-archives-manifest.csv").write_text("eval_root\npaper-example\n")
    assets = tmp_path / "assets"
    assets.mkdir()
    archive = assets / "paper-reproduction.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(source, arcname="reproduction")
    with (data / "reproduction-archives-manifest.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["filename", "archive_root", "release_tag", "sha256"])
        writer.writeheader()
        writer.writerow({"filename": archive.name, "archive_root": "reproduction", "release_tag": "flowark-evaluation-logs-v1", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
    monkeypatch.setattr(fetch.urllib.request, "urlretrieve", lambda *args: pytest.fail("local bundle must not access the network"))
    return fetch, root, assets


def test_extracts_and_reuses_the_shared_bundle_without_replacing_local_state(bundle, monkeypatch):
    fetch, root, assets = bundle
    fetch.fetch_reproduction_data(root, assets)
    target = root / "artifact-data/reproduction"
    assert (target / "recompute.py").read_text() == "print('ready')\n"
    local = target / "knowledge-control/execution/r1/local.json"
    local.parent.mkdir(parents=True)
    local.write_text("local state")
    monkeypatch.setattr(fetch, "download_asset", lambda **args: pytest.fail("verified data must be reused"))
    fetch.fetch_reproduction_data(root, assets)
    assert local.read_text() == "local state"


def test_modified_existing_bundle_fails_without_overwriting_it(bundle):
    fetch, root, assets = bundle
    fetch.fetch_reproduction_data(root, assets)
    target = root / "artifact-data/reproduction/recompute.py"
    target.write_text("changed by user\n")
    with pytest.raises(ValueError, match="incomplete|checksum mismatch"):
        fetch.fetch_reproduction_data(root, assets)
    assert target.read_text() == "changed by user\n"


def test_catalog_must_match_the_evaluation_manifest(bundle):
    fetch, root, assets = bundle
    (root / "data/evaluation-archives-manifest.csv").write_text("eval_root\npaper-other\n")
    with pytest.raises(ValueError, match="catalog differs"):
        fetch.fetch_reproduction_data(root, assets)
    assert not (root / "artifact-data/reproduction").exists()


def test_file_manifest_cannot_escape_the_bundle(bundle):
    fetch, root, assets = bundle
    path = root / "data/reproduction-files-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "../outside"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="file path"):
        fetch.fetch_reproduction_data(root, assets)
    assert not (root / "artifact-data/reproduction").exists()
