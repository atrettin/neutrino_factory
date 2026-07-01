#!/usr/bin/env bash
# Download precomputed GENIE cross-section spline XML files from FNAL SciSoft and
# stage them where the GENIE adapter expects them.
#
# Computing xsec splines on the fly is one of the most expensive steps of event
# generation, and flux-driven (-f spectrum) runs require precomputed total-xsec
# splines. FNAL publishes ready-made splines per code version + tune at
#   https://scisoft.fnal.gov/scisoft/packages/genie_xsec/<upsver>/
#
# For each catalogued tune this script downloads the matching tarball, extracts
# only the master spline (gxspl-NUsmall.xml) out of its deep directory structure,
# and stages it as:
#   <software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml
#
# Usage: setup/download_genie_xsec.sh [--code-version R-3_06_00] [--tune NAME]...
#                                     [--software-root DIR] [--force]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/download_genie_xsec.sh [OPTIONS]

Download GENIE cross-section spline XML files from FNAL SciSoft and stage them at
<software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml.

Options:
  --code-version V   GENIE code version (default: R-3_06_00). Must be catalogued.
  --tune NAME        Tune to download (repeatable). Default: all catalogued tunes
                     for the code version.
  --software-root D  Root to stage into (default: $NF_SOFTWARE_ROOT or ./software).
  --force            Re-download and re-stage even if xsecs.xml already exists.
  --help             Show this help.

Tunes not published on SciSoft (HTTP 404) are warned about and skipped.
EOF
}

CODE_VERSION="R-3_06_00"
SOFTWARE_ROOT="${NF_SOFTWARE_ROOT:-$PWD/software}"
FORCE=0
TUNES=()
while (($#)); do
  case "$1" in
    --code-version|--tag)
      [[ $# -ge 2 ]] || fail "$1 requires an argument"
      CODE_VERSION="$2"; shift 2 ;;
    --tune)
      [[ $# -ge 2 ]] || fail "--tune requires an argument"
      TUNES+=("$2"); shift 2 ;;
    --software-root)
      [[ $# -ge 2 ]] || fail "--software-root requires an argument"
      SOFTWARE_ROOT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

require_command curl
require_command tar
nf_validate_code_version genie "$CODE_VERSION"

# If no tunes were given, download every catalogued tune for this code version.
if ((${#TUNES[@]} == 0)); then
  read -r -a TUNES <<< "$(nf_config_versions genie "$CODE_VERSION")"
  ((${#TUNES[@]} > 0)) || fail "No catalogued tunes for genie $CODE_VERSION"
fi

# Derive SciSoft naming from the code version: R-3_06_00 -> ups v3_06_00, dotted 3.06.00.
VER="${CODE_VERSION#R-}"
UPSVER="v${VER}"
DOTVER="${VER//_/.}"
BASE_URL="https://scisoft.fnal.gov/scisoft/packages/genie_xsec/${UPSVER}"

TAGSAFE="$(nf_tag_safe "$CODE_VERSION")"
XSEC_DIR="${SOFTWARE_ROOT}/genie/genie_xsec/${TAGSAFE}"
CACHE_DIR="${SOFTWARE_ROOT}/genie/download/${TAGSAFE}"
ensure_dir "$XSEC_DIR"
ensure_dir "$CACHE_DIR"

staged=()
skipped=()
for tune in "${TUNES[@]}"; do
  dest="${XSEC_DIR}/${tune}/xsecs.xml"
  if [[ -f "$dest" && "$FORCE" -ne 1 ]]; then
    log "Already staged: ${dest} (use --force to re-download)"
    staged+=("$tune")
    continue
  fi

  tunekey="${tune//_/}"   # underscores stripped, case preserved: G18_10a_02_11a -> G1810a0211a
  filename="genie_xsec-${DOTVER}-noarch-${tunekey}-k250-e1000.tar.bz2"
  url="${BASE_URL}/${filename}"
  cache="${CACHE_DIR}/${filename}"
  member="genie_xsec/${UPSVER}/NULL/${tunekey}-k250-e1000/data/gxspl-NUsmall.xml"

  if [[ ! -f "$cache" || "$FORCE" -eq 1 ]]; then
    log "Downloading ${url}"
    if ! curl -fL --retry 3 -o "${cache}.part" "$url"; then
      log "WARNING: could not download ${filename} (tune '${tune}' not published?); skipping"
      rm -f "${cache}.part"
      skipped+=("$tune")
      continue
    fi
    mv "${cache}.part" "$cache"
  else
    log "Using cached ${cache}"
  fi

  log "Extracting ${member} -> ${dest}"
  ensure_dir "${XSEC_DIR}/${tune}"
  if ! tar -xjOf "$cache" "$member" > "${dest}.part" 2>/dev/null; then
    log "WARNING: ${member} not found in ${filename}; skipping tune '${tune}'"
    rm -f "${dest}.part"
    skipped+=("$tune")
    continue
  fi
  # Sanity-check the extracted file is the XML spline, not an error page.
  if [[ "$(head -c 5 "${dest}.part")" != "<?xml" ]]; then
    log "WARNING: extracted file for '${tune}' does not look like XML; skipping"
    rm -f "${dest}.part"
    skipped+=("$tune")
    continue
  fi
  mv "${dest}.part" "$dest"
  log "Staged ${dest}"
  staged+=("$tune")
done

write_metadata "${SOFTWARE_ROOT}/genie/metadata/${TAGSAFE}" \
  "genie_xsec_code_version=${CODE_VERSION}" \
  "genie_xsec_staged=${staged[*]:-}" \
  "genie_xsec_skipped=${skipped[*]:-}"

log "Done. Staged tunes: ${staged[*]:-none}. Skipped: ${skipped[*]:-none}."
