# Configuration

The common YAML config is the source of truth for both the local and Slurm execution paths.

## Top-level sections

- `run`: run name, seed, executor mode, stub-mode toggle, and default generator log verbosity
- `macros` (optional): reusable parameterized job templates
- `jobs`: the run itself — one entry per generator version x initial state
- `storage`: roots for software, outputs, working files, and container images
- `slurm`: job resources for MPP submission (default partition: `alma` — required by the new MPP Slurm cluster)

Nothing about the physics is configured globally. See `configs/schema/run_config.yaml`
for the annotated template.

## Jobs

A **job** is one generator version on one initial state, with its own event
budget and chunking:

```yaml
jobs:
  - name: optional_explicit_label     # otherwise derived; see "Job labels" below
    generator: genie                  # required
    code_version: "R-3_06_00"         # required
    config_version: "G18_10a_02_11a"  # required
    events: 100000                    # default 100
    chunks: 20                        # default 1; must not exceed `events`
    log_level: essential              # optional; overrides run.log_level for this job
    flux: {type: power_law, particle: numu, emin_gev: 0.1, emax_gev: 50.0, gamma: -2.0}
    target: {nucleus: C12}
    physics: {mode: inclusive, current: cc}
```

The per-job flux and event budget are what make a single-file production run
possible. The generators do not agree on what a run is: GiBUU samples phase
space and weights events by cross section rather than rejection-sampling them,
so it needs a flatter spectrum (`gamma = -1`, flat in ln E) and an order of
magnitude more events than GENIE, NuWro or NEUT to reach the same statistical
uncertainty. Under a single global `flux` and `events` those runs had to live in
separate files and separate submissions.

## Macros and matrix expansion

`macros` and `matrix` are sugar over the `jobs` list. They are expanded away
before anything else reads the configuration — validation included — so nothing
downstream knows they exist. `neutrino-factory expand --config <cfg>` prints
exactly what they materialize to; `--json` adds the full job dictionaries with
the parameter set each came from.

```yaml
macros:
  rejection:
    params:
      generator: null        # null = required: must be supplied
      code_version: null
      config_version: null
      particle: numu         # anything else = a default
      nucleus: C12
    job:
      generator: "{{generator}}"
      code_version: "{{code_version}}"
      config_version: "{{config_version}}"
      events: 200000
      chunks: 20
      flux: {type: power_law, particle: "{{particle}}", emin_gev: 0.1, emax_gev: 50.0, gamma: -2.0}
      target: {nucleus: "{{nucleus}}"}

jobs:
  - use: rejection
    matrix:
      particle: [numu, numubar]
      nucleus: [C12, O16, Ar40]
      include:
        - {generator: genie, code_version: "R-3_06_00", config_version: "G18_10a_02_11a"}
        - {generator: nuwro, code_version: "nuwro_25.11", config_version: "default"}
      exclude:
        - {generator: nuwro, nucleus: Ar40}
```

**Placeholder syntax.** A value that is *only* a placeholder **must be quoted** —
bare `{{x}}` is YAML flow-mapping syntax and fails to parse. A quoted whole-value
placeholder keeps the substituted value's **type**: `"{{events}}"` with
`events: 100000` yields the integer `100000`, so numeric fields can be
templated. A placeholder inside a longer string (`scan_{{particle}}`)
interpolates as text and the result stays a string; it is never re-parsed as
YAML. The `${VAR:-default}` environment syntax is unrelated, works anywhere
including inside a macro body, and cannot collide with `{{...}}`.

**Precedence**, lowest to highest: macro `params` defaults, then `with:`, then
the matrix combination, then keys written directly on the `jobs` entry (deep-merged
last, which is how a single use overrides one field of a macro). Supplying a
parameter the macro does not declare is an error, not a no-op — it is almost
always a typo that would otherwise leave the intended field at its default.

**`include` is a coupled axis**, not GitHub Actions' append-and-patch `include`:
each mapping supplies several parameters that travel together and is
cross-multiplied with the ordinary axes. That is how the (generator,
code_version, config_version) triple stays consistent while flavour and nucleus
vary independently. **`exclude`** drops combinations matching all of an entry's
key/value pairs; an exclude naming a key no combination has is an error, since a
silent no-op would generate jobs the user believes are gone.

A `matrix` without a `use` is also valid: the entry itself becomes the body and
its parameters are whatever `matrix`/`with` supply.

## Job labels and output layout

Each job gets a label, used in every path it produces:

```
<generator>_<code_version>+<config_version>_<particle>_<nucleus>_<current>
```

tokenized so it is filesystem-safe (`+` and other non-`[A-Za-z0-9._-]`
characters become `_`), or the job's explicit `name:` verbatim. So:

```
work/raw/<label>/<run>_<label>_chunkNNN/
output/chunks/<label>/<run>_<label>_chunkNNN.h5
output/merged/<run>_<label>.h5
```

A label is a **pure function of its own job** and is never disambiguated against
its neighbours. Labels end up in filenames of physics samples, and adding an
unrelated job to a configuration must not silently rename another job's outputs.
Two jobs whose labels collide are therefore a hard error naming both entries;
give one of them an explicit `name:` (a macro can template it, e.g.
`name: "{{particle}}_{{nucleus}}_{{emin}}to{{emax}}"`).

## Target PDG

`target.pdg` is derived from `target.nucleus` using the standard `10LZZZAAAI`
nuclear code (`C12` -> `1000060120`), so a hand-written PDG can no longer
disagree with the nucleus it claims to describe. An explicit `pdg:` still
overrides and logs a warning when the two disagree. Nucleus names are parsed as
an element symbol plus mass number (`C12`, `Ar40`, `Fe56`, `Xe136`); an
unparsable or impossible name fails at configuration time rather than inside a
running Slurm task. The generators' (Z, N) composition is derived the same way,
so any isotope a generator itself supports is usable without a code change.

## Seeds

A task's seed is `blake2b(run.seed | job label | chunk id)` reduced into
`[1, 2_000_000_000]` — positive and inside the signed 32-bit range Fortran
generators need. Hashing rather than offset arithmetic (`run.seed +
generator_index * 1000 + chunk`) means a seed depends only on the run seed and
the chunk's own identity, so inserting or removing a job never shifts another
job's random stream, and there is no chunk count at which the layout collides.
`build_task_manifest` still asserts that all seeds in a manifest are distinct.

## Weak current: `physics.current`

| Value | What is generated |
|---|---|
| `cc` (default) | Charged-current interactions only. |
| `nc` | Neutral-current interactions only. |
| `inclusive` | Both, in the generator's own cross-section proportion. |

The restriction is applied in the generator's **own configuration**, not by
filtering events afterwards, so the requested event count is met and the cross
section reconstructed into `xsec_weight` is that of the selected current alone:

| Generator | Native mechanism |
|---|---|
| GENIE | `--event-generator-list CC` / `NC`; omitted for `inclusive`. The cross-section spline sum used for `xsec_weight` is filtered on the matching `proc:Weak[CC]`/`proc:Weak[NC]` tag so it covers exactly the channels that could be generated. |
| NuWro | The `dyn_*` switches in `params.txt` (all ten `dyn_<channel>_<current>`, plus `dyn_hyp_cc`). |
| NEUT | `NEUT-MODE -1` with a `NEUT-CRS`/`NEUT-CRSB` mask that zeroes the other current's channels; `inclusive` uses NEUT's normal `NEUT-MODE 0`. |
| GiBUU | `process_ID` (CC=2, NC=3, negated for antineutrinos). A jobcard selects a single current, so `inclusive` runs **two passes** (see below). |

Every event in the common HDF5 output carries the resulting current as the
boolean `is_cc` column. It is a required column with no default: the
`interaction` label alone cannot recover it, since categories such as `qel`
deliberately span both currents (GENIE's `qel` flag and NEUT's modes 1 and 51/52
all map to it).

**GiBUU `inclusive` is two runs.** GiBUU's jobcard admits exactly one
`process_ID`, so an inclusive run generates a CC pass and an NC pass in separate
subdirectories of the task work directory (`cc/`, `nc/`) and concatenates their
events. This is exact rather than approximate because GiBUU weights each event
by an absolute per-nucleon cross section, so the two passes' weights add to
sigma_CC + sigma_NC. The requested event count is the budget for the run as a
whole: the ensembles are split evenly over the passes.

Note that GiBUU's *event counts* do not carry the CC:NC ratio the way the other
generators' do — it samples phase space and weights by cross section, so an
inclusive GiBUU run comes out near half CC by event count while the cross-section
split lives in `xsec_weight` (measured: 45% of events CC, 69% of sigma CC, on a
0.5–5 GeV numu carbon run). Always split GiBUU output by weight, not by counting.

**NuWro's neutrino-electron channel is off** (`dyn_lep = 0`) for every current,
including `inclusive`, although NuWro's own default enables it. Its target is an
atomic electron rather than a nucleon, so its cross section is not commensurable
with the per-nucleon normalization the common output's `xsec_weight` uses.

## Generator log verbosity: `run.log_level`

Generators are extremely chatty by default. `run.log_level` controls how much of
their own output reaches stdout (and hence `work/logs/*.out` under Slurm):

| `run.log_level` | What you get |
|---|---|
| `default` | The generator's stock logging (unchanged behaviour). |
| `verbose` | Debug-level logging, for chasing a generator-internal problem. |
| `essential` | The initial job configuration, the output-file writes, and warnings/errors. |
| `quiet` | The initial job configuration plus warnings/errors only — constant-size output. |

Measured on a 5-event GENIE run (`configs/smoke/genie_c12.yaml`): 32928 lines at
`default`, 270 at `essential`.

**Picking between `essential` and `quiet`:** GENIE emits the output-file writes
and a one-line-per-event `Adding event N to output tree` on the *same* stream at
the same priority, so `essential` keeps one short line per generated event —
still ~200x less than `default`, but it grows with the event count. For large
production arrays where even that is too much, use `quiet`, whose output size
does not depend on the event count.

The names are generator-agnostic, but **only the GENIE adapter acts on them so
far**; NuWro, NEUT and GiBUU currently ignore the setting.

For GENIE the level is mapped onto `gevgen`/`gntpc`'s `--message-thresholds`
option, using the messenger presets shipped inside the GENIE image
(`Messenger_rambling.xml`, `Messenger_laconic.xml`). `essential` adds a small
generated overlay (`nf_messenger_essential.xml`, written into the task's work
directory) that re-raises GENIE's `Ntp` stream so the output ROOT file is still
named as it is opened and saved.

The job-configuration banner survives `essential` and `quiet` because GENIE
applies `--message-thresholds` in `Initialize()`, *after* `GetCommandLineArgs()`
has already printed it. Everything from cross-section spline loading onwards —
the `XSecSplLst` spline-by-spline `NOTICE`s, the `GMCJDriver` chatter about flux
rays that did not interact, and the per-event GHEP record dumps — is silenced.

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

## Slurm manifest schema (v4)

`neutrino-factory submit --executor slurm` writes `work/manifests/<run>.json`
with a compact schema (manifest version 4).

Top-level keys:
- `manifest_version`
- `created_utc`
- `run_name`
- `config_path`
- `executor`
- `jobs` — the materialized jobs, recorded once as provenance for what was planned
- `tasks`

Task keys:
- `task_index`
- `job_index`, `job_label` — the job this task belongs to
- `generator_name`
- `code_version`
- `config_version`
- `chunk_id`
- `start_event`
- `event_count`
- `seed`
- `run_name`
- `flux`

One task is produced per (job, chunk). Jobs are heterogeneous, so the total task
count is the **sum** over jobs of each job's own chunk count, never a product.

`job_index` resolves against the configuration at run time (`local.job_view_config`),
and `job_label` is cross-checked against it: a manifest written before the
configuration was edited is detected and reported instead of silently running
the wrong job.

**One array, one set of resources.** `slurm.time`, `mem` and `cpus_per_task`
apply to the whole array, but jobs are now heterogeneous — a 200k-event GENIE job
and a 2M-event GiBUU job share one wall clock. Size them for the longest and
largest job, not the average.

## Example

Use `configs/examples/power_law_numu_Ar.yaml` as the initial reference and
`configs/examples/production_grid.yaml` for a full macro + matrix production run
(30 jobs across two flavours, three nuclei and five generator versions, with
GiBUU on its own flux and statistics).

For GENIE-specific tune runs with staged precomputed cross sections:

```yaml
jobs:
  - use: rejection
    matrix:
      include:
        - {generator: genie, code_version: "R-3_06_00", config_version: "G18_10a_02_11a"}
        - {generator: genie, code_version: "R-3_06_00", config_version: "AR23_20i_00_000"}
```

If `genie/genie_xsec/<tag-safe>/<config_version>/xsecs.xml` exists under
`storage.software_root`, GENIE execution adds `--cross-sections` automatically
(staged from FNAL's `gxspl-NUsmall.xml` by `setup/download_genie_xsec.sh`).
Computing splines on the fly is expensive, and flux-driven runs require them, so
staging these files is strongly recommended. `neutrino-factory list-generators`
only lists a GENIE tune as available once its `xsecs.xml` is present on disk.

Each job computes a generator-specific version identifier. For GENIE this is
`<code_version>+<config_version>`. It forms part of the job label, so multiple
versions of one generator can run in the same campaign without their outputs
colliding.

## Stub-mode recommendation

For development on a machine without generator images:

```yaml
run:
  executor: local
  stub_mode: true
```

This keeps the pipeline fully testable while the real generator commands and cluster deployment are being refined.
