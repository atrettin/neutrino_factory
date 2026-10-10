# Cluster verification: reweight universes (NuWro and GENIE)

A checklist for confirming, on the MPCDF/ODSL cluster, the payload changes made
for reweight universes (branch `feature/nuwro-universes`). They add two
binaries:

- `nf_reweight` to the NuWro payload;
- `nf_genie_reweight`, built with GENIE Reweight R-1_04_02, to the GENIE payload.

Locally both were verified under Docker only; the Apptainer payload wrappers and
the composed `nf-base.sif` cannot be run from the dev machine. Background is in
[generators/nuwro.md](generators/nuwro.md#reweight-universes) and
[generators/genie.md](generators/genie.md#reweight-universes-and-variations).

Run everything from a bare host shell on an interactive node (odslserv01/02),
from the repository root, on the branch.

## 1. Rebuild the payloads and recompose `nf-base.sif`

```bash
bash setup/build_apptainer_images.sh --only nuwro,genie --force
```

**Expected:** both builds succeed, and `%test` passes.

- NuWro's `%test` checks `bin/nf_reweight` (the wrapper) and
  `nuwro/bin/nf_reweight` (the binary).
- GENIE's checks `bin/nf_genie_reweight` and `genie/bin/nf_genie_reweight`.

If a build fails:

- **The NuWro `make bin/nf_reweight` step fails:** look at the `nf_reweight`
  step in `setup/apptainer/nuwro.def`. It appends a copy of `reweight_to`'s
  Makefile rule.
- **The GENIE Reweight step fails:** look at the "GENIE Reweight +
  nf_genie_reweight" block in `setup/apptainer/genie.def`. It clones Reweight,
  runs `make`, and appends a copy of `grwght1p`'s Makefile rule. A link error
  naming `py*_` symbols means the Pythia6 library directory is missing from
  `LD_LIBRARY_PATH` in that `%post`.

## 2. Check the binaries resolve in the composed image

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" nf-run nuwro nuwro_25.11 nf_reweight
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" nf-run genie R-3_06_00 nf_genie_reweight
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" which nf_reweight nf_genie_reweight
```

**Expected:**

- The first command prints
  `[nf_reweight] ERROR: usage: nf_reweight <events.root> <universes.txt> <weights.root>`
  and exits 2.
- The second prints
  `[nf_genie_reweight] ERROR: usage: nf_genie_reweight <events.ghep.root> ...`
  and exits 2, **with no** `cling::AutoLoadingVisitor ... Missing FileEntry`
  lines before it. Those lines mean the Reweight headers did not reach
  `genie/src/` in the payload.
- The third prints two paths under `/usr/local/bin/`, the default-version
  symlinks.

If one reports `nf-run: no payload wrapper`, that payload's `nf-payload.json` or
`bin/` wrapper is missing.

## 3. End-to-end smoke runs

```bash
for cfg in nuwro_c12_universes genie_c12_universes; do
  apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" \
    env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli \
    submit --config "configs/smoke/$cfg.yaml" --executor local
done
```

The GENIE run needs the G18_10a_02_11a spline staged, as for `genie_c12.yaml`.

**Expected:** both runs succeed. Each chunk's work directory under
`$NF_WORK_ROOT/raw/` holds `universes.txt`, `universes.json` and
`universe_weights.root`. Then check the merged files:

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" python3 - <<'EOF'
import glob, json, h5py, numpy as np, os
for name in ("smoke_nuwro_c12_universes", "smoke_genie_c12_universes"):
    path = sorted(glob.glob(os.path.expandvars(
        f"$NF_OUTPUT_ROOT/merged/{name}_*.h5")))[-1]
    with h5py.File(path) as f:
        w = f["events/universe_weights"][()]
        meta = json.loads(f["metadata"].attrs["universes"])
        qel = f["events/interaction"].asstr()[()] == "qel"
        v = f["events/variation_weights"][()] if "variation_weights" in f["events"] else None
        vmeta = json.loads(f["metadata"].attrs["variations"]) if v is not None else None
    print(name, w.shape, np.isfinite(w).all(), sorted(meta["parameters"]))
    print("  QE weights vary:", w[qel].std() > 0)
    if v is not None:
        print("  variations:", v.shape, np.isfinite(v).all(), vmeta["columns"])
EOF
```

**Expected:**

```
smoke_nuwro_c12_universes (1000, 10) True ['mecNorm', 'qel_minerva_ff_scale']
  QE weights vary: True
smoke_genie_c12_universes (500, 10) True ['MFP_pi', 'MaCCQE', 'MaCCRES', 'NormCCMEC']
  QE weights vary: True
  variations: (500, 2) True ['RPA_CCQE', 'DecayAngMEC']
```

## If something fails

- **Generation works but reweighting fails with "unavailable":** the binary is
  not on `PATH` inside the image. Recheck step 2.
- **`non-finite universe weight`:** the reweighter returned NaN or inf for some
  events. Note the parameters and report the case. Under Docker on the dev
  machine every allowlisted parameter gave only finite weights.
- **`nf_genie_reweight` exits 1 after "There must be at least one cushion term"
  or a FATAL from `GReWeightINuke`:** the tune's FSI model is not hA2018. The
  FSI dials only work with hA2018 tunes.
