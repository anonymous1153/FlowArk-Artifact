# FlowArk Artifact

This repository contains the artifact for the FlowArk paper: the FlowArk implementation,
the Studio interface, benchmark inputs, evaluation logs, release manifests, and setup
scripts. The released data also include the manual audit records for the reported
Relative F1 values. Large archives for evaluation logs, manual audit records, and
Android app source code are distributed through GitHub Releases.

FlowArk Studio is a web interface for inspecting the released evaluation results and
logs, searching individual runs, and launching predefined reproduction presets.

## Quick Start

On macOS or Linux, run the repository-root launcher:

```bash
./run_flowark_artifact.sh
```

The launcher asks what to prepare:

- `View paper results only`: downloads the released evaluation logs, manual audit
  records, and benchmark JSON files, then starts Studio for result inspection.
- `Prepare source code and run experiments`: also downloads and extracts the Android apps
  source code archives from the 171-app corpus used to construct Main49 and Strat15.
  These archives are needed to rerun the benchmark evaluations. This mode requires
  substantially more download time and disk space.

For non-interactive use:

```bash
./run_flowark_artifact.sh --results-only
./run_flowark_artifact.sh --with-source
```

Useful options:

```bash
./run_flowark_artifact.sh --results-only --port 8999
./run_flowark_artifact.sh --with-source --no-start
./run_flowark_artifact.sh --results-only --install-uv
```

The script uses `uv` to create or reuse the Python environment. If `uv` is not installed,
interactive runs ask whether to install it. Non-interactive runs can pass `--install-uv`.
Without that flag, the script prints the installation link and exits before changing
artifact data.

## Repository Layout

- `flowark/`, `flowark_studio/`, `main.py`: FlowArk runtime, evaluation harness, and Studio interface.
- `data/benchmarks/`: benchmark JSON templates for Main49 and Strat15.
- `data/workloads/`: current corpus counts and Main49/Strat15 selection and coverage.
- `data/source-archives-manifest.csv`: Android apps source code archive manifest and SHA-256 checksums.
- `data/source-archives-sha256.txt`: source archive checksum list.
- `data/source-corpus-summary.json`: counts and coverage for the 171-app corpus and its workloads.
- `data/evaluation-archives-manifest.csv`: Studio evaluation log archive manifest and SHA-256 checksums.
- `data/reproduction-archives-manifest.csv`: experiment catalog and offline reproduction archive.
- `data/reproduction-files-manifest.json`: checksums for the files in the reproduction archive.
- `data/release-bundles.json`: release bundle checksums and their archive members.
- `data/manual-audit-archives-manifest.csv`: manual audit archive manifest and SHA-256 checksums.
- `data/manual-audit-archives-sha256.txt`: manual audit archive checksum list.
- `run_flowark_artifact.sh`: macOS/Linux one-click launcher for preparing data and starting Studio.
- `scripts/fetch_artifact_data.py`: download, verification, extraction, and benchmark setup.
- `scripts/start_studio.py`: start Studio with repository-local artifact data.
- `release-assets/`: local staging area for GitHub Release assets. This directory is ignored by Git.
- `artifact-data/`: downloaded and extracted data. This directory is ignored by Git.

The corpus contains **171 apps**, including **150 apps with 5,890 eligible source
occurrences**. Main49 contains 49 apps and 4,649 occurrences, covering **78.93%** of
the corpus's eligible sources. Strat15 contains 15 apps and 1,284 occurrences.
The `main50` filename and preset key are compatibility aliases for Main49. See the
[corpus and workload description](data/workloads/261001-dataset-171-main49.md)
for the coverage denominators and selection rules.

## Manual Data Preparation

The root launcher is the recommended entry point. Use the commands below when preparing
data and starting Studio separately.

To download evaluation logs, manual audit records, and benchmark JSON files:

```bash
uv run python scripts/fetch_artifact_data.py --evaluation-logs --manual-audit-logs --benchmarks
```

To download and extract the 171-app source corpus for rerunning Main49 and Strat15
evaluations:

```bash
uv run python scripts/fetch_artifact_data.py --source-code-archives --extract-source --benchmarks
```

To prepare evaluation logs, manual audit records, benchmarks, and Android apps source
code archives in one command:

```bash
uv run python scripts/fetch_artifact_data.py --all
```

The script verifies SHA-256 checksums before extracting any archive. Evaluation-log
preparation also installs the experiment catalog and offline reproduction data. To
prepare only those shared files, run:

```bash
uv run python scripts/fetch_artifact_data.py --reproduction
```

## Start Studio

The root launcher starts Studio automatically unless `--no-start` is provided. To start
Studio manually after data preparation:

```bash
uv run python scripts/start_studio.py --port 8999
```

The launcher downloads and verifies the data before starting Studio. The manual start
command uses data already prepared on disk.

Studio discovers the published evaluations from `artifact-data/reproduction/catalog.json`
and presents their results as read-only entries. Use the run list, summary, transcript,
knowledge panel, and artifact browser to inspect each task. Knowledge cards use recorded
event snapshots and the selected task's input snapshots. A match without a saved body
remains visible. A missing cost is shown as incomplete.

New evaluations write to
`artifact-data/studio-state/<workspace-id>/evals/executions/<task-id>/` by default.
The `FLOWARK_DATA_ROOT` environment variable can select a different state directory.
Published results remain discoverable from the repository-local data.

## Configure Model Access

To rerun evaluations, provide an Anthropic-compatible or OpenAI-compatible model gateway.
In Studio, the evaluation launch form asks for:

- dataset preset: `Strat15` or `Main49`
- API format: Anthropic-compatible or OpenAI-compatible
- base URL
- API key
- model id

For Studio-launched runs, the API key is passed directly to the evaluation process and
omitted from task parameters and evaluation secret sidecars. For command-line runs, use
environment variables or a local `.env` file:

```bash
ANTHROPIC_BASE_URL=https://your-gateway.example/api/anthropic
ANTHROPIC_AUTH_TOKEN=your-api-key
ANTHROPIC_MODEL=your-model-id
```

For an OpenAI-compatible gateway, use:

```bash
OPENAI_BASE_URL=https://your-gateway.example/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
```

Then run an evaluation with a prepared benchmark JSON:

```bash
uv run python main.py evaluation run \
  --input artifact-data/benchmarks/source-first-v3.2-strat15.json \
  --modes naive \
  --opencode-provider anthropic \
  --opencode-model your-model-id \
  --llm-judge off
```

## Run the GLM-5.3 Experiments

Prepare source code with `./run_flowark_artifact.sh --with-source`, then open the
**New Eval** form. Select a paper experiment, condition, and independent run. Select
**All conditions** and **All three** runs to launch the complete series. Supply a model gateway
that serves `glm-5.3`.

| Paper experiment | Conditions | Tasks per evaluation | Complete series |
| --- | --- | ---: | ---: |
| GLM-5.3: three paired runs | Standard, FlowArk | 1,284 across 15 apps | 6 evaluations |
| Knowledge content control | Standard, FlowArk, Cross-app knowledge | 314 | 9 evaluations |

Each evaluation uses `repeats=1`, a 1,800-second task timeout, and disables repeated
knowledge injection within a session. The form defaults to 12 workers and submits the
series with force-parallel dispatch. Paper presets require the complete task set and
reject task limits or application filters.

For the paired experiment, Standard runs without knowledge and permits concurrent tasks
from the same app. FlowArk serializes tasks within each app, and each of its three
evaluations starts with a fresh knowledge scope.

For knowledge content control, all conditions permit concurrent tasks within each app.
The three conditions share the round's 314-task cohort. Standard uses the knowledge-off
baseline. FlowArk uses the recorded task-start knowledge; Cross-app knowledge applies
the prepared donor knowledge. Each task has an isolated scope. The runtime checks the
prepared inputs and protocol settings before execution.

Studio prepares missing knowledge-control execution inputs from the published fixtures
and extracted source code before submitting the series. To prepare them separately,
run the following command for each round. Use `r2` with `--round 2`, then `r3` with
`--round 3`:

```bash
uv run python -m flowark.experiments.content_control_preparation \
  --output-dir artifact-data/reproduction/knowledge-control/execution/r1 \
  --round 1 \
  --source-root artifact-data/source-code
```

The command discovers the published evaluation directory under `artifact-data/studio-state/`.
Use `--evals-dir` to select it explicitly when more than one copy is available.
Preparation creates a 314-task benchmark, per-task fixtures, and a manifest. Existing
prepared inputs are checked before reuse. This step makes no model calls.

## Recompute the Reported Metrics

The released reproduction data include offline commands for costs, tokens, manual-audit
quality, knowledge coverage, and source-reading statistics. See
[`artifact-data/reproduction/README.md`](artifact-data/reproduction/README.md) after
preparing the data. These calculations use the saved results and need no model gateway.

To reproduce the manual-audit sample after preparing the audit records, run:

```bash
uv run --offline --no-project python -B scripts/sample_manual_audit.py --check
```

The script selects 20% of each app's Strat15 tasks by sorting SHA-256 hashes of
stable task keys with a fixed seed, rounding each app's quota to the nearest
integer with halves rounded up. It checks that the selected 257 tasks match the
published audit records exactly. The [sampling manifest](data/manual-audit-sampling.json)
records the seed, task keys, and per-app population and sample counts.

## Release Assets

The large artifact files are hosted in three GitHub Releases:

- `flowark-evaluation-logs-v1`: two bundles containing 28 evaluation archives and the shared reproduction archive.
- `flowark-manual-audit-v1`: one manual audit archive for the Relative F1 results.
- `flowark-source-archives-v1`: nine bundles containing the source archives for all 171 Android apps.

The three releases contain 12 downloadable assets. The launcher downloads each
needed bundle once, verifies its checksum, and caches its member archives in the
usual data directories. Temporary downloaded bundles are removed after extraction.

See [Release Assets](docs/release-assets.md) for the manifests and download options.
