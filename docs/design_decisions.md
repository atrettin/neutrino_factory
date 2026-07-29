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

## GENIE flux: a histogram we write, divided out from the one GENIE saved (2026-07)

**Context.** A 1M-event run over 0.1–50 GeV showed a sawtooth in both the raw and
the `xsec_weight`-weighted GENIE event rate below ~1 GeV: a slow rise followed by a
sharp drop, repeating, with a second interfering period. The framework passed the
power-law flux to `gevgen` as a ROOT TF1 string (`-f "x^(-2.0)"`) on the assumption
that GENIE samples the function continuously.

**Finding (from `Apps/gEvGen.cxx`, `TH1FluxDriver`, GENIE R-3_06_00).** It does not.
Every `-f` input ends up as a `TH1D` that `GCylindTH1Flux::GenerateNext` samples with
`TH1::GetRandom`, which is **uniform within a bin** — so the generated flux density is
always piecewise constant. The three input branches differ in how the TH1D is built:

- **TF1 string** (the branch we used): `new TH1D("spectrum", ..., 300, emin, emax)`
  followed by `spectrum->FillRandom("input_func", 100000)`. The generated flux is
  therefore not merely binned into 300 uniform bins — it is *Monte-Carlo estimated*
  from only 100k entries. For `E^-2` over 0.1–50 GeV about 63% of those entries land
  in the first bin and bins above ~15 GeV hold single-digit entries (>40% Poisson
  noise). `RandomGen::InitRandomGenerators` seeds `gRandom`, so the noise realization
  differs per chunk.
- **ROOT file** (`-f file.root,hist[,WIDTH]`): the histogram is `Clone()`d verbatim —
  no resampling, arbitrary (including log) binning preserved. Bins not strictly inside
  the `-e` range are zeroed, and contents are multiplied by the bin width when the axis
  is variable-width or when the third field is `WIDTH`.
- Text file: rejection-sampled into the same 300-bin histogram.

The observed sawtooth was the 300-bin generated flux beating against the *different*
grid the weight divided by — `flux.to_histogram(nbins=500)`, rebuilt from the run
config. Events were being divided by a flux they were never drawn from.

**Decision, two parts.**

1. **Generation.** A power-law flux is no longer passed as a TF1 string. The adapter
   writes `nf_flux.root` into the task work directory — `GENIE_FLUX_NBINS = 1000`
   log-spaced bins holding the flux *density* — and passes
   `-f nf_flux.root,nf_flux,WIDTH`, taking the clone branch. Log spacing gives constant
   relative resolution (~0.27%/bin over 0.1–50 GeV), which no uniform binning can give
   a steeply falling spectrum, and the explicit `WIDTH` field converts our density to
   the per-bin sampling probability regardless of binning. The `-e` bounds are widened
   by a relative `1e-12` because gevgen zeroes bins that round outside the range.
2. **Normalization.** `GenieNormalizer` no longer rebuilds the flux from the run
   config. It reads `input-flux.root` — which gevgen writes into its working directory
   on every run — and uses that histogram's **native binning** as the denominator
   (`_flux_grid` in `translators/genie.py`). That file is the only authoritative record
   of what was sampled: it reflects the clipping, the width multiplication, and (for
   legacy TF1-driven runs) the per-chunk noise realization. Its contents are per-bin
   integrals, so they are divided by the bin widths on load
   (`HistogramFlux.from_root_file(..., contents_are_counts=True)`).

A missing `input-flux.root` is a hard error. Falling back to the config flux would
silently restore the original bug and produce a wrong normalization that still looks
physical — the failure mode the project's "fail loudly" posture exists to prevent.

**Caveat.** Only part 2 is required for correctness; part 1 is a quality fix. Part 2
alone yields unbiased weights even against the noisy 300-bin histogram, but the
*variance* at high energy would stay bounded by the flux histogram's 100k entries
rather than by the event count.

**Verified** (local Docker, R-3_06_00/G18_10a_02_11a, numu on C12, 0.1–50 GeV): gevgen's
`input-flux.root` came back with edges bit-identical to `nf_flux.root`, contents equal
to our density × bin width, and no zeroed bins — i.e. cloned, not resampled. Across the
resulting events `xsec_weight · E^-2` is constant to 1.25% (the residual expected from
0.27%/bin log binning), with no step structure.

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

**`numEnsembles` vs `num_runs_SameEnergy`.** Both scale statistics; only the
first is normalized out for you, which is why `num_runs` is the divisor above
and why we scale chunk size with `numEnsembles`. Read from the release2025
source: `initNeutrino.f90:1292` sets `perweight = totalWeight/float(numtry)`,
where `numtry` counts nucleon test-particles over *all* ensembles
(`realParticles` is indexed `(ensemble, particle)`) — so doubling `numEnsembles`
halves each weight and leaves the sum invariant. `num_runs_sameEnergy` never
enters `perweight`; GiBUU divides by it in its own analysis
(`neutrinoAnalysis.f90:3515`) and hands both counts to the consumer as ROOT
branches (`EventOutput.f90:1132`). Confirmed by running one jobcard three ways:
doubling ensembles gave 2.04x the events at 0.86x the weight sum, doubling runs
gave 2.02x the events at 2.17x the weight sum (spread is GiBUU's heavy tails).

Because `numEnsembles` is auto-normalized, a GiBUU chunk estimates sigma
regardless of its size — the property that makes `num_runs`, not the event count,
the right `xsec_norm_count` for merging.

**One file per run.** `num_runs_SameEnergy = N` writes
`EventOutput.Pert.00000001.root` .. `...0000000N.root`. The normalizer globs
every part and fails loudly if it finds fewer than `num_runs`; reading only the
first while still dividing by N would silently report sigma/N (measured: 4.07
instead of 8.13 on a real 2-run job).

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

## NEUT paths are limited to 80 characters

NEUT reads filenames into 80-character Fortran buffers and **truncates anything
longer without complaining** — the failure surfaces much later as
`Fortran runtime error: End of file` on a path that has silently lost its tail.
A real cluster work directory overruns this easily
(`/ptmp/.../work/raw/neut/<version>/<run>_<gen>_<version>_chunk000` is ~90
characters before the filename).

Everything NEUT is handed is therefore a **bare filename**, resolved against the
working directory: the card (`neut.card`), its `EVCT-FILENM 'flux.root'`, the
output (`events.neut.root`), and `$RANFILE` (`ranseed.dat`). `local.run_task`
launches with `cwd=work_dir`, which is the same assumption the other adapters'
relative output paths already make, and the payload wrapper deliberately does
not `cd` (unlike NuWro's, which must). Keep it that way — an absolute path
anywhere in this chain is a latent bug that only appears on deep work roots.

The one absolute path NEUT still receives is `$NEUT_CRSPATH`, set by the payload
wrapper to `/opt/nf/generators/neut/<code_version>/neut/share/neut/crsdat` (~62
characters). That fits, but it is close enough that a longer `code_version`
would break it.

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

## Derived kinematic variables in the common output

**Context.** The common format originally carried a single kinematic quantity,
`energy_gev` (the incoming neutrino energy), which is not enough for the standard
neutrino cross-section measurements the harmonized output exists to support.
All four generators expose the incoming-neutrino and outgoing-lepton
four-vectors; the normalizers simply were not reading them.

**Decision.** Eight lab-frame columns are derived in one shared module,
`src/neutrino_factory/kinematics.py`: `q2_gev2`, `bjorken_x`, `inelasticity_y`,
`lepton_energy_gev`, `lepton_momentum_gev`, `lepton_p_parallel_gev`,
`lepton_p_transverse_gev`, `lepton_costheta`.

**Recomputed, not read.** GENIE's `gst` tree already precomputes `Q2`, `x`, `y`
and `cthl`, and it would have been less code to use them. We recompute from the
four-vectors for all three generators anyway, because the promise of the common
format is that a Q^2 histogram from GENIE means exactly the same thing as one
from GiBUU — which only holds if one formula produces all of them. GENIE further
ships *two* variants (`Q2`/`x`/`y` "true" and `Q2s`/`xs`/`ys` reconstructed from
the hadronic system), so "use the native branch" is not even unambiguous within
one generator. `tests/test_genie_normalizer.py` pins our definitions against
GENIE's own branches for a reference scatter.

**Conventions.**
- The beam axis is taken **per event** from the incoming neutrino three-momentum,
  never assumed to be +z, so the parallel/transverse split and `cos(theta)` stay
  correct for off-axis or divergent beams.
- Bjorken-x uses a **fixed** isoscalar nucleon mass `NUCLEON_MASS_GEV`
  ((m_p + m_n)/2), not the per-event struck-nucleon mass. Only GiBUU and NuWro
  expose the hit nucleon; a fixed mass makes x identically defined everywhere.
- `Q^2 = -(p_nu - p_l)^2` is non-negative for this process up to floating-point
  noise near forward scattering, so it is clamped at 0 rather than blanked.
- `inelasticity_y` can come out slightly **negative** when Fermi motion of the
  struck nucleon pushes the outgoing lepton above the beam energy. That is
  physical and is passed through; only `bjorken_x` (which would diverge) is
  blanked when the energy transfer is non-positive.
- The lepton variables are filled for NC as well as CC events — for NC the
  "lepton" is the scattered neutrino. Detector observability is a downstream
  question, not the generator-harmonization layer's to decide.

**NEUT needs the four-vectors carried across explicitly.** The other three
generators write them into their native output, so the normalizer just reads more
branches. NEUT's NeutVect output cannot be read from Python at all (see the
`nf_flatten.C` section above), and the flattener wrote only `mode`, `pdgnu`,
`enu_gev` and `totcrs` -- the four-vectors were dropped before Python ever saw
them. `nf_flatten.C` therefore gained `nu_p{x,y,z}_gev`, `lep_{e,px,py,pz}_gev`
and `pdglep`, converted to GeV in the macro so the flat tree is single-unit.

**Do not read NEUT's outgoing lepton at `PartInfo(2)`.** The usual layout is
[0] beam neutrino, [1] struck nucleon, [2] outgoing lepton, and 49 of 50 events
in a real numu-CC C12 run follow it. The exception is **2p2h (Mode 2), which has
two initial-state nucleons at [1] and [2], putting the lepton at [3]** -- a fixed
index would have read a neutron as the outgoing lepton and silently corrupted
Q^2/x/y for the entire MEC channel. The flattener instead scans from index 1 for
the first particle with |PDG| in 11..16 (the charged lepton for CC, the scattered
neutrino for NC; leptons do not rescatter, so there is no FSI copy to confuse
it), and reports `pdglep = 0` when it finds none, which blanks the kinematics for
that event. Verified by dumping every NeutVect entry of a real run and comparing
against the flattened tree: 50/50 events agree.

**Placeholders.** Undefined values use a clearly unphysical marker, but *two* of
them: `MISSING = -1.0` for the non-negative-definite columns, and
`MISSING_SIGNED = -999.0` for `lepton_p_parallel_gev` and `lepton_costheta`,
whose physical ranges include -1 (a backward-scattered lepton genuinely has
`cos(theta) = -1`). Using a single -1 would have made backward scatters
indistinguishable from missing data. Blanking rules: `bjorken_x` for coherent
events (no struck nucleon) and for non-positive energy transfer;
`lepton_costheta` when the lepton momentum is zero; the directional variables
when the beam momentum is zero; and the whole block when the generator does not
supply an outgoing lepton (NuWro's `e/out` can be empty) or the four-vectors are
non-finite.

**Schema mechanics.** `common_output.py` grew a single `NUMERIC_FIELD_SPECS`
table (name -> dtype, default) that the writer, reader and merger are all driven
off, replacing the column names that were hard-coded in four places. Two
consequences: the writer emits placeholders for any column an event dict omits,
so stub/JSON mode and the NEUT normalizer need no changes; and the reader
synthesizes columns absent from a file, so HDF5 written before this change stays
readable and mergeable. `validate_output.REQUIRED_COLUMNS` now tracks the schema
directly, so those older files *do* fail validation and should be regenerated.

Files: `kinematics.py`, `common_output.py`, `normalizers/{genie,gibuu,nuwro}.py`,
`normalizers/base.py` (`interaction_from_flags`, shared by GENIE and NuWro),
`validate_output.py`, `tests/kinematics_reference.py` (the reference scatter all
three normalizer test modules assert against).

**Verified on real generator output (2026-07-28).** 20k-event GENIE, NuWro and
NEUT runs plus an 83k-event GiBUU run, all `numu` CC on C12 with a γ=-2 power-law
flux over 0.5–5 GeV, pass the structural invariants event-by-event (Q² ≥ 0, exact
energy-transfer closure, |cos θ| ≤ 1, p_∥² + p_T² = |p|², coherent events blanked
and only those) and agree on the cross-section-weighted distributions. Bjorken-x
for quasi-elastic peaks at 0.75–0.85 with a weighted median of 0.825 (GENIE),
0.815 (NuWro), 0.825 (NEUT) and 0.809 (GiBUU) — a broad peak *below* 1, not the sharp x=1 of
free-nucleon QE, because Fermi motion and binding smear it and the fixed `M_N`
does not absorb that. The GiBUU number needs care: its QE weight is extremely
concentrated (Kish n_eff = 35 out of 14579 events; the top 1% of events carry 86%
of the weight), so it is quoted with a bootstrap CI of [0.797, 0.851] rather than
as a point estimate — see the GiBUU weighting section above for why.

## `analyze-kinematics`: weighted by default, with the weight efficiency in view

**Decision.** `kinematics_report.py` (CLI: `analyze-kinematics`) reports every
mean and median **weighted by `xsec_weight`**, never raw, and prints a
weight-efficiency table alongside — Kish `n_eff = (Σw)² / Σw²` per interaction
channel, plus the share of weight in the heaviest 1% of events.

**Why.** Unweighted event distributions are simply not the physical ones for a
cross-section-weighted generator, and the failure is silent: a 100k-event GiBUU
run reports a perfectly healthy-looking quasi-elastic sample whose effective size
is 35 events. Making the weighted statistic the only one available removes the
foot-gun; showing `n_eff/n` next to it says how much to trust the number. The
efficiency is computed on `|w|` so GiBUU's negative interference weights cannot
cancel into a meaningless ratio, while `Σw` is reported signed.

**Placeholders are excluded per variable, and counted.** A `blank` column shows
how many events were dropped, so `bjorken_x` for a coherent selection reports
"0 used, 127 blank" rather than silently averaging in `-1`.

**The weighted quantile uses the midpoint convention** — cumulative weight
evaluated at the centre of each point's weight, not its upper edge. The naive
cumulative sum biases the median low by half a bin (it puts the median of 0..100
at 49.5); a unit test pins the uniform-weight case to the ordinary median.

Files: `kinematics_report.py`, `cli.py` (`cmd_analyze_kinematics`),
`tests/test_kinematics_report.py`.

## Merging chunks averages cross-section weights, it does not sum them

**Context.** Every generator's `xsec_weight` column is a *per-chunk* estimate of
the cross section: for one chunk on its own,
`sum(xsec_weight in an energy bin) / bin_width` converges to sigma(E). Merging
used to concatenate chunks and nothing else, so an N-chunk run reported roughly
N times the true cross section — measured at 2.06x for a 2-chunk NEUT run whose
single-chunk equivalent was correct to 1.03.

This affected **all four generators**, not three. An earlier note here claimed
GiBUU was exempt because its weights are per-event; that was wrong. GiBUU's
weights sum to sigma *within one run* (`num_runs_SameEnergy = 1` per chunk), so
two chunks are two independent estimates and adding them double-counts exactly
as elsewhere.

**Decision.** Each chunk declares the denominator its weights were divided by —
the `D` in `compute_xsec_weight`'s `numerator_i / (D * phi_hat(E_i))` — as
metadata `xsec_norm_count`, via the abstract `ConfigTranslator.xsec_norm_count`.
`merge_hdf5_files` scales chunk *c* by `D_c / sum(D)`, turning concatenation into
a weighted average. Equivalently, the merged weights are what a single run of
`sum(D)` events would have produced.

**Why the denominator is not just the event count.** For the rejection-sampled
and unweighted generators (GENIE, NuWro, NEUT) it is the chunk's event count:
one event is one sample of the estimator. For GiBUU it is `num_runs`, because its
per-event weights already sum to sigma within a run — weighting GiBUU chunks by
their event count would weight them by how many interactions GiBUU happened to
produce, which itself varies with the cross section. That divergence is why the
method is abstract rather than defaulting to `len(events)`.

**Properties.** A single input is a no-op (share = 1), so one-chunk runs are
unchanged. The merged file records the summed `sum(D)`, so merging merged files
stays correct. Stub output declares no count and is left untouched — its weights
are placeholders, not cross sections — and a set mixing declared with undeclared
inputs is refused rather than half-rescaled.

**Validation** (local Docker, comparing chunk counts against the same physics):

| Generator | Events | 1 chunk | 2 chunks | 4 chunks |
|---|---|---|---|---|
| NEUT (vs its own evtrt/flux per bin) | 4000 | 1.015 | 1.018 | 1.011 |
| GiBUU (relative to 1 chunk) | 40000 | 1.000 | 0.929 | 1.005 |
| NuWro (relative to 1 chunk) | 4000 | 1.000 | 0.956 | — |

The residual spread is Monte-Carlo noise, and GiBUU needs the larger sample for a
meaningful comparison: its weights are heavy-tailed, and in a 1000-event run a
single event carried 72% of one chunk's total. Before the fix the same NEUT
comparison gave 2.06 at two chunks.
