#!/usr/bin/env bash
# Make the NEUT Docker image available locally for the given code version.
#
# Unlike every other generator, NEUT is not built from source: its source code is
# not publicly available. The NUISANCE collaboration publishes a tutorial image
# containing a working NEUT build, so this script pulls that image and retags it
# under the project's uniform "<generator>:<code_version>" convention. There is
# consequently no setup/Dockerfile.neut for setup/apptainer/neut.def to mirror —
# see docs/generators/neut.md.
#
# Usage: setup/setup_neut.sh [5.7.0-nuint2024]
#        setup/setup_neut.sh --code-version 5.7.0-nuint2024
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/setup_neut.sh [CODE_VERSION]
       setup/setup_neut.sh --code-version CODE_VERSION

Pull the published NEUT-bearing image for the given NEUT code_version and retag
it as the catalogued image name. CODE_VERSION must be catalogued
(see: neutrino-factory list-generators --generator neut).

Options:
  --code-version V  NEUT code version (default: 5.7.0-nuint2024)
  --list-versions   Print catalogued NEUT code/config versions and exit
  --help            Show this help
EOF
}

CODE_VERSION=""
while (($#)); do
  case "$1" in
    --code-version|--tag)
      [[ $# -ge 2 ]] || { echo "$1 requires an argument" >&2; exit 1; }
      CODE_VERSION="$2"; shift 2 ;;
    --list-versions) nf_list_versions neut; exit 0 ;;
    --help|-h) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) [[ -z "$CODE_VERSION" ]] || { echo "NEUT code_version provided more than once" >&2; exit 1; }
       CODE_VERSION="$1"; shift ;;
  esac
done

CODE_VERSION="${CODE_VERSION:-5.7.0-nuint2024}"
nf_validate_code_version neut "$CODE_VERSION"

IMAGE_NAME="$(nf_catalog_image neut "$CODE_VERSION")"
[[ -n "$IMAGE_NAME" ]] || fail "No image catalogued for neut code_version '$CODE_VERSION'"

SOURCE_IMAGE="$(nf_catalog_build_arg_value neut "$CODE_VERSION")"
[[ -n "$SOURCE_IMAGE" ]] || fail \
  "No source image catalogued for neut code_version '$CODE_VERSION' (expected NeutAdapter.CODE_VERSIONS[...]['source_image'])"

require_command docker

nf_default_paths
META_DIR="$NF_SOFTWARE_ROOT/neut/metadata"

# The source image is multi-arch. Pin linux/amd64 like every other project image
# (containers.docker_wrap always passes --platform linux/amd64), so an Apple
# Silicon machine that would otherwise get the arm64 variant stays consistent and
# runs it under Rosetta 2.
echo "Pulling ${SOURCE_IMAGE} for linux/amd64 (~5 GB, this takes a while)..."
docker pull --platform linux/amd64 "$SOURCE_IMAGE"
docker tag "$SOURCE_IMAGE" "$IMAGE_NAME"

write_metadata "$META_DIR" \
  "generator=NEUT" \
  "code_version=$CODE_VERSION" \
  "image=$IMAGE_NAME" \
  "source_image=$SOURCE_IMAGE" \
  "status=pulled-from-published-image"

echo ""
echo "Image available: ${IMAGE_NAME} (retagged from ${SOURCE_IMAGE})"
echo ""
echo "To run neutroot2 (NEUT reads its RANLUX seed from \$RANFILE):"
echo "  docker run --platform linux/amd64 --rm \\"
echo "    -v \"\$PWD\":/work -w /work -e RANFILE=/work/ranseed.dat \\"
echo "    ${IMAGE_NAME} \\"
echo "    neutroot2 neut.card events.neut.root"
