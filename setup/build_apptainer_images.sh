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
# The catalog is the single source of truth: buildable generators, their code
# versions, image tags, and per-generator build args all come from
# `list-generators --json` — this script hard-codes NO generator names.
nf_cli() {
  apptainer exec "$NF_BASE_BOOTSTRAP_SIF" \
    env PYTHONPATH="$REPO_ROOT/src" python3 -m neutrino_factory.cli "$@"
}

# Emit one TSV row per buildable (generator, code_version):
#   gen \t code_version \t image \t build_arg_name \t build_arg_value
# Optional $1 restricts to a comma-separated generator list.
catalog_rows() {
  local only="${1:-}"
  nf_cli list-generators --json \
    | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c '
import json, sys
only = [g for g in sys.argv[1].split(",") if g] if len(sys.argv) > 1 else []
for r in json.load(sys.stdin)["generators"]:
    if not r.get("buildable"):
        continue
    if only and r["generator"] not in only:
        continue
    ba = r.get("build_arg") or {}
    print("\t".join([r["generator"], r["code_version"], r["image"],
                     ba.get("name", ""), ba.get("value", "")]))
' "$only"
}

# Read a field from a payload SIF's descriptor. json_field <sif> <path> <expr(d)>.
json_field() {
  apptainer exec "$1" cat "$2" 2>/dev/null \
    | apptainer exec "$NF_BASE_BOOTSTRAP_SIF" python3 -c \
        "import json,sys; d=json.load(sys.stdin); print($3)" 2>/dev/null
}

tag_safe() { printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/_/g'; }

# Verify a freshly built payload SIF against its own descriptor: every declared
# binary wrapper must be present and executable.
verify_payload_sif() {
  local sif="$1" gen="$2" cv="$3" cv_safe desc bins b
  cv_safe="$(tag_safe "$cv")"
  desc="/opt/nf/generators/$gen/$cv_safe/nf-payload.json"
  bins="$(json_field "$sif" "$desc" '" ".join(d["binaries"])')" || return 1
  [[ -n "$bins" ]] || return 1
  for b in $bins; do
    apptainer exec "$sif" test -x "/opt/nf/generators/$gen/$cv_safe/bin/$b" || return 1
  done
  return 0
}

declare -a BUILT=() SKIPPED=()
if [[ "$COMPOSE_ONLY" -ne 1 ]]; then
  if [[ -n "$CODE_VERSION" ]]; then
    [[ -n "$ONLY" && "$ONLY" != *,* ]] \
      || fail "--code-version requires --only with exactly one generator"
  fi

  while IFS=$'\t' read -r gen cv image ba_name ba_value; do
    [[ -n "$gen" ]] || continue
    [[ -z "$CODE_VERSION" || "$cv" == "$CODE_VERSION" ]] || continue
    def="$SCRIPT_DIR/apptainer/${gen}.def"
    [[ -f "$def" ]] || { log "No definition for '$gen' ($def) — skipping"; SKIPPED+=("$gen"); continue; }

    args=()
    [[ -n "$ba_name" ]] && args+=(--build-arg "$ba_name=$ba_value")
    args+=(--build-arg "JOBS=$JOBS")

    sif="$(sif_path_for "$image")"
    build_def "$sif" "$def" "${args[@]}"

    if verify_payload_sif "$sif" "$gen" "$cv"; then
      log "$sif OK"
      BUILT+=("$gen:$cv -> $sif")
    else
      log "WARNING: $sif failed its smoke test"
      SKIPPED+=("$gen:$cv ($sif failed smoke test)")
    fi
  done < <(catalog_rows "$ONLY")
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

# ── Compose unified nf-base runtime from all built payload SIFs ────────────────
# Auto-discovery: every catalogued (generator, code_version) whose payload SIF
# exists is composed in — this is how multiple versions of one generator end up
# side by side. Adding a version = build its payload SIF, recompose.
declare -a PAYLOADS=()  # entries: "sif|gen|cv"
while IFS=$'\t' read -r gen cv image ba_name ba_value; do
  [[ -n "$gen" ]] || continue
  sif="$(sif_path_for "$image")"
  [[ -f "$sif" ]] || continue
  PAYLOADS+=("$sif|$gen|$cv")
done < <(catalog_rows)

if [[ "${#PAYLOADS[@]}" -eq 0 ]]; then
  log "WARNING: No generator payload SIFs available; keeping nf-base as bootstrap runtime only."
  cp -f "$NF_BASE_BOOTSTRAP_SIF" "$NF_BASE_SIF"
else
  # Generate the variable-length head (Apptainer defs have no loop): one
  # localimage stage per payload, then the python:3.13-slim final stage, then one
  # generic %files copy per payload; append the static tail (nf-base.def).
  tmp_nf_base_def="$(mktemp)"
  idx=0
  for entry in "${PAYLOADS[@]}"; do
    IFS='|' read -r sif gen cv <<< "$entry"
    {
      printf 'Bootstrap: localimage\n'
      printf 'From: %s\n' "$sif"
      printf 'Stage: payload_%s\n\n' "$idx"
    } >> "$tmp_nf_base_def"
    idx=$((idx + 1))
  done
  {
    printf 'Bootstrap: docker\n'
    printf 'From: python:3.13-slim\n'
    printf 'Stage: final\n\n'
  } >> "$tmp_nf_base_def"
  idx=0
  for entry in "${PAYLOADS[@]}"; do
    IFS='|' read -r sif gen cv <<< "$entry"
    cv_safe="$(tag_safe "$cv")"
    {
      printf '%%files from payload_%s\n' "$idx"
      printf '    /opt/nf/generators/%s/%s /opt/nf/generators/%s/%s\n\n' \
        "$gen" "$cv_safe" "$gen" "$cv_safe"
    } >> "$tmp_nf_base_def"
    idx=$((idx + 1))
  done
  cat "$SCRIPT_DIR/apptainer/nf-base.def" >> "$tmp_nf_base_def"

  build_def "$NF_BASE_SIF" "$tmp_nf_base_def"
  rm -f "$tmp_nf_base_def"

  apptainer exec "$NF_BASE_SIF" python3 -c "import yaml, h5py, numpy" \
    || fail "nf-base.sif (composed) failed its smoke test"
  apptainer exec "$NF_BASE_SIF" command -v nf-run >/dev/null \
    || fail "nf-base.sif (composed) is missing the nf-run dispatcher"

  # Generic, descriptor-driven verification (no per-generator knowledge): every
  # payload's declared binaries and smoke_paths must exist in the composed image,
  # and each generator's default binary must be on $PATH (default symlink).
  declare -A DEFAULT_SEEN=()
  for entry in "${PAYLOADS[@]}"; do
    IFS='|' read -r sif gen cv <<< "$entry"
    cv_safe="$(tag_safe "$cv")"
    root="/opt/nf/generators/$gen/$cv_safe"
    desc="$root/nf-payload.json"

    bins="$(json_field "$NF_BASE_SIF" "$desc" '" ".join(d["binaries"])')"
    [[ -n "$bins" ]] || fail "nf-base.sif (composed) missing descriptor for $gen:$cv"
    for b in $bins; do
      apptainer exec "$NF_BASE_SIF" test -x "$root/bin/$b" \
        || fail "nf-base.sif (composed) missing wrapper $root/bin/$b"
    done

    paths="$(json_field "$NF_BASE_SIF" "$desc" '" ".join(d.get("smoke_paths", []))')"
    for p in $paths; do
      apptainer exec "$NF_BASE_SIF" test -e "$root/$p" \
        || fail "nf-base.sif (composed) missing smoke path $root/$p"
    done

    if [[ -z "${DEFAULT_SEEN[$gen]:-}" ]]; then
      default_bin="$(json_field "$NF_BASE_SIF" "$desc" 'd.get("default_binary","")')"
      if [[ -n "$default_bin" ]]; then
        apptainer exec "$NF_BASE_SIF" command -v "$default_bin" >/dev/null \
          || fail "nf-base.sif (composed) missing default symlink for $gen ($default_bin)"
      fi
      DEFAULT_SEEN[$gen]=1
    fi
  done

  log "nf-base.sif (composed) OK"
  log "Composed payloads:"
  for entry in "${PAYLOADS[@]}"; do
    IFS='|' read -r sif gen cv <<< "$entry"
    log "  $gen:$cv"
  done
fi

log "Check the catalog in your cenv session with: neutrino-factory list-generators --built"
