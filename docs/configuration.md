# Configuration

The common YAML config is the source of truth for both the local and Slurm execution paths.

## Top-level sections

- `run`: run name, total event count, seed, executor mode, and stub-mode toggle
- `flux`: neutrino flux model and energy range
- `target`: nuclear target description
- `physics`: generic interaction settings
- `generators`: versioned generator entries (multiple entries per generator are supported)
- `splitting`: how the total event count is chunked into jobs
- `storage`: roots for software, outputs, working files, and container images
- `slurm`: job resources for MPP submission (default partition: `alma` — required by the new MPP Slurm cluster)

## Environment: `.env` and precedence

`neutrino-factory setup` writes the storage roots and the container runtime
choice (`NF_CONTAINER_RUNTIME=docker|apptainer|auto`) to a `.env` file at the
repo root. Every CLI invocation, setup script, rendered sbatch script, and
task launcher loads this file automatically with **setdefault semantics**, giving
the precedence order:

1. real environment variables (always win),
2. `.env` values,
3. built-in defaults (`./software`, `./output`, `./work`, `./software/images`).

The `storage` config values reference the same variables via `${VAR:-default}`
expansion, so one `.env` drives the YAML config, the shell scripts, and the
Slurm jobs consistently.

## Slurm manifest schema (v3)

`neutrino-factory submit --executor slurm` writes `work/manifests/<run>.json`
with a compact schema (manifest version 3).

Top-level keys:
- `manifest_version`
- `created_utc`
- `run_name`
- `config_path`
- `executor`
- `tasks`

Task keys:
- `task_index`
- `generator_name`
- `code_version`
- `config_version`
- `chunk_id`
- `start_event`
- `event_count`
- `seed`
- `run_name`
- `flux`

Derived values (not stored as explicit task fields):
- generator version identifier: `<code_version>+<config_version>`
- tokenized version directory name: non `[A-Za-z0-9._-]` replaced with `_`
- total task count: `len(tasks)`

## Example

Use `configs/examples/power_law_numu_Ar.yaml` as the initial reference.

For GENIE-specific tune runs with staged precomputed cross sections:

```yaml
generators:
  genie:
    versions:
      - enabled: true
        code_version: R-3_06_00
        config_version: G18_10a_02_11a
      - enabled: true
        code_version: R-3_06_00
        config_version: AR23_20i_00_000
```

If `genie/genie_xsec/<tag-safe>/<config_version>/xsecs.xml` exists under
`storage.software_root`, GENIE execution adds `--cross-sections` automatically
(staged from FNAL's `gxspl-NUsmall.xml` by `setup/download_genie_xsec.sh`).
Computing splines on the fly is expensive, and flux-driven runs require them, so
staging these files is strongly recommended. `neutrino-factory list-generators`
only lists a GENIE tune as available once its `xsecs.xml` is present on disk.

Each enabled generator entry computes a generator-specific version identifier. For GENIE this is
`<code_version>+<config_version>`. This identifier is used in task planning and output layout so multiple versions of one
generator can run in the same campaign.

## Stub-mode recommendation

For development on a machine without generator images:

```yaml
run:
  executor: local
  stub_mode: true
```

This keeps the pipeline fully testable while the real generator commands and cluster deployment are being refined.
