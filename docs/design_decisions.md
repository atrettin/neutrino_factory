# Design decisions

A dated log of architectural rationale and caveats: why the framework is built
the way it is, and what was ruled out along the way. It records *decisions*;
what is true of the code today is described in the topic documents listed in
[README.md](README.md).

Entries whose subject matter later moved into a topic document are kept here as
dated pointers, so the chronology stays navigable.

## Dual container pathways: Docker locally, Apptainer on the cluster (2026-07)

**Decision.** Generators (and, on the cluster, the CLI itself) run in
containers under one of two runtimes selected by `NF_CONTAINER_RUNTIME`:
Docker for local development, Apptainer for the MPCDF/ODSL cluster. Earlier
docs claimed Docker was the long-term HPC strategy; that was wrong — the
cluster forbids Docker (root escalation) and supports only Apptainer.

**Why env-driven runtime selection (not YAML).** The same run config must work
unchanged on the laptop and the cluster; only the machine differs. Runtime and
image location are machine facts, so they live in the environment
(`NF_CONTAINER_RUNTIME`, `NF_IMAGE_ROOT`), persisted per checkout in the
repo-root `.env` written by `neutrino-factory setup`. Precedence: real env >
`.env` > defaults.

## Unified Apptainer runtime image on the cluster

**Decision.** On the cluster, Python still never launches containers from
inside Python. However, Slurm now enters one unified runtime image
(`nf-base.sif`) for every task, and that image provides both the framework
Python runtime and generator entry wrappers.

**Why.**
- Apptainer-in-Apptainer does not work on this cluster (user-tested), so the
  local Docker pattern (Python wraps the generator in a container command)
  still cannot transfer.
- MPCDF removed the module system; the host Python is 3.9. Any modern Python
  must itself come from a container.
- MPCDF auto-mounts `/u`, `/ptmp`, `/cvmfs` inside containers, so host paths
  resolve unchanged inside. (`/scratch` turned out not to be mounted inside
  containers, so the project dropped its scratch-root concept entirely —
  everything lives on `/ptmp`.)

**Consequences.**
- The native-binary-first ordering in every adapter's `build_run_command`
  remains load-bearing on the cluster.
- The unified `nf-base.sif` carries one Python runtime plus framework
  dependencies; the project code is still provided from the repo checkout via
  `PYTHONPATH`, so code changes need no image rebuild.
- `sbatch` is invisible inside containers, so `submit` prints the `sbatch`
  command for the user to run in a host shell instead of failing.

## Hand-written Apptainer definitions (no spython, no image transfer)

**Decision.** Each `setup/apptainer/<gen>.def` is written by hand, mirroring
its `setup/Dockerfile.<gen>` (multi-stage structure preserved via `Stage:` +
`%files from`), and SIFs are built natively on odslserv01/02. No
Docker-tarball transfer, no registry.

**Why.** Automated Dockerfile→def translation (spython) is unreliable for
multi-stage Dockerfiles and would add a dependency for a one-shot command;
native cluster builds avoid multi-GB image transfers and produce genuinely
native x86_64 binaries (native builds also helped rule emulation out as the
cause of the GENIE MEC crash, later traced to ROOT being built without
Minuit2). The cost is deliberate duplication: **the Dockerfile and the def are
parallel implementations of one build and are only correct while they agree**
(each def's header names its source).

**Divergences allowed between the pairs**: defs drop `--platform` pinning, add
the Python runtime deps, and (NuWro) relocate the ROOTEGPythia6 build dir out
of `/tmp`, because Apptainer bind-mounts the host `/tmp` over the image's at
runtime and would shadow the baked-in dictionary path.

## Discovery-based, version-namespaced Apptainer composition

**Decision.** `nf-base.sif` is composed by *auto-discovering* every built
per-generator payload SIF from the catalog, not from a fixed generator list.
Each payload stages a **self-contained, version-namespaced** tree under
`/opt/nf/generators/<gen>/<code_version>/` and ships its own `bin/<binary>`
wrapper plus an `nf-payload.json` descriptor. `build_apptainer_images.sh`
generates the composed def (one `localimage` stage + one generic `%files` copy
per payload) and `nf-base.def` is a generator-agnostic tail that installs a
single `nf-run <gen> <code_version> <binary>` dispatcher and descriptor-driven
default-version symlinks. Under the apptainer runtime each adapter's
`build_run_command` rewrites the native command to the explicit `nf-run` form
via `containers.apptainer_dispatch`.

**Why.**
- Extensibility was the requirement: adding a generator or a new version of an
  existing one must not touch `nf-base.def` or the build script's generator
  logic. Discovery + generated stages achieve that; the only inputs are a
  `<gen>.def` and a `CODE_VERSIONS` entry (with `build_arg_name`).
- **Multiple versions of one generator must coexist** in the same image.
  Version-namespaced payload paths + explicit `nf-run` version dispatch make
  that possible; distinct image tags (`<name>:<code_version>`) already yield
  distinct SIF filenames.
- Per-generator env/launch knowledge was duplicated in up to five places, with
  the *richest* form (NuWro's `-i/-o` absolutize + `cd`, GiBUU's binary `find`)
  living wrongly inside `nf-base.def`. Moving it into each payload's shipped
  wrapper makes the payload the single source of truth.

**Consequences / trade-offs.**
- The adapters' native-binary-first probe (`shutil.which`) is preserved by
  keeping bare default-version symlinks; the executed command still names the
  version explicitly. Detection logic is unchanged, and its branch order is
  load-bearing: Apptainer cannot nest, so a task already inside the image must
  take the native branch.
- Payloads bundle their own ROOT, so `nf-base.sif` grows roughly linearly with
  the number of composed versions (accepted: recompose is fast, disk on
  `/ptmp`). GiBUU's ROOT (from the `rootproject/root` base) is folded into its
  payload; NuWro keeps relying on `libpcre3` from `nf-base.def`'s fixed runtime
  baseline rather than bundling it, to avoid perturbing the ROOT-linked
  generators that also need it.
- Wrappers are **self-locating** (`readlink -f "$0"`), so their text is
  version-independent and `{{ }}` templating is confined to `%files`/
  `%environment`/`%test` (never inside a quoted `%post` heredoc).
- Code versions must be filesystem-safe: `nf-run` and payload staging apply the
  same `[^A-Za-z0-9._-]→_` transform, so the dispatch arg and the on-disk
  directory never diverge.
- Moving wrappers out of `nf-base.def` touches no Dockerfile; the
  `/opt/nf/generators/<cv>` staging is Apptainer-composition-only and has no
  Docker counterpart, so the def/Dockerfile pairing does not apply to it.

## GENIE flux: a histogram we write, divided out from the one GENIE saved (2026-07)

2026-07 — moved to [generators/genie.md](generators/genie.md#flux-handling).

## GiBUU cross-section weight (`xsec_weight`) normalization

2026-07 — moved to [generators/gibuu.md](generators/gibuu.md#cross-section-weight).

## GiBUU non-resonant background events are `dis`, and every channel is enabled (2026-07)

2026-07 — moved to [generators/gibuu.md](generators/gibuu.md#why-the-non-resonant-background-is-dis).

## NEUT: a payload extracted from a published image, not built from source

2026-07 — moved to [generators/neut.md](generators/neut.md#versions-and-provenance).

## NEUT output flattening (`nf_flatten.C`)

2026-07 — moved to [generators/neut.md](generators/neut.md#output-flattening-nf_flattenc).

## NEUT cross-section weight (`xsec_weight`) normalization

2026-07 — moved to [generators/neut.md](generators/neut.md#cross-section-weight).

## NEUT flux: log bins of per-bin integrals, divided out on NEUT's own grid (2026-07)

2026-07 — moved to [generators/neut.md](generators/neut.md#flux-handling).

## NEUT paths are limited to 80 characters

2026-07 — moved to [generators/neut.md](generators/neut.md#every-path-neut-sees-must-be-a-bare-filename).

## NEUT is not bit-reproducible from its seed

2026-07 — moved to [generators/neut.md](generators/neut.md#seeding).

## Derived kinematic variables in the common output

2026-07 — moved to [physics.md](physics.md#derived-kinematic-variables).

## `analyze-kinematics`: weighted by default, with the weight efficiency in view

2026-07 — moved to [physics.md](physics.md#weighted-statistics-and-weight-efficiency).

## Merging chunks averages cross-section weights, it does not sum them

2026-07 — moved to [physics.md](physics.md#merging-chunks-averages-it-does-not-sum).

## Weak current: enforced natively, recorded per event as `is_cc`

2026-07 — moved to [physics.md](physics.md#weak-current).

## Plots decide CC vs. NC from the data, not from `physics.current`

`plot-output` splits the cross-section figure into a CC and an NC panel when a
dataset is *inclusive*, and it decides that from the `is_cc` column of the file
it is plotting — a panel is drawn for a current only if the file actually holds
events of that current — rather than from the run configuration's
`physics.current`.

Two reasons. First, `--input` mode is handed a bare HDF5 file with no config in
sight, and the file is the authority on its own contents in any case; keying off
the data means the single-file and `--config` paths cannot disagree. Second, a
configured-inclusive run that produced no NC events (a small run, or a generator
whose NC channels were all masked out) would otherwise get an empty NC panel
that looks like a physics result of zero. One panel labelled CC is the honest
rendering of a file that contains only CC events.

The consequence to be aware of: the panel layout of a figure describes the
sample, not the request. An inclusive run whose NC events are simply missing
produces a one-panel figure, which is a signal worth investigating rather than a
plotting artefact — `check-status` and `analyze-kinematics` are the tools for
confirming the run itself is complete.

## The probe flavour is one table, validated at config time (2026-07)

2026-07 — moved to [physics.md](physics.md#probes-and-targets).

## GENIE: a probe with no staged spline is an error, not a zero weight (2026-07)

2026-07 — moved to [generators/genie.md](generators/genie.md#cross-section-weight).

## A job label is a pure function of its own job

Output paths are keyed by the job label
(`<generator>_<versions>_<particle>_<nucleus>_<current>`, or an explicit
`name:`). Labels are never disambiguated against the other jobs in a
configuration — two jobs that produce the same label are a hard error instead.

The alternative, appending a suffix on collision, would make a label depend on
the rest of the file: adding an unrelated job could silently rename another job's
HDF5 outputs, invalidating anything downstream that referenced them by name.
These files are provenance records for physics samples, so path stability under
edits to the configuration matters more than the convenience of never having to
name a job. The error message says which two entries collided and that an
explicit `name:` resolves it.

## Seeds are hashed, not laid out arithmetically

A task's seed is `blake2b(run.seed | job label | chunk id) % 2_000_000_000 + 1`
rather than the previous `run.seed + generator_index * 1000 + chunk_id`.

Two properties motivated the change. First, the offset scheme collided outright
at 1000 or more chunks per job, which per-job chunking makes reachable. Second,
and more importantly, it made every seed depend on a job's *position* in the
configuration, so inserting a job re-seeded every job after it — re-running a
campaign with one generator added would have silently regenerated different
events for all the others. Hashing the job's own identity makes a seed
reproducible from `run.seed` alone and independent of its neighbours.

The modulus keeps a seed (plus GiBUU's `PASS_SEED_OFFSET` of 1e6 for the NC pass
of an inclusive run) inside the signed 32-bit range Fortran generators require.
`build_task_manifest` asserts all seeds in a manifest are distinct: a hash
collision is astronomically unlikely and silently catastrophic, since two tasks
would generate identical events and inflate the sample's statistics.

## GiBUU runs without FSI transport, because the output is inclusive (2026-08)

**Decision.** GiBUU jobcards are rendered with `numTimeSteps = 0`, which
disables the final-state-interaction transport loop. Confirmed as correct rather
than changed; the code comment that described it as "sufficient for the
ROOT-output smoke test" was rewritten, since it framed a deliberate physics
setting as a provisional shortcut.

**Why.** The common output records only initial-vertex quantities — the weight,
the interaction label, and the two lepton four-vectors — and GiBUU fixes all of
them before transport begins. GiBUU's own shipped neutrino jobcards document
`numTimeSteps = 0` as the setting for inclusive cross sections. Measured both
ways on one jobcard and seed: the hadron multiplicity rose from 1.867 to 3.077
per event while `sum(weight)`, `evType` and both lepton four-vectors came back
bit-identical. Full mechanism and numbers in
[generators/gibuu.md](generators/gibuu.md#final-state-interactions-are-switched-off-deliberately).

**Caveat, and the condition that reverses this.** The equivalence is a property
of *what we record*, not of the physics: FSI reshapes every hadronic observable.
Adding one — pion multiplicity, knocked-out nucleons, visible/calorimetric
energy, or a "CCQE-like" topology classification — requires enabling transport,
and at that point `write_pert`'s documented habit of omitting events with no
surviving particles has to be checked, since it would silently remove those
events' weight from the cross-section sum.

## Plots are grouped by the configuration, not by the events

`make_config_plots` groups merged outputs into comparison figures by the
`(flux.particle, target.nucleus)` of the **job**, not by the `probe`/`target`
columns of the events in each file.

Three reasons. The event columns are reduced to a single label per file, which
degenerates to `"mixed"` when a file disagrees with itself — a grouping key must
not be data-dependent that way. The set of figures a run produces should be
derivable from the configuration alone, without opening any HDF5. And if a
generator ever writes an unexpected target string, the figure should still be
filed under the initial state that was actually requested, with the discrepancy
visible in the per-dataset title (which does come from the events) rather than
silently splitting one comparison into two.

Cross sections on different nuclei, or for different flavours, are not
comparable quantities, so generators are only ever drawn on shared axes within
one such group.
