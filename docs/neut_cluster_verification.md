# NEUT: cluster verification checklist

Everything in the NEUT integration was verified locally on macOS with the Docker
runtime (see `docs/design_decisions.md` for what was measured). The Apptainer
pathway is written but **not verified** — the dev machine cannot run Apptainer
and has no cluster access. This is the hand-off: run it once on the cluster and
report back.

Work through the steps in order; each names what success looks like.

---

## 0. Prerequisites

- Run on an **ODSL interactive node (`odslserv01`/`odslserv02`)**, not the Slurm
  head node `mppui1` — Apptainer builds are known to fail there.
- `.env` present at the repo root with `NF_IMAGE_ROOT` pointing at shared
  `/ptmp`, and `APPTAINER_CACHEDIR` set to somewhere with room.

**Disk is the thing most likely to bite here.** Stage 1 of `setup/apptainer/neut.def`
bootstraps from `nuisancemc/tutorial:nuint2024`, a **~5 GB** published image —
far larger than any other payload's base layer. Apptainer will pull and unpack it
into `APPTAINER_CACHEDIR` and a temp dir before the final payload (~1.5 GB) is
staged. Check free space in both before starting:

```bash
df -h "$APPTAINER_CACHEDIR" "${APPTAINER_TMPDIR:-/tmp}" "$NF_IMAGE_ROOT"
```

Budget ~15 GB transient. If `/tmp` is small, set `APPTAINER_TMPDIR` to a
roomier filesystem before step 1.

## 1. Build the payload and recompose `nf-base.sif`

```bash
setup/build_apptainer_images.sh --only neut --code-version 5.7.0-nuint2024
```

Expected:

- The catalog query resolves NEUT as buildable and passes
  `--build-arg NEUT_SOURCE_IMAGE=nuisancemc/tutorial:nuint2024`. If instead you
  see `No catalog entry for neut code_version '5.7.0-nuint2024'`, the CLI in the
  bootstrap image is stale — rebuild it with `--bootstrap`.
- `<NF_IMAGE_ROOT>/neut_5.7.0-nuint2024.sif` is written, and the script logs
  `… OK` (its `verify_payload_sif` checks that a wrapper exists and is
  executable for **both** declared binaries, `neutroot2` and `nf-neut-flatten`).
- `nf-base.sif` is then recomposed unconditionally, with NEUT among the payloads.

Note this build runs `apptainer build` from the repo root (changed in this work)
so `neut.def`'s `%files` can stage `setup/neut/nf_flatten.C` and
`setup/neut/nf-neut-flatten` by repo-relative path. If you see
`stat setup/neut/nf_flatten.C: no such file or directory`, that is the change not
having taken effect — check `build_def()` in `setup/build_apptainer_images.sh`.

## 2. Confirm the payload is self-contained inside the composed image

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" nf-run neut 5.7.0-nuint2024 neutroot2
```

**Expected: a Fortran error, not a success.** With no card in the working
directory NEUT aborts with
`Fortran runtime error: Cannot open file 'neut.card'`. What matters is the two
lines *before* it:

```
 CRSPATH_ENV: /opt/nf/generators/neut/5.7.0-nuint2024/neut/share/neut/crsdat
 Reading neut.card as card
```

That proves the binary launched, its shared libraries resolved, and
`$NEUT_CRSPATH` points into the payload. A missing-library error here instead
means the payload is incomplete — see "If it fails" below.

Also confirm ROOT survived relocation, since the flattener needs it:

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  /opt/nf/generators/neut/5.7.0-nuint2024/root/bin/root --version
```

Expected: `ROOT Version: 6.30/04`.

Then check the flattener's environment:

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  bash -c 'nf-run neut 5.7.0-nuint2024 nf-neut-flatten 2>&1 | head -2'
```

Expected: the usage line `usage: nf-neut-flatten <input.root> <output.root>`.

## 3. Run the smoke config through the local executor, inside the image

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  submit --config configs/smoke/neut_c12.yaml --executor local
```

Expected: two container-side stages run (`neutroot2`, then `nf_flatten:` printing
`wrote <run.events> events`), and the CLI prints one `chunk_outputs` and one
`merged_outputs` path.

Ignore `Error in cling::AutoLoadingVisitor: Missing FileEntry for
/opt/neut-src/neutclass/*.h` — the image has no NEUT source tree, so Cling cannot
autoload; the macro still compiles because the wrapper passes `NF_NEUT_INCDIR`.
It is noise, not a failure.

## 4. Check the merged output

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" python3 - <<'PY'
import h5py, collections, glob
p = glob.glob("<NF_OUTPUT_ROOT>/merged/smoke_neut_c12_neut_*.h5")[0]
f = h5py.File(p); ev = f["events"]
print("N =", len(ev["event_id"]))
print("interactions:", collections.Counter(x.decode() for x in ev["interaction"][:]))
xw = ev["xsec_weight"][:]
print("xsec_weight zero count:", int((xw == 0).sum()), " range:", xw.min(), xw.max())
print("code_version:", f["metadata"].attrs["code_version"])
PY
```

Expected, matching what the Docker run produced locally: `run.events` events;
all nine columns populated; more than one interaction category (in a 50-event
local run: qel 27, res 13, dis 4, mec 4, coh 2); **no** zero `xsec_weight`;
`code_version` = `5.7.0-nuint2024`.

## 5. Cross-check the normalization against NUISANCE

This is the check that matters most — it validates the cross-section
normalization against an independent implementation rather than against itself.
The composed `nf-base.sif` has no NUISANCE, so run it in the *source* image:

```bash
cd "<NF_WORK_ROOT>/raw/neut/5.7.0-nuint2024_default/smoke_neut_c12_neut_5.7.0-nuint2024_default_chunk000"

# our number
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" python3 -c "
import uproot
f = uproot.open('events.flat.root')
# The pair is named after the beam flavour ('flux_numu'/'evtrt_numu' for numu,
# 'flux_nueb'/'evtrt_nueb' for nuebar, 'fluxhisto'/'ratehisto' as NEUT's
# fallback), so pick it out by prefix rather than by a fixed name.
flux = next(k.split(';')[0] for k in f.keys() if k.startswith('flux'))
rate = next(k.split(';')[0] for k in f.keys() if k.startswith(('evtrt', 'ratehisto')))
print('ours:', f[rate].to_numpy()[0].sum() / f[flux].to_numpy()[0].sum())"

# independent reference
apptainer exec docker://nuisancemc/tutorial:nuint2024 \
  nuisflat -i NEUT:events.neut.root -f GenericFlux -o /tmp/nf.root 2>&1 \
  | grep "Event/Flux"
```

Expected: the two agree to every printed digit. Locally this was `1.4677402686`
vs NUISANCE's `Event/Flux : 1.46774e-38 cm2/nucleon`.

## 6. Run it through the real Slurm array path

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  submit --config configs/smoke/neut_c12.yaml --executor slurm --dry-run
```

Check the rendered script enters `nf-base.sif` before Python, then submit without
`--dry-run` (see `docs/mpp_cluster_usage.md`). Inside the image `neutroot2` is
native on `$PATH`, so the adapter's native-first branch fires and emits
`nf-run neut 5.7.0-nuint2024 neutroot2 …` — no Apptainer nesting.

Re-apply the step-4 checks to the per-chunk HDF5. Note that a Slurm array run
currently leaves only per-chunk files (an existing item in `.claude/TODOS.md`).

Multi-chunk merged files are safe to judge normalization from: `merge_hdf5_files`
averages the per-chunk estimates rather than summing them (see "Merging chunks
averages cross-section weights" in `docs/design_decisions.md`). This was a real
bug — an N-chunk merge used to report N times the cross section — so if a merged
file comes back an integer multiple too large, suspect that first.

## 7. Worth reporting back: is NEUT deterministic on native x86_64?

Locally, two runs with the *same* seed diverge after ~4 events. This was traced
to NEUT itself, not the harness: it reproduces on native aarch64 and on x86_64
under Rosetta, and with ASLR disabled. It would be useful to know whether it also
happens on a native x86_64 cluster node.

```bash
cd "$(mktemp -d)" && cp <the smoke run's work dir>/{neut.card,flux.root,ranseed.dat} .
for x in 1 2; do
  RANFILE=ranseed.dat apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
    nf-run neut 5.7.0-nuint2024 neutroot2 neut.card o$x.root 2>&1 \
    | tr -d '\000' | grep -a '^Ev\.#' > seeds$x.txt
done
diff seeds1.txt seeds2.txt && echo "DETERMINISTIC" || echo "DIVERGES (as on the dev machine)"
```

Either answer is fine for the integration — chunking only needs different seeds
to give different events, which they do — but if the cluster says
`DETERMINISTIC`, the caveat in `docs/design_decisions.md` should be narrowed.

---

## If it fails

**A missing shared library when `neutroot2` starts (step 2).**
`ldd` on the dev machine showed `neutroot2` needing only `libgfortran5`,
`libpcre3`, `liblzma5`, `libz` and `libstdc++` — all already in `nf-base.def`'s
fixed runtime baseline, so nothing was added there. If something else turns out
to be missing, the first suspect is CERNLIB: `/opt/cern` was **not** staged into
the payload because `ldd` reported it unused (it appears to be statically linked
into `libNEUT.a`). Add it to the `%files from source` block in
`setup/apptainer/neut.def` alongside `/opt/neut` and rebuild. Otherwise add the
missing package to `nf-base.def`'s baseline list.

**The flatten stage fails (step 3).** Three likely causes:
- `ROOT is not on $PATH` / `root: not found` — ROOT's `bin/root` is an *absolute*
  symlink to `/opt/root/v6-30-04/bin/root.exe` in the source image and dangles
  once relocated, which bash reports as "not found". `neut.def`'s `%post`
  rewrites it (and NEUT's two absolute include links) relative; the `%test`
  section now runs `root --version` from the payload, so a build that gets this
  wrong fails at build time. If it resurfaces, check
  `readlink "$PREFIX/root/bin/root"` — it should be plain `root.exe`.
- `nf_flatten.C not found next to …` — the `%files` staging in `neut.def` did not
  land. Check `apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" ls
  /opt/nf/generators/neut/5.7.0-nuint2024/share/`.
- `unknown type name 'NeutVect'` — Cling did not get the NEUT headers. The
  payload wrapper exports `NF_NEUT_INCDIR`; verify
  `/opt/nf/generators/neut/5.7.0-nuint2024/neut/include/neutvect.h` exists and
  that the wrapper at `.../bin/nf-neut-flatten` sets the variable.

**`neut-config` errors with "pkg-config thinks NEUT is installed to …".** The
`sed` in `neut.def`'s `%post` that repoints `NEUT.pc`'s `prefix=` after
relocation did not apply. The flattener does not depend on this (it is given
`NF_NEUT_INCDIR` directly), but it will confuse anyone debugging by hand.

**`Fortran runtime error: End of file` in `nerdseed.F`, on a path ending
mid-directory.** NEUT truncated a filename to its 80-character Fortran buffer.
Everything NEUT is handed must be a bare filename resolved against the working
directory — see "NEUT paths are limited to 80 characters" in
`docs/design_decisions.md`.

**Every chunk generates identical events.** `$RANFILE` is not reaching NEUT, so
it fell back to the RANLUX default seed. Confirm the run log contains
`RANLUX INITIALIZED BY RLUXGO FROM SEEDS <your seed> 0 0`. Under Apptainer the
adapter sets `RANFILE` in the process environment (`generators/neut.py`,
native branch), which `subprocess` inherits.
