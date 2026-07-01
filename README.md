# Neutrino Factory

`Neutrino Factory` is a Python CLI (`neutrino-factory`) plus Bash setup scripts for running multiple neutrino event generators from one common YAML configuration. Generators run inside Docker containers so their native dependencies are isolated and reproducible across local machines and HPC clusters.

Targets:
- `GENIE` — Docker image built and working
- `NuWro` — Docker image built and working
- `GiBUU` — Docker image built and working (event generation; ROOT→HDF5 normalizer still a stub)
- `NEUT` — catalogued but not buildable; NEUT source is not freely available, so this backend is blocked indefinitely

The repository is structured around a **local-first workflow**:
1. validate and plan runs locally,
2. test the orchestration without cluster access,
3. later submit the same manifest to the MPP Slurm cluster.

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
├── configs/                  # schema templates and example user configs
├── docs/                     # usage, cluster notes, extension guide
├── jobs/                     # Slurm wrappers
├── scripts/                  # helper utilities
├── setup/                    # per-generator Docker build scripts + Dockerfiles
├── src/neutrino_factory/     # Python package
└── tests/                    # unit and smoke tests
```

## Environment variables

| Variable | Meaning |
| --- | --- |
| `NF_SOFTWARE_ROOT` | Root for generator binaries and staged cross-section (xsec) files |
| `NF_OUTPUT_ROOT` | Final output location for normalized and merged products |
| `NF_WORK_ROOT` | Manifests, plans, logs, and temporary metadata |
| `NF_SCRATCH_ROOT` | Scratch area for heavy temporary I/O; on MPP this should point to `/ptmp/$USER/...` |
| `NF_EXECUTION_MODE` | `local` or `slurm`; defaults to `local` |

## Quickstart

### 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Review the example config

```bash
cat configs/examples/power_law_numu_Ar.yaml
```

### 3. Validate the config

```bash
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
```

### 4. Run a local smoke test

```bash
neutrino-factory submit \
  --config configs/examples/power_law_numu_Ar.yaml \
  --executor local
```

This creates a manifest, runs synthetic stub tasks for enabled generators, normalizes the outputs into `HDF5`, and merges them.
Multiple version entries for the same generator can run in parallel (for example, two GENIE tunes).

### 5. Render the Slurm submission without submitting

```bash
neutrino-factory submit \
  --config configs/examples/power_law_numu_Ar.yaml \
  --executor slurm \
  --dry-run
```

Later, on the MPP cluster, follow `docs/slurm_submission_testing.md`.

## Local-first development notes

This repository is being implemented from a machine **without active cluster-node access**, so:
- all initial verification should use the `local` executor,
- the Slurm path should be verified in `--dry-run` mode here,
- real `sbatch` submission should wait until the repo is on the MPP cluster.

## Generator setup (Docker)

Each generator is built into a Docker image by its own setup script. The image
tag is derived from the catalog (`src/neutrino_factory/catalog.py`) via the
config's `code_version` — there is no manual `docker_image` config key. At
runtime the adapter runs the generator inside that image if it is present, and
otherwise falls back to a native binary on `$PATH`.

- `setup/setup_all.sh`
- `setup/setup_genie.sh`
- `setup/setup_nuwro.sh`
- `setup/setup_gibuu.sh`
- `setup/setup_neut.sh` (not buildable — NEUT source is not freely available)

Each script accepts `--list-versions` (delegates to `neutrino-factory
list-generators`) and validates the requested `code_version` against the
catalog. Use `neutrino-factory list-generators --built` to see which catalogued
images are present locally.

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

Early-stage but functional locally. GENIE, NuWro, and GiBUU build and run in
Docker, and every generator also runs in synthetic `stub_mode` for
development without real binaries. Known gaps are tracked in the source tree:
NuWro/GiBUU still use a monoenergetic flux approximation, the GiBUU ROOT→HDF5
normalizer is a stub, NEUT is blocked on source availability, and the Slurm
submission path has only been exercised in `--dry-run` mode (no live cluster
access yet).
