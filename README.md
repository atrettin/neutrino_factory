# Neutrino Factory

`Neutrino Factory` is a Python CLI (`neutrino-factory`) plus Bash setup scripts for running multiple neutrino event generators from one common YAML configuration. Generators run inside containers so their native dependencies are isolated and reproducible. Two container pathways are supported:

- **Docker** — local development and testing (macOS/Linux laptops).
- **Apptainer** — HPC cluster execution (MPCDF/ODSL), where Docker is unavailable. SIF images are built natively on the cluster from hand-written definition files (`setup/apptainer/*.def`) that mirror the Dockerfiles.

Targets:
- `GENIE` — Docker image built and working
- `NuWro` — Docker image built and working
- `GiBUU` — Docker image built and working
- `NEUT` — Docker image pulled and working; NEUT source is not freely available, so the image is extracted from the published NUISANCE tutorial image rather than built

## Features

- common YAML run configuration
- per-generator config translators
- per-generator output normalizers
- common output format in `HDF5`
- local executor for development and smoke testing
- MPP-oriented Slurm job-array scaffolding
- Docker-based generator builds, one setup script per generator for easy extension
- different generator versions can coexist and run in parallel
- a global generator catalog (`neutrino-factory list-generators`) that pins each run to an exact code version + config version (GENIE tune)

## Repository layout

```text
.
├── configs/                  # schema templates and example user configs
├── docs/                     # knowledge base (start at docs/README.md)
├── jobs/                     # Slurm wrappers
├── scripts/                  # helper utilities
├── setup/                    # Docker build scripts + Dockerfiles; apptainer/ defs + cluster build script
├── src/neutrino_factory/     # Python package
└── tests/                    # unit and smoke tests
```

[`docs/README.md`](docs/README.md) is the index of the documentation: the
architecture and config schema, per-generator and physics domain knowledge, the
container pathways, and the cluster runbook. Start there.
`docs/architecture.md` is the map of the Python package: module layout, how a
config becomes tasks, and where outputs land.

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


## Quickstart
### Quickstart A — local development (Docker)

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
```

The local run creates a manifest, runs each job (or its stub), normalizes the
outputs into `HDF5`, and merges each job's chunks. A configuration is a list of
jobs, each pairing one generator version with one initial state (flux, target,
weak current) and its own event budget — so several generators, neutrino
flavours and nuclear targets can run side by side from one file and one
submission. `macros` and `matrix` keep such a file short; run

```bash
neutrino-factory expand --config configs/examples/production_grid.yaml
```

to see the jobs a configuration materializes to before submitting it. See
`docs/configuration.md`.

You can also merge from a config directly:

```bash
neutrino-factory merge --config configs/examples/power_law_numu_Ar.yaml
```

In this mode, the CLI discovers expected chunk and merged outputs from the
config, validates each chunk for required metadata/columns, warns about
missing or invalid chunks, and merges the valid subset per expected merged
output.

### Quickstart B — HPC cluster (Apptainer, MPP cluster)

For HPC applications, the Python environment as well as the event generators
need to be containerized. The setup below builds each generator (or code
version thereof) in a "payload" container, then assembles a base image
`nf-base.sif` containing all of the generator executables as well as 
Python.

The following instructions are aimed at the MPP cluster, but can be used 
on any similar Slurm setup by changing paths.

```bash
# 1. On odslserv01: clone onto /ptmp (shared, 6 TB/user, not backed up)
git clone <repo-url> /ptmp/mpp/$USER/neutrino_factory/repo
cd /ptmp/mpp/$USER/neutrino_factory/repo

# 2. Build Apptainer images in a plain host shell (outside any container):
#    bootstrap runtime, generator payloads, and composed nf-base.sif.
bash setup/build_apptainer_images.sh

# 3. Stage GENIE cross sections and verify built images
bash setup/download_genie_xsec.sh
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" env PYTHONPATH="$PWD/src" \
  python3 -m neutrino_factory.cli list-generators --built

# 4. After images exist, create and enter a cenv based on nf-base.sif
cenv --create nf-env "$NF_IMAGE_ROOT/nf-base.sif"
cenv nf-env

# 5. One-time setup inside the cenv
pip install -e .
neutrino-factory setup --pathway apptainer --no-build

# 6. Render the Slurm job, then submit from a host shell on the head node
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm
# ...prints:  sbatch /ptmp/.../work/slurm/<run>.sbatch   -> run that on mppui1
neutrino-factory check-status --config configs/examples/power_law_numu_Ar.yaml
```

**Note:** `cenv` is optional, it is merely a convenience wrapper to create an interactive
environment based on an Apptainer image. If it is unavailable, Python can still be run 
with `apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" env PYTHONPATH="$PWD/src" python3 ...`.
The cluster scripts all use `apptainer` directly, so submission does not depend on `cenv`.
Usage reference: https://github.com/oschulz/container-env

The full runbook, including the new-cluster Slurm requirements and filesystem
guidance, is in `docs/mpp_cluster_usage.md`.


## Common output format

Every generator is normalized into the same HDF5 layout: a `metadata` group
(version identity, flux, requested event count), a `run` group (`event_count`),
and an `events` group holding one 1-D dataset per column.

| column | unit | meaning |
| --- | --- | --- |
| `event_id`, `seed` | – | bookkeeping |
| `generator`, `probe`, `target` | – | `genie`/`gibuu`/`neut`/`nuwro`, flavour, nucleus |
| `interaction` | – | `qel`, `res`, `dis`, `coh`, `mec`, `other` |
| `energy_gev` | GeV | incoming neutrino energy |
| `weight` | generator-native | raw generator weight, passed through verbatim |
| `xsec_weight` | 1e-38 cm²/nucleon | harmonized cross-section weight |
| `q2_gev2` | GeV² | four-momentum transfer, `Q² = -(p_ν - p_l)²` |
| `bjorken_x` | – | `Q² / (2 M_N ν)` |
| `inelasticity_y` | – | `ν / E_ν`, with `ν = E_ν - E_l` |
| `lepton_energy_gev` | GeV | outgoing lepton energy |
| `lepton_momentum_gev` | GeV | outgoing lepton momentum |
| `lepton_p_parallel_gev` | GeV | lepton momentum along the beam (signed) |
| `lepton_p_transverse_gev` | GeV | lepton momentum transverse to the beam |
| `lepton_costheta` | – | cosine of the lepton scattering angle |

The kinematic variables are all lab-frame, derived from the incoming-neutrino
and outgoing-lepton four-vectors by one shared formula
(`neutrino_factory.kinematics`) rather than from each generator's own
precomputed branches, so they mean the same thing whichever generator produced
them. Where a variable is not defined for an event it carries a clearly
unphysical placeholder: `-1` for the non-negative quantities, `-999` for
`lepton_p_parallel_gev` and `lepton_costheta` (whose physical range includes
`-1`). Notably, `bjorken_x` is `-1` for coherent events. See
`docs/physics.md` for the full convention.

Inspect a file's kinematic content with `analyze-kinematics`, which prints event
counts by interaction type, the weight efficiency of each channel, and per-
interaction tables of mean/median/range for every kinematic variable:

```bash
# a single file
neutrino-factory analyze-kinematics --input output/merged/run_genie_ver.h5

# or discover every merged output a config produces, each under its own heading
neutrino-factory analyze-kinematics --config configs/smoke/genie_c12.yaml
```

Means and medians are weighted by `xsec_weight` and placeholders are excluded.
The weight-efficiency table reports the Kish effective sample size
`n_eff = (Σw)² / Σw²` — the number of unweighted events carrying the same
statistical power. Read it before trusting any distribution: GiBUU samples phase
space uniformly and weights by cross section, so its quasi-elastic channel can
show `n_eff/n` below 1 % and raw event counts badly overstate what the sample
supports.

## Generator setup (containers)

The image tag is derived from the catalog (`src/neutrino_factory/catalog.py`)
via the config's `code_version` — there is no manual image config key. The
active runtime is chosen by `NF_CONTAINER_RUNTIME` (persisted in `.env`).

**Docker (local):** each generator is built by its own setup script —
`setup/setup_all.sh`, `setup/setup_genie.sh`, `setup/setup_nuwro.sh`,
`setup/setup_gibuu.sh`, `setup/setup_neut.sh` (which pulls and retags a published
image instead of building — NEUT source is not freely available). Each accepts
`--list-versions` and validates the
requested `code_version` against the catalog. At runtime the adapter prefers a
native binary on `$PATH` and otherwise wraps the generator in `docker run`.

**Apptainer (cluster):** SIFs are built by `setup/build_apptainer_images.sh`
from the hand-written definitions in `setup/apptainer/*.def` (each mirrors its
`setup/Dockerfile.*` — update both together; `neut.def` is the exception, having
no Dockerfile to mirror). The Slurm array task runs inside
the unified `nf-base.sif` image, which provides one Python runtime plus
generator wrappers in a single interactive environment.

Use `neutrino-factory list-generators --built` to see which catalogued images
are present for the active runtime.

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

**Note:** `neutrino-factory list-generators` only lists a GENIE tune as available once its `xsecs.xml` is present!
If you have installed GENIE but no config version is shown as available, this is the likely culprit.
