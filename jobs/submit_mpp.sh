#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"
PYTHON_BIN="${PYTHON:-python3}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config.yaml> [--submit]" >&2
  exit 1
fi

CONFIG_PATH="$1"
shift
DRY_RUN="--dry-run"

while (($#)); do
  case "$1" in
    --submit)
      DRY_RUN=""
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$DRY_RUN" ]]; then
  echo "Rendering the Slurm script without submission. Use --submit on the cluster to run sbatch."
fi

exec "$PYTHON_BIN" -m neutrino_factory.cli submit \
  --config "$CONFIG_PATH" \
  --executor slurm \
  ${DRY_RUN}
