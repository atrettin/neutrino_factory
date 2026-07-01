#!/usr/bin/env bash
# Build the GiBUU Docker image for the given release.
# Usage: setup/setup_gibuu.sh [release2025]
#        setup/setup_gibuu.sh --code-version release2025
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/setup_gibuu.sh [CODE_VERSION]
       setup/setup_gibuu.sh --code-version CODE_VERSION

Build a Docker image for the given GiBUU code_version (default: release2025).
CODE_VERSION must be catalogued (see: neutrino-factory list-generators --generator gibuu).
GiBUU is downloaded as HEPForge release tarballs (NOT the stale GitHub mirror)
and built with ROOT event output enabled via the RootTuple library.
The image tag is taken from the catalog and targets linux/amd64 so it runs on
Apple Silicon via Rosetta 2 as well as x86_64 hosts.

Options:
  --code-version V  GiBUU release to build (e.g. release2025)
  --jobs N          Parallel make jobs inside the image (default: 4)
  --list-versions   Print catalogued GiBUU code/config versions and exit
  --help            Show this help
EOF
}

CODE_VERSION=""
JOBS="${NF_BUILD_JOBS:-4}"
while (($#)); do
  case "$1" in
    --code-version|--tag)
      [[ $# -ge 2 ]] || { echo "$1 requires an argument" >&2; exit 1; }
      CODE_VERSION="$2"; shift 2 ;;
    --jobs)
      [[ $# -ge 2 ]] || { echo "--jobs requires an argument" >&2; exit 1; }
      JOBS="$2"; shift 2 ;;
    --list-versions) nf_list_versions gibuu; exit 0 ;;
    --help|-h) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) [[ -z "$CODE_VERSION" ]] || { echo "GiBUU code_version provided more than once" >&2; exit 1; }
       CODE_VERSION="$1"; shift ;;
  esac
done

CODE_VERSION="${CODE_VERSION:-release2025}"
nf_validate_code_version gibuu "$CODE_VERSION"
IMAGE_NAME="$(nf_catalog_image gibuu "$CODE_VERSION")"
[[ -n "$IMAGE_NAME" ]] || fail "No image catalogued for gibuu code_version '$CODE_VERSION'"

# The Dockerfile keys its tarball downloads on the bare release year (e.g. 2025).
GIBUU_RELEASE="${CODE_VERSION#release}"

require_command docker

echo "Building Docker image ${IMAGE_NAME} (this takes ~20-40 minutes)..."
docker build \
  --platform linux/amd64 \
  --build-arg "GIBUU_RELEASE=${GIBUU_RELEASE}" \
  --build-arg "JOBS=${JOBS}" \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/Dockerfile.gibuu" \
  "${SCRIPT_DIR}"

echo ""
echo "Image built: ${IMAGE_NAME}"
echo ""
echo "GiBUU reads its jobcard (a Fortran namelist) from stdin and writes the"
echo "ROOT event file into the working directory:"
echo "  docker run --platform linux/amd64 --rm \\"
echo "    -v \"\$PWD\":/work -w /work \\"
echo "    ${IMAGE_NAME} \\"
echo "    bash -c \"GiBUU.x < /work/job.job\""
