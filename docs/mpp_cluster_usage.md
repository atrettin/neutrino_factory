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
- **Inverted container layering**: Apptainer cannot nest, so the sbatch array
  task runs `apptainer exec <generator>.sif bash jobs/run_task.sh …` — the CLI
  executes *inside* the generator image, where the generator binary is native
  on `$PATH`. All other CLI use goes through `bin/nf`, which wraps the small
  `nf-base.sif` orchestration image.
- Run everything from a **plain host shell**. The cenv-based VSCode environment
  is itself an Apptainer container, and `apptainer` does not work inside it.

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

# 2. Bootstrap the orchestration image (fast: python:3.13-slim + pip deps).
#    The image root defaults to <repo>/software/images (on /ptmp, since the
#    repo is) and is recorded in .env so bin/nf and later builds agree on it;
#    set NF_IMAGE_ROOT beforehand to choose a different location.
bash setup/build_apptainer_images.sh --bootstrap

# 3. Interactive setup: choose the apptainer pathway, accept the /ptmp defaults.
#    Writes .env (storage roots, NF_CONTAINER_RUNTIME=apptainer, cache dir).
bin/nf setup --pathway apptainer

# 4. Build the generator images (ROOT/GENIE compile from source — hours each;
#    GiBUU is fastest, it reuses a prebuilt ROOT base). Rerunning is safe:
#    existing SIFs are skipped without --force.
bash setup/build_apptainer_images.sh --only gibuu
bash setup/build_apptainer_images.sh --only genie
bash setup/build_apptainer_images.sh --only nuwro

# 5. Stage GENIE cross-section splines and check the catalog
bash setup/download_genie_xsec.sh
bin/nf list-generators --built
```

If you later change `NF_IMAGE_ROOT` (via `.env` or the wizard), rerun
`bash setup/build_apptainer_images.sh --bootstrap` — it re-creates nf-base at
the new location quickly.

## Submitting a run

```bash
# On mppui1 (ssh from odslserv01), in the repo:
bin/nf validate-config --config configs/examples/power_law_numu_Ar.yaml
bin/nf submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm
```

`sbatch` is not visible inside the container, so `submit` renders the job
array script and prints the exact command to run in the host shell:

```bash
sbatch /ptmp/mpp/$USER/neutrino_factory/work/slurm/<run_name>.sbatch
```

The rendered script sources the repo `.env`, maps each array task index to its
generator's SIF, and runs the task inside it. Monitor and validate with:

```bash
squeue --me
bin/nf check-status --config configs/examples/power_law_numu_Ar.yaml
```

Per-generator merged HDF5 files appear under `$NF_OUTPUT_ROOT/merged/`. Chunk
merging currently happens in the local pipeline; after a Slurm run, merge
chunks explicitly with `bin/nf merge` if needed.

## Troubleshooting

- `apptainer build` fails → are you on odslserv01/02? Builds fail on mppui1.
- "binary not on $PATH and runtime is not docker" from a task → the task was
  launched outside its generator image; use the rendered sbatch script (or
  `apptainer exec <generator sif> bash jobs/run_task.sh …` manually).
- `bin/nf` reports nf-base.sif missing → run
  `bash setup/build_apptainer_images.sh --bootstrap` on an odslserv node.
- GENIE MEC (2p2h) crashes were only ever observed under amd64 *emulation* on
  the dev laptop; on the cluster's native x86_64, re-test the default
  event-generator list before restricting to CCQE (see STUBS.md).
