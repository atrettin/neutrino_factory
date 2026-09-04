# AGENTS.md — OpenCode instructions for neutrino-factory

## Project purpose
Neutrino monte carlo event generator orchestration. Compare cross sections between physics models (GENIE, NuWro, NEUT, GiBUU). Academic use only.

## Key conventions
- **A run is a list of jobs**, each pairing one generator version (`code_version` + `config_version`) with one initial state. Group outputs by job, never by `(generator, version)`.
- Cross sections in `xsec_weight` column: **1e-38 cm² per target nucleon**. Kinematics: lab-frame, GeV/GeV². Merging chunks **averages** weights; never sums.
- Fail loudly instead of filling placeholder values that could be mistaken for physical data.
- Container images derive from adapters' version catalogs — no manual image config key.

## Environment setup
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
neutrino-factory setup --pathway docker  # writes .env
```
 purge venv and SIF files from version control.

## Developer commands
- Lint: `pyright`
- Test: `python -m pytest` (single file: `python -m pytest tests/test_<module>.py`)
- Validate config: `neutrino-factory validate-config --config <path>`
- Local smoke test: `neutrino-factory submit --config <path> --executor local`
- Render Slurm script: `neutrino-factory submit --config <path> --executor slurm --dry-run`
- Merge outputs: `neutrino-factory merge --config <path>`
- Analyze kinematics: `neutrino-factory analyze-kinematics --config <path>`

## Cluster constraints
- **No Docker** on MPP cluster — use Apptainer only. Two workflows exist:
  - **Bare shell (host)**: First-time setup and Apptainer image management (building, pulling) must run from a plain host shell on an interactive cluster node using Bash scripts only. No Python available.
  - **Inside `nf-base.sif`**: All framework usage (`neutrino-factory` CLI, generator execution, Python scripts) requires entering the Apptainer image first (via `cenv nf-env` or `apptainer exec`).
- Cluster scripts use `apptainer` directly; interactive work via `cenv nf-env`.
- Mandatory Slurm partition: `--partition=alma` on new cluster. Max duration: 1 day.
- Filesystems: `/u` (home, 125 GB, backed up), `/ptmp/mpp/$USER` (6 TB, shared, no backup). Use `/ptmp` for repo, images, output, work. `/scratch` is NOT accessible inside containers.
- outbound HTTPS works for downloading sources during container builds.
- OpenCode agents: cannot run cluster code directly. Provide user with concise commands to run and report results.

## Architecture essentials
- Entry point: `neutrino-factory` CLI (`src/neutrino_factory/cli.py`)
- Config: YAML schema in `configs/`, validated by `config.py`
- Generators run in containers (Docker locally, Apptainer on cluster) selected by `NF_CONTAINER_RUNTIME` (auto-prefers Docker)
- Output format: HDF5 with `metadata`, `run`, `events` groups. Common columns defined in `common_output.py`
- Normalization: each generator's raw output → common HDF5 via per-generator normalizers
- Version catalog: `src/neutrino_factory/catalog.py` maps `code_version` + `config_version` to container images

## Testing
- Tests focus on correctness of mathematical functions and core logic, not full coverage
- Plotting code excluded from tests (likely to change without consequence)
- Run specific tests: `python -m pytest tests/test_<module>.py::TestClass::test_method`
- Generated test data lives in `tests/` or `scratch/` — do not commit to repo

## Documentation
- Primary docs: `docs/README.md` (index), `docs/architecture.md`, `docs/configuration.md`
- Generator-specific: `docs/generators/<name>.md`
- Cluster runbook: `docs/mpp_cluster_usage.md`
- Physics conventions: `docs/physics.md`
- Design decisions: `docs/design_decisions.md`

## Important paths
- `configs/` — schema templates and example configs
- `setup/` — Dockerfiles, Apptainer defs, build scripts
- `src/neutrino_factory/` — Python package
- `jobs/` — Slurm wrappers
- `notebooks/` — Jupyter notebooks (optional dependency)
- `NF_SOFTWARE_ROOT` — generator binaries and xsec files
- `NF_OUTPUT_ROOT` — normalized HDF5 output
- `NF_WORK_ROOT` — manifests, plans, logs, temp metadata
- `NF_IMAGE_ROOT` — container images (SIF files)

## Reference files
- `ENVIRONMENT.md` — current environment facts (machine, cluster access, Docker/Apptainer status)
- `.env` — written by `neutrino-factory setup`, loaded by CLI and scripts
- `docs/README.md` – Framework documentation index. When making changes to the framework, always keep the documentation updated by using the `docs-maintenance` skill in the same session.