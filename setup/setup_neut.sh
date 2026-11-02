#!/usr/bin/env bash
# Make the NEUT Docker image available locally for the given code version.
#
# NEUT's source is access-restricted, so there are two kinds of code version
# (see docs/generators/neut.md):
#   - with a catalogued git_ref (5.4.2): built from source by setup/Dockerfile.neut.
#     The source tarball is read from setup/neut/src/neut-<ref>.tar.gz; if it is
#     missing it is downloaded with your GitHub credentials (`gh auth token`),
#     which need read access to neut-devel/neut.
#   - without one (5.7.0-nuint2024): the NUISANCE tutorial image's NEUT build is
#     pulled and retagged under the "<generator>:<code_version>" convention.
#
# Usage: setup/setup_neut.sh [--jobs N] [CODE_VERSION]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: setup/setup_neut.sh [CODE_VERSION]
       setup/setup_neut.sh --code-version CODE_VERSION

Build (from source) or pull the NEUT image for the given NEUT code_version.
CODE_VERSION must be catalogued (see: neutrino-factory list-generators --generator neut).

Options:
  --code-version V  NEUT code version (default: 5.7.0-nuint2024)
  --jobs N          Parallel compile jobs for a source build (default: 4)
  --list-versions   Print catalogued NEUT code/config versions and exit
  --help            Show this help
EOF
}

CODE_VERSION=""
JOBS=4
while (($#)); do
  case "$1" in
    --jobs)
      [[ $# -ge 2 ]] || { echo "--jobs requires an argument" >&2; exit 1; }
      JOBS="$2"; shift 2 ;;
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

GIT_REF="$(nf_catalog_git_ref neut "$CODE_VERSION")"
if [[ -n "$GIT_REF" ]]; then
  TARBALL="$SCRIPT_DIR/neut/src/neut-${GIT_REF}.tar.gz"
  if [[ ! -s "$TARBALL" ]]; then
    require_command gh
    require_command curl
    echo "Downloading NEUT ${GIT_REF} source to ${TARBALL}..."
    mkdir -p "$(dirname "$TARBALL")"
    curl -fsSL -H "Authorization: token $(gh auth token)" \
      "https://api.github.com/repos/neut-devel/neut/tarball/${GIT_REF}" -o "$TARBALL.part"
    mv "$TARBALL.part" "$TARBALL"
  fi
  echo "Building Docker image ${IMAGE_NAME} from NEUT ${GIT_REF} source..."
  docker build \
    --platform linux/amd64 \
    --build-arg "NEUT_SOURCE_IMAGE=${SOURCE_IMAGE}" \
    --build-arg "NEUT_REF=${GIT_REF}" \
    --build-arg "JOBS=${JOBS}" \
    -t "${IMAGE_NAME}" \
    -f "${SCRIPT_DIR}/Dockerfile.neut" \
    "${SCRIPT_DIR}"
  STATUS="built-from-source:${GIT_REF}"
else
  # The source image is multi-arch. Pin linux/amd64 like every other project image
  # (containers.docker_wrap always passes --platform linux/amd64), so an Apple
  # Silicon machine that would otherwise get the arm64 variant stays consistent and
  # runs it under Rosetta 2.
  echo "Pulling ${SOURCE_IMAGE} for linux/amd64 (~5 GB, this takes a while)..."
  docker pull --platform linux/amd64 "$SOURCE_IMAGE"
  docker tag "$SOURCE_IMAGE" "$IMAGE_NAME"
  STATUS="pulled-from-published-image"
fi

write_metadata "$META_DIR" \
  "generator=NEUT" \
  "code_version=$CODE_VERSION" \
  "image=$IMAGE_NAME" \
  "source_image=$SOURCE_IMAGE" \
  "status=$STATUS"

echo ""
echo "Image available: ${IMAGE_NAME} (${STATUS}, base ${SOURCE_IMAGE})"
echo ""
echo "To run neutroot2 (NEUT reads its RANLUX seed from \$RANFILE):"
echo "  docker run --platform linux/amd64 --rm \\"
echo "    -v \"\$PWD\":/work -w /work -e RANFILE=/work/ranseed.dat \\"
echo "    ${IMAGE_NAME} \\"
echo "    neutroot2 neut.card events.neut.root"
