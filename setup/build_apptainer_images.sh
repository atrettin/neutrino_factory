#!/usr/bin/env bash
# Build the project's Apptainer images natively on the cluster from the
# hand-written definition files in setup/apptainer/.
#
# Builds are known to work on the ODSL interactive nodes (odslserv01/02) and to
# FAIL on the Slurm head node (mppui1) — run this on an odslserv node. The SIFs
# land in $NF_IMAGE_ROOT (shared /ptmp), so every node sees them.
#
# Usage:
#   setup/build_apptainer_images.sh [--bootstrap] [--only genie,gibuu]
#                                   [--code-version V] [--jobs N] [--force]
#                                   [--compose-only] [--accept-defaults]
#
#   --bootstrap       Build only nf-base.sif (Python/bootstrap runtime only,
#                     no generator payload composition). Works
#                     before any .env exists; pass NF_IMAGE_ROOT explicitly or
#                     accept the default.
#   --only LIST       Comma-separated generators to build (default: all
#                     buildable: genie gibuu nuwro). After generator builds,
#                     nf-base composition is attempted with all currently
#                     available payload SIFs (partial composition supported).
#   --code-version V  Code version for the selected generator(s); only valid
#                     together with a single-generator --only.
#   --jobs N          Parallel build jobs inside apptainer build (default: 32 —
#                     odslserv nodes have 128 cores but user CPU time is capped
#                     at 1/4; builds also run under nice -n 15).
#   --force           Rebuild SIFs even if they already exist.
#   --compose-only    Recompose nf-base.sif using currently available payload
#                     SIFs only; skip all generator payload builds.
#   --accept-defaults Skip interactive prompts and use default values.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

BOOTSTRAP_ONLY=0
ONLY=""
CODE_VERSION=""
JOBS="${NF_BUILD_JOBS:-32}"
FORCE=0
COMPOSE_ONLY=0
ACCEPT_DEFAULTS=0

prompt_image_root() {
  local default_root="$1" response custom

  if [[ ! -t 0 ]]; then
    fail "Interactive prompt required for bootstrap image root. Re-run with --accept-defaults to skip prompts."
  fi

  log "Bootstrap image root default: $default_root"
  while true; do
    printf 'Use default NF_IMAGE_ROOT [%s]? [Y/n]: ' "$default_root"
    IFS= read -r response
    response="${response:-y}"

    case "$response" in
      y|Y|yes|YES|Yes)
        export NF_IMAGE_ROOT="$default_root"
        return 0
        ;;
      n|N|no|NO|No)
        while true; do
          printf 'Enter NF_IMAGE_ROOT path: '
          IFS= read -r custom
          custom="$(echo "$custom" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
          [[ -n "$custom" ]] || { log "Path cannot be empty."; continue; }
          if [[ "$custom" == ~* ]]; then
            custom="${custom/#\~/$HOME}"
          fi
          export NF_IMAGE_ROOT="$custom"
          return 0
        done
        ;;
      *)
        log "Please answer y or n."
        ;;
    esac
  done
}

persist_image_root() {
  local env_file="$REPO_ROOT/.env" tmp_file

  if [[ ! -f "$env_file" ]]; then
    printf 'NF_IMAGE_ROOT=%s\n' "$NF_IMAGE_ROOT" > "$env_file"
    log "Recorded NF_IMAGE_ROOT in $env_file"
    return 0
  fi

  tmp_file="$(mktemp)"
  awk -v value="$NF_IMAGE_ROOT" '
    BEGIN { written = 0 }
    /^NF_IMAGE_ROOT=/ {
      if (!written) {
        print "NF_IMAGE_ROOT=" value
        written = 1
      }
      next
    }
    { print }
    END {
      if (!written) {
        print "NF_IMAGE_ROOT=" value
      }
    }
  ' "$env_file" > "$tmp_file"
  mv "$tmp_file" "$env_file"
  log "Recorded NF_IMAGE_ROOT in $env_file"
}

while (($#)); do
  case "$1" in
    --bootstrap) BOOTSTRAP_ONLY=1 ;;
    --only) ONLY="$2"; shift ;;
    --code-version) CODE_VERSION="$2"; shift ;;
    --jobs) JOBS="$2"; shift ;;
    --force) FORCE=1 ;;
    --compose-only) COMPOSE_ONLY=1 ;;
    --accept-defaults) ACCEPT_DEFAULTS=1 ;;
    --help|-h)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fail "Unknown argument: $1" ;;
  esac
  shift
done

require_command apptainer
nf_default_paths

if [[ "$BOOTSTRAP_ONLY" -eq 1 && "$ACCEPT_DEFAULTS" -ne 1 ]]; then
  prompt_image_root "$NF_IMAGE_ROOT"
  ensure_dir "$NF_IMAGE_ROOT"
fi

case "$(hostname)" in
  odslserv*) : ;;
  *)
    log "WARNING: host '$(hostname)' is not an odslserv node. Unprivileged"
    log "WARNING: apptainer builds are known to fail on the Slurm head node;"
    log "WARNING: if this build errors out, rerun it on odslserv01/02."
    ;;
esac

export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$NF_IMAGE_ROOT/../apptainer_cache}"
ensure_dir "$APPTAINER_CACHEDIR"
log "Image root:      $NF_IMAGE_ROOT"
log "Apptainer cache: $APPTAINER_CACHEDIR"

# Persist the image root so later invocations (setup wizard, cenv sessions,
# and sbatch scripts) resolve the same location without exporting it by hand.
# The wizard prefills its prompts from .env, so this value carries through.
persist_image_root

# Map an image tag (gen:code_version) to its SIF path, mirroring
# containers.sif_path: non-[A-Za-z0-9._-] characters become '_'.
sif_path_for() {
  local image="$1"
  printf '%s/%s.sif\n' "$NF_IMAGE_ROOT" "$(echo "$image" | sed 's/[^A-Za-z0-9._-]/_/g')"
}

build_def() {
  local sif="$1" def="$2"
  shift 2
  if [[ -f "$sif" && "$FORCE" -ne 1 ]]; then
    log "Exists, skipping (use --force to rebuild): $sif"
    return 0
  fi
  log "Building $sif from $def"
  nice -n 15 apptainer build --force "$@" "$sif" "$def"
}

# ── nf-base bootstrap runtime (always ensured, fast) ─────────────────────────
NF_BASE_SIF="$NF_IMAGE_ROOT/nf-base.sif"
NF_BASE_BOOTSTRAP_SIF="$NF_IMAGE_ROOT/nf-base-bootstrap.sif"
build_def "$NF_BASE_BOOTSTRAP_SIF" "$SCRIPT_DIR/apptainer/nf-base.bootstrap.def"
apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c "import yaml, h5py, numpy" \
  || fail "nf-base-bootstrap.sif failed its smoke test"
log "nf-base-bootstrap.sif OK"

if [[ "$BOOTSTRAP_ONLY" -eq 1 ]]; then
  cp -f "$NF_BASE_BOOTSTRAP_SIF" "$NF_BASE_SIF"
  log "Bootstrap complete."
  log "Next steps (ODSL recommended, host shell):"
  log "  1) Build generator payloads + composed nf-base:"
  log "     bash setup/build_apptainer_images.sh"
  log "  2) Stage GENIE splines:"
  log "     bash setup/download_genie_xsec.sh"
  log "  3) Only after images are complete, create cenv:"
  log "     cenv --create nf-env $NF_BASE_SIF"
  log "     cenv nf-env"
  log "  4) Inside cenv: pip install -e ."
  log "     neutrino-factory setup --pathway apptainer --no-build"
  exit 0
fi

# ── Generator images ──────────────────────────────────────────────────────────
# Catalog queries run inside nf-base (the host Python is too old for the CLI).
nf_cli() {
  apptainer exec "$NF_BASE_BOOTSTRAP_SIF" \
    env PYTHONPATH="$REPO_ROOT/src" python3 -m neutrino_factory.cli "$@"
}

declare -a BUILT=() SKIPPED=()
if [[ "$COMPOSE_ONLY" -ne 1 ]]; then
  GENERATORS=(genie gibuu nuwro)
  if [[ -n "$ONLY" ]]; then
    IFS=',' read -r -a GENERATORS <<< "$ONLY"
  fi
  if [[ -n "$CODE_VERSION" && "${#GENERATORS[@]}" -ne 1 ]]; then
    fail "--code-version requires --only with exactly one generator"
  fi

  declare -A IMAGE_BY_GEN=()
  for gen in "${GENERATORS[@]}"; do
    def="$SCRIPT_DIR/apptainer/${gen}.def"
    [[ -f "$def" ]] || { log "No definition for '$gen' ($def) — skipping"; SKIPPED+=("$gen"); continue; }

    # Resolve the code version and image tag from the catalog.
    row="$(nf_cli list-generators --generator "$gen" --json)"
    cv="$CODE_VERSION"
    if [[ -z "$cv" ]]; then
      cv="$(echo "$row" | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c \
        'import json,sys; print(json.load(sys.stdin)["generators"][0]["code_version"])')"
    fi
    image="$(echo "$row" | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c \
      'import json,sys
cv=sys.argv[1]
rows=[r for r in json.load(sys.stdin)["generators"] if r["code_version"]==cv]
print(rows[0]["image"] if rows else "")' "$cv")"
    [[ -n "$image" ]] || fail "Unknown $gen code_version '$cv' (not in the catalog)"
    IMAGE_BY_GEN["$gen"]="$image"

    # Per-generator build-arg naming (must match the %arguments in the .def).
    case "$gen" in
      genie) args=(--build-arg "GENIE_TAG=$cv") ;;
      nuwro) args=(--build-arg "NUWRO_TAG=$cv") ;;
      gibuu) args=(--build-arg "GIBUU_RELEASE=${cv#release}") ;;
      *) args=() ;;
    esac
    args+=(--build-arg "JOBS=$JOBS")

    sif="$(sif_path_for "$image")"
    build_def "$sif" "$def" "${args[@]}"

    # Smoke test: generator payload binary is present.
    case "$gen" in
      genie) check_cmd="command -v gevgen" ;;
      gibuu) check_cmd="command -v GiBUU.x" ;;
      nuwro) check_cmd="command -v nuwro" ;;
      *) check_cmd="true" ;;
    esac
    if apptainer exec "$sif" bash -c "$check_cmd" >/dev/null 2>&1; then
      log "$sif OK"
      BUILT+=("$gen:$cv -> $sif")
    else
      log "WARNING: $sif failed its smoke test"
      SKIPPED+=("$gen ($sif failed smoke test)")
    fi
  done
else
  log "Compose-only mode: skipping generator payload builds."
fi

log "──────────────────────────────────────────────"
log "Built/verified:"
for entry in "${BUILT[@]:-}"; do [[ -n "$entry" ]] && log "  $entry"; done
if [[ "${#SKIPPED[@]}" -gt 0 ]]; then
  log "Skipped/problems:"
  for entry in "${SKIPPED[@]}"; do log "  $entry"; done
fi

# Compose unified nf-base runtime from generator payload images.
declare -a INCLUDED_GENS=() MISSING_GENS=()

resolve_payload_sif() {
  local generator="$1"
  local row cv image sif

  row="$(nf_cli list-generators --generator "$generator" --json)"
  cv="$(echo "$row" | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c \
    'import json,sys; rows=json.load(sys.stdin)["generators"]; print(rows[0]["code_version"] if rows else "")')"
  [[ -n "$cv" ]] || return 1

  image="$(echo "$row" | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c \
    'import json,sys
rows=json.load(sys.stdin)["generators"]
cv=sys.argv[1]
hit=[r for r in rows if r["code_version"]==cv]
print(hit[0]["image"] if hit else "")' "$cv")"
  [[ -n "$image" ]] || return 1

  sif="$(sif_path_for "$image")"
  [[ -f "$sif" ]] || return 1
  printf '%s\n' "$sif"
}

if GENIE_SIF="$(resolve_payload_sif genie)"; then
  INCLUDED_GENS+=("genie")
else
  GENIE_SIF="$NF_BASE_BOOTSTRAP_SIF"
  MISSING_GENS+=("genie")
fi

if GIBUU_SIF="$(resolve_payload_sif gibuu)"; then
  INCLUDED_GENS+=("gibuu")
else
  GIBUU_SIF="$NF_BASE_BOOTSTRAP_SIF"
  MISSING_GENS+=("gibuu")
fi

if NUWRO_SIF="$(resolve_payload_sif nuwro)"; then
  INCLUDED_GENS+=("nuwro")
else
  NUWRO_SIF="$NF_BASE_BOOTSTRAP_SIF"
  MISSING_GENS+=("nuwro")
fi

if [[ "${#INCLUDED_GENS[@]}" -eq 0 ]]; then
  log "WARNING: No generator payload SIFs available; keeping nf-base as bootstrap runtime only."
  cp -f "$NF_BASE_BOOTSTRAP_SIF" "$NF_BASE_SIF"
else
  tmp_nf_base_def="$(mktemp)"
  sed \
    -e "s|__GENIE_SIF__|$GENIE_SIF|g" \
    -e "s|__GIBUU_SIF__|$GIBUU_SIF|g" \
    -e "s|__NUWRO_SIF__|$NUWRO_SIF|g" \
    "$SCRIPT_DIR/apptainer/nf-base.def" > "$tmp_nf_base_def"
  build_def "$NF_BASE_SIF" "$tmp_nf_base_def"
  rm -f "$tmp_nf_base_def"
  apptainer exec "$NF_BASE_SIF" python3 -c "import yaml, h5py, numpy" \
    || fail "nf-base.sif (composed) failed its smoke test"

  # Verify wrapper presence for included payloads only.
  for gen in "${INCLUDED_GENS[@]}"; do
    case "$gen" in
      genie) apptainer exec "$NF_BASE_SIF" bash -lc "command -v gevgen && command -v gntpc" >/dev/null ;;
      gibuu) apptainer exec "$NF_BASE_SIF" bash -lc "command -v GiBUU.x" >/dev/null ;;
      nuwro) apptainer exec "$NF_BASE_SIF" bash -lc "command -v nuwro" >/dev/null ;;
    esac || fail "nf-base.sif (composed) missing expected wrapper for $gen"
  done

  # NuWro-specific runtime sanity: the wrapper must run NuWro from its install
  # directory so relative data/ lookups resolve, and the key SF table must be
  # present in the composed image.
  if [[ " ${INCLUDED_GENS[*]} " == *" nuwro "* ]]; then
    apptainer exec "$NF_BASE_SIF" bash -lc "\
      test -r /opt/nf/generators/nuwro/nuwro/data/sf/pke_12C_new.dat && \
      grep -q 'cd \"\$prefix/nuwro\"' /usr/local/bin/nuwro && \
      grep -q 'caller_cwd=\"\$(pwd)\"' /usr/local/bin/nuwro\
    " >/dev/null || fail "nf-base.sif (composed) NuWro wrapper/data self-check failed"
  fi

  log "nf-base.sif (composed) OK"
fi

if [[ "${#INCLUDED_GENS[@]}" -gt 0 ]]; then
  log "Composed with generator payloads: ${INCLUDED_GENS[*]}"
fi
if [[ "${#MISSING_GENS[@]}" -gt 0 ]]; then
  log "Composed without generator payloads: ${MISSING_GENS[*]}"
fi

log "Check the catalog in your cenv session with: neutrino-factory list-generators --built"
