# Design decisions

Architectural rationale and caveats. Append new decisions here.

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
Minuit2). The cost is deliberate duplication: **Dockerfile
and def must be updated together** (each def's header names its source).

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
  version explicitly. Detection logic is unchanged — **do not reorder those
  branches.**
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
  Docker counterpart, so the "update both together" pairing is not triggered.

## GiBUU cross-section weight (`xsec_weight`) normalization

**Context.** The common-output `xsec_weight` convention (see
`translators/base.py`): histogramming events by energy, weighting by
`xsec_weight`, and dividing by the bin width yields the average cross section in
that bin in **1e-38 cm² per target nucleon**. NuWro implemented this; GiBUU did
not (weights defaulted to `1.0`, and `GiBUUTranslator` was non-instantiable).

**Decision.** `GiBUUTranslator.compute_xsec_weight` uses
`xsec_weight_i = raw_weight_i / (num_runs · φ̂(E_i))`, where `φ̂` is the
unit-integral-normalized flux density (same `to_histogram(500)` binning written
into GiBUU's user-flux file) and `num_runs = num_runs_SameEnergy` (=1).

**Why this differs from NuWro** (rejection-sampled → per-event weight *is* the
flux-averaged σ, needs `1/n_events`): GiBUU is **phase-space sampled and weighted
by cross section**. Established from GiBUU's `neutrinoAnalysis.f90` header and the
production KM3NeT `km3buu` wrapper (which reads the identical `RootTuple`/`weight`
branch):
- The raw perweight is **already in 1e-38 cm²** → no `XSEC_SCALE` (`1e38`) factor.
- It is **already per nucleon** (km3buu multiplies by `A` to recover the
  whole-nucleus σ) → no `1/A`.
- Within one run, `Σ perweight = σ_flux-folded` (numEnsembles is already folded
  in; only `num_runs` remains). Because a spectrum run is flux-folded, recovering
  the differential `σ(E)` still requires dividing out `φ̂` — the earlier TODO note
  "no 1/flux recipe" was imprecise: what differs from NuWro is the *constant*
  (`num_runs`, no `1e38`), not the presence of the flux division.
- Negative interference weights are passed through unchanged.

Files: `translators/gibuu.py` (`NUM_RUNS_SAME_ENERGY`, `compute_xsec_weight`),
`normalizers/gibuu.py` (`_normalize_root` wires it in via the sidecar
`flux_config`). Monoenergetic runs skip the flux division (`raw / num_runs`).

## NEUT: a payload extracted from a published image, not built from source

**Context.** Every other generator is built from a git ref by a Dockerfile plus a
hand-mirrored Apptainer def. NEUT's source code is not publicly available, so
there is nothing to build. The NUISANCE collaboration publishes a tutorial image
(`nuisancemc/tutorial:nuint2024`) carrying a working NEUT 5.7.0 build.

**Decision.** Both container pathways extract NEUT from that image:
`setup/setup_neut.sh` pulls and retags it as `neut:<code_version>` for Docker,
and `setup/apptainer/neut.def` bootstraps stage 1 from it and stages only
`/opt/neut` and the ROOT build it links against (~1.5 GB of the ~5 GB image).
Consequences, each a deliberate departure from the rules elsewhere in this file:

- **There is no `setup/Dockerfile.neut`**, so the "each def mirrors its
  Dockerfile — update both together" pairing does not apply to NEUT.
- `code_version` is **`5.7.0-nuint2024`**: the NEUT release plus the image tag it
  came from. Since the build is not reproducible from source, the image tag is
  the real pin, and a re-push of that tag would otherwise be invisible.
- `NeutAdapter` overrides `is_buildable` to treat a `source_image` catalog entry
  as a source (the base class recognizes only `git_ref`), so the catalog-driven
  `build_apptainer_images.sh` picks NEUT up with no generator-specific code, and
  overrides `build_arg` to pass `NEUT_SOURCE_IMAGE` rather than the code version.
- `build_apptainer_images.sh` now runs `apptainer build` from the repo root, so
  `neut.def`'s `%files` can stage project files (the flattener) by repo-relative
  path. No other def has a host `%files` block, so nothing else changes.
- NEUT.pc bakes in the original install prefix and `neut-config` refuses to run
  when that disagrees with its own location, so the def repoints it after
  relocation.

## NEUT output flattening (`nf_flatten.C`)

**Context.** `neutroot2` writes a `neuttree` whose `vectorbranch` holds
`NeutVect` objects. uproot deserializes their scalar members from the file's
streamers but fails on the nested `TObjArray` of `NeutPart` ("invalid class-tag
reference"), so NEUT's native output cannot be read from Python at all.

**Decision.** A project-owned ROOT macro, `setup/neut/nf_flatten.C`, run through
`setup/neut/nf-neut-flatten` as a second stage — the NEUT analogue of GENIE's
`gevgen` → `gntpc`, dispatched by `NeutAdapter._run_flatten` through the same
native/container branches as generation. It writes an `nf_neut` tree of plain
scalars (`mode`, `pdgnu`, `enu_gev`, `totcrs`) and copies NEUT's normalization
histograms across.

**Alternatives rejected.** NEUT ships `neutclass_to_tree`, but its `nework`
branch is a Fortran leaf-list containing `pne[100][3]`, which uproot mis-parses
(it reads the dtype as `(3,)` rather than `(100,3)`); reading it would mean
hand-maintaining a 2808-byte numpy dtype that must track NEUT's common block.
NUISANCE's `nuisflat` produces a genuinely flat tree, but `ldd` shows it linking
GENIE, NuWro, LHAPDF and Pythia — the payload would grow from ~1.5 GB to
essentially the whole image.

## NEUT cross-section weight (`xsec_weight`) normalization

**Decision.** `NeutTranslator.compute_xsec_weight` uses
`xsec_weight_i = σ_avg / (n_events · φ̂(E_i))` — structurally identical to GENIE,
because NEUT is likewise unweighted and samples events with density proportional
to `flux(E) · σ(E)`.

**Where σ_avg comes from.** Unlike GENIE, no external spline file is needed:
when sampling a flux histogram (`EVCT-MPV 3`), NEUT writes both that histogram
(`flux_numu`) and the resulting event rate (`evtrt_numu`, = flux × σ) into its
own output, and the ratio of their integrals *is* the flux-averaged total cross
section. `NeutNormalizer` reads them and injects the ratio into the translated
config as `flux_averaged_xsec_1e38`.

**Validation** (not self-consistency — independent references):
- *Units and per-nucleon convention.* The ratio does not scale with A:
  regenerating the same flux on C12/O16/Ar40/CH gives 0.655/0.663/0.675/0.636,
  the small isospin-driven spread of a per-nucleon quantity rather than the
  factor ~3.3 a whole-nucleus quantity would show between C12 and Ar40. So there
  is no `XSEC_SCALE` and no division by mass number, unlike GENIE.
- *Absolute value.* NUISANCE, reading the same file through its own NEUT input
  handler, reports `Event/Flux : 1.46774e-38 cm2/nucleon` where this ratio is
  1.4677402686 — agreement to every printed digit.
- *Sampling law.* The generated energy spectrum tracks `evtrt`, not `flux`: over
  ten coarse bins the summed absolute difference in normalized shape is 0.035
  against `evtrt` versus 0.52 against `flux`. This is what licenses the formula.
- *End to end.* For a 4000-event single-chunk C12 run, `Σ xsec_weight / bin_width`
  per energy bin reproduces NEUT's own `evtrt/flux` ratio per bin to
  1.03 ± 0.05 — Monte-Carlo noise at that sample size.

## NEUT is not bit-reproducible from its seed

**Finding.** NEUT reads its RANLUX seed from the file named by `$RANFILE` when
the card sets `NEUT-RAND 0` (`NeutAdapter._write_seed_file` writes the 25
integers Fortran's `(5X,5I12)` read expects; only the first is used as the
RLUXGO seed). That works — the log confirms `RANLUX INITIALIZED BY RLUXGO FROM
SEEDS <seed> 0 0`, different seeds diverge immediately, and the same seed
reproduces the opening events exactly.

But two runs with the *same* seed diverge after ~4 events: NEUT's own `Ev.# N
SEEDS` trace shows it consuming a different number of random numbers from there
on. This is NEUT, not the harness — it reproduces on native aarch64 and on
x86_64 under Rosetta, and with ASLR disabled (`setarch -R`), which rules out
emulation and address-layout effects. The likely cause is uninitialized state
inside NEUT; the source is unavailable to confirm.

**Consequence.** Chunking is still safe — that requires only that different
chunk seeds give different, independent event sets, which holds. What is lost is
bit-exact re-running of a given chunk. Configs remain reproducible in
distribution, not in individual events.
