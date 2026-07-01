#!/usr/bin/env bash
# Build the GENIE Docker image for the given tag.
# Usage: setup/setup_genie.sh [R-3_06_00]
#        setup/setup_genie.sh --tag R-3_06_00
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/setup_genie.sh [CODE_VERSION]
       setup/setup_genie.sh --code-version CODE_VERSION

Build a Docker image for the given GENIE code_version (default: R-3_06_00).
CODE_VERSION must be catalogued (see: neutrino-factory list-generators --generator genie).
The image tag is taken from the catalog and targets linux/amd64 so it runs on
Apple Silicon via Rosetta 2 as well as x86_64 hosts.

Options:
  --code-version V  GENIE code version to build (git tag / release)
  --jobs N          Parallel make jobs inside the image (default: 4)
  --download-xsec   After building, download precomputed cross-section splines
                    for all catalogued tunes of this code version and stage them
                    (see setup/download_genie_xsec.sh). ~428 MB per tune.
  --list-versions   Print catalogued GENIE code/config versions and exit
  --help            Show this help
EOF
}

CODE_VERSION=""
JOBS="${NF_BUILD_JOBS:-4}"
DOWNLOAD_XSEC=0
while (($#)); do
  case "$1" in
    --code-version|--tag)
      [[ $# -ge 2 ]] || { echo "$1 requires an argument" >&2; exit 1; }
      CODE_VERSION="$2"; shift 2 ;;
    --jobs)
      [[ $# -ge 2 ]] || { echo "--jobs requires an argument" >&2; exit 1; }
      JOBS="$2"; shift 2 ;;
    --download-xsec) DOWNLOAD_XSEC=1; shift ;;
    --list-versions) nf_list_versions genie; exit 0 ;;
    --help|-h) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) [[ -z "$CODE_VERSION" ]] || { echo "GENIE code_version provided more than once" >&2; exit 1; }
       CODE_VERSION="$1"; shift ;;
  esac
done

CODE_VERSION="${CODE_VERSION:-R-3_06_00}"
nf_validate_code_version genie "$CODE_VERSION"
IMAGE_NAME="$(nf_catalog_image genie "$CODE_VERSION")"
[[ -n "$IMAGE_NAME" ]] || fail "No image catalogued for genie code_version '$CODE_VERSION'"

require_command docker

echo "Building Docker image ${IMAGE_NAME} (this takes ~45-90 minutes)..."
docker build \
  --platform linux/amd64 \
  --build-arg "GENIE_TAG=${CODE_VERSION}" \
  --build-arg "JOBS=${JOBS}" \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/Dockerfile.genie" \
  "${SCRIPT_DIR}"

echo ""
echo "Image built: ${IMAGE_NAME}"
echo ""

if [[ "$DOWNLOAD_XSEC" -eq 1 ]]; then
  echo "Downloading precomputed cross-section splines for ${CODE_VERSION}..."
  "$SCRIPT_DIR/download_genie_xsec.sh" --code-version "$CODE_VERSION"
  echo ""
fi

echo "Quick verification (100 CCQE events on carbon, splines computed on the fly,"
echo "no external xsec files needed):"
echo "  docker run --platform linux/amd64 --rm \\"
echo "    -v \"\$PWD\":/work -w /work \\"
echo "    ${IMAGE_NAME} \\"
echo "    gevgen -n 100 -p 14 -t 1000060120 -e 0.5,5.0 \\"
echo "      --tune G18_10a_02_11a --event-generator-list CCQE \\"
echo "      --seed 12345 -o test_events.root"
echo ""
echo "Note: the default (MEC-inclusive) event list currently crashes under"
echo "amd64 emulation; restrict with --event-generator-list CCQE. For production,"
echo "supply precomputed splines via --cross-sections instead of on-the-fly."
