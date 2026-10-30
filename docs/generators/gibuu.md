# GiBUU

GiBUU is the odd one out: it is **phase-space sampled and weighted by cross
section**, where the other three generators are rejection-sampled and
unweighted. Almost every difference below follows from that. It is built from
HEPForge release tarballs and run from a generated jobcard, with full transport
(FSI) on. The conventions its output is normalized onto are in
[../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

## At a glance

| | |
|---|---|
| Code versions | `release2025` |
| `config_version` | `default` only |
| Sampling | phase-space sampled, cross-section weighted; weights can be **negative** |
| Raw σ → per nucleon | already 1e-38 cm² per nucleon, no conversion |
| `physics.current` | `process_ID`; `inclusive` is two concatenated passes ([Weak current](#weak-current)) |
| Job section | `gibuu.qes_oversampling` ([QE oversampling](#qe-oversampling); [QE passes](#qe-passes)) |
| FSI | transport for 150 × 0.2 fm = 30 fm |
| `run.log_level` | ignored |
| Reweight universes | none |

## Setup

The image downloads the GiBUU release, its input data and the RootTuple library
from HEPForge (`wget --content-disposition`); there is no separate data staging
step. The GitHub mirror is stale and not used.

## Configuring a job

- **Event counts are approximate.** GiBUU simulates whole-nucleus *ensembles*,
  and the framework sizes their number from the requested event count assuming
  ~A/2 events per ensemble. The number of events written differs from the
  request (e.g. 821 for a 1000-event C12 request; 3435 for 4000 on Ar40).
- **`inclusive` splits the event budget over two passes**, CC and NC. The CC:NC
  split by *event count* is not the physical one; it lives in the weights
  ([Reading the output](#weights-are-the-physics)).
- **Every channel is on**: QE, Δ, higher resonances, DIS, 1π and 2π background,
  2p2h QE. 2p2h Δ is unavailable in this release.
- **Monoenergetic beams work** (a degenerate energy range switches GiBUU to its
  fixed-energy mode).
- **Oversample QE for low-energy precision** with `gibuu.qes_oversampling`
  ([below](#qe-oversampling)).
- **Transport costs about 4× the wall clock** of a run without it.

### QE oversampling

Quasi-elastic weights are heavy-tailed, and QE carries most of the variance of
the cross section below a few GeV, in CC and NC alike
([Validation](#qe-oversampling-1)). `qes_oversampling: K` runs K extra
QE-only passes per weak current and averages their QE events with the full
pass's:

```yaml
- generator: gibuu
  ...
  gibuu:
    qes_oversampling: 10    # integer, 0 (default) to 50
```

- **What it buys.** The QE effective sample size grows (K+1)-fold. Per CPU hour,
  K = 10 gives the inclusive σ 2–3× more statistical power below 1 GeV, ~2× at
  1–2 GeV and ~1.4× at 2–5 GeV. Above 5 GeV it costs power, because those bins
  are not QE-limited.
- **What it costs.** A QE pass takes ~1/3 of its current's full pass (C12 0.34,
  Ar40 0.27), so K = 10 multiplies the task's wall clock by ~4.5 (C12) or ~3.7
  (Ar40). Size `events` per chunk for that: 2.5e5 requested events at K = 10
  ran 4.9 h on C12 and 5.7 h on Ar40.
- **Choosing K.** The best K is 5–10. The optimum is ~10 where QE carries ≥ 98%
  of the variance (below 1 GeV), and 3–6 at 1–5 GeV.
- **Files grow ~3.3×.** A QE-only pass writes ~2.5–3× as many QE events as the
  full pass, but they carry the same effective sample size: the extra events
  have small weights. At K = 10 a chunk holds ~3.4× the events of a K = 0 chunk.
- **The cross section is unchanged.** Every pass estimates the σ of the channels
  it samples, so `xsec_weight` of a QE event is divided by K+1 and every other
  event's is untouched. The `weight` column stays GiBUU's native per-pass weight:
  sum `xsec_weight`, not `weight`, in an oversampled run.
- **Only QE.** Δ, the higher resonances, the 1π/2π background and 2p2h already
  reach ≤ 1% relative error where they contribute, and DIS 0.2–0.5%.
- **Not part of the job label.** Two jobs that differ only in it need distinct
  `name:`s.

## Reading the output

### Weights are the physics

Every event carries a per-nucleon cross-section weight, and the event *density*
carries no normalization. In practice:

- **Always split by weight, never by counting events.** An inclusive run is
  roughly half CC by event count (45% on a 0.5–5 GeV numu C12 run) but 69% CC by
  σ.
- **Negative weights are physical** (interference terms) and must be summed as
  they are.
- **Weights are heavy-tailed.** A 100k-event run's quasi-elastic sample had an
  effective size of 35 events. Check `analyze-kinematics`'s weight-efficiency
  table and quote bootstrap intervals for channel statistics
  ([physics.md](../physics.md#weighted-statistics-and-weight-efficiency)).

### Interaction labels and `is_cc`

`native_interaction_code` is `evType`, GiBUU's production label (`prod_id`). It
describes the interaction at the vertex, not the observed final state. Its table
is closed:

| `evType` | GiBUU meaning | Common label |
|---|---|---|
| 1 | nucleon (QE) | `qel` |
| 2–31 | non-strange baryon resonance (2 = Δ) | `res` |
| 32 | π neutron-background | `dis` |
| 33 | π proton-background | `dis` |
| 34 | DIS | `dis` |
| 35 | 2p2h QE | `mec` |
| 36 | 2p2h Δ | `mec` (cannot occur in release2025) |
| 37 | two-pion background | `dis` |

GiBUU never emits `other` or `coh`; an `evType` outside 1–37 raises.
`resonant_primary` follows `evType`: 1 for 2–31, 0 for 32–34 and 37.
`RootTuple` has no per-event current branch, so `is_cc` comes from which pass
(`cc/` or `nc/`) the event was read from.

### The non-resonant background is `dis`

`evType` 32/33/37 are GiBUU's non-resonant shallow-inelastic contribution,
generated only for 1.2 < W < `REScutW` (default 2.0 GeV). They are labelled `dis`
because that is what GENIE, the taxonomy's reference, would call the same event:
GENIE's non-resonant background comes out of its DIS generator
([physics.md](../physics.md#the-shallow-inelastic-region)). GiBUU's own NuHepMC
exporter disagrees and labels them SIS, which this framework does not have.

### `w_true_gev` and the struck nucleon

`nuc_*` holds the struck nucleon for every event. With GiBUU's default
`storeNucleon = 2` it is the **bound** nucleon, including its mean-field
potential, so its invariant mass sits below `M_N` and `qel` `w_true_gev` sits
~40 MeV below the nucleon mass
([physics.md](../physics.md#how-each-generators-qe-vertex-closes)).

### `w_true_gev` is blank for 2p2h

GiBUU stores only one nucleon of a 2p2h pair, so the pair mass that defines
`w_true_gev` cannot be formed. 2p2h events (`evType` 35/36) carry the
placeholder; `w_gev` is unaffected.

### The particle list is a snapshot

The final-state columns are summarized from the perturbative particle list at the
end of transport, which is a snapshot, not a list of escaped particles. Some
nucleons are still inside the nucleus. The normalizer
(`normalizers/gibuu._leave_nucleus`):

- **drops nucleons with `E < m_N`**, GiBUU's own `neutrinoAnalysis/IsBound`
  criterion (kinetic plus potential energy below zero): bound, never leaving;
- **puts the remaining nucleons on shell** at their energy, which a static
  potential conserves on the way out, so their kinetic energy is the asymptotic
  `E − m_N` at GiBUU's single nucleon mass, 0.938 GeV.

The list holds no leptons, only hadrons. Transport does not change the cross
section or any kinematic column: those are fixed at the initial vertex
([FSI transport](#fsi-transport)).

## Model notes

- **2p2h is QE-like only.** 2p2h Δ is not in release2025.
- **`include2pi` matters for the total, not just a category.** With
  `new_eN = .true.` (the default) GiBUU scales the 1π background *down* above
  W = 1.267 GeV "to allow for 2pi contribution"; with 2π off that strength would
  never be added back. The framework switches it on.
- **The background and DIS do not double-count.** GiBUU damps the background with
  `Sigmoid(W, REScutW, -0.05)` exactly where the PYTHIA/DIS piece turns on.
- **`evType` is a vertex label.** FSI never changes it.

## Reweight universes

GiBUU has no reweight support in this framework.

## Known limitations

- **2p2h Δ is unavailable.** `include2p2hDelta` aborts release2025 via
  `notInRelease("2p2p Delta")` (`initNeutrino.f90:689`); the feature is
  unpublished. The mapping keeps `evType` 36 for a future release
  ([upstream issue 1](#upstream-issues)).
- **Weights are heavy-tailed**; effective sample sizes can be orders of
  magnitude below the event count.
- **Flux resolution is untested.** GiBUU gets 500 *equidistant* flux bins, the
  same choice that produced a visible staircase below ~1 GeV in NEUT before it
  moved to log bins. Whether GiBUU shows the same effect over a wide energy range
  has not been checked. GiBUU requires equidistant bins, so log spacing is not an
  option.
- `config_version` is `"default"` only.
- `run.log_level` is ignored.

---

*Reference: the sections below describe how the adapter works, why, and the
evidence. Users can stop here.*

Files: `generators/gibuu.py`, `translators/gibuu.py`, `normalizers/gibuu.py`,
`setup/Dockerfile.gibuu`, `setup/apptainer/gibuu.def`.

## Implementation

### Run stages

**GiBUU reads its jobcard from stdin** (`GiBUU.x < job.job`) and writes its ROOT
output into the **current working directory** under a fixed name,
`EventOutput.Pert.<run>.root`. The task therefore has **one subdirectory per
pass**, named after it (`cc`, `nc`, `cc_qe01`, …) and created uniformly however
many passes the run has, each with its own copy of the flux table (the
jobcard's `FileNameFlux` is CWD-relative). The passes are chained with `set -e`.

`path_to_input` is written as the placeholder `@NF_GIBUU_INPUT@` and resolved by
the adapter: `/opt/GiBUU/buuinput` under Docker,
`/opt/nf/generators/gibuu/<code_version>/GiBUU/buuinput` in the Apptainer
payload.

Like every adapter, `build_run_command` runs a binary found on `$PATH` before considering a container, since a cluster task already runs inside the Apptainer image, which cannot nest ([../apptainer_image.md](../apptainer_image.md)).

`CODE_VERSIONS` carries `release2025`, with `git_ref` holding the release tag so
the catalog treats it as buildable. The build arg strips the prefix
(`release2025` → `GIBUU_RELEASE=2025`).

### Jobcard

Fortran namelist, rendered by `_render_jobcard`:

| Key | Value | Why |
|---|---|---|
| `version` | the release year | GiBUU **refuses to run** unless it matches the code release |
| `eventtype` | 5 | neutrino induced |
| `EventFormat` | 4 | RootTuple ROOT output |
| `numTimeSteps` / `delta_T` | 150 / 0.2 fm | FSI transport out to 30 fm |
| `densitySwitch` / `pauliSwitch` | 2 / 2 | static density and Pauli blocking (fixed target) |
| `process_ID` | ±2 (CC) / ±3 (NC) | negative for antineutrinos, from the PDG sign |
| `flavor_ID` | 1/2/3 | e/μ/τ, independent of ν vs ν̄ |
| `numEnsembles` | from the event count | see below |
| `includeQE`, `includeDELTA`, `includeRES`, `includeDIS`, `include1pi`, `include2pi`, `include2p2hQE` | all `.true.` | all but `includeQE` default to `.false.`; a QE pass keeps only `includeQE` |

**Ensemble sizing.** `numEnsembles = ceil(events / (A/2 · n_passes))`, from the
empirical yield of ~A/2 events per ensemble (~6 on carbon). GiBUU aborts for
`numEnsembles < 100` unless the value is negative (its documented "enforce
anyway" override), so small counts are negated.

### FSI transport

The jobcard runs transport for `numTimeSteps × delta_T = 30 fm`, the values the
shipped neutrino jobcards use (e.g. `testRun/jobCards/005_Neutrino_SBND_nu.job`,
which asks that the distance "significantly exceed the radius of the target
nucleus"). `inputGeneral.f90:514` sets `time_max = numTimeSteps * delta_T`, and
`PhaseSpaceEvolution` in `GiBUU.f90` propagates, rescatters and absorbs hadrons
until then. `EventOutput`'s `write_pert` then writes the perturbative list as it
stands, which is what the normalizer summarizes.

**Transport leaves the cross section and the vertex columns alone**:

- `weight`, `evType`, `lepIn_*`, `lepOut_*` and `nuc_*` are written from
  `neutrinoProdInfo`, which "stores information about the initial neutrino
  event", is written only from `initNeutrino.f90` and never updated.
- Transport never recomputes a weight: every `%perweight =` assignment outside
  `init/` (`collisionTerm.f90`, `master_1Body.f90`) inherits the parent's weight.
- The outgoing lepton does not interact strongly, so every derived kinematic
  column is an initial-vertex quantity.

The one thing transport *could* change is the event count: `write_pert` skips an
event with no perturbative particle left (`if (NUP == 0) cycle`). No event has
been lost this way ([Validation](#transport)).

### Flux

`nuExp = 99` (user flux) + `nuXsectionMode = 16` (EXP_dSigmaMC) reads an
equidistant `energy[GeV] flux` table from `FileNameFlux`. `FLUX_NBINS = 500`;
GiBUU allocates the arrays dynamically.

- **Energies are equidistant bin centres.** GiBUU's `read_fluxfile` requires
  equidistant bins and takes the energy as the middle of the bin. Leading `#`
  lines are allowed. The flux column is a relative weight, normalized internally.
- **The path is `./flux.dat`.** GiBUU's `ExpandPath` uses a filename verbatim
  only if it contains a `/`.

A degenerate energy range falls back to fixed-energy `nuExp = 0` +
`nuXsectionMode = 6` (dSigmaMC) with `enu` in `&nl_SigmaMC`.

### Cross-section weight

The `weight` branch of `RootTuple` is a perturbative weight in **1e-38 cm² per
nucleon**, the common convention, so no scale factor or `÷ A` is applied
(`code/analysis/neutrinoAnalysis.f90` header; the KM3NeT `km3buu` wrapper
multiplies the same branch by A to get the whole-nucleus value). Within one run
the sum over all events is the flux-folded total cross section, so the
differential σ(E) still needs the unit-normalized flux density divided out:

```
xsec_weight_i = raw_weight_i / (num_runs · phi_hat(E_i))
```

Monoenergetic runs skip the flux division: `raw / num_runs`.

`xsec_norm_count` is **`num_runs`, not the event count**. GiBUU normalizes
`numEnsembles` out itself: `initNeutrino.f90:1292` sets
`perweight = totalWeight/float(numtry)`, where `numtry` counts nucleon
test-particles over *all* ensembles, so doubling `numEnsembles` halves each
weight and leaves the sum invariant. `num_runs_sameEnergy` never enters
`perweight`; GiBUU divides by it in its own analysis
(`neutrinoAnalysis.f90:3515`) and writes both counts as ROOT branches
(`EventOutput.f90:1132`).

`NUM_RUNS_SAME_ENERGY` is 1 and lives in one place, so the jobcard and the weight
normalization cannot drift. The normalizer reads *every*
`EventOutput.Pert.*.root` part and fails loudly if it finds fewer than
`num_runs`.

### Weak current

A jobcard selects exactly one `process_ID`, so `inclusive` runs **two passes**, in
`cc/` and `nc/`, whose events are concatenated. The ensembles are split evenly over the
passes. This is exact because GiBUU is cross-section weighted: each pass's
weights already sum to that current's cross section, so the union sums to
σ_CC + σ_NC with no reweighting.

### QE passes

`qes_oversampling = K` appends K QE-only passes per current, `cc_qe01` …
`cc_qeK`, then `nc_qe01` …, each with `includeQE` alone and the same
`numEnsembles` as its current's full pass. Every pass is described in the
translated config by `name`, `current` and `channels` (`all` or `qe`). The
normalizer:

- refuses a run whose pass directories differ from the translated passes, in
  either direction: a missing QE pass would leave the others carrying 1/(K+1) of
  a σ only K passes estimated;
- refuses a QE pass containing any `evType` other than 1 (`evType` is written once
  at the vertex and never changed by transport);
- multiplies each event's raw weight by 1/n before `compute_xsec_weight`, where n
  is the number of passes of its current whose channel set contains its `evType`
  (`pass_combination_scale`): 1/(K+1) for QE, 1 otherwise. GiBUU normalizes
  `perweight` by the nucleon test-particle count, not by the channels switched on,
  so a QE pass's weights sum to σ_QE and the average is unbiased.

`xsec_norm_count` stays `num_runs`: the combination happens inside the chunk, so
each chunk file is one σ estimate and merging is unchanged.

Pass *i* (in the order above) draws seed `task seed + i · PASS_SEED_OFFSET`
(`1_000_000`). The full passes come first, so their seeds and jobcards do not
depend on K. The cap of K = 50 keeps the last seed inside int32: task seeds reach
2·10⁹, which leaves room for 148 passes.

### RootTuple branches

Tree `RootTuple`, one file per run per pass. Branches read: `lepIn_E` (GeV),
`weight`, `evType`, `lepIn_P{x,y,z}`, `lepOut_{E,Px,Py,Pz}`,
`nuc_{E,Px,Py,Pz}` (GeV; `nuc_charge` is a charge, not a PDG code, and is not
read), and the perturbative particle list (`barcode`/`E`/`Px`/`Py`/`Pz`).
Concatenating across runs is correct; they are parts of one estimate. A missing
pass or run file is a hard error, since it would silently understate σ.

The `nuc_*` branches are written in the same `NeutrinoProdInfo_Get` block as
`weight`/`evType` (`code/inputOutput/EventOutput.f90`), so they are filled for
every event. `storeNucleon` (default 2 = bound) is not overridden; GiBUU's own
comment notes that a real check of four-momentum conservation is only possible
with that setting.

For 2p2h, `mom_nuc` in `tneutrinoProdInfo` is a single `real, dimension(0:3)`,
and `doStoreNeutrinoInfo` (`initNeutrino.f90`) passes it `eN%nucleon`, never
`eN%nucleon2`, although GiBUU's own 2p2h cross section is
`abs4Sq(eN%boson%mom + eN%nucleon%mom + eN%nucleon2%mom)` (`lepton2p2h.f90`).
`nucleon2` appears nowhere in the output path; its only other use is
`ResidueAddPH` for the nuclear residue. The normalizer therefore blanks
`w_true_gev` for `evType` 35 and 36.

`evType` is `prod_id` (`EventOutput.f90:1138`), whose closed table is
`code/init/neutrino/initNeutrino.f90:296-307` (`max_finalstate_ID = 37`).

## Container build

`setup/apptainer/gibuu.def` mirrors `setup/Dockerfile.gibuu` (see
[../containers.md](../containers.md)). Non-obvious points:

- RootTuple needs **C++17 pinned**, the same fix km3buu applies.
- RootTuple is built **without `-j`**: the nested `cmake --build` loses the make
  jobserver file descriptor.
- ROOT comes from the `rootproject/root` base image and is copied into the
  GiBUU payload, so `nf-base.sif` carries a separate ROOT for GiBUU.

## Design notes

### Weighting determines everything

Because the raw weight *is* a cross section, three things follow that would be
wrong for the other generators: `D` is the run count, not the event count;
`inclusive` can be two passes concatenated; and statistics must be weighted.

### `num_runs`, not the event count, as the merge denominator

Using the event count would weight chunks by how many interactions GiBUU happened
to produce, which itself varies with the cross section. Because `numEnsembles`
is normalized out, a chunk estimates σ regardless of size. Raising
`NUM_RUNS_SAME_ENERGY` is safe only because every output part is read; reading
the first while dividing by N would silently report σ/N. Prefer `numEnsembles`
for more statistics anyway.

### One subdirectory per pass

The fixed output name means a second pass sharing a directory would overwrite the
first. The subdirectories are created even for one pass so the normalizer has a
single layout. `set -e` makes a failing pass fail the task rather than be masked
by the last pass's exit status.

### Seed offsets cannot collide

Task seeds are hashed into `[1, SEED_MODULUS]` rather than laid out
arithmetically, so the pass offsets can only collide by the same negligible
hash coincidence `build_task_manifest` already checks for, and the translator
asserts that the largest pass seed stays inside the int32 range Fortran seeds
need.

### QE oversampling as passes inside the task (2026-10)

The QE passes run in the same task as the full passes, as the CC/NC split does,
rather than as separate chunks. The chunk file is then a complete σ estimate,
so `xsec_norm_count`, merging and partial merges need no notion of channels.
Separate tasks would parallelize better but would make merging channel-aware,
and a partial merge missing QE chunks would silently misweight the rest. The
cost is longer tasks, which the chunk size absorbs.

Oversampling QE, rather than fixing GiBUU's QE sampling, was chosen because it
needs no change to GiBUU. The heavy QE weights are not in a corner of phase space:
the top 1% (56% of σ at 0.5–1 GeV, numu C12 CC) sit at typical Q² and angles,
so the sampling mismatches dσ across the whole peak, and the source-level fix
would be a patch to GiBUU's QE kinematics sampling. More samples help
linearly: 10 chunks gave 9.6–9.96× the per-bin QE `n_eff` of one.

### Path handling

`./flux.dat` is CWD-relative because every pathway runs GiBUU with the work
directory as CWD; an absolute `/work` path would be valid only under Docker.
`@NF_GIBUU_INPUT@` is resolved by the adapter because the buuinput location is
runtime- and version-dependent.

### A closed `evType` table

An out-of-range `evType` means our reading of the output is wrong, not that GiBUU
invented a channel, so it raises. A catch-all `other` is what hid the missing
32/33 mapping (see below).

### The non-resonant background is counted, as `dis` (2026-07)

A high-statistics run once put ~10% of its events into `other`, more than all of
`res`: the mapping covered only 1, 2–31, 34 and 35/36. These events must be
counted, since dropping them would understate the inclusive cross section
(`neutrinoXsection.f90:564-700`: a MAID-like amplitude with resonances
subtracted, or the Bosted–Christy background fit). `res` would be wrong, because
GENIE's `res` is purely resonant and GiBUU's background has the resonances
subtracted. GENIE fills the gst `dis` flag from `ProcInfo().IsDeepInelastic()`
(`gNtpConv.cxx:646-649`), and its DIS generator creates `kScDeepInelastic` for
all W (`DISInteractionListGenerator.cxx:77`) with the KNO tune applied below
`Wcut` (`KNOTunedQPMDISPXSec.cxx:195-237`). GiBUU's NuHepMC exporter
(`EventOutput.f90:1447-1499`) assigns 32/33 → `SIS_ID` 500/501 and 37 → 502,
distinct from `DIS_ID` 600; the coarser merge is accepted for cross-generator
comparability.

### FSI on (2026-10)

The jobcards used to set `numTimeSteps = 0`, GiBUU's documented setting for
*inclusive* cross sections, correct while the common output recorded no hadronic
observable. With it the list is the primary-vertex one. The final-state columns
need the post-transport list. GiBUU can cut bound nucleons itself
(`applyCuts = 2` in `&neutrinoAnalysis`), but only for `FinalEvents.dat`, not the
RootTuple, hence `_leave_nucleus`.

## Validation

### Transport

**C12** (release2025, local Docker, 1000 requested events, E^-2 over 0.5–5 GeV,
same seed with and without transport): 821 events both ways, `sum(weight)`
0.982030 both ways, `weight`/`evType`/`lepIn_*`/`lepOut_*` bit-identical, hadrons
per event 1.867 → 3.077.

**Ar40** (2026-10-05, 4000 requested events, same seed both ways): 3435 events
both ways, `weight`, `evType` and `lepOut_E` bit-identical, `sum(weight)` =
1.131102. Wall clock 65.6 s against 15.9 s (Apple Silicon, Docker under Rosetta).

> **Unverified:** these notes and physics.md quote 1.867 and 1.87 hadrons per event
> before transport, on C12 and Ar40 respectively. The near-identical baseline on
> two different nuclei is suspicious and should be rechecked.

### Bound nucleons

In the Ar40 run, 25% of the listed nucleons had `E < m_N` and sat inside the
nucleus (median radius 3.7 fm); another 12% were unbound but still inside, with
four-vector mass ~0.90 GeV. Escaped nucleons came out exactly on shell at
0.938 GeV. Without the bound-nucleon step the QE sample would have read 1.37
protons and 0.63 neutrons per event (instead of 1.03 and 0.45), and no QE event
would have lost its nucleon.

### Ensembles and runs

Running one jobcard three ways: doubling `numEnsembles` gave 2.04× the events at
0.86× the weight sum; doubling `num_runs_SameEnergy` gave 2.02× the events at
2.17× the weight sum. Reading only the first part of a real 2-run job while
dividing by 2 reported 4.07 instead of 8.13.

### 2p2h `w_true_gev` from one nucleon

On a 20k-event numu CC C12 run, computing `w_true_gev` from the single stored
nucleon gave a median of 0.924 GeV starting near `M_N`, where GENIE, NEUT and
NuWro give 2.18–2.25 GeV with a floor at 2 `M_N`.

### Heavy tails

In one 100k-event run the quasi-elastic sample had Kish `n_eff = 35` out of 14579
events, with the top 1% of events carrying 86% of the weight. Pooled over
0.1–500 GeV, `n_eff` is even lower (2–30 per 1.8M events in
`keystone_prod_v3_pilot`), but that mostly reflects the flux division spreading
weights across energies; per energy bin the QE efficiency is ~2%.

### QE oversampling

`keystone_prod_v3_pilot` (numu C12, 10 chunks, 9M events, γ = −1). QE share of σ
and of the variance of the inclusive σ, CC, per energy bin:

| E (GeV) | 0.1–0.2 | 0.2–0.5 | 0.5–1 | 1–2 | 2–5 | 5–10 | 10–50 | 50–500 |
|---|---|---|---|---|---|---|---|---|
| QE share of σ | 0.94 | 0.73 | 0.49 | 0.29 | 0.15 | 0.07 | 0.01 | 0.00 |
| QE share of variance | 1.00 | 0.99 | 0.98 | 0.96 | 0.91 | 0.78 | 0.17 | 0.02 |
| inclusive rel. error | 6.1% | 2.1% | 1.1% | 0.9% | 0.6% | 0.5% | 0.2% | 0.2% |
| QE rel. error | 6.4% | 2.8% | 2.1% | 3.0% | 3.9% | 6.3% | 7.4% | 27% |

NC is the same (QE share of the variance 0.86–0.99 below 5 GeV), and so are
numubar Ar40 and the other flavors and nuclei. Every other channel stays at
≤ 1% relative error where it carries ≥ 5% of σ. The per-bin QE `n_eff` grew
9.6–9.96× from one chunk to ten.

The gain at K passes follows from the variance shares: with QE at a fraction f of
the variance, the variance becomes f/(K+1) + (1−f) of today's.

`gibuu_qes_oversampling_pilot` (2026-10-29; numu, 4 chunks of 2.5e5 requested
events, K = 10) measured the cost and checked the QE-only jobcard:

| Job | full passes (cc+nc) | one QE pass | task | events / chunk |
|---|---|---|---|---|
| C12, γ = −1 | 66 min | 11.2 (cc) / 11.6 (nc) min | 4.9 h | 769k |
| C12, γ = −1.5 | 62 min | 12.3 / 13.1 min | 5.3 h | 1.18M |
| Ar40, γ = −1 | 92 min | 12.5 / 12.6 min | 5.7 h | 782k |

- **Yield vs. statistics.** A QE pass writes 2.3–3.3× the full pass's QE events.
  The ratio rises with energy, from ~1.1 at 0.1–0.2 GeV to ~6 above 50 GeV. Its
  per-bin QE `n_eff` nevertheless equals the full pass's in every bin, so the
  extra events carry small weights.
- **Unbiased.** Per energy bin, σ_QE from the 40 QE passes and from the
  10-chunk v3 pilot (independent seeds, equal QE `n_eff`) agree to within
  ±5–7% from 0.2 to 10 GeV. The pulls mostly fall within ±2, the largest is
  3.4, and the deviations have no consistent sign across jobs. Above 10 GeV the
  QE passes' σ_QE scatters up to 2× because one or two of the 40 passes carry most
  of it (max/mean 17–30). Medians and trimmed means agree, and QE is < 1% of σ
  there. Inclusive σ agrees with v3 to within ±3% in every bin from 0.2 to
  500 GeV.
- **Gain per CPU hour** (variance × CPU time, v3 / pilot), CC:

  | E (GeV) | 0.1–0.2 | 0.2–0.5 | 0.5–1 | 1–2 | 2–5 | 5–10 | 10–50 | 50–500 |
  |---|---|---|---|---|---|---|---|---|
  | C12, γ = −1 | 2.4 | 2.6 | 2.3 | 2.1 | 1.6 | 0.95 | 0.28 | 0.26 |
  | Ar40, γ = −1 | 2.9 | 2.6 | 2.4 | 2.2 | 1.5 | 0.90 | 0.39 | 0.27 |
  | C12, γ = −1.5 | 6.5 | 6.2 | 5.7 | 3.1 | 1.4 | 0.61 | 0.08 | 0.02 |

  NC is within ~20% of CC. After oversampling, QE still carries ≥ 94% of the
  variance below 0.5 GeV and 40–65% at 1–5 GeV. The cost model
  1 / ((f/(K+1) + 1−f)(1 + rK)), with QE variance share f and QE-pass cost ratio
  r, reproduces the measured gains and puts the optimum K at ~10 below 1 GeV and
  at 3–6 at 1–5 GeV.
- **The steeper spectrum** moves statistics from above 5 GeV to below 2 GeV:
  per CPU hour, on top of oversampling, ~2.5× below 1 GeV and ~1.5× at 1–2 GeV. GiBUU's user flux uses
  equidistant bins (`FLUX_NBINS` over 0.1–500 GeV, ~1 GeV wide), so γ shapes
  sampling only above ~1 GeV.

### Background mapping

The high-statistics run that exposed the missing 32/33 mapping put 31 885 of
313 389 events into `other`.

### Per-nucleon normalization

From GiBUU's source and the km3buu cross-check only (see
[Cross-section weight](#cross-section-weight)).

> **Unverified:** physics.md used to say this was checked by rerunning the same
> flux on several nuclei; no such GiBUU measurement is recorded.

## Upstream issues

Not reported.

1. **2p2h Δ is not in the release**: `include2p2hDelta` aborts in
   `initNeutrino.f90:689`.
2. **The second 2p2h nucleon is not written** to the RootTuple, so the pair's
   invariant mass, which GiBUU's own cross section uses, cannot be reconstructed.
