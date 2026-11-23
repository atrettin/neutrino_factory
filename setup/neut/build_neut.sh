#!/usr/bin/env bash
# Build NEUT from a source tarball and install it at /opt/neut, replacing the
# NEUT that ships with the build image (nuisancemc/tutorial:nuint2024).
#
# Usage: build_neut.sh <neut-source.tar.gz> [jobs]
#
# Shared by setup/Dockerfile.neut and setup/apptainer/neut.def. The build image
# provides everything NEUT's autotools build needs: CERNLIB (2023 build),
# ROOT 6.30/04 and gfortran 11. Installing over /opt/neut means the image's own
# environment (PATH, LD_LIBRARY_PATH, NEUT_CRSPATH, neut-config) applies to the
# new build unchanged.
set -euo pipefail

TARBALL="$1"
JOBS="${2:-4}"
BUILD=/tmp/neut-build
# Installed straight at its final path: neut-config and the binaries' rpath
# bake in the prefix, so a build installed elsewhere and moved breaks them.
PREFIX=/opt/neut

# NEUT's configure wants the CERNLIB 2005 layout, $CERN/$CERN_LEVEL/{bin,lib,
# include}, with the PDF headers under include/pdf804. The image's CERNLIB 2023
# is installed flat at /opt/cern with them under include/pdf, so present it
# through a symlink shim.
SHIM=/opt/cernshim/2005
mkdir -p "$SHIM/include"
ln -sfn /opt/cern/bin "$SHIM/bin"
ln -sfn /opt/cern/lib "$SHIM/lib"
for f in /opt/cern/include/*; do ln -sfn "$f" "$SHIM/include/"; done
ln -sfn /opt/cern/include/pdf "$SHIM/include/pdf804"
export CERN=/opt/cernshim CERN_LEVEL=2005

rm -rf "$BUILD" && mkdir -p "$BUILD"
tar -xzf "$TARBALL" -C "$BUILD" --strip-components=1

# $1: a cross-section table that must have been installed, relative to crsdat/.
check_install() {
  test -x "$PREFIX/bin/neutroot2"
  test -f "$PREFIX/share/neut/crsdat/$1"
  test -f "$PREFIX/include/neutvect.h"
}

# NEUT 6 builds with CMake from the repository root (NEUT 5 ships autotools under
# src/). It bundles CERNLIB 2005 itself, so the shim above goes unused, and fetches
# toml11, FortModGen, NucDeEx and Eigen at configure time (network needed). The
# install layout matches NEUT 5's (bin, lib, include, share/neut/{crsdat,Cards}),
# so the image's environment still applies. See docs/generators/neut.md, "Container build".
if [[ -f "$BUILD/CMakeLists.txt" ]]; then
  # NEUT 6 needs CMake >= 3.17; bootstrap a newer one from PyPI if the image's is older.
  if ! cmake -P <(echo 'if(CMAKE_VERSION VERSION_LESS 3.17)
message(FATAL_ERROR "old")
endif()') >/dev/null 2>&1; then
    python3 -m pip install --no-cache-dir "cmake>=3.17,<4"
    hash -r
  fi
  # DOWNLOAD_DATA: the ED-RMF tables come from the private neut-devel/neut-data repo (no access); only QE model "rmf" needs them.
  # WERROR: NEUT's -Werror is tuned to its CI's compilers, not this gcc 11.
  # NUHEPMC: we read the neutroot output only (it is the one nf_flatten.C reads).
  cmake -S "$BUILD" -B "$BUILD/build" \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DNEUT_DOWNLOAD_DATA=OFF \
    -DNEUT_WERROR_ENABLED=OFF \
    -DNEUT_NUHEPMC_ENABLED=OFF \
    -DNEUT_BUILTIN_NUHEPMC=OFF
  cmake --build "$BUILD/build" -j"$JOBS"
  rm -rf "$PREFIX"
  cmake --install "$BUILD/build"
  # NEUT 6 moved the Bodek-Yang tables into crsdat/BY/.
  check_install BY/94mod_p.dat
  test -f "$PREFIX/share/neut/Cards/NEUT.Defaults.toml"
  rm -rf "$BUILD"
  exit 0
fi

# A GitHub tarball gives every file the same mtime, so make decides the
# autotools outputs are stale and tries to rerun aclocal-1.13, which the image
# lacks. Touch them in dependency order instead.
cd "$BUILD/src"
touch aclocal.m4
sleep 1
touch configure $(find . -name Makefile.in)

# Non-void functions that fall off their end without a return. That is undefined
# behaviour, and GCC >= 8 at -O2 compiles it into a fall-through into whatever
# code follows: AddNuclei crashes neutroot2 with "double free" right after the
# Nieves 1p1h setup, before the first event. Add the missing returns.
sed -i '/^int AddNuclei(int nuclei){/,/^}/ s/^}/  return 0;\n}/' n1p1h/src/N1p1hFortranInterface.C
sed -i 's/static bool Destroy() { delete fInstance; }/static bool Destroy() { delete fInstance; return true; }/' \
  neutclass/neutrootTreeSingleton.h
sed -n '/^int AddNuclei(int nuclei){/,/^}/p' n1p1h/src/N1p1hFortranInterface.C | grep -q 'return 0;'
grep -q 'delete fInstance; return true;' neutclass/neutrootTreeSingleton.h

mkdir -p "$BUILD/build" && cd "$BUILD/build"
../src/configure --prefix="$PREFIX"
# neutgeom's tools link -lEve, which this ROOT was built without; none of them
# (genev, event_rate, dumptotpau_nd280) is used here, and nothing else needs Eve.
sed -i 's/ -lEve / /' neutgeom/Makefile
make -j"$JOBS"
rm -rf "$PREFIX"
make install

check_install 94mod_p.dat
rm -rf "$BUILD"
