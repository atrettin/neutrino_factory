# Neutrino Factory

`Neutrino Factory` is a Python CLI (`neutrino-factory`) plus Bash setup scripts for running multiple neutrino event generators from one common YAML configuration. Generators run inside containers so their native dependencies are isolated and reproducible. Two container pathways are supported:

- **Docker** — local development and testing (macOS/Linux laptops).
- **Apptainer** — HPC cluster execution (MPCDF/ODSL), where Docker is unavailable. SIF images are built natively on the cluster from hand-written definition files (`setup/apptainer/*.def`) that mirror the Dockerfiles.

Targets:
- `GENIE` — Docker image built and working
- `NuWro` — Docker image built and working
- `GiBUU` — Docker image built and working (event generation; ROOT→HDF5 normalizer still a stub)
- `NEUT` — catalogued but not buildable; NEUT source is not freely available, so this backend is blocked indefinitely

The workflow is **develop locally, deploy to the cluster**:
1. validate, plan, and smoke-test runs locally with Docker (or stub mode),
2. deploy the same repo to the ODSL cluster with the Apptainer pathway,
3. submit the same config as a Slurm job array on the MPP cluster.

## Features

- common YAML run configuration
- per-generator config translators
- per-generator output normalizers
- initial common output format in `HDF5`
- local executor for development and smoke testing
- MPP-oriented Slurm job-array scaffolding
- Docker-based generator builds, one setup script per generator for easy extension
- a global generator catalog (`neutrino-factory list-generators`) that pins each run to an exact code version + config version (GENIE tune)

## Repository layout

```text
.
├── bin/                      # bin/nf — cluster CLI wrapper (runs inside nf-base.sif)
├── configs/                  # schema templates and example user configs
├── docs/                     # usage, cluster notes, extension guide
├── jobs/                     # Slurm wrappers
├── scripts/                  # helper utilities
├── setup/                    # Docker build scripts + Dockerfiles; apptainer/ defs + cluster build script
├── src/neutrino_factory/     # Python package
└── tests/                    # unit and smoke tests
```

## Environment variables

All of these are usually written once by `neutrino-factory setup` into the
repo-root `.env` file, which every CLI invocation and setup script loads
automatically (already-set environment variables always win).

| Variable | Meaning |
| --- | --- |
| `NF_SOFTWARE_ROOT` | Root for generator binaries and staged cross-section (xsec) files |
| `NF_OUTPUT_ROOT` | Final output location for normalized and merged products |
| `NF_WORK_ROOT` | Manifests, plans, logs, and temporary metadata |
| `NF_IMAGE_ROOT` | Container image storage (Apptainer SIF files) |
| `NF_CONTAINER_RUNTIME` | `docker`, `apptainer`, or `auto` (default: prefer docker, then apptainer) |
| `NF_EXECUTION_MODE` | `local` or `slurm`; defaults to `local` |

## Quickstart A — local development (Docker)

The two pathways diverge at installation: locally the CLI is pip-installed on
the host; on the cluster the host Python is too old, so the CLI itself runs
inside a container (see Quickstart B).

```bash
# 1. Install the CLI
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure directories and the docker pathway (writes .env)
neutrino-factory setup --pathway docker

# 3. Build generator images + stage GENIE cross sections (hours; optional —
#    every generator also runs in synthetic stub_mode without them)
bash setup/setup_all.sh
bash setup/download_genie_xsec.sh

# 4. Validate and run a local smoke test
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor local

# 5. Render the Slurm submission without submitting
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml \
  --executor slurm --dry-run
```

The local run creates a manifest, runs each enabled generator (or its stub),
normalizes the outputs into `HDF5`, and merges them. Multiple version entries
of the same generator can run side by side (for example, two GENIE tunes).

## Quickstart B — HPC cluster (Apptainer, MPCDF/ODSL)

Do **not** `pip install` on the cluster — the system Python (3.9) is too old
and MPCDF has no module system. Everything Python runs inside the `nf-base`
container via the `bin/nf` wrapper; the generator images additionally contain
the generator binaries. Builds must run on an interactive node
(`odslserv01`/`02`), not the Slurm head node.

```bash
# 1. On odslserv01: clone onto /ptmp (shared, 6 TB/user, not backed up)
git clone <repo-url> /ptmp/mpp/$USER/neutrino_factory/repo
cd /ptmp/mpp/$USER/neutrino_factory/repo

# 2. Bootstrap the orchestration image (fast; records NF_IMAGE_ROOT in .env),
#    then configure
bash setup/build_apptainer_images.sh --bootstrap
bin/nf setup --pathway apptainer     # accept the /ptmp defaults

# 3. Build the generator images (hours each) and stage GENIE cross sections
bash setup/build_apptainer_images.sh
bash setup/download_genie_xsec.sh
bin/nf list-generators --built

# 4. Render the Slurm job, then submit from a host shell on the head node
bin/nf submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm
# ...prints:  sbatch /ptmp/.../work/slurm/<run>.sbatch   -> run that on mppui1
bin/nf check-status --config configs/examples/power_law_numu_Ar.yaml
```

The full runbook, including the new-cluster Slurm requirements and filesystem
guidance, is in `docs/mpp_cluster_usage.md`.

## Generator setup (containers)

The image tag is derived from the catalog (`src/neutrino_factory/catalog.py`)
via the config's `code_version` — there is no manual image config key. The
active runtime is chosen by `NF_CONTAINER_RUNTIME` (persisted in `.env`).

**Docker (local):** each generator is built by its own setup script —
`setup/setup_all.sh`, `setup/setup_genie.sh`, `setup/setup_nuwro.sh`,
`setup/setup_gibuu.sh` (`setup/setup_neut.sh` is not buildable — NEUT source is
not freely available). Each accepts `--list-versions` and validates the
requested `code_version` against the catalog. At runtime the adapter prefers a
native binary on `$PATH` and otherwise wraps the generator in `docker run`.

**Apptainer (cluster):** SIFs are built by `setup/build_apptainer_images.sh`
from the hand-written definitions in `setup/apptainer/*.def` (each mirrors its
`setup/Dockerfile.*` — update both together). The container layering is
*inverted*: Apptainer cannot nest, so the Slurm array task enters the
generator's SIF first and runs the CLI inside it, where the generator binary is
native on `$PATH`. The generator images therefore also contain a Python
runtime; the small `nf-base.sif` covers everything else (wizard, validation,
merge, plots) via `bin/nf`.

Use `neutrino-factory list-generators --built` (or `bin/nf list-generators
--built` on the cluster) to see which catalogued images are present for the
active runtime.

### GENIE code versions and cross-section splines

`setup/setup_genie.sh` builds the image for a catalogued GENIE code version
(default `R-3_06_00`), passed positionally or via `--code-version`/`--tag`:

```bash
setup/setup_genie.sh R-3_06_00
setup/setup_genie.sh --code-version R-3_06_00 --download-xsec
```

The code version must be catalogued (see `neutrino-factory list-generators
--generator genie`); distinct code versions produce distinct image tags, so
multiple GENIE versions coexist.

When `--download-xsec` is enabled, `setup/setup_genie.sh` runs `setup/download_genie_xsec.sh`, which
downloads the tarball for each catalogued tune from the matching SciSoft directory
(`R-3_06_00` -> `v3_06_00`), extracts only `gxspl-NUsmall.xml` out of the deep archive, and stages it
to `genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml`. The download step can also be run standalone:

```bash
setup/download_genie_xsec.sh --code-version R-3_06_00 --tune G18_10a_02_11a
```

At runtime, GENIE tasks add `--cross-sections <xsecs.xml>` automatically when a staged file exists for
the requested `code_version` + `config_version`. If no file is found, the run logs a warning and
proceeds without it (fine for stub-mode or fixed-energy runs, but flux-driven runs require the splines).
`neutrino-factory list-generators` only lists a GENIE tune as available once its `xsecs.xml` is present.

Generator output directories include both generator name and generator version identifier, so different
versions of the same generator coexist without file collisions.

## Status

Functional locally; first real HPC deployment (ODSL/MPP cluster via the
Apptainer pathway) is the current objective. GENIE, NuWro, and GiBUU build and
run in Docker, and every generator also runs in synthetic `stub_mode` for
development without real binaries. Known gaps are tracked in the source tree:
the GiBUU ROOT→HDF5 normalizer is a stub, NEUT is blocked on source
availability, and the Apptainer pathway is written but not yet verified on the
cluster (the dev machine is macOS, where Apptainer cannot run).
