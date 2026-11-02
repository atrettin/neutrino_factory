# Cluster verification: NEUT 5.4.2 and GENIE R-2_12_8

Both versions build and pass their smoke tests under local Docker
([generators/neut.md](generators/neut.md#neut-542-against-570),
[generators/genie.md](generators/genie.md#genie-2128-against-r-3_06_00)). The
Apptainer side — `setup/apptainer/neut.def`'s stage-1 source build,
`setup/apptainer/genie.def`'s `R-2_*` branch, and the `CODE_VERSION` build arg
`setup/build_apptainer_images.sh` now passes — has not been run. This is the
checklist for running it on odslserv01/02, following
[mpp_cluster_usage.md](mpp_cluster_usage.md).

## 1. Stage the NEUT source (host shell)

NEUT's source is access-restricted, and the cluster build cannot fetch it. Copy
the tarball from a machine where `setup/setup_neut.sh 5.4.2` has downloaded it:

```bash
# on the laptop
scp setup/neut/src/neut-5.4.2.tar.gz <cluster>:/ptmp/mpp/$USER/neutrino_factory/setup/neut/src/
```

Expected: `ls -l setup/neut/src/neut-5.4.2.tar.gz` on the cluster shows ~19 MB.
Without it, `neut.def` silently packages the image's own NEUT 5.7.0 under the
`5.4.2` name; step 2's version check catches that.

## 2. Build the payloads (bare host shell, repo root)

```bash
bash setup/build_apptainer_images.sh --only neut  --code-version 5.4.2    --accept-defaults
bash setup/build_apptainer_images.sh --only genie --code-version R-2_12_8 --accept-defaults
```

Expected:
- NEUT build log: `bash /nf_neut_host/build_neut.sh` runs configure and make
  (several minutes), then `%test` passes and `root --version` prints 6.30/04.
  The log must **not** contain `No .../neut-5.4.2.tar.gz: using the image's own
  NEUT build`.
- GENIE build log: `***** The source tree corresponds to GENIE version 2.12.8`;
  the first `make -j` may fail at `bin/gevgen` (link before libraries) and the
  second pass completes. `%test` passes.
- Each build ends with `<sif> OK` and the composed `nf-base.sif` is rebuilt.

Check the NEUT version that landed in the payload (5.4.2 reports itself as 5.4.1):

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  /opt/nf/generators/neut/5.4.2/neut/bin/neut-config --version     # 5.4.1
```

## 3. Stage the GENIE 2 spline (host shell)

```bash
bash setup/download_genie_xsec.sh --code-version R-2_12_8
```

Expected: `Staged tunes: default`; then
`grep -c ',MEC;' $NF_SOFTWARE_ROOT/genie/genie_xsec/R-2_12_8/default/xsecs.xml`
prints `0` and `grep -c '<spline ' …` prints `24272`.

## 4. Smoke runs (inside `nf-base.sif`)

```bash
for c in neut542_c12 genie2_c12; do
  apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
    env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
    submit --config configs/smoke/$c.yaml --executor local
done
```

Expected: each prints a JSON summary with one `merged_outputs` file. NEUT spends
most of its time on the Nieves 1p1h tables before the first event; the
`Missing FileEntry` lines from the flatten stage are harmless.

Then one Slurm submission of either config with `--executor slurm`, to exercise
the payload through `nf-run` on a compute node.

## If it fails

- **NEUT configure: `pdf804/pilot.h does not exist`** or link errors on
  `-ljetset74`/`-lpdflib804`: the CERNLIB shim in `setup/neut/build_neut.sh`
  (the image's `/opt/cern` layout changed).
- **`neutroot2` aborts with `double free` after `Neutrino 16 Lepton 15`**: the
  missing-return patch in `build_neut.sh` did not apply.
- **`neut-config` prints a path under `/opt/neut`**: the `INSTALL_ROOT` rewrite
  in `neut.def` stage 2 did not match.
- **GENIE: `AppInit.h: 'string' was not declared`**: the `sed` in `genie.def`'s
  `R-2_*` branch did not apply.
- **`gevgen` segfaults in `TH1FluxDriver`**: a third `-f` field reached GENIE 2
  (`GenieAdapter._flux_arg`).
