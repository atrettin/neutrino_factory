#!/usr/bin/env bash
# Build the NuWro Docker image for the given tag.
# Usage: setup/setup_nuwro.sh [nuwro_25.11]
#        setup/setup_nuwro.sh --tag nuwro_25.11
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/setup_nuwro.sh [CODE_VERSION]
       setup/setup_nuwro.sh --code-version CODE_VERSION

Build a Docker image for the given NuWro code_version (default: nuwro_25.11).
CODE_VERSION must be catalogued (see: neutrino-factory list-generators --generator nuwro).
The image tag is taken from the catalog and targets linux/amd64 so it runs on
Apple Silicon via Rosetta 2 as well as x86_64 hosts.

Options:
  --code-version V  NuWro code version to build (git tag / release)
  --jobs N          Parallel make jobs inside the image (default: 4)
  --list-versions   Print catalogued NuWro code/config versions and exit
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
    --list-versions) nf_list_versions nuwro; exit 0 ;;
    --help|-h) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) [[ -z "$CODE_VERSION" ]] || { echo "NuWro code_version provided more than once" >&2; exit 1; }
       CODE_VERSION="$1"; shift ;;
  esac
done

CODE_VERSION="${CODE_VERSION:-nuwro_25.11}"
nf_validate_code_version nuwro "$CODE_VERSION"
IMAGE_NAME="$(nf_catalog_image nuwro "$CODE_VERSION")"
[[ -n "$IMAGE_NAME" ]] || fail "No image catalogued for nuwro code_version '$CODE_VERSION'"

require_command docker

echo "Building Docker image ${IMAGE_NAME} (this takes ~30-60 minutes)..."
docker build \
  --platform linux/amd64 \
  --build-arg "NUWRO_TAG=${CODE_VERSION}" \
  --build-arg "JOBS=${JOBS}" \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/Dockerfile.nuwro" \
  "${SCRIPT_DIR}"

echo ""
echo "Image built: ${IMAGE_NAME}"
echo ""
echo "To run nuwro (NuWro must run from /opt/nuwro to locate its data/):"
echo "  docker run --platform linux/amd64 --rm \\"
echo "    -v \"\$PWD\":/work -w /opt/nuwro \\"
echo "    ${IMAGE_NAME} \\"
echo "    nuwro -o /work/events.root -i /work/params.txt"
