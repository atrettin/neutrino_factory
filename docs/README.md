# neutrino-factory documentation

The project's knowledge base: how the framework is put together, what each
generator does and why, and how to run it locally and on the cluster. Start from
the table below rather than reading in file order — each document is
self-contained and cross-links to the others.

For the project pitch, installation and a quickstart, see the
[repository README](../README.md).

## Start here

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | The module map and data flow of the Python package, and the run-configuration model (a run is a list of jobs). Read before changing how a config becomes tasks or where outputs land. |
| [configuration.md](configuration.md) | The run YAML schema in full: sections, jobs, macros and matrix expansion, job labels and the output layout, seeds, `physics.current`, `.env` precedence, and the Slurm task-manifest schema. |

## Domain knowledge

| Document | What it covers |
|---|---|
| [physics.md](physics.md) | The physics contract every backend is normalized onto: units and frames, the `xsec_weight` definition and its per-nucleon convention, chunk merging as an average, flux density vs. per-bin integrals, probes and targets, weak current, the interaction taxonomy, derived kinematics, and weighted statistics. |
| [generators/genie.md](generators/genie.md) | GENIE: runtime-discovered tunes, the two-stage `gevgen` → `gntpc` run, why the flux histogram is written by hand, and how σ is reconstructed by summing cross-section splines. |
| [generators/nuwro.md](generators/nuwro.md) | NuWro: the inline `beam_energy` spectrum in MeV, the `dyn_*` channel switches, and why its per-event weight is a single run-wide constant that is already per nucleon. |
| [generators/neut.md](generators/neut.md) | NEUT: the payload extracted from a published image, the 80-character Fortran path limit, the `NEUT-CRS` slot tables that substitute for a CC/NC switch, the `nf_flatten.C` second stage, and the mode → interaction table. |
| [generators/gibuu.md](generators/gibuu.md) | GiBUU: cross-section weighting rather than rejection sampling, and everything that follows — `num_runs` as the merge denominator, `inclusive` as two concatenated passes, negative weights, and the `evType` table. |
| [generator_versioning.md](generator_versioning.md) | The two-axis `code_version` + `config_version` model, adapters as the source of truth for versions, runtime tune discovery, and validation strictness. |

## Operations

| Document | What it covers |
|---|---|
| [containers.md](containers.md) | The two container pathways (Docker locally, Apptainer on the cluster), how `NF_CONTAINER_RUNTIME` selects between them, image naming from the version catalog, and the Dockerfile ↔ def pairing. |
| [apptainer_image.md](apptainer_image.md) | How the unified `nf-base.sif` is composed from per-generator payload SIFs, and how adapters dispatch into it via `nf-run`. |
| [mpp_cluster_usage.md](mpp_cluster_usage.md) | The MPCDF/ODSL runbook: execution model, filesystems, the mandatory `--partition=alma`, first-time setup, submitting a run, working up from a first scheduler-only submission, and troubleshooting. |
| [performance.md](performance.md) | Measured generator runtimes on one machine: the machine facts, per-generator and per-GENIE-tune wall clocks and per-event rates at numu/Fe56/CC, the fixed overheads (GENIE's ~23 s spline load, NEUT's ~142 s), the 27× tune-rate spread and its model-construction cause, and Slurm-sizing formulae. |

## Extending

| Document | What it covers |
|---|---|
| [adding_generators.md](adding_generators.md) | The four places to touch when adding a backend, the Apptainer payload contract, and the variants (a generator you cannot build from source; a second processing stage). |

## History

| Document | What it covers |
|---|---|
| [design_decisions.md](design_decisions.md) | A dated log of architectural rationale and caveats — why things are the way they are, and what was ruled out. Entries whose subject later moved into a topic document above are kept as dated pointers. |
