#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

nf_default_paths

ONLY=""
while (($#)); do
  case "$1" in
    --only)
      ONLY="$2"
      shift 2
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

GENERATORS=(genie gibuu neut nuwro)
if [[ -n "$ONLY" ]]; then
  IFS=',' read -r -a GENERATORS <<< "$ONLY"
fi

for generator in "${GENERATORS[@]}"; do
  script="$SCRIPT_DIR/setup_${generator}.sh"
  [[ -x "$script" ]] || fail "Missing or non-executable setup script: $script"
  log "Running setup for ${generator}"
  "$script"
done

log "Setup scaffold completed"
