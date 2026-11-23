# Cluster verification: GENIE R-2_12_8

GENIE R-2_12_8 builds and passes its smoke test under local Docker
([generators/genie.md](generators/genie.md#genie-2128-against-r-3_06_00)). The
Apptainer side — `setup/apptainer/genie.def`'s `R-2_*` branch, built through the
`CODE_VERSION` build arg that `setup/build_apptainer_images.sh` passes — has not
been run. This is the checklist for running it on odslserv01/02, following
[mpp_cluster_usage.md](mpp_cluster_usage.md).

## 1. Build the payload (bare host shell, repo root)

```bash
bash setup/build_apptainer_images.sh --only genie --code-version R-2_12_8 --accept-defaults
```

Expected: the build log shows
`***** The source tree corresponds to GENIE version 2.12.8`; the first `make -j`
may fail at `bin/gevgen` (link before libraries) and the second pass completes.
`%test` passes, the build ends with `<sif> OK`, and the composed `nf-base.sif` is
rebuilt.

## 2. Stage the GENIE 2 spline (host shell)

```bash
bash setup/download_genie_xsec.sh --code-version R-2_12_8
```

Expected: `Staged tunes: default`; then
`grep -c ',MEC;' $NF_SOFTWARE_ROOT/genie/genie_xsec/R-2_12_8/default/xsecs.xml`
prints `0` and `grep -c '<spline ' …` prints `24272`.

## 3. Smoke run (inside `nf-base.sif`)

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  submit --config configs/smoke/genie2_c12.yaml --executor local
```

Expected: a JSON summary with one `merged_outputs` file.

Then one Slurm submission of the same config with `--executor slurm`, to
exercise the payload through `nf-run` on a compute node.

## If it fails

- **`AppInit.h: 'string' was not declared`**: the `sed` in `genie.def`'s
  `R-2_*` branch did not apply.
- **`gevgen` segfaults in `TH1FluxDriver`**: a third `-f` field reached GENIE 2
  (`GenieAdapter._flux_arg`).
