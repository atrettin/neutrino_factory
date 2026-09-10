# AGENTS.md — OpenCode instructions for neutrino-factory

## Project purpose
Neutrino monte carlo event generator orchestration. Compare cross sections between physics models (GENIE, NuWro, NEUT, GiBUU). Academic use only.

## Key conventions
- **A run is a list of jobs**, each pairing one generator version (`code_version` + `config_version`) with one initial state. Group outputs by job, never by `(generator, version)`.
- Cross sections in `xsec_weight` column: **1e-38 cm² per target nucleon**. Kinematics: lab-frame, GeV/GeV². Merging chunks **averages** weights; never sums.
- Fail loudly instead of filling placeholder values that could be mistaken for physical data.
- Container images derive from adapters' version catalogs — no manual image config key.
- Bash scripts: Always run `bash -n <script>` to check syntax after editing.

## Developer commands
Before running any of the commands below, ensure the correct environment is active. Follow this logic flow:

1. **Check if `neutrino-factory` command resolves**: `which neutrino-factory`
   - If yes: All commands are available, run them directly
   - If no: Continue to step 2

2. **Determine environment type**:
   - **Local environment**: Check for `.venv` directory in repo root
     - If `.venv` exists: Activate with `source .venv/bin/activate`
     - If no `.venv`: Create with `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
   - **Cluster environment**: Check for `apptainer` command availability
     - If `apptainer` is available: You're in a bare shell on the cluster
       - Run `neutrino-factory` commands with `apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" env PYTHONPATH="$PWD/src" python3 -m neutrino_factory.cli <command> ...`
       - Run `apptainer` and `bash` commands directly in the bare shell
     - If `apptainer` is NOT available: You're already inside a container
       - If `neutrino-factory` is still not available: STOP and run `pip install -e ".[dev]"` to install the package and its requirements
       - If `neutrino-factory` IS available: Continue with commands directly

- Lint: `pyright`
- Test: `python -m pytest` (single file: `python -m pytest tests/test_<module>.py`)
- Validate config: `neutrino-factory validate-config --config <path>`
- Local smoke test: `neutrino-factory submit --config <path> --executor local`
- Render Slurm script: `neutrino-factory submit --config <path> --executor slurm --dry-run`
- Merge outputs: `neutrino-factory merge --config <path>`
- Analyze kinematics: `neutrino-factory analyze-kinematics --config <path>`

## Cluster constraints
- **No Docker** on MPP cluster — use Apptainer only. Workflows:
  - **Bare shell (host)**: First-time setup and Apptainer image management (building, pulling) must run from a plain host shell on an interactive cluster node using Bash scripts only. No Python available.
  - **Inside `nf-base.sif`**: Production runs (`neutrino-factory` CLI, generator execution, Python scripts) via `cenv nf-env`.
  - **Inside `nf-dev.sif`**: Contains all packages `nf-base.sif` contains, and also packages necessary for development, testing and remote management with VSCode (`curl`, `git`, `nano`, `wget`, `pyright`, `pytest`, etc.). See `setup/apptainer/nf-dev.def` for full package list. If you are on a cluster, can run `neutrino-factory` but are missing the development packages, STOP and tell the user to build the development image. This CANNOT be done by you if you are already running from inside a container, because `apptainer` is not available.
- Cluster scripts use `apptainer` directly; interactive work via `cenv nf-env` or `cenv nf-dev`
- Mandatory Slurm partition: `--partition=alma` on new cluster. Max duration: 1 day.
- Filesystems: `/u` (home, 125 GB, backed up), `/ptmp/mpp/$USER` (6 TB, shared, no backup). Use `/ptmp` for repo, images, output, work. `/scratch` is NOT accessible inside containers.

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