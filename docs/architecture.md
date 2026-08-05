# Architecture

`neutrino-factory` is a Python CLI that orchestrates Monte Carlo neutrino event
generators (GENIE, NuWro, NEUT, GiBUU) for Slurm cluster execution, with a local
mode for development. Every generator's output is normalized into one common
HDF5 format.

This document is the module map and the data flow. The configuration *schema*
lives in [configuration.md](configuration.md); the rationale behind specific
choices lives in [design_decisions.md](design_decisions.md).

## Source layout

- `src/neutrino_factory/cli.py` — CLI entry point (subcommands: `setup`,
  `validate-config`, `expand`, `plan`, `submit`, `run-task`, `merge`,
  `list-generators`, `check-status`, `plot-output`, `analyze-kinematics`);
  `main()` loads the repo-root `.env` first
- `src/neutrino_factory/config.py` — YAML loading, env-var expansion,
  validation, defaults; `.env` loader (`load_env_file`, setdefault semantics —
  real env wins)
- `src/neutrino_factory/jobs.py` — job expansion: `macros` + `matrix` into
  concrete jobs, job labels, hashed task seeds. Runs before validation, so
  nothing downstream knows macros exist
- `src/neutrino_factory/layout.py` — the single source of truth for output paths
  (raw, chunk, merged), keyed by job label; used by both `local.py` and
  `validate_output.py`
- `src/neutrino_factory/containers.py` — container runtime selection
  (`NF_CONTAINER_RUNTIME`: docker/apptainer/auto), SIF path mapping under
  `NF_IMAGE_ROOT`, runtime-aware `image_available`, and `docker_wrap` (the only
  place a container command is constructed)
- `src/neutrino_factory/setup_wizard.py` — interactive `neutrino-factory setup`
  (pathway choice, storage directories, writes `.env`, offers builds)
- `src/neutrino_factory/slurm.py` — task manifest building, sbatch script
  rendering
- `src/neutrino_factory/local.py` — local pipeline (`run_task`, `run_local`,
  `run_task_from_manifest`) and `job_view_config`
- `src/neutrino_factory/common_output.py` — HDF5 read/write/merge (the common
  output format)
- `src/neutrino_factory/merge.py` — thin wrapper over
  `common_output.merge_hdf5_files`
- `src/neutrino_factory/catalog.py` — generator-agnostic version catalog
  *facade*: dispatches version queries (code versions, config versions, image
  tag, buildability, validation) to each generator's adapter class via the
  registry, plus generator-agnostic helpers (`version_identifier`,
  `image_built`, `_tag_safe`). Per-generator version knowledge lives on the
  adapters (`CODE_VERSIONS` + version-API classmethods in `generators/base.py`),
  not here
- `src/neutrino_factory/particles.py` — the six neutrino probes and their PDG
  codes, plus nucleus name parsing (`nucleus_pdg`, `nucleus_composition`)
- `src/neutrino_factory/generators/` — `GeneratorAdapter` base class + the
  per-generator adapters
- `src/neutrino_factory/translators/` — `ConfigTranslator` per generator
  (neutrino_factory config → generator-native params)
- `src/neutrino_factory/normalizers/` — `OutputNormalizer` per generator (raw
  output → common HDF5)
- `src/neutrino_factory/kinematics.py` — the derived kinematic columns, one
  shared formula for every generator (see [physics.md](physics.md))
- `src/neutrino_factory/plots.py` — plotting of common-output files. Split into
  axis-level helpers that draw onto a caller-owned axes (`plot_interactions`,
  `plot_energy`, `plot_xsec_by_interaction`), figure builders (`figure_xsec`,
  `figure_channel_comparison`) that take `plt` as an argument, and the batch
  entry points `make_plots` / `make_config_plots` behind `plot-output`. Only the
  batch entry points import pyplot themselves, and they force the non-interactive
  `Agg` backend — which is what lets an interactive caller reuse the same helpers
  under its own backend
- `src/neutrino_factory/kinematics_report.py` — console report behind
  `analyze-kinematics` (weighted per-variable statistics, Kish effective sample
  size)

Outside the package, `notebooks/explore_output.ipynb` is a guided Jupyter tour of
a single output file — the HDF5 layout, the placeholder convention, cross section
vs. energy, the kinematic variables weighted by `xsec_weight`, and a chosen
energy slice as a double differential cross section in `x` and `y`. It imports
the axis-level helpers from `plots.py` rather than restating them, so the
interactive and batch figures cannot drift apart. Jupyter is the optional
`notebook` extra in `pyproject.toml`; nothing in the package imports it.

## Data flow

```
config YAML
  → config.py: merge defaults, expand ${env} vars,
               expand macros/matrix into concrete jobs (jobs.py), validate
  → slurm.py: task manifest (JSON), one task per job × chunk
  → per task (local.py):
        job_view_config  → translate config → run generator (real or stub)
                         → normalize output to HDF5
  → merge each job's chunks into one HDF5 file
```

## The run-configuration model

A run is a **list of jobs**. A job is one generator version on one initial state
(flux × target × physics) with its own event budget and chunking — nothing about
the physics is configured globally. This is what lets one file describe a whole
production grid, including generators that need different fluxes and statistics
from each other; see [configuration.md](configuration.md) for the schema and
[design_decisions.md](design_decisions.md) for why.

**Adapters, translators and normalizers never see the jobs list.** They read
`config["flux"]`, `config["target"]` and `config["physics"]` exactly as before;
`local.job_view_config` grafts the running job's blocks onto the global
configuration at the one point where a task becomes work. Those three blocks are
*replaced* rather than merged — a histogram-flux job merged over a power-law base
would carry both sets of keys and describe two different fluxes at once.

Consequences worth knowing when changing this layer:

- Anything that predicts or writes an output path must go through `layout.py`.
  `local.py` writes the files and `validate_output.expected_outputs` predicts
  them for `check-status`, `merge --config`, `plot-output` and
  `analyze-kinematics`.
- Anything that groups outputs — merging, plotting — must group by **job**, not
  by `(generator, version)`. Two jobs can share a generator version and differ
  in neutrino flavour or target nucleus.
- Jobs are heterogeneous, so a run's task count is the **sum** over jobs of each
  job's chunk count, never a product.
