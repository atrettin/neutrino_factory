# GiBUU

Generator-specific domain knowledge. The conventions this is normalized onto are
in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

Files: `generators/gibuu.py`, `translators/gibuu.py`, `normalizers/gibuu.py`,
`setup/Dockerfile.gibuu`, `setup/apptainer/gibuu.def`.

GiBUU is the odd one out in this framework: it is **phase-space sampled and
weighted by cross section**, where the other three are rejection-sampled and
unweighted. Almost every difference below follows from that.

## Versions and provenance

Distributed as **HEPForge release tarballs**, not from git — the GitHub mirror is
stale. `CODE_VERSIONS` carries `release2025`, with `git_ref` holding the release
tag so the catalog treats it as buildable. The build arg strips the prefix
(`release2025` → `GIBUU_RELEASE=2025`). Downloads use
`wget --content-disposition`.

`config_version` is `"default"` only.

## How it is run

**GiBUU reads its jobcard from stdin** (`GiBUU.x < job.job`) and writes its ROOT
output into the **current working directory** under a fixed name,
`EventOutput.Pert.<run>.root`.

That fixed output name forces the layout: **one subdirectory per pass**, created
uniformly whether the run has one pass or two, so the normalizer has a single
layout to read. A second pass sharing a directory would overwrite the first. Each
pass gets its own copy of the flux table, since the jobcard's `FileNameFlux` is
CWD-relative. The passes are chained with `set -e` so a failing pass fails the
task rather than being masked by the exit status of the last one.

`path_to_input` is written as the placeholder `@NF_GIBUU_INPUT@` and resolved by
the adapter, because the buuinput location is runtime- and version-dependent:
`/opt/GiBUU/buuinput` under Docker, but
`/opt/nf/generators/gibuu/<code_version>/GiBUU/buuinput` in the
version-namespaced Apptainer payload.

**Native binary first.** The branch order is load-bearing, as for every adapter.

### Jobcard settings that matter

Fortran namelist, rendered by `_render_jobcard`:

| Key | Value | Why |
|---|---|---|
| `version` | the release year | GiBUU **refuses to run** unless the jobcard version matches the code release |
| `eventtype` | 5 | neutrino induced |
| `EventFormat` | 4 | RootTuple ROOT output |
| `numTimeSteps` | 0 | no FSI transport — GiBUU's documented setting for inclusive cross sections, see below |
| `densitySwitch` / `pauliSwitch` | 2 / 2 | static density and Pauli blocking (fixed target) |
| `process_ID` | ±2 (CC) / ±3 (NC) | negative for antineutrinos, from the **PDG sign**, not the name |
| `flavor_ID` | 1/2/3 | e/μ/τ, independent of ν vs ν̄ |

### Final-state interactions are switched off, deliberately

`numTimeSteps = 0` disables GiBUU's FSI transport entirely:
`inputGeneral.f90:514` sets `time_max = numTimeSteps * delta_T`, so
`GiBUU.f90`'s `PhaseSpaceEvolution : do while (time < time_max - delta_T/2.)`
loop never executes. No hadron is propagated, rescattered or absorbed.

**This is GiBUU's own documented setting for inclusive cross sections, not a
shortcut.** Every shipped neutrino jobcard in `testRun/jobCards/` carries the
comment *"for inclusive cross sections set numTimeSteps = 0"* next to the value
it uses, and `005_Neutrino_FASERnu.job` ships with `numTimeSteps = 0`.

It is exact for what this framework records, because **the cross section is
fixed at the initial vertex**:

- `weight`, `evType`, `lepIn_*`, `lepOut_*` and `nuc_*` are all written from
  `neutrinoProdInfo` — a module whose own header says it "stores information
  about the initial neutrino event", written only from `initNeutrino.f90` and
  never updated afterwards.
- Transport never recomputes a weight. Every `%perweight =` assignment outside
  `init/` (in `collisionTerm.f90`, `master_1Body.f90`) *inherits* the parent's
  weight into the final state.
- The outgoing lepton does not interact strongly, so every kinematic column the
  common output derives — Q², x, y, the lepton angles — is an initial-vertex
  quantity.

**Verified empirically** (release2025, local Docker, 1000-event C12 CC run,
E^-2 flux over 0.5–5 GeV, same jobcard and same seed run both ways):

| | `numTimeSteps = 0` | `numTimeSteps = 150` |
|---|---|---|
| Time steps executed | 0 | 150 (out to 30 fm) |
| Events written | 821 | 821 |
| `sum(weight)` | 0.982030 | 0.982030 |
| `weight`, `evType`, `lepIn_*`, `lepOut_*` | — | **bit-identical** |
| Hadrons per event | 1.867 | 3.077 |

FSI demonstrably did real work — it raised the hadron multiplicity by 65% — and
changed the inclusive cross section by exactly nothing.

**When this stops being true.** The equivalence holds *only* because the common
output records no hadronic observables. Enabling FSI becomes mandatory the
moment any are added — pion multiplicities, knocked-out nucleons, calorimetric
or visible energy, or an experiment-style "CCQE-like" topology classification,
all of which FSI reshapes. Note also that `evType` is a *production* label
(GiBUU's `prod_id`), so it describes the interaction at the vertex rather than
the observed final state, with or without transport.

If FSI is ever enabled, two things follow: `numTimeSteps * delta_T` must
comfortably exceed the nuclear radius (the shipped cards use 150 × 0.2 fm =
30 fm), and `EventOutput.f90`'s `write_pert` notes that *"events with no
particles in the output … are not included in the list and thus produce no
output"* — so an event whose hadrons are all absorbed would drop out of the file
and take its weight with it. That did not occur in the run above (both counts
were 821), but it would need checking before trusting a summed cross section
from an FSI-enabled run.

### Ensemble sizing

Each GiBUU ensemble simulates the whole nucleus, but only a fraction of its A
nucleons yields an accepted interaction, so the perturbative event yield is
empirically **~A/2 events per ensemble** (measured ~6 events/ensemble on carbon).
`numEnsembles` is sized to land near the requested event count.

GiBUU warns and **aborts for `numEnsembles < 100`** unless the value is given
negative — its documented "enforce anyway" override for small runs — so small
counts are negated.

An inclusive run splits its ensembles evenly over the two passes, so the
requested event count is the budget for the run as a whole rather than per
current. The resulting CC:NC split is **not 50:50**: each pass yields whatever
its own cross section produces from half the ensembles.

### `numEnsembles` versus `num_runs_SameEnergy`

Both scale statistics; **only the first is normalized out for you**, which is why
`num_runs` is the divisor in the weight and why chunk size is scaled with
`numEnsembles`. Read from the release2025 source: `initNeutrino.f90:1292` sets
`perweight = totalWeight/float(numtry)`, where `numtry` counts nucleon
test-particles over *all* ensembles (`realParticles` is indexed
`(ensemble, particle)`) — so doubling `numEnsembles` halves each weight and
leaves the sum invariant. `num_runs_sameEnergy` never enters `perweight`; GiBUU
divides by it in its own analysis (`neutrinoAnalysis.f90:3515`) and hands both
counts to the consumer as ROOT branches (`EventOutput.f90:1132`).

Confirmed by running one jobcard three ways: doubling ensembles gave 2.04× the
events at 0.86× the weight sum; doubling runs gave 2.02× the events at 2.17× the
weight sum (the spread is GiBUU's heavy tails).

Because `numEnsembles` is auto-normalized, a GiBUU chunk estimates σ regardless
of its size — the property that makes `num_runs`, not the event count, the right
`xsec_norm_count` for merging.

`NUM_RUNS_SAME_ENERGY` is 1 and lives in one place so the jobcard and the weight
normalization cannot drift. Raising it is safe **only** because the normalizer
reads *every* `EventOutput.Pert.*.root` part; reading the first while dividing by
N would silently report σ/N (measured: 4.07 instead of 8.13 on a real 2-run job).
The normalizer fails loudly if it finds fewer parts than `num_runs`.

## Flux handling

`nuExp = 99` (user flux) + `nuXsectionMode = 16` (EXP_dSigmaMC) reads an
equidistant `energy[GeV] flux` table from `FileNameFlux`. `FLUX_NBINS = 500`;
GiBUU allocates the flux arrays dynamically.

Two details:

- **Energies are equidistant bin centers.** GiBUU's `read_fluxfile` requires
  equidistant bins and takes the energy as the middle of the bin. Leading `#`
  comment lines are allowed. The flux column is a relative weight, normalized
  internally by GiBUU.
- **The leading `./` in `./flux.dat` matters.** GiBUU's `ExpandPath` uses a
  filename verbatim only if it contains a `/`; without it the name would be
  expanded against GiBUU's input tree. The path is deliberately CWD-relative,
  since every pathway runs GiBUU with the work directory as CWD — an absolute
  `/work` path would be valid only under Docker.

A degenerate energy range falls back to fixed-energy `nuExp = 0` +
`nuXsectionMode = 6` (dSigmaMC) with `enu` in `&nl_SigmaMC`.

## Cross-section weight

Every generated interaction carries a perturbative weight (the `weight` branch of
`RootTuple`). Two properties, established from GiBUU's own source
(`code/analysis/neutrinoAnalysis.f90` header) and cross-checked against the
production KM3NeT `km3buu` wrapper, which reads the identical branch:

- **Units are already 1e-38 cm²** — exactly the common convention — so no
  `XSEC_SCALE` factor is applied.
- **Already per nucleon**: km3buu multiplies by the mass number `A` to recover
  the whole-nucleus cross section, confirming the raw value is per nucleon. No
  division by `A`.

Within a single run the sum over all events reproduces the flux-folded total
cross section. Because a spectrum run's weights are therefore already
flux-folded, recovering the differential σ(E) still requires dividing out the
unit-normalized flux density:

```
xsec_weight_i = raw_weight_i / (num_runs · phi_hat(E_i))
```

What differs from NuWro is the *constant* (`num_runs`, no `1e38`), not the
presence of the flux division.

**Negative weights (interference terms) are passed through unchanged** — they are
physical and must be summed as-is. This is why the weight-efficiency statistic in
`analyze-kinematics` is computed on `|w|`.

`xsec_norm_count` returns **`num_runs`, not the event count** — the one generator
where these differ. Using the event count would weight chunks by how many
interactions GiBUU happened to produce, which itself varies with the cross
section.

Monoenergetic runs (fixed-energy mode 6) skip the flux division entirely:
`raw / num_runs`, since there is no flux to divide out and the differential
`/bin_width` convention is degenerate at a single energy.

**Heavy tails.** GiBUU's weight is extremely concentrated in some channels: in
one 100k-event run the quasi-elastic sample had Kish `n_eff = 35` out of 14579
events, with the top 1% of events carrying 86% of the weight. Quote bootstrap
intervals rather than point estimates for GiBUU channel statistics, and use
`analyze-kinematics`'s efficiency table before trusting a number.

## Weak current: `inclusive` is two passes

A GiBUU jobcard selects exactly one `process_ID`, so there is no inclusive run.
`inclusive` is generated as **two passes**, one per current, in `cc/` and `nc/`
subdirectories with different seeds (`PASS_SEED_OFFSET = 1_000_000` on the NC
pass), whose events are concatenated.

That is **exact rather than an approximation** because GiBUU is
cross-section-weighted: each event carries an absolute per-nucleon weight and
each pass's weights already sum to that current's cross section, so the union
sums to σ_CC + σ_NC with no reweighting. A rejection-sampled generator could not
be combined this way.

`RootTuple` has **no per-event current branch**, so `is_cc` comes from which pass
directory the event was read from.

The seed offset is safe because task seeds are hashed into `[1, SEED_MODULUS]`
rather than laid out arithmetically, so it can only collide by the same
negligible hash coincidence `build_task_manifest` already checks for, and the sum
stays inside the int32 range Fortran seeds need.

## Output and interaction taxonomy

Tree `RootTuple`, one file per run per pass. Branches: `lepIn_E` (the incoming
neutrino energy, **GeV**), `weight`, `evType`, `lepIn_P{x,y,z}`,
`lepOut_{E,Px,Py,Pz}` and the struck nucleon `nuc_{E,Px,Py,Pz}` (also GeV; the
companion `nuc_charge` is a charge, not a PDG code, and is not read).
Concatenating across runs is correct — they are parts of one estimate. A missing
pass or run file is a hard error, since it would silently understate σ.

The `nuc_*` branches are written inside the same `NeutrinoProdInfo_Get` block as
`weight`/`evType`/`lepIn_*` (`code/inputOutput/EventOutput.f90`), so they are
filled for every event that reaches the file, and GiBUU has no coherent channel
to blank. What they contain is governed by the jobcard's `storeNucleon`, which
defaults to **2 = bound**:
the nucleon including its mean-field potential, so its invariant mass sits below
`M_N`. GiBUU's own comment notes that a real check of energy and momentum
conservation is only possible with that setting. The framework does not override
it.

**Only one nucleon is stored, which makes `w_true_gev` uncomputable for 2p2h.**
`mom_nuc` in `tneutrinoProdInfo` is a single `real, dimension(0:3)`, and
`doStoreNeutrinoInfo` (`initNeutrino.f90`) passes it `eN%nucleon` — never
`eN%nucleon2`. That second nucleon is not incidental to the channel: GiBUU's own
2p2h cross section is `abs4Sq(eN%boson%mom + eN%nucleon%mom + eN%nucleon2%mom)`
(`lepton2p2h.f90`), i.e. the pair's invariant mass. But `nucleon2` appears
nowhere in the output path — its only other use is `ResidueAddPH` for the nuclear
residue — so the pair cannot be reconstructed from a RootTuple file.

The normalizer therefore blanks `w_true_gev` for `evType` 35 and 36 rather than
computing it from the single stored nucleon. That would have produced a
one-nucleon invariant mass under a column defined as the struck *system*: on a
20k-event numu CC C12 run it gave a median of 0.924 GeV starting near `M_N`,
where GENIE, NEUT and NuWro give 2.18–2.25 GeV with a floor at 2 `M_N`. `w_gev`
is unaffected, since it uses no nucleon at all.

`evType` is GiBUU's `prod_id` (`code/inputOutput/EventOutput.f90:1138`), whose
authoritative and **closed** table is
`code/init/neutrino/initNeutrino.f90:296-307` (`max_finalstate_ID = 37`):

| `evType` | GiBUU meaning | Common label |
|---|---|---|
| 1 | nucleon (QE) | `qel` |
| 2–31 | non-strange baryon resonance (2 = Delta) | `res` |
| 32 | π neutron-background | `dis` |
| 33 | π proton-background | `dis` |
| 34 | DIS | `dis` |
| 35 | 2p2h QE | `mec` |
| 36 | 2p2h Delta | `mec` (unreachable, see below) |
| 37 | two-pion background | `dis` |

An `evType` outside 1–37 raises `ValueError` naming the code rather than
producing `other`. Since the code space is closed, an out-of-range value means
our reading of the output is wrong, not that GiBUU invented a channel — and the
catch-all is what hid the bug below. GiBUU consequently never emits `other`, nor
`coh`.

### Why the non-resonant background is `dis`

*Moved here from `design_decisions.md` (2026-07).*

A high-statistics run put ~10% of its events (31 885 of 313 389) into the `other`
bucket — more than the entire `res` category. The mapping covered only
1 → `qel`, 2–31 → `res`, 34 → `dis`, 35/36 → `mec`; the missing 32/33 accounted
for the whole bucket.

**They must be counted.** 32/33/37 are GiBUU's *non-resonant* shallow-inelastic
contribution, generated only for 1.2 < W < `REScutW` (default 2.0 GeV) either
from a MAID-like amplitude with the resonance contributions subtracted or from
the Bosted–Christy background fit (`neutrinoXsection.f90:564-700`). They do not
double-count anything: GiBUU damps them with `Sigmoid(W, REScutW, -0.05)` exactly
where the PYTHIA/DIS piece turns on (`case (chDIS)` returns unless
`W > REScutW - 0.1`). Dropping them would understate the inclusive cross section.

**They are labelled `dis` because the taxonomy is pinned to GENIE** — an event
gets the category it would have had if GENIE had produced it. GENIE has no
shallow-inelastic category: its non-resonant background is produced by the DIS
generator (`DISInteractionListGenerator.cxx:77` creates `kScDeepInelastic` for
all W) with the KNO multiplicity tune applied below `Wcut`
(`KNOTunedQPMDISPXSec.cxx:195-237`), and `gNtpConv.cxx:646-649` fills the gst
`dis` flag straight from `ProcInfo().IsDeepInelastic()`. `res` would be wrong:
GENIE's `res` is purely Rein–Sehgal resonant, and GiBUU's background has the
resonances subtracted.

**Caveat.** GiBUU disagrees with this grouping internally: its own NuHepMC
exporter (`EventOutput.f90:1447-1499`) assigns 32/33 → `SIS_ID` 500/501 and
37 → 502, distinct from `DIS_ID` 600. The coarser merge is accepted because
cross-generator comparability with GENIE/NEUT/NuWro — none of which expose a
shallow category either — is worth more than a distinction only one generator can
make. A future `sis` label would have to be introduced for all four generators at
once, and for GENIE it is not recoverable from the gst output.

### Every channel is enabled

All GiBUU channel switches except `includeQE` default to `.false.`, so the
jobcard sets them all explicitly: `includeQE`, `includeDELTA`, `includeRES`,
`includeDIS`, `include1pi`, `include2pi`, `include2p2hQE`.

**`include2pi` is easy to forget and biases the total low** rather than merely
omitting a category: with `new_eN = .true.` (the default,
`neutrinoParms.f90:260`) GiBUU scales the 1π background *down* above W = 1.267
"to allow for 2pi contribution" (`neutrinoXsection.f90:652-656`), and with 2π off
that strength is never added back. Its events land in `dis` with the rest of the
non-resonant background.

## Container build

`setup/apptainer/gibuu.def` mirrors `setup/Dockerfile.gibuu` (see
[../containers.md](../containers.md)). Non-obvious points:

- RootTuple needs **C++17 pinned** — the same fix km3buu applies.
- RootTuple is built **without `-j`**: the nested `cmake --build` loses the make
  jobserver file descriptor.
- ROOT (from the `rootproject/root` base) is folded into the payload, so
  `nf-base.sif` carries GiBUU's own copy.

## Known limitations

- **2p2h Delta is unavailable.** `include2p2hDelta` aborts release2025 via
  `notInRelease("2p2p Delta")` (`initNeutrino.f90:689`) because the feature is
  unpublished. MEC is therefore 2p2h-QE only and `evType` 36 cannot occur in this
  release — the mapping keeps it for a future one. Tracked in `.claude/TODOS.md`.
- Weights are heavy-tailed; effective sample sizes can be orders of magnitude
  below the event count. See the weight-efficiency note above.
- `config_version` is `"default"` only.
- `run.log_level` is ignored — only the GENIE adapter maps it.
- **A GiBUU abort on a bad jobcard key currently surfaces as a confusing
  `FileNotFoundError`** for a missing task JSON, because `local.run_task` does
  not check the generator's exit status. Tracked in `.claude/TODOS.md`.
