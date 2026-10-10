# Release Assets

Large data files are distributed as 12 assets in the three releases below. Each
bundle is smaller than 1.9 GB. The downloader uses the bundle and archive manifests
to verify SHA-256 checksums before preparing the data.

| Release | Assets | Contents |
| --- | ---: | --- |
| `flowark-evaluation-logs-v1` | 2 | 28 evaluation archives and the shared experiment catalog, reproduction scripts, inputs, and reference results. |
| `flowark-manual-audit-v1` | 1 | Reviewer records, accepted records, and aggregate quality metrics. |
| `flowark-source-archives-v1` | 9 | Source archives for all 171 Android apps used to construct the workloads. |

The [bundle manifest](../data/release-bundles.json) lists each downloadable bundle
and its members. The individual archive manifests retain the filenames and
checksums for [evaluations](../data/evaluation-archives-manifest.csv),
[reproduction data](../data/reproduction-archives-manifest.csv),
[source code](../data/source-archives-manifest.csv), and
[manual audit records](../data/manual-audit-archives-manifest.csv).

The downloader installs all members of a fetched bundle into their archive caches,
then removes the temporary downloaded bundle. Later requests reuse those members.
Reading the paper results downloads only the evidence and manual audit releases;
the source bundles are needed when preparing new evaluations.

## Download and Inspect

From the repository root, download the published results, audit records, and benchmark
inputs, then start Studio:

```bash
./run_flowark_artifact.sh --results-only
```

To also download and extract the source corpus for new evaluations:

```bash
./run_flowark_artifact.sh --with-source
```

Add `--no-start` to prepare the data without starting Studio. The [repository
README](../README.md) describes model access, the experiment presets, and offline
metric recomputation.

## Use Local Archives

The downloader checks `release-assets/` for matching archives before fetching them from
GitHub. To use another local archive directory:

```bash
uv run python scripts/fetch_artifact_data.py \
  --evaluation-logs --manual-audit-logs --benchmarks \
  --local-assets-dir /path/to/archives
```

The local directory can contain the release bundles or individual member archives.
Local archives are subject to the same manifest checks. Extracted evaluations are
validated and reused when their archive identity matches the manifest. An inconsistent
or unverified existing evaluation stops the download command; a separate
`FLOWARK_DATA_ROOT` selects a fresh destination.
Replaced download files are moved to the platform trash directory by default.
`FLOWARK_RETIRED_DATA_ROOT` can select another retention directory.

## Source Corpus

The source manifest contains **171 Android apps** with one source version per app.
Of these, 150 apps contain **5,890 eligible source occurrences**. Main49 contains
4,649 occurrences (**78.93%**); Strat15 contains 15 apps and 1,284 occurrences.

The [source checksum list](../data/source-archives-sha256.txt),
[corpus summary](../data/source-corpus-summary.json), and
[workload description](../data/workloads/261001-dataset-171-main49.md) provide archive
checksums, coverage denominators, and workload selection details.

Source preparation places each app under its archive filename, matching the
benchmark source paths. It preserves regular files and internal links, and reports
links outside the app directory and special build entries that cannot be extracted
portably. Extraction completes in a temporary directory before installation.
An existing source directory is reused only when its completion marker matches the
archive. An unverified directory is left in place; use a separate checkout to
prepare a fresh source corpus.

The manual-audit [sampling script](../scripts/sample_manual_audit.py) and
[sampling manifest](../data/manual-audit-sampling.json) are included in the
repository. They reproduce the 257-task sample and its per-app counts from the
Strat15 benchmark and check it against the released audit records.

The individual manifests list 28 evaluation archives, one reproduction archive,
171 source archives, and one manual-audit archive. They are delivered through the
12 release assets above. The reproduction archive's
[file manifest](../data/reproduction-files-manifest.json) verifies its extracted files.
