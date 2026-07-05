# Release Assets

## flowark-evaluation-logs-v1

This release contains the public, anonymized Studio evaluation logs used by the FlowArk paper.
Each asset is one compressed paper evaluation root and can be extracted into the repository-local
Studio state directory with `scripts/fetch_artifact_data.py`. Reviewers can use
`uv run python scripts/run_artifact.py` to fetch these logs and start Studio in one command.

Assets:

- `paper-main50-standard-opencode.tar.gz`
- `paper-main50-flowark-enabled-opencode.tar.gz`
- `paper-strat15-glm-4-7-standard-opencode.tar.gz`
- `paper-strat15-glm-4-7-flowark-enabled-opencode.tar.gz`
- `paper-strat15-deepseek-v4-flash-standard-opencode.tar.gz`
- `paper-strat15-deepseek-v4-flash-flowark-enabled-opencode.tar.gz`
- `paper-strat15-minimax-m3-standard-opencode.tar.gz`
- `paper-strat15-minimax-m3-flowark-enabled-opencode.tar.gz`
- `paper-strat15-mem0-enabled-opencode.tar.gz`
- `paper-strat15-analysis-log-rag-baseline.tar.gz`
- `paper-strat15-ablation-m1-generic.tar.gz`
- `paper-strat15-ablation-m2-embedding.tar.gz`
- `paper-strat15-ablation-m3-start-only.tar.gz`

## flowark-source-archives-v1

This release contains the Android apps source code archives (the Main50/Strat15 benchmark dataset)
referenced by the public benchmark JSON files.
The repository tracks `data/source-archives-manifest.csv` and `data/source-archives-sha256.txt`
for file names, package names, sizes, and checksums. The archives themselves are distributed as
GitHub Release assets instead of Git blobs.
