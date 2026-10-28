# NuWro

NuWro is built from source off the NuWro/nuwro GitHub tags and run in a single
stage from a generated `params.txt`. Its per-event weight is one run-wide
constant that is already per nucleon. Reweight universes are supported for the QE
axial form factor and the channel normalizations. The conventions its output is
normalized onto are in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

## At a glance

| | |
|---|---|
| Code versions | `nuwro_25.11` |
| `config_version` | `default` only; no parameter-set versioning yet |
| Sampling | unweighted (rejection-sampled) |
| Raw σ → per nucleon | bare cm², already per nucleon → `× 1e38`, no `÷ A` |
| `physics.current` | the `dyn_*` channel switches ([Weak current](#weak-current)) |
| FSI | `FSI_on = 1`, pinned |
| `run.log_level` | ignored |
| Reweight universes | yes, QE axial form factor and channel norms ([Reweight universes](#reweight-universes)) |

## Setup

`neutrino-factory setup` and the generic image build cover NuWro; there is no
data staging step. Its image needs Pythia6, built by the shared
`setup/lib/build_pythia6.sh` ([Container build](#container-build)).

## Configuring a job

There are no NuWro-specific knobs in the run config besides
`nuwro.universes`. Everything else NuWro needs is generated:

- **Flux.** Any framework flux works; NuWro gets it as a 500-bin equal-width
  histogram ([Implementation](#flux)).
- **Target.** Any nucleus works. It is selected by proton and neutron count
  derived from the name, so no table entry is needed.
- **Dynamics.** All channels of the selected current are on, except ν-e
  scattering (`dyn_lep`), which is off for every current because its target is an
  atomic electron ([Weak current](#weak-current)).
- **Physics defaults that matter downstream** are NuWro's own and not
  overridden: the spectral function for QE (`sf_method = 1`), the hybrid RES
  model (`res_kind = 2`), and `res_dis_cut = 1900` MeV.

**Keep `NF_WORK_ROOT` short** when testing; NuWro aborts on long work-directory
paths ([Known limitations](#known-limitations)).

## Reading the output

### Interaction labels and `is_cc`

The `e/flag/flag.{qel,res,dis,coh,mec}` flags go through the same priority chain
as GENIE's (qel → res → dis → coh → mec → other). `is_cc` comes from `flag.cc`, a
separate flag, since the class flags span both currents.
`native_interaction_code` is `e/dyn`, which distinguishes the CC and NC variant
of each channel (0 = CC QE, 2 = RES, 4 = DIS, 6 = COH, 8 = MEC).

**NuWro's `res` is not purely resonant.** Its RES channel blends non-resonant
background into itself well below `res_dis_cut`
([Model notes](#the-resdis-split-is-a-hard-cut-plus-a-blend-inside-res)). Use
`resonant_primary` to separate the two.

### `resonant_primary` comes from `flag.res_delta`

`e/flag/flag.res_delta` marks an event whose hadronic final state came from the
resonant term; when false, the final state was hadronized by PYTHIA, i.e. the
non-resonant piece. It is the source of the common output's
[`resonant_primary`](../physics.md#the-resonant_primary-column). Its fraction
falls with W exactly as the blending predicts: always true at the Δ peak, rarely
true near 1.9 GeV, never for `dyn_dis` ([Validation](#res_delta-tracks-the-blend)).

The flag's meaning depends on the RES model, so `e/par/par.res_kind` is checked.
Under the hybrid model (`res_kind = 2`, the default) every resonant final state
sets it. Under `resevent2.cc` (`res_kind != 2`) the below-PYTHIA-threshold branch
does not, so the flag would read false across the Δ peak. The normalizer raises
rather than fill `resonant_primary` from such a run.

### `w_true_gev` is NuWro's own W

`w_true_gev` is the invariant mass of the pre-FSI outgoing hadrons, `e/out[1:]`.
That is NuWro's own `event::W()`, the W its RES channel sampled and cut on, so
`res` and `dis` split exactly at `res_dis_cut` in this column.

It is computed by passing `derive_kinematics` the nucleon that balances the
hadrons, `Σ e/out[1:] − p_ν + p_l`, not the nucleon stored in `e/in`, which is
the one *before* NuWro subtracted the binding energy
([Model notes](#binding-energy-at-the-vertex)). Consequences:

- `qel` `w_true_gev` is exactly the nucleon mass for every event. With the
  spectral function a correlated QE event also carries a spectator SRC partner at
  `e/out[2]`; for `qel` only `e/out[1]` is used.
- `mec` comes out the same either way: its stored vertex already balances.
- Coherent events stay blank: their `e/out[1:]` is the lone pion, and `m_π` is
  not a struck-system W.
- `e/in` is used only to decide *whether* an event has a struck system: it must
  carry a nucleon (PDG 2112/2212). A few events carry an atomic electron there
  (ν-e elastic); they get the placeholder.

### Final-state content

The final-state columns are summarized from `e/post` ("particles leaving the
nucleus"), which includes the outgoing lepton (the summary excludes it). `e/out`
is the pre-FSI list.

## Model notes

### The RES/DIS split is a hard cut, plus a blend inside RES

Three parameters govern it (`src/params_all.h`, MeV, defaults not overridden):

| Parameter | Default |
|---|---|
| `res_dis_cut` | 1900 |
| `res_dis_blending_start` | 1600 |
| `res_dis_blending_end` | 1900 |

NuWro writes them per event into `treeout` (`e/par/par.res_dis_cut`,
`e/par/par.res_dis_blending_{start,end}`), so a run can be checked against its
own output.

**The two channels are disjoint at `res_dis_cut`.** RES samples W in
`[1080 MeV, min(res_dis_cut, kinematic max)]` (`src/dis/res_kinematics.cc`); DIS
samples W from `[res_dis_cut, Wmax]` (`src/dis/disevent.cc`) and returns weight 0
if the beam cannot reach the cut. So a `res` event never exceeds 1.9 GeV and a
`dis` event never falls below it.

**The blending is a different thing**: it interpolates the *composition of the
RES channel*, not the choice between channels. `resevent2.cc` describes the
model as Δ-resonant × `alfadelta(W)` + DIS-like single-pion × `alfadis(W)` +
non-SPP DIS, "the main idea behind is that DIS contribution simulates
non-resonant part".

**`res_dis_blending_start` is a kink, not the onset.** `src/dis/alfa.cc` makes
the non-resonant fraction piecewise-linear in three pieces:

```
W in (1080, W_min):   alfa * (W - 1080) / (W_min - 1080)          0     -> alfa
W in [W_min, W_max):  alfa + (1 - alfa) * (W - W_min)/(W_max - W_min)   alfa -> 1
W >= W_max:           1
```

with `alfa` a channel-dependent base of 0, 0.2 or 0.3. The background ramps up
from **W = 1080 MeV**, reaches `alfa` at `res_dis_blending_start` and 1 at
`res_dis_blending_end`. For channels whose base is zero, `betadis` substitutes
`W_min = 1300 − 75 · bkgrscaling` (default 1300 MeV, where the observed
distribution does turn on). The blending start is therefore not one number across
channels, which is why plots draw only `res_dis_cut`.

The consequence: **by 1.9 GeV the RES channel is entirely non-resonant
background, yet still labelled `res`.** This is the same kind of mixing that
makes NEUT's single-pion modes unalignable, arrived at by a different route
([physics.md](../physics.md#the-shallow-inelastic-region)). Unlike NEUT, NuWro
says which it was, through `flag.res_delta`.

### Binding energy at the vertex

NuWro solves each vertex against a *copy* of the struck nucleon with a binding
energy subtracted from its energy alone, but stores the unbound nucleon in
`e/in`. So the four-vectors in `e/in` and `e/out` do not balance in energy:

- **RES and DIS:** `target.t -= get_binding_energy(...)` in `res_kinematics.cc`;
  `nuc0.t -= _E_bind` in `disevent.cc`. The hadrons are built in that copy's rest
  frame with invariant mass W and boosted back. The three-momentum balances
  exactly; the energy is short by `E_b`, median ~33 MeV.
- **QE:** `qelevent1.cc` subtracts it before solving the kinematics
  (`aa.t -= _E_bind`). The stored QE vertex has a median energy deficit of
  ~6 MeV and a median missing three-momentum of ~18 MeV.

For the framework's runs (`nucleus_target = 2`, local Fermi gas):

```
_E_bind = nucleus::Ef(N0) + kaskada_w,   Ef = sqrt(k_F(local)^2 + M^2) - M
```

(`nucleus.cc:143-157`, `kaskada_w = 7` MeV). `k_F` is the **local** Fermi
momentum, so `_E_bind` varies event by event with the density at the vertex,
roughly 7 to 32 MeV across carbon. This is the local density approximation of
Juszczak, Nowak & Sobczyk (NuInt04, *Spectrum of recoil nucleons in quasi-elastic
neutrino-nucleus interactions*). The momentum-dependent optical potential from
the same paper is a different option (`nucleus_target = 6`) and is not used.

`E_b` is not stored and depends on the local density, which is why `w_true_gev`
reads the outgoing hadrons rather than correcting `e/in`. A W reconstructed from
`e/in` would sit a median ~40–55 MeV too high for `res`/`dis` and leak events past
`res_dis_cut`. Not everything about the QE vertex is explained yet
([Open questions](#open-questions)).

## Reweight universes

A job with a `nuwro.universes` block (schema in
[../configuration.md](../configuration.md#reweight-universes-and-variations))
gets a second stage after generation. It writes one weight per event per
universe, σ_universe/σ_central at the event's stored kinematics, into
`events/universe_weights`. The sampling conventions are in
[../physics.md](../physics.md#reweight-universes).

A physics parameter's central value is the generation's own, read from the
output, in NuWro units; in linear mode `sigma` is absolute in those units. The
channel norms have central 1.

### Supported parameters

| Parameter | Status in nuwro_25.11 | NuWro default 1σ |
|---|---|---|
| `qel_minerva_ff_scale` | **Works under the default `qel_axial_ff_set = 8`** (MINERvA axial FF). It is the axial form factor's 1σ band weight: `ff.cc` evaluates `zExp − bandWeight·√uncSq`, central 0, documented range −1…+1. | ±1 (absolute) |
| `qel_deuterium_ff_scale` | The same, for `qel_axial_ff_set = 9`. | ±1 (absolute) |
| `qel_cc_axial_mass`, `qel_nc_axial_mass` | Act only under the dipole axial FF (`qel_axial_ff_set` 1–3). Under the default set 8 the axial mass has no effect, and the adapter refuses them. | ±20 %, ±16 % |
| `qel_s_axial_mass`, `delta_s` | Strange axial FF. Accepted, but act on NC only. The normalizer refuses a sample in which no universe moves any weight off 1. | ±16 %, ±10 % |
| `qelNorm resNorm disNorm cohNorm mecNorm ccNorm ncNorm antyNorm` | Applied by the framework as flat factors from the event flags (central 1), because NuWro's own norm engine never runs ([upstream issue 1](#upstream-issues)). | `qelNorm` +15 / −10 %, `mecNorm` ±25 %; none for the others |
| `pion_axial_mass`, `pion_C5A` | **Rejected.** Under the hybrid RES model `rewRES` returns NaN for every RES event ([upstream issue 2](#upstream-issues)). Generation does not depend on them either. | — |
| bba07, zexp, 2comp/3comp, `qel_cc_vector_mass`, `SPPBkgScale`, `dynNorm*` | Rejected ([upstream issues 3–4](#upstream-issues)). | — |

**Choosing `sigma`.** The last column is NuWro's own uncertainty, the
`errPlus`/`errMinus` pair in `src/rew/rewparams.h`. NuWro applies them as
fractions of the run's value. Since a linear `sigma` here is absolute, a
fractional default becomes either `log: true` with that fraction as `sigma`, or a
linear `sigma` equal to the fraction times the central value: ±20 % on
`qel_cc_axial_mass = 1200` MeV is a linear `sigma` of 240. Caveats:

- **MINERvA and deuterium band scales.** Their table entry is meaningless as a
  fraction (their central is 0). The 1σ that applies is the band's own ±1, hence
  `sigma: 1.0` in linear mode.
- **Asymmetric entry.** `qelNorm` is +15 / −10 %, and a universe throw is
  symmetric.
- **Norms without a default.** `resNorm`, `disNorm`, `cohNorm`, `ccNorm`,
  `ncNorm` and `antyNorm` have no NuWro default.

### Coverage

What the universes cover is set by what NuWro can reweight: the QE axial form
factor plus the channel norms. DIS, MEC shape, FSI, the spectral function,
binding and Pauli blocking have no knob. The universe spread is therefore a lower
bound on NuWro's model uncertainty. The reweight stage is cheap: 100 universes on
a 20k-event chunk take seconds.

## Known limitations

- **NuWro aborts on a long work-directory path.** `*** buffer overflow detected
  ***: terminated` (SIGABRT) right at the "Run real events" banner, before any
  event, independent of flavour or nucleus. A 213-character chunk work directory
  failed, a ~120-character one ran (odslserv01, 2026-10-09). The default
  `NF_WORK_ROOT` layout on `/ptmp` (~146 characters) works. The exact limit is not
  known; keep `NF_WORK_ROOT` short (e.g. directly under `/ptmp/mpp/$USER`).
- **`numubar` on W184 aborts** with the same message, but *after* generation, at
  every event count ≥ 3k (2k passes). Other flavours on W184 and `numubar` on
  Fe56 are unaffected. The cause is not diagnosed (probably unrelated to the path
  limit), and the production grid drops `nuwro/W184/numubar`.
- `config_version` is `"default"` only; real parameter-set versions are not yet
  defined.
- `run.log_level` is ignored.

---

*Reference: the sections below describe how the adapter works, why, and the
evidence. Users can stop here.*

Files: `generators/nuwro.py`, `translators/nuwro.py`, `normalizers/nuwro.py`,
`setup/Dockerfile.nuwro`, `setup/apptainer/nuwro.def`,
`setup/nuwro/nf_reweight.cc`.

## Implementation

### Run stage and `params.txt`

The adapter writes a `params.txt` of `key = value` lines into the work directory
and invokes `nuwro -o events.root -i params.txt`. Everything the run needs is in
that file, including the flux. It also pins `FSI_on = 1`, the intranuclear
cascade that fills `e/post`. That is NuWro's default too, but with it off NuWro
copies `e/out` into `e/post` (`nuwro.cc`), which would make the final-state
columns silently pre-FSI.

**NuWro resolves `data/` relative to its binary.** Under Docker the container
runs with workdir `/opt/nuwro` and is given `/work/params.txt` and
`/work/events.root` explicitly. The Apptainer payload wrapper absolutizes
relative `-i`/`-o` arguments, then `cd`s into the install tree; this is the one
generator wrapper that must `cd`.

Like every adapter, `build_run_command` runs a binary found on `$PATH` before considering a container, since a cluster task already runs inside the Apptainer image, which cannot nest ([../apptainer_image.md](../apptainer_image.md)).

Adding parameter-set versions means extending `config_versions` on the adapter
and mapping each to a set of `params.txt` overrides.

### Flux

NuWro cannot take a continuous function. With `beam_type = 0` the spectrum is
encoded inline in `beam_energy`:

- a single value → monoenergetic beam;
- `E0 E1 a0 a1 … a(n-1)` → `n` **equal-width** bins over `[E0, E1]` with
  unnormalized bin weights `a_i`.

That matches `Flux.to_histogram` exactly. `FLUX_NBINS = 500`; NuWro's parser caps
at 5000. **Energies are in MeV** in this string; `_beam_energy` is the only place
the conversion happens on the input side.

The target is `target_type = 0` plus `nucleus_p` / `nucleus_n`, from
`particles.nucleus_composition`.

NuWro does not stamp the sampled flux into its output, so the normalizer rebuilds
the histogram from the run config with the same `to_histogram(nbins=FLUX_NBINS)`
call the translator used ([Design notes](#the-flux-is-rebuilt-from-the-config)).

### Cross-section weight

NuWro rejection-samples, and every accepted event's raw `weight` (`e/weight`) is
**the same constant for the whole run**: the flux-averaged total cross section in
cm². It is set in `NuWro::real_events` (`src/nuwro.cc`) as
`e->weight = _procesy.total();`, where `chooser::total()` (`src/chooser.h`) sums
the flux-averaged cross section over all *active* channels. The energy dependence
lives entirely in how many events land in each energy bin.

```
xsec_weight_i = raw_weight_i · 1e38 / (n_events · phi_hat(E_i))
```

`xsec_norm_count` is the chunk's event count.

**Already per nucleon; do not divide by A.** Each event's target nucleon is
drawn by `nucleus::get_nucleon()` (`src/nucleus.cc`) as one representative
nucleon, proton vs. neutron with probability equal to its isotopic fraction,
i.e. uniformly over all A nucleons, and no factor of A is applied afterwards. The
ensemble average of the raw weight is therefore the isospin-weighted per-nucleon
cross section.

### Weak current

The current is applied through NuWro's `dyn_*` switches, written out **in full**
for every current, including the channels that are off, so the params file
states the complete dynamics set rather than inheriting half of it from NuWro's
shipped `data/params.txt` (CC-only).

The nuclear-target grid is `dyn_<channel>_<current>` over
`DYNAMICS_CHANNELS = (qel, res, dis, coh, mec)`. Three channels sit outside it:

| Switch | Setting | Why |
|---|---|---|
| `dyn_hyp_cc` | follows the CC switch | Quasi-elastic hyperon production, a genuine CC channel (antineutrinos only) |
| `dyn_lep` | **off for every current** | ν-e scattering. On in NuWro's defaults and mixes both currents. Its target is an atomic *electron*, so its cross section is not on the per-nucleon normalization `xsec_weight` uses, and the `interaction` label would only mark it `other` |
| `dyn_qel_el` | off | (Quasi-)elastic *electron* scattering, for electron beams |

No `compute_xsec_weight` change accompanies the current selection: the raw weight
is `chooser::total()` over the *active* channels, so restricting channels
rescales it by construction.

### `treeout` branches

All momenta are in **MeV** (`MEV_PER_GEV = 1000.0`); the normalizer converts on
read.

- `e/in/in.{t,x,y,z}` (E, px, py, pz): index 0 is the beam neutrino, followed by
  the struck initial-state system, whose size is not fixed: none for coherent,
  one nucleon for `qel`/`res`/`dis`, two or three for `mec`, occasionally an
  atomic electron. Nucleons are selected by `e/in/in.pdg`, not by index.
- `e/out/out.{t,x,y,z}`: index 0 is the outgoing lepton, then the pre-FSI
  hadrons. It **can be empty**; short entries are zero-padded and flagged by
  `_has_leading`, which is passed to `derive_kinematics(valid=...)` so the whole
  kinematic block is blanked rather than filled with zeros. With the spectral
  function a correlated QE event has `out = [l, N1, N2]` (`sfevent.cc`).
  `flag.isCorrelated` cannot be used to detect that: outside QE it holds garbage.
- `e/post/post.{pdg,t,x,y,z}`: the particles leaving the nucleus after the
  cascade, including the lepton.
- `e/dyn`, `e/weight`, `e/flag/flag.cc`, `e/flag/flag.{qel,res,dis,coh,mec}`,
  `e/flag/flag.res_delta`, `e/par/par.res_kind`.

### `nf_reweight`

`src/rew/` holds three engines (`rewQEL`, `rewRES`, `rewNorm`), a table of
reweightable parameters (`rewparams.h`), and two drivers: `reweight_to` (one
parameter setting per process) and `reweight_along` (fixed ±2σ shifts from the
hardcoded errors; cannot throw universes). Each event is reweighted relative to
its own stored generation parameters (`e->par`). `rewQEL` recomputes the
free-nucleon `qel_sigma(Eν, q², …)`, times the RPA ratio when `qel_rpa` is 1 or
3; non-QE events get weight 1 from it.

The framework builds **`nf_reweight`** (`setup/nuwro/nf_reweight.cc`), a driver
over the same `REW`/`rew` objects adapted from `reweight_to.cc`. It evaluates
every universe inside one event loop and skips `SetupSPP` (the single-pion table
setup) unless a RES parameter is active. Unlike `reweight_to`, it writes a
non-finite ratio as it is instead of replacing it with 0, so the normalizer can
fail on it.

```
nf_reweight <events.root> <universes.txt> <weights.root>
```

`universes.txt` has the parameter names on the first line, then one universe's
absolute values (NuWro units) per line. The adapter writes it and
`universes.json` (the resolved metadata) into the task's work directory, reading
each universe's central value from the run's own `e/par` branches and refusing a
parameter whose value varies across the file. Norm-only universes need no NuWro
call; the norms are applied in Python from the event flags.

## Container build

`setup/apptainer/nuwro.def` mirrors `setup/Dockerfile.nuwro` (see
[../containers.md](../containers.md)). Non-obvious points:

- **Pythia6 is required** and built by the shared `setup/lib/build_pythia6.sh`
  (GENIE needs it too).
- ROOT 6.26+ dropped `TPythia6`/`libEGPythia6`, so the build adds the standalone
  **ROOTEGPythia6** package with `BUILTIN=ON`.
- **That build must not happen under `/tmp`.** The build path is baked into the
  ROOT PCM, and Apptainer bind-mounts the host `/tmp` over the image's at
  runtime, which would shadow the recorded dictionary path. It is built under
  `/opt` instead, one of the few deliberate divergences between the def and its
  Dockerfile.
- NuWro's `CMakeLists.txt` hardcodes the install prefix to the source directory,
  so it is built in place.
- `nf_reweight` is compiled into the NuWro tree after the main build. Its
  Makefile rule is `reweight_to`'s with the name swapped, so the link line
  cannot drift. The Apptainer payload exposes it as `bin/nf_reweight`, a wrapper
  with the same environment as `nuwro` but without the `cd`: its arguments are
  task-local paths, and the QE engine reads no `data/` files.

## Design notes

### The flux is rebuilt from the config

Elsewhere the framework normalizes against the flux the generator itself
recorded, because rebuilding it can silently disagree with what was sampled
([physics.md](../physics.md#flux)). NuWro records nothing, but here rebuilding is
safe: the translator and normalizer use one code path with equal-width binning,
and NuWro reads the bins as given, with no reinterpretation in between.

### `w_true_gev` from the outgoing hadrons (2026-09)

`w_true_gev` used to be computed from `e/in` like every other generator's, and
2.8% of `res` fell above `res_dis_cut`. Two alternatives were ruled out: taking
NuWro's W directly (`event::W()` is a method, not a stored branch), and
subtracting the binding energy ourselves (it would mean reimplementing NuWro's
density profile). Dated entry:
[design_decisions.md](../design_decisions.md#nuwros-w_true_gev-is-taken-from-the-outgoing-hadrons-2026-09).

### Why not `reweight_to`

Each `reweight_to` call spends ~12.5 s before its event loop, whatever the event
count, mostly in `SetupSPP`, which it runs unconditionally. At 100 universes
that is ~21 min per chunk, against 83 s to generate the chunk.

### Why no weighted generation

NuWro's `save_test_events = 2` would cover phase-space tails better, but the
reweightable parameters do not change where σ is non-zero, so ratio weights on
rejection-sampled events are already unbiased. The weighted mode also drops
zero-weight events and carries a running normalization
([upstream issue 7](#upstream-issues)). Dated entry:
[design_decisions.md](../design_decisions.md#reweight-universes-for-nuwro-2026-10).

## Validation

Unless stated otherwise: nuwro_25.11, numu CC on C12, E^-2 power law over
0.5–5 GeV, 20k events, `sf_method = 1`, local Docker.

### Per-nucleon normalization

NuWro's own console total, rescaled by `1e38`, comes out around 1 near 1 GeV,
the expected per-nucleon magnitude.

### `w_true_gev` reproduces `res_dis_cut`

The residual `p_ν + p_N(e/in) − p_l − Σ e/out[1:]` has median ΔE 33.4 MeV for
`res` and `dis` (1st/99th percentiles ~9/46 MeV), 0 for `mec`, and |Δp| ≤ 5e-4
MeV everywhere: the binding energy is the whole difference. From `e/in`, W sat a
median 42 MeV (`res`) and 54 MeV (`dis`) above NuWro's own, and 236 of 8400 `res`
events (2.8%) were above `res_dis_cut`. From the outgoing hadrons, `res` ends at
1.89997 GeV and `dis` starts at 1.90054 GeV, with nothing on the wrong side; a
fresh 100k-event run agrees (1.89994 / 1.90005).

### QE vertex

`e/out`'s outgoing nucleon has invariant mass 0.9383 at the 1st, 50th and 99th
percentiles. A reconstruction from `e/in` puts 83% of QE events within 30 MeV of
`m_p`. Over 6576 QE events the stored vertex has a median energy deficit of
5.9 MeV and median missing three-momentum of 18 MeV. 1642 of 8218 QE events carry
an SRC partner, exactly those with `flag.isCorrelated`; that flag is set on 13424
of all 20k events.

### `res_delta` tracks the blend

`flag.res_delta` is true in 100% of `res` events below 1.21 GeV, 93.6% at
1.3–1.4, 45.8% at 1.5–1.6, 7.5% at 1.8–1.9 and 1.3% at 1.9–2.0 GeV; never for
`dyn_dis`. The non-resonant part of `res` starts at 1.27 GeV, and 534 of its 2008
events (26%) lie below 1.6 GeV, so `res_dis_blending_start` is not the onset.

### `e/in` multiplicities

On a 100k-event numu/Ar40 run, coherent events carry no initial-state hadron,
`qel`/`res`/`dis` exactly one nucleon, `mec` two or three, and 9 events an atomic
electron.

### `nf_reweight`

100 universes × 20k events take 2.3 s, against ~12.5 s of fixed setup per
`reweight_to` call (12.7 s for 200 events, 13.4 s for 20k; dev Mac). Weights are
identical to `reweight_to`'s (max |Δ| = 0). Reweighting to the central value
gives weight ≡ 1.

### QE closure

A sample reweighted to `qel_minerva_ff_scale = +1` against one generated directly
at that value: QE σ 0.4480 against 0.4498 ×10⁻³⁸ cm² (nominal 0.4042), agreeing
to 0.4%; the Q² ratio is consistent with 1 in all eight bins up to 3 GeV²; other
channels unchanged.

### RES parameters do not affect generation

A sample generated at `pion_axial_mass = 1.04` has the same RES σ as one at 0.94
(0.4131 ×10⁻³⁸ cm² both, same seed).

## Upstream issues

Bugs and pitfalls found in nuwro_25.11, to report to the NuWro team. None has
been reported yet.

1. **The `*Norm` reweight parameters do nothing.** In `src/rew/rewparams.h` they
   are registered with engine `"rewDyn"`, but the `Reweighters` in
   `src/rew/Reweighters.h` are named `rewNorm`, `rewQEL` and `rewRES`. So
   `REW("rewDyn")` returns the `End` sentinel, `calcNorm` never runs, and every
   weight is 1. Nothing warns. *Workaround:* the norms are applied in Python.
2. **RES reweighting returns NaN under the hybrid model.** `calcRES`
   (`src/rew/rewRES.cc`) multiplies by `e.res_angrew`, which is set only in
   `src/dis/resevent2.cc` (`res_kind ≠ 2`). Under the default hybrid model it
   holds uninitialized memory (−2.35×10⁻¹⁸⁵ in every event of a 20k C12 sample).
   The nominal is ≈0 and the ratio NaN, which `reweight_to` hides as 0 (issue 5):
   all 8400 RES events got weight 0 when reweighted to their own central values.
   *Workaround:* RES parameters are rejected.
3. **`qel_cc_vector_mass` and `SPPBkgScale` are listed as reweightable but never
   read** anywhere in `src/`.
4. **Some reweight parameters carry over between events.** `RewParams::init`
   (`rewparams.h`) resets only nine parameters from `e->par`. The bba07, zexp
   and 2comp/3comp parameters keep the value `set()` gave them at event 0, so
   from event 1 on the nominal is already computed at the new value and the
   weight is ≈1. Separately, `reweight_along` never calls `ff_configure` after
   `setTwk`, so form-factor parameters held in `ff.cc` globals do not respond
   there. *Workaround:* rejected.
5. **`reweight_to` silently maps a NaN weight to 0** (`if (weight != weight)
   weight = 0;`), making a failed evaluation look like a physical zero.
   `nf_reweight` does not do this.
6. **`save_test_events` writes to `"weighted." + <-o value>`** (`src/nuwro.cc`,
   `test_events`), prefixing the whole path, so an absolute `-o` points into a
   non-existent directory. Mode 1 also has `finishevent` commented out, so its
   weighted events have no FSI. Not used by the framework.
7. **Mode 2 weighted events carry a running normalization.** Each saved weight
   is divided by the channel ratio `_procesy.ratio(k)` as estimated at that
   point in the run, then multiplied by `saved/(i+1)`, so early events are
   normalized with a less converged estimate than late ones. Not used by the
   framework.
8. **Buffer overflows**: the long-path abort and the `numubar` × W184 abort under
   [Known limitations](#known-limitations).

## Open questions

- **The QE vertex is not fully explained by the binding energy.** The `e/in`-based
  W has a 5% tail beyond 100 MeV (99th percentile 227 MeV, 99.9th 458 MeV), far
  larger than any binding energy, and equally present with and without an SRC
  partner (4.9% and 5.4%). The missing three-momentum is not a pure rescale along
  the outgoing nucleon's direction, which is what the paper's "exit from the
  nuclear potential" step would produce. And the `e/in`-based W depends on the
  struck nucleon's momentum by only ~3 MeV across 0–300 MeV, much weaker than
  `_E_bind`'s own ~30 MeV variation, implying most of `_E_bind` is already in the
  (off-shell, median mass 0.8965) nucleon `e/in` reports.
