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
- `Prepare for rerunning evaluations`: also downloads and extracts the Android apps
  source code archives (the Main50/Strat15 benchmark dataset) needed to rerun the
  benchmark evaluations. This mode requires substantially more download time and disk
  space.

For non-interactive use:

```bash
./run_flowark_artifact.sh --results-only
./run_flowark_artifact.sh --rerun-ready
```

Useful options:

```bash
./run_flowark_artifact.sh --results-only --port 8999
./run_flowark_artifact.sh --rerun-ready --no-start
./run_flowark_artifact.sh --results-only --install-uv
```

The script uses `uv` to create or reuse the Python environment. If `uv` is not installed,
interactive runs ask whether to install it. Non-interactive runs can pass `--install-uv`.
Without that flag, the script prints the installation link and exits before changing
artifact data.

## Repository Layout

- `flowark/`, `flowark_studio/`, `main.py`: FlowArk runtime, evaluation harness, and Studio interface.
- `data/benchmarks/`: benchmark JSON templates for Main50 and Strat15.
- `data/workloads/`: workload documentation for Main50 and Strat15.
- `data/source-archives-manifest.csv`: Android apps source code archive manifest and SHA-256 checksums.
- `data/evaluation-archives-manifest.csv`: Studio evaluation log archive manifest and SHA-256 checksums.
- `data/manual-audit-archives-manifest.csv`: manual audit archive manifest and SHA-256 checksums.
- `data/manual-audit-archives-sha256.txt`: manual audit archive checksum list.
- `run_flowark_artifact.sh`: macOS/Linux one-click launcher for preparing data and starting Studio.
- `scripts/fetch_artifact_data.py`: download, verification, extraction, and benchmark setup.
- `scripts/start_studio.py`: start Studio with repository-local artifact data.
- `release-assets/`: local staging area for GitHub Release assets. This directory is ignored by Git.
- `artifact-data/`: downloaded and extracted data. This directory is ignored by Git.

## Manual Data Preparation

The root launcher is the recommended entry point. Use the commands below when preparing
data and starting Studio separately.

To download evaluation logs, manual audit records, and benchmark JSON files:

```bash
uv run python scripts/fetch_artifact_data.py --evaluation-logs --manual-audit-logs --benchmarks
```

To download and extract all Android apps source code archives (the Main50/Strat15
benchmark dataset) for rerunning evaluations:

```bash
uv run python scripts/fetch_artifact_data.py --source-code-archives --extract-source --benchmarks
```

To prepare evaluation logs, manual audit records, benchmarks, and Android apps source
code archives in one command:

```bash
uv run python scripts/fetch_artifact_data.py --all
```

The script verifies SHA256 checksums before extracting any archive.

## Start Studio

The root launcher starts Studio automatically unless `--no-start` is provided. To start
Studio manually after data preparation:

```bash
uv run python scripts/start_studio.py --port 8999
```

Studio starts after evaluation logs and manual audit records are verified and extracted.
On the first run this preparation step can take several minutes; later runs reuse the
extracted data.

Studio uses `artifact-data/studio-state/` as its data root and keeps evaluation logs
under that repository-local directory.

## Configure Model Access

To rerun evaluations, provide an Anthropic-compatible or OpenAI-compatible model gateway.
In Studio, the evaluation launch form asks for:

- dataset preset: `Strat15` or `Main50`
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

## Release Assets

The large artifact files are hosted in three GitHub Releases:

- `flowark-evaluation-logs-v1`: compressed Studio evaluation roots and logs.
- `flowark-manual-audit-v1`: manual audit records for the Relative F1 results.
- `flowark-source-archives-v1`: Android apps source code archives (the Main50/Strat15 benchmark dataset).

See `docs/release-assets.md` for the expected release asset names.
