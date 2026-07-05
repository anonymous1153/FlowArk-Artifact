#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="127.0.0.1"
PORT="8999"
MODE=""
START_STUDIO=1
SKIP_FETCH=0
LOCAL_ASSETS_DIR=""
INSTALL_UV=0

usage() {
  cat <<'EOF'
FlowArk artifact one-click launcher for macOS and Linux.

Usage:
  ./run_flowark_artifact.sh [options]

Modes:
  --results-only   Download paper evaluation logs and benchmark JSON files, then start Studio.
  --rerun-ready    Also download and extract Android apps source code archives
                   (the Main50/Strat15 benchmark dataset) for rerunning evaluations.

Options:
  --host HOST              Studio host, default: 127.0.0.1
  --port PORT              Studio port, default: 8999
  --local-assets-dir DIR   Use local release assets when present.
  --install-uv             Install uv automatically if it is missing.
  --no-start               Prepare data only; do not start Studio.
  --skip-fetch             Start Studio without fetching or verifying data first.
  -h, --help               Show this help.

If no mode is provided in an interactive terminal, the script asks which mode to use.
In a non-interactive terminal, it defaults to --results-only.
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  if [[ "$INSTALL_UV" -ne 1 && -t 0 ]]; then
    read -r -p "uv is required but was not found. Install uv now? [y/N]: " install_choice
    case "${install_choice:-N}" in
      y|Y|yes|YES) INSTALL_UV=1 ;;
      *) ;;
    esac
  fi

  if [[ "$INSTALL_UV" -ne 1 ]]; then
    die "uv is required. Install uv first or rerun with --install-uv: https://docs.astral.sh/uv/"
  fi

  command -v curl >/dev/null 2>&1 || die "curl is required to install uv automatically"
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installation finished, but uv is still not on PATH"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --results-only)
      MODE="results-only"
      shift
      ;;
    --rerun-ready|--all)
      MODE="rerun-ready"
      shift
      ;;
    --host)
      [[ $# -ge 2 ]] || die "--host requires a value"
      HOST="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port requires a value"
      PORT="$2"
      shift 2
      ;;
    --local-assets-dir)
      [[ $# -ge 2 ]] || die "--local-assets-dir requires a value"
      LOCAL_ASSETS_DIR="$2"
      shift 2
      ;;
    --install-uv)
      INSTALL_UV=1
      shift
      ;;
    --no-start)
      START_STUDIO=0
      shift
      ;;
    --skip-fetch)
      SKIP_FETCH=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  if [[ -t 0 ]]; then
    cat <<'EOF'
Choose artifact preparation mode:
  1) View paper results only
     Downloads public evaluation logs and benchmark JSON files, then starts Studio.
  2) Prepare for rerunning evaluations
     Also downloads and extracts Android apps source code archives
     (the Main50/Strat15 benchmark dataset). This is much larger.
EOF
    read -r -p "Select [1/2, default 1]: " choice
    case "${choice:-1}" in
      1) MODE="results-only" ;;
      2) MODE="rerun-ready" ;;
      *) die "invalid selection: $choice" ;;
    esac
  else
    MODE="results-only"
  fi
fi

ensure_uv

cd "$ROOT_DIR"

fetch_cmd=(uv run python scripts/fetch_artifact_data.py)
run_cmd=(uv run python scripts/run_artifact.py --host "$HOST" --port "$PORT")

if [[ "$SKIP_FETCH" -eq 1 ]]; then
  run_cmd+=(--skip-fetch)
else
  case "$MODE" in
    results-only)
      fetch_cmd+=(--evaluation-logs --benchmarks)
      ;;
    rerun-ready)
      fetch_cmd+=(--evaluation-logs --benchmarks --source-code-archives --extract-source)
      run_cmd+=(--with-source-code-archives --extract-source)
      ;;
    *)
      die "unsupported mode: $MODE"
      ;;
  esac
fi

if [[ -n "$LOCAL_ASSETS_DIR" ]]; then
  fetch_cmd+=(--local-assets-dir "$LOCAL_ASSETS_DIR")
  run_cmd+=(--local-assets-dir "$LOCAL_ASSETS_DIR")
fi

echo "FlowArk artifact root: $ROOT_DIR"
echo "Mode: $MODE"

if [[ "$START_STUDIO" -eq 0 ]]; then
  if [[ "$SKIP_FETCH" -eq 1 ]]; then
    echo "--skip-fetch and --no-start were both provided; nothing to do."
    exit 0
  fi
  echo "Preparing artifact data without starting Studio..."
  "${fetch_cmd[@]}"
  echo "Done. Start Studio later with:"
  echo "  uv run python scripts/start_studio.py --host $HOST --port $PORT"
  exit 0
fi

echo "Preparing data and starting FlowArk Studio at http://$HOST:$PORT"
"${run_cmd[@]}"
