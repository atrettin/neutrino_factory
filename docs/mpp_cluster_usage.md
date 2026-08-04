# ODSL / MPP cluster usage (Apptainer pathway)

End-to-end runbook for deploying and running neutrino-factory on the
MPCDF/ODSL cluster. The cluster has **no Docker**, **no module system**, and a
system Python (3.9) too old for a native install — everything Python runs
inside Apptainer containers.

## Execution model

- **Build on odslserv01/02** (interactive nodes) — unprivileged `apptainer
  build` works there and is known to *fail* on the Slurm head node.
- **Submit from mppui1.t2.rzg.mpg.de** (Slurm head node; `ssh mppui1` from an
  odslserv node). All storage is on shared `/ptmp`, so both hosts see the same
  repo, images, and outputs.
- **Unified runtime image**: the sbatch array task runs inside
  `nf-base.sif` for every task. The unified image hosts the framework Python
  runtime and generator wrappers in one place, so interactive usage also enters
  this image once and runs CLI commands there.
- Use `cenv` for interactive sessions on ODSL. Create a container environment
  from `nf-base.sif`, enter it, install the project once with `pip install -e .`,
  then run `neutrino-factory` directly in that session.
- Run setup/build commands from a **plain host shell** on odslserv nodes.

Reference: https://github.com/oschulz/container-env

## Filesystems

| Path | Properties | Use for |
| --- | --- | --- |
| `/u/...` (home) | 125 GB, backed up, slow | nothing from this project |
| `/ptmp/mpp/$USER` | 6 TB/user GPFS, shared, **no backup** | repo, SIF images, software, output, work |

MPCDF auto-mounts `/u`, `/ptmp`, and `/cvmfs` inside every Apptainer
container, so paths under those trees resolve unchanged inside. `/scratch`
(node-local SSD) is **not** accessible from inside containers, so the project
does not use it — everything lives on `/ptmp`.

## Slurm: the NEW MPP cluster requires `--partition=alma`

Submitting from `mppui1`/`mppui2` targets the **new** MPP Slurm cluster
(see https://docs.t2.mpcdf.mpg.de/resources/computing-new/), which **requires
`#SBATCH --partition=alma`**. The config default is therefore `alma`.

| Property | Value |
| --- | --- |
| Partition | `alma` (mandatory) |
| Max job duration | 1 day (`1-00:00:00`) |
| Nodes | 28× Intel Xeon (32 cores / 187 GB) + 12× AMD EPYC (64 cores / 256 GB) |
| Queue limits | 25 000 queued / 10 000 running jobs per user |

Beware: a job submitted there with an old-cluster partition name (`short`,
`standard`, …) is accepted, runs for a few seconds, and dies **without ever
creating its log files** — an easy failure mode to misdiagnose. Note also that
`/u` and `/ptmp` are *not* shared between the old and new clusters; the
odslserv nodes share `/ptmp` with the new cluster.

## First-time setup

```bash
# 1. On odslserv01: clone onto /ptmp
git clone <repo-url> /ptmp/mpp/$USER/neutrino_factory/repo
cd /ptmp/mpp/$USER/neutrino_factory/repo

# 2. Build Apptainer images from a plain host shell (outside any container).
#    This builds bootstrap + generator payload images and composes nf-base.sif.
#    Rerunning is safe: existing SIFs are skipped without --force.
bash setup/build_apptainer_images.sh

# 3. Stage GENIE cross-section splines and check the catalog in nf-base.sif
bash setup/download_genie_xsec.sh
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" env PYTHONPATH="$PWD/src" \
  python3 -m neutrino_factory.cli list-generators --built

# 4. After all images are built, create and enter an interactive cenv session
cenv --create nf-env "$NF_IMAGE_ROOT/nf-base.sif"
cenv nf-env

# 5. One-time setup inside the cenv session
pip install -e .
neutrino-factory setup --pathway apptainer --no-build
```

If you later change `NF_IMAGE_ROOT` (via `.env` or the wizard), rerun
`bash setup/build_apptainer_images.sh --bootstrap` — it re-creates nf-base at
the new location quickly.

## Submitting a run

```bash
# On mppui1 (ssh from odslserv01), in the repo:
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm
```

`sbatch` is not visible inside the container, so `submit` renders the job
array script and prints the exact command to run in the host shell:

```bash
sbatch /ptmp/mpp/$USER/neutrino_factory/work/slurm/<run_name>.sbatch
```

The rendered script sources the repo `.env` and runs each array task inside the
unified `nf-base.sif` runtime image. Monitor and validate with:

```bash
squeue --me
neutrino-factory check-status --config configs/examples/power_law_numu_Ar.yaml
```

Per-generator merged HDF5 files appear under `$NF_OUTPUT_ROOT/merged/`. Chunk
merging currently happens in the local pipeline; after a Slurm run, merge
chunks explicitly with `neutrino-factory merge` if needed.

## First submission on a new cluster

Working up from a scheduler-only smoke test isolates Slurm problems from
generator problems, which otherwise present identically (a task that dies before
writing anything).

**1. Start from a deliberately tiny config**: one job with `events: 12` and
`chunks: 2`, `run.stub_mode: true`, `slurm.time: "00:05:00"`, `slurm.mem: 1G`.
Stub mode exercises the whole path — manifest, array, normalization, merge —
without needing a single generator image.

**2. Point the storage roots at a shared filesystem** (on MPCDF, `/ptmp`), via
`.env` or the environment:

```bash
export NF_SOFTWARE_ROOT=/ptmp/mpp/$USER/neutrino_factory/software
export NF_OUTPUT_ROOT=/ptmp/mpp/$USER/neutrino_factory/output
export NF_WORK_ROOT=/ptmp/mpp/$USER/neutrino_factory/work
```

**3. Validate and render before submitting.**

```bash
neutrino-factory validate-config --config <config>
neutrino-factory submit --config <config> --executor slurm --dry-run
```

Inspect what lands under `work/manifests/`, `work/slurm/` and `work/logs/`. The
manifest schema is documented in [configuration.md](configuration.md); check that
the rendered sbatch script enters `nf-base.sif` before Python, and that the
partition is right (on the new MPP cluster, `alma` — see above).

**4. Submit, then monitor.**

```bash
sbatch work/slurm/<run_name>.sbatch
squeue --me
sacct -j <jobid> --format=JobID,State,Elapsed,MaxRSS
```

`jobs/submit_mpp.sh <config> --submit` wraps the render-and-submit pair.

**5. Confirm the outputs**: logs under `$NF_WORK_ROOT/logs/`, normalized chunk
outputs under `$NF_OUTPUT_ROOT/chunks/`, merged HDF5 under
`$NF_OUTPUT_ROOT/merged/`.

**6. Only then switch to a real generator**: set `run.stub_mode: false`, enable a
single generator, keep the event count and walltime small, and confirm the
binary resolves inside the image (`nf-run <gen> <code_version> <binary>`) before
scaling to a multi-generator production run.

## Troubleshooting

- `apptainer build` fails → are you on odslserv01/02? Builds fail on mppui1.
- "binary not on $PATH and runtime is not docker" from a task → the task was
  launched outside the unified runtime image; use the rendered sbatch script.
- `nf-base.sif` missing → run
  `bash setup/build_apptainer_images.sh --bootstrap` on an odslserv node.
- `neutrino-factory: command not found` in cenv → run one-time `pip install -e .`
  from the repo root inside that cenv session.
- GENIE MEC (2p2h) segfault on the first MEC event → the SIF was built from a
  def that lacked `-Dminuit2=ON` in the ROOT build (root cause diagnosed
  2026-07-15: GENIE null-derefs the Minuit2 minimizer it requests from
  ROOT::Math::Factory). Rebuild `genie_R-3_06_00.sif` from the current
  `setup/apptainer/genie.def` (see STUBS.md).
