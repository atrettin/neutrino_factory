#!/usr/bin/env bash
set -euo pipefail

nf_timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  printf '[%s] %s\n' "$(nf_timestamp)" "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

ensure_dir() {
  mkdir -p "$1"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

# Load KEY=value pairs from the repo-root .env (written by `neutrino-factory
# setup`) for any variable not already set — the real environment always wins.
nf_load_env_file() {
  local script_dir repo_root env_file line key value
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "$script_dir/../.." && pwd)"
  env_file="$repo_root/.env"
  [[ -f "$env_file" ]] || return 0
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]%%#*}"
    value="$(echo "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^['\"]//" -e "s/['\"]$//")"
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < "$env_file"
}

nf_default_paths() {
  nf_load_env_file
  export NF_SOFTWARE_ROOT="${NF_SOFTWARE_ROOT:-$PWD/software}"
  export NF_OUTPUT_ROOT="${NF_OUTPUT_ROOT:-$PWD/output}"
  export NF_WORK_ROOT="${NF_WORK_ROOT:-$PWD/work}"
  export NF_IMAGE_ROOT="${NF_IMAGE_ROOT:-$NF_SOFTWARE_ROOT/images}"

  ensure_dir "$NF_SOFTWARE_ROOT"
  ensure_dir "$NF_OUTPUT_ROOT"
  ensure_dir "$NF_WORK_ROOT"
  ensure_dir "$NF_IMAGE_ROOT"
}

# --- Generator catalog access -------------------------------------------------
# The neutrino-factory catalog (src/neutrino_factory/catalog.py) is the single
# source of truth for buildable code versions and their compatible config
# versions. These helpers expose it to the build scripts via the CLI, which is
# installed on build hosts (see ENVIRONMENT.md).

nf_require_cli() {
  command -v neutrino-factory >/dev/null 2>&1 || fail \
    "neutrino-factory CLI not found. Install the package first: pip install -e ."
}

# Print a human-readable table of a generator's buildable code/config versions.
nf_list_versions() {
  local generator="$1"
  nf_require_cli
  neutrino-factory list-generators --generator "$generator"
}

# Echo the space-separated code versions catalogued for a generator.
nf_code_versions() {
  local generator="$1"
  nf_require_cli
  neutrino-factory list-generators --generator "$generator" --json \
    | python3 -c 'import json,sys; print(" ".join(r["code_version"] for r in json.load(sys.stdin)["generators"]))'
}

# Echo the filesystem-safe directory name for a code_version (catalog._tag_safe).
nf_tag_safe() {
  nf_require_cli
  python3 -c 'import sys
from neutrino_factory import catalog
print(catalog._tag_safe(sys.argv[1]))' "$1"
}

# Echo the Docker image tag catalogued for a generator + code version, or empty.
nf_catalog_image() {
  local generator="$1" code_version="$2"
  nf_require_cli
  neutrino-factory list-generators --generator "$generator" --json \
    | python3 -c 'import json,sys
cv=sys.argv[1]
for r in json.load(sys.stdin)["generators"]:
    if r["code_version"]==cv:
        print(r["image"] or "")
        break' "$code_version"
}

# Fail unless code_version is catalogued for the generator.
nf_validate_code_version() {
  local generator="$1" code_version="$2"
  local known
  known="$(nf_code_versions "$generator")"
  for candidate in $known; do
    [[ "$candidate" == "$code_version" ]] && return 0
  done
  fail "Unknown $generator code_version '$code_version'. Catalogued: $known"
}

write_metadata() {
  local target_dir="$1"
  shift
  ensure_dir "$target_dir"
  {
    printf 'timestamp=%s\n' "$(nf_timestamp)"
    printf 'host=%s\n' "$(hostname)"
    while (($#)); do
      printf '%s\n' "$1"
      shift
    done
  } > "$target_dir/build_info.txt"
}
