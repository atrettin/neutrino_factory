#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/setup/lib/common.sh"

nf_default_paths

for generator in genie neut nuwro; do
  install_dir="$NF_SOFTWARE_ROOT/$generator"
  metadata_file="$install_dir/metadata/build_info.txt"

  if [[ -f "$metadata_file" ]]; then
    log "[$generator] metadata present: $metadata_file"
  else
    log "[$generator] metadata missing (run setup/setup_${generator}.sh first)"
  fi
done

GENIE_ROOT="$NF_SOFTWARE_ROOT/install/genie"
GENIE_CURRENT_ENV="$GENIE_ROOT/env.sh"

if [[ -f "$GENIE_CURRENT_ENV" ]]; then
  log "[genie] running smoke test via current environment: $GENIE_CURRENT_ENV"
  # shellcheck disable=SC1090
  source "$GENIE_CURRENT_ENV"
  genie-config --version
  gevgen --help >/dev/null 2>&1 || test $? -eq 1
else
  log "[genie] current runtime environment file missing: $GENIE_CURRENT_ENV"
fi

found_tagged_env=0
for genie_env in "$GENIE_ROOT"/env-*.sh; do
  [[ -f "$genie_env" ]] || continue
  found_tagged_env=1
  log "[genie] running smoke test via tagged environment: $genie_env"
  # shellcheck disable=SC1090
  source "$genie_env"
  genie-config --version
  gevgen --help >/dev/null 2>&1 || test $? -eq 1
done

if [[ "$found_tagged_env" -eq 0 ]]; then
  log "[genie] no tagged env scripts found under: $GENIE_ROOT"
fi

log "Installation validation finished"
