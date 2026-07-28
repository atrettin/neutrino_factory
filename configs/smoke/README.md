# End-to-end smoke tests

One minimal real (non-stub) config per buildable generator, each running a small
`numu` CC job on carbon (C12). They exercise the full pipeline: config →
translate → run the real generator → normalize to the common HDF5. Use them to
confirm the composed `nf-base.sif` actually works after (re)building images.

| Config | Generator | Events | Notes |
|---|---|---|---|
| `genie_c12.yaml` | GENIE `R-3_06_00` / `G18_10a_02_11a` | 50 | needs the tune's xsec spline staged |
| `nuwro_c12.yaml` | NuWro `nuwro_25.11` | 50 | |
| `gibuu_c12.yaml` | GiBUU `release2025` | 1000 | more events on purpose: GiBUU weights uniformly-sampled events by cross section (no rejection sampling), so a representative dataset needs many events — and it's fast |
| `neut_c12.yaml` | NEUT `5.7.0-nuint2024` | 50 | two-stage: `neutroot2` then `nf-neut-flatten` |

## Run on the cluster (inside the composed image)

`--executor local` runs the whole pipeline in-process. On the cluster, enter the
composed image and run the CLI (host Python is too old, so everything runs inside
`nf-base.sif`):

```bash
# GENIE also needs its cross-section spline staged once:
bash setup/download_genie_xsec.sh --tune G18_10a_02_11a

apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  submit --config configs/smoke/genie_c12.yaml --executor local
```

Swap in `nuwro_c12.yaml` / `gibuu_c12.yaml` / `neut_c12.yaml` for the others. A successful run
writes a normalized HDF5 under `$NF_OUTPUT_ROOT`; inspect it with:

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  check-status --config configs/smoke/genie_c12.yaml
```

To exercise the real Slurm array path instead of `local`, render and submit with
`--executor slurm` (see `docs/mpp_cluster_usage.md`).

## Run locally with Docker (dev machine)

The same configs work under the Docker runtime on a dev machine (where the
generator images are built), no Apptainer needed:

```bash
neutrino-factory submit --config configs/smoke/genie_c12.yaml --executor local
```
