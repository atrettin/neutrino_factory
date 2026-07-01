#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"
PYTHON_BIN="${PYTHON:-python3}"

CONFIG_PATH="${1:-}"
MANIFEST_PATH="${2:-}"
TASK_INDEX="${3:-${SLURM_ARRAY_TASK_ID:-0}}"

if [[ -z "$CONFIG_PATH" || -z "$MANIFEST_PATH" ]]; then
  echo "Usage: $0 <config.yaml> <manifest.json> [task-index]" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m neutrino_factory.cli run-task \
  --config "$CONFIG_PATH" \
  --manifest "$MANIFEST_PATH" \
  --task-index "$TASK_INDEX" \
  --execution-mode "${NF_EXECUTION_MODE:-slurm}"
