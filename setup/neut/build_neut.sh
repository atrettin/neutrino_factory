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

test -x "$PREFIX/bin/neutroot2"
test -f "$PREFIX/share/neut/crsdat/94mod_p.dat"
test -f "$PREFIX/include/neutvect.h"
rm -rf "$BUILD"
