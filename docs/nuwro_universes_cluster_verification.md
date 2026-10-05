# Cluster verification: NuWro reweight universes

A checklist for confirming, on the MPCDF/ODSL cluster, the NuWro payload changes
made for reweight universes (branch `feature/nuwro-universes`). Locally they were
verified under Docker only; the Apptainer payload wrapper and the composed
`nf-base.sif` cannot be run from the dev machine. Background is in
[generators/nuwro.md](generators/nuwro.md#reweight-universes).

Run everything from a bare host shell on an interactive node (odslserv01/02),
from the repository root, on the branch.

## 1. Rebuild the NuWro payload and recompose `nf-base.sif`

```bash
bash setup/build_apptainer_images.sh --only nuwro --force
```

**Expected:** the build succeeds, and `%test` passes. `%test` now also checks
for `bin/nf_reweight` (the wrapper) and `nuwro/bin/nf_reweight` (the binary).
If `make bin/nf_reweight` fails, look at the `nf_reweight` step in
`setup/apptainer/nuwro.def`. It appends a copy of `reweight_to`'s Makefile rule.

## 2. Check the binary resolves in the composed image

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" nf-run nuwro nuwro_25.11 nf_reweight
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" which nf_reweight
```

**Expected:** the first command prints
`[nf_reweight] ERROR: usage: nf_reweight <events.root> <universes.txt> <weights.root>`
and exits 2. The second prints `/usr/local/bin/nf_reweight`, the default-version
symlink. If it reports `nf-run: no payload wrapper`, `nf-payload.json` or the
`bin/` wrapper is missing from the payload.

## 3. End-to-end smoke run

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
  env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
  submit --config configs/smoke/nuwro_c12_universes.yaml --executor local
```

**Expected:** the run succeeds. The chunk's work directory under
`$NF_WORK_ROOT/raw/` holds `universes.txt`, `universes.json` and
`universe_weights.root`. Then check the merged file:

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" python3 - <<'EOF'
import glob, json, h5py, numpy as np, os
path = sorted(glob.glob(os.path.expandvars(
    "$NF_OUTPUT_ROOT/merged/smoke_nuwro_c12_universes_*.h5")))[-1]
with h5py.File(path) as f:
    w = f["events/universe_weights"][()]
    meta = json.loads(f["metadata"].attrs["universes"])
    qel = f["events/interaction"].asstr()[()] == "qel"
print(w.shape, np.isfinite(w).all(), sorted(meta["parameters"]))
print("QE weights vary:", w[qel].std() > 0)
EOF
```

**Expected:** `(1000, 10) True ['mecNorm', 'qel_minerva_ff_scale']` and
`QE weights vary: True`.

## If something fails

- **Generation works but reweighting fails with "unavailable":** `nf_reweight`
  is not on `PATH` inside the image. Recheck step 2.
- **`non-finite universe weight`:** `nf_reweight` returned NaN or inf for some
  events. Note the parameters and report the case. Under Docker on the dev
  machine the QE engine gave only finite weights.
