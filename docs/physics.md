# Physics conventions

The physics contract every generator backend is normalized onto. Anything that
reads, writes or compares the common HDF5 output depends on the conventions
below. How each generator's raw output is turned into these quantities is in
that generator's doc under [generators/](generators/); this document only
summarizes it, one table cell per generator.

The topics come first. [Design notes](#design-notes) explains why some
conventions are the way they are, and [Validation](#validation) collects the
measurements that back them.

## Units and frames

| Quantity | Unit | Where |
|---|---|---|
| Neutrino / lepton energies and momenta | GeV | `common_output.py`, `kinematics.py` |
| `q2_gev2` | GeV² | `kinematics.py` |
| `xsec_weight` | 1e-38 cm² per target **nucleon** | `translators/base.py` |
| Flux density | arbitrary units per GeV | `flux.py` |
| `bjorken_x`, `inelasticity_y`, `lepton_costheta` | dimensionless | `kinematics.py` |

All kinematics are **lab frame**. Generators that work in other units convert at
the normalizer boundary, so nothing downstream of `normalizers/` ever sees MeV
(NuWro's `treeout` and NEUT's `NeutPart` four-momenta are both MeV natively).

## The cross-section weight

`xsec_weight` is the single column that carries physics normalization. Its
contract is stated once, on `ConfigTranslator.compute_xsec_weight`
(`translators/base.py:28-37`):

> Histogram events by energy, weight by `xsec_weight`, divide by the bin width →
> the average differential cross section in that bin, in 1e-38 cm² per target
> nucleon.

Every implementation has the shape

```
xsec_weight_i = numerator_i / (D · phi_hat(E_i))
```

where `phi_hat` is the unit-integral-normalized flux density and `D` is the
normalization denominator, recorded per chunk as HDF5 metadata
`xsec_norm_count`. What varies between generators is the numerator and `D`:

| Generator | Sampling | Numerator | `D` |
|---|---|---|---|
| GENIE | unweighted | flux-averaged σ, reconstructed from the staged splines ([genie.md](generators/genie.md#cross-section-weight)) | event count |
| NuWro | unweighted | the run-wide constant raw `weight` ([nuwro.md](generators/nuwro.md#cross-section-weight)) | event count |
| NEUT | unweighted | ∫evtrt / ∫flux from NEUT's own output ([neut.md](generators/neut.md#cross-section-weight)) | event count |
| GiBUU | phase-space sampled, cross-section weighted | the raw per-event `weight` ([gibuu.md](generators/gibuu.md#cross-section-weight)) | number of GiBUU **runs** |

For the three unweighted generators the event *density* in energy encodes σ(E).
For GiBUU the event density carries no normalization; the weights do, and they
can be negative.

### Per-nucleon, always

The column is per target nucleon regardless of the nucleus. Generators differ in
what they hand over:

| Generator | Raw scale | Per-nucleon conversion |
|---|---|---|
| GENIE | natural units, whole-nucleus splines | `× 1e38 / GENIE_UNITS_CM2`, then **÷ A** |
| NuWro | bare cm², already per-nucleon | `× 1e38`, **no** `/A` |
| NEUT | already 1e-38 cm² per nucleon | none |
| GiBUU | already 1e-38 cm² per nucleon | none |

GENIE's splines are keyed on the whole-nucleus PDG code, which is why it is the
one generator that divides. The evidence for each row is in that generator's
Validation section.

### Merging chunks averages, it does not sum

Every generator's `xsec_weight` column is a *per-chunk* estimate of the cross
section: for one chunk on its own, `sum(xsec_weight in an energy bin) /
bin_width` converges to σ(E). Concatenating N chunks naively would report N
times the true cross section. This applies to **all four generators**,
GiBUU included: its weights sum to σ *within one run*, so two chunks are two
independent estimates.

`merge_hdf5_files` therefore scales chunk *c* by `D_c / Σ D`, turning
concatenation into a weighted average. The merged weights are what a single run
of `Σ D` events would have produced.

- A single input is a no-op (share = 1).
- The merged file records the summed `Σ D`, so merging merged files stays correct.
- Stub output declares no count and is left untouched (its weights are
  placeholders). A set mixing declared with undeclared inputs is refused.
- Universe and variation weights are ratios; merging concatenates them and never
  rescales them. Their inputs must come from the same draw: the metadata must
  match exactly, except the parameter `values`, which may differ by a relative
  1e-12 (`exp` of a log throw can differ in the last bit between CPUs).

Validated on NEUT, GiBUU and NuWro: see [Validation](#chunk-merging).

## Flux

A flux is a callable returning a number proportional to the flux **density**
versus energy in GeV, in arbitrary units (`flux.py:1-18`). Each concrete flux
knows its support `[emin_gev, emax_gev]` and the probe it describes;
`to_histogram` produces the binned form handed to generators and used to draw
synthetic energies in stub mode. `PowerLawFlux` takes `gamma` directly from the
config, so an E^-2 spectrum is `gamma: -2.0`.

Three facts govern every generator's flux handling:

- **Density versus per-bin integral.** ROOT-based samplers (`TH1::GetRandom`
  and NEUT's `Ufm2TH1dist`) pick a bin in proportion to its raw bin *content*,
  ignoring the bin width. Feeding a density on an unequal-width grid generates a
  spectrum tilted by one power of the bin width. Each adapter either hands over
  per-bin integrals or tells the generator to multiply by the width.
  `HistogramFlux.from_root_file(..., contents_are_counts=True)` is the inverse
  conversion on readback.
- **The generated flux is piecewise constant on the input bin edges.** Energy is
  uniform *within* a bin for every histogram-driven generator here, so the
  reconstructed σ(E) can carry no structure finer than the flux binning. The
  number of flux bins is a **resolution setting**. Log-spaced bins give constant
  relative resolution, the sensible choice for a steeply falling spectrum.
- **Normalize against what was actually sampled.** The flux in the
  `xsec_weight` denominator must be the histogram the generator drew from, on
  its own binning. GENIE and NEUT stamp that histogram into their output, and
  their normalizers read it back with no fallback to the config. Rebuilding
  from the config would silently restore a normalization bug whose output still
  looks physical. NuWro does not stamp its flux; its normalizer rebuilds the
  histogram from the config with the same call the translator used, which is
  identical by construction.

| Generator | Bins handed over | Normalized against |
|---|---|---|
| GENIE | 1000 log bins of density, `WIDTH` flag ([genie.md](generators/genie.md#flux)) | `input-flux.root`, written by `gevgen` |
| NuWro | 500 equal-width bins inline in `beam_energy` ([nuwro.md](generators/nuwro.md#flux)) | the same histogram, rebuilt from config |
| NEUT | 1000 log bins of per-bin integrals ([neut.md](generators/neut.md#flux)) | `flux_<flavour>` from NEUT's output |
| GiBUU | 500 equidistant bin centres in a text table ([gibuu.md](generators/gibuu.md#flux)) | the unit-normalized config flux |

## Probes and targets

`flux.particle` accepts the six neutrino probes: `nue`, `numu`, `nutau` and
their antineutrinos. `particles.py` is the single table mapping them to PDG
codes. Every translator derives its generator-native beam setting from it
(GENIE's numeric `-p`, NuWro's `beam_particle`, NEUT's `EVCT-IDPT`, GiBUU's
`flavor_ID` plus the sign of `process_ID`), so a flavour is supported by all four
generators or by none. An unknown name fails at `validate-config`. Whether a
generator also has *cross sections* for a flavour is a separate question: GENIE's
shipped splines, for example, have no ν_τ ([genie.md](generators/genie.md#known-limitations)).

The antineutrino sign is always taken from the PDG code
(`particles.is_antineutrino`), never from the probe's name.

Targets are parsed from names (`C12`, `Ar40`) into `Z`, `A` and the PDG
`10LZZZAAAI` nuclear code by `nucleus_z_a` / `nucleus_pdg` /
`nucleus_composition` (`particles.py:77-117`). There is no nuclide table, so any
element-plus-mass-number name resolves.

## Weak current

`physics.current` (`cc` | `nc` | `inclusive`) is applied through each generator's
**own** configuration rather than by filtering the normalized output. Filtering
afterwards would waste the discarded events and leave `xsec_weight` normalized to
the inclusive cross section while the sample covered only one current.

| Generator | Native mechanism | Details |
|---|---|---|
| GENIE | `--event-generator-list CC`/`NC`; the spline sum is filtered to match | [genie.md](generators/genie.md#weak-current) |
| NuWro | the `dyn_*` channel switches | [nuwro.md](generators/nuwro.md#weak-current) |
| NEUT | `NEUT-MODE -1` plus a `NEUT-CRS` slot mask | [neut.md](generators/neut.md#weak-current) |
| GiBUU | `process_ID`; `inclusive` is two concatenated passes | [gibuu.md](generators/gibuu.md#weak-current) |

[configuration.md](configuration.md#weak-current-physicscurrent) is the user-facing
reference for the setting.

**`is_cc` is a required column with no default.** The `interaction` label cannot
stand in for it: `qel` covers CCQE and NC elastic alike, so an inclusive run
without this column is unusable for anything current-specific. A default would be
indistinguishable from a measured value, so `write_common_hdf5` raises instead.

Validated for all four generators: see [Validation](#weak-current-1).

## Interaction taxonomy

The common `interaction` column takes one of `qel`, `res`, `dis`, `coh`, `mec`,
`other`. GENIE's `gst` and NuWro's `treeout` expose boolean branches with these
very names and share one priority chain,
`normalizers/base.interaction_from_flags` (qel → res → dis → coh → mec → other).
GiBUU maps an integer `evType` and NEUT a mode number; both use explicit, closed
tables that raise on an unknown code rather than falling through to `other`.
The column is stored as an HDF5 enum with fixed codes
(`common_output.INTERACTION_CODES`), and the writer rejects any other label.

**The categories are pinned to GENIE**: an event gets the category it would have
had if GENIE had produced it. `qel` deliberately covers NC elastic as well as
CCQE, which is why `is_cc` cannot be inferred from `interaction`.

### The shallow-inelastic region

**The region roughly 1.2 < W < 2.0 GeV is the one place the four generators
genuinely disagree.** Each decomposes it differently, and none exposes a `sis`
category:

| Generator | How the region is produced | Our label |
|---|---|---|
| GENIE | RES generator (full below the tune's `Wcut`, zero above) **plus** the DIS generator supplying non-resonant background, which extends *below* `Wcut` too | `res` / `dis` respectively ([genie.md](generators/genie.md#the-resdis-boundary-is-the-tunes-wcut)) |
| GiBUU | non-resonant 1π/2π background, `evType` 32/33/37, resonances subtracted | `dis` ([gibuu.md](generators/gibuu.md#the-non-resonant-background-is-dis)) |
| NEUT | multi-π modes 21/41 (1.3 < W < 2.0); single-π modes 11/12/13 lump resonant *and* non-resonant together | `dis` / `res` |
| NuWro | `dyn_res` / `dyn_dis`, disjoint at a hard `res_dis_cut` = 1.9 GeV; but the RES channel blends non-resonant background into *itself* from W = 1.08 GeV upward | `res` / `dis` ([nuwro.md](generators/nuwro.md#the-resdis-split-is-a-hard-cut-plus-a-blend-inside-res)) |

Labelling GiBUU's background `dis` reproduces what GENIE would have called the
same event, since GENIE's non-resonant background comes out of its DIS generator.
NEUT is the one that cannot be aligned by relabelling: its single-pion modes do
not separate the resonant and non-resonant pieces at all. NuWro's `res` is
likewise not purely resonant near the boundary. The
[`resonant_primary`](#the-resonant_primary-column) column says which mechanism
made each event where the generator can tell.

A future `sis` label would have to be introduced for all four generators at once,
and for GENIE it is not recoverable from the gst output.

**This is why the common output carries W.** The `w_gev` and `w_true_gev` columns
(see [derived kinematic variables](#derived-kinematic-variables)) are computed by
one formula from four-vectors every generator supplies, so a cut on them is a cut
on the same physical quantity in all four, which no relabelling of the channel
flags can achieve. `w_true_gev` is the one to compare against a generator's own
threshold; `w_gev` is the one to compare *between* generators.

### The NEUT code as a candidate cross-generator axis

NEUT's mode numbering is the closest thing the field has to a lingua franca for
fine-grained channels, but **only two of the four generators emit one**:

| Generator | NEUT code | Source |
|---|---|---|
| NEUT | native: `mode` **is** the NEUT code | generated channel |
| GENIE | native: gst `neut_code` | `genie::utils::ghep::NeutReactionCode`, counts primary hadrons |
| NuWro | **none**; only `e/dyn` plus the `flag.*` booleans | derivable: NUISANCE's `ConvertNuwroMode` counts `e/out` |
| GiBUU | **none**; only `evType` | derivable: NUISANCE's `ConvertModeGiBUUtoNEUT`, a code→code switch |

Two properties to be clear about before treating it as a topology label:

- **It classifies the primary hadronic system, not the observable final state.**
  GENIE counts particles "in the primary hadronic system (_before_ intranuclear
  rescattering)"; NUISANCE's NuWro converter counts the pre-FSI `e/out`; NEUT's
  `mode` is assigned at production. So it is a *pre-FSI topological*
  classification: generator-agnostic and finer than "which module produced the
  event", but not an observable.
- **The derived mappings are lossy, and the GiBUU one is partly invalid.**
  `ConvertModeGiBUUtoNEUT` collapses all 30 resonance codes (2–31) onto
  single-pion modes regardless of how the resonance decayed, returns **10** (CC)
  and **30** (NC) for the 1π background (neither is a NEUT mode), and maps NC
  2p2h to **42**, which in NEUT means NC 1η.

It also disagrees substantially with our labels where the generator's own
module and the produced topology differ. In GENIE, a few percent of `res` events
carry multi-π codes and a few percent of `dis` events carry single-π codes
([genie.md](generators/genie.md#neut_code-against-our-labels)). Neither labelling
is wrong: one is *which generator produced it*, the other *what came out*. Only
the second is commensurable with NEUT.

The common output carries each generator's own code verbatim in
[`native_interaction_code`](#native_interaction_code-is-the-one-non-universal-column):
the NEUT code itself for NEUT and GENIE, but `e/dyn` and `evType` for NuWro and
GiBUU, so it is not yet a cross-generator axis.

### The `resonant_primary` column

`interaction` says which channel the generator assigned an event.
`resonant_primary` says how the primary hadronic system was actually *made*:
`1` resonant, `0` non-resonant, `-1` unknown or not applicable
(`common_output.RESONANT_PRIMARY_*`).

| Generator | Source | Agrees with `interaction`? |
|---|---|---|
| GENIE | the channel itself: `res` is the Rein–Sehgal/Berger–Sehgal calculation, `dis` the DIS generator that supplies the non-resonant background | yes, by construction |
| GiBUU | `evType`: 2–31 are resonances, 32/33 (1π background), 34 (DIS) and 37 (2π background) are not | yes, by construction |
| NuWro | `e/flag/flag.res_delta` ([nuwro.md](generators/nuwro.md#resonant_primary-comes-from-flagres_delta)) | **no**: see below |
| NEUT | none; modes 11/12/13 lump the two mechanisms and no flag separates them | `-1` for every event |

**NuWro is why this is a column rather than a footnote.** Its RES channel blends
non-resonant background into itself, so a large part of what it labels `res` is
what GENIE would have labelled `dis`. On a 20k-event numu CC C12 run, **10.0% of
NuWro's `res` events, but 38.6% of its `res` cross section**, carry
`resonant_primary = 0`. Re-bucketing those into `dis` would move NuWro from
`res` 46.5% / `dis` 23.2% of the total cross section to 28.5% / 41.1%, inside the
range the other generators span (GENIE 37.2/38.0, NEUT 34.1/41.1, GiBUU
26.1/46.7).

That re-bucketing is deliberately **not** applied to `interaction` (see
[Design notes](#resonant_primary-instead-of-relabelling)). Downstream code that
wants the mechanism should cut on `resonant_primary` directly, and treat `-1` as
"this generator cannot say".

**`resonant_primary` is only meaningful for `res` and `dis`.** Quasi-elastic,
coherent and 2p2h have no pion-production mechanism to attribute, so they carry
`-1` everywhere.

## Derived kinematic variables

Ten lab-frame columns are derived in one shared module, `kinematics.py`, by the
same formula for every generator: `q2_gev2`, `bjorken_x`, `w_gev`, `w_true_gev`,
`inelasticity_y`, `lepton_energy_gev`, `lepton_momentum_gev`,
`lepton_p_parallel_gev`, `lepton_p_transverse_gev`, `lepton_costheta`. Native
precomputed branches (such as GENIE's `Q2`/`x`/`y`) are not used; see
[Design notes](#kinematics-are-recomputed-not-read).

### The two hadronic-mass columns

W gets **two** columns, because the two useful definitions genuinely differ and
both are wanted. See [the interaction taxonomy](#interaction-taxonomy) for why W
is carried at all.

| Column | Definition | Needs |
|---|---|---|
| `w_gev` | `W² = M_N² + 2 M_N ν − Q²`, with the fixed isoscalar `NUCLEON_MASS_GEV` and the nucleon taken at rest | the two lepton four-vectors only |
| `w_true_gev` | `W² = (p_ν + p_N − p_l)²`, against the per-event struck system | the struck system `p_N`, which each generator exposes differently (table below) |

**`w_gev` is the observable, experimental W.** It is frame-dependent and smeared
relative to the true one, because Fermi motion and binding are not in the
formula. It is identically defined for every generator, including any that never
exposes a hit nucleon. Use it to compare *between* generators.

**`w_true_gev` is the invariant mass the generators themselves cut on**:
GENIE's `Wcut`, NEUT's 1.3 < W < 2.0 multi-π window, NuWro's `res_dis_cut`. It
is a real Lorentz invariant. Because it is tied to each generator's off-shell and
binding treatment, it is not comparable across generators at better than ~5%
(see [how each QE vertex closes](#how-each-generators-qe-vertex-closes)).

**`p_N` is the struck initial-state hadronic *system*, not a single nucleon.**
For 2p2h it is the correlated pair. That choice is forced by GENIE, whose
struck-nucleon branches carry the two-nucleon cluster for MEC events. Two
consequences:

- **MEC `w_true_gev` is a *pair* mass** and must not be read on the same axis as
  the other channels. It sits above 2 `M_N` (measured minima 1.88–1.93 GeV),
  roughly one nucleon mass above the quasi-elastic peak, while MEC `w_gev` sits
  below it. For `qel`, `res` and `dis` the median gap `w_true_gev − w_gev` is at
  most 0.07 GeV, genuine Fermi/binding smearing.
- **GiBUU cannot supply the pair**, so its 2p2h events carry the `w_true_gev`
  placeholder ([gibuu.md](generators/gibuu.md#w_true_gev-is-blank-for-2p2h)).
  `w_gev` is unaffected.

Where each generator's `p_N` comes from:

| Generator | Source | Absent when |
|---|---|---|
| GENIE | gst `En/pxn/pyn/pzn`, flagged by `hitnuc` | `hitnuc == 0`, which is exactly the coherent set |
| NuWro | inferred from the pre-FSI outgoing hadrons, `Σ e/out[1:] − p_ν + p_l`, because `e/in` stores the nucleon before binding ([nuwro.md](generators/nuwro.md#w_true_gev-is-nuwros-own-w)) | `e/in` holds no nucleon (ν-e); coherent events are blank anyway |
| NEUT | summed by `nf_flatten.C` into `nuc_*_gev`, with `n_nuc` | `n_nuc == 0` |
| GiBUU | `nuc_E/nuc_Px/nuc_Py/nuc_Pz`, the *bound* nucleon (`storeNucleon = 2`) | **2p2h** (`evType` 35/36): only one of the pair is written |

The ROOT-based generators write **zeros, not a sentinel**, when there is no
struck nucleon. A zero four-vector is finite, so each normalizer passes an
explicit validity mask instead of relying on a finiteness check.

### How each generator's QE vertex closes

For a quasi-elastic event nothing is produced but the lepton and one nucleon, so
`p_ν + p_N(initial) − p_l = p_N(final)`. An escaping nucleon is on shell, so if
the reported `p_N` is the one the vertex used, `w_true_gev` for `qel` is a delta
function at `m_p` or `m_n`. How close each generator comes is a direct measure
of how its stored nucleon relates to the one its vertex used (medians, 20k-event
numu CC C12 runs):

| Generator | stored initial `m_N`, `qel` | stored initial `m_N`, all channels | `qel` `w_true_gev` | offset |
|---|---|---|---|---|
| GENIE | 0.9006 (off shell) | 0.9045 | 0.9383 | exact (1st and 99th percentiles both 0.9383) |
| NuWro | 0.8965 (off shell) | — | 0.9383 | exact by construction since `p_N` is inferred from the outgoing hadrons; from `e/in` it would be +1 MeV with a 0.78–1.12 spread |
| NEUT | 0.9396 (**on shell**, = `m_n`) | 0.9396 | 0.9922 | +53 MeV, 1–99% spread 0.96–1.05 |
| GiBUU | 0.8878 (bound, potential included) | 0.8895 | 0.8963 | −43 MeV, 0.72–1.05 |

The two `m_N` columns come from different validation passes (the `qel`
medians from the vertex-closure study, the all-channel medians from the
2026-08-05 W validation, whose GiBUU run had 16.6k events).

GENIE reports the off-shell nucleon that makes its own vertex exactly two-body.
NuWro's offset has been traced to a local-density-dependent binding energy
([nuwro.md](generators/nuwro.md#binding-energy-at-the-vertex)). NEUT's and
GiBUU's binding treatments have not been read; see
[Open questions](#open-questions).

The practical consequence: **`w_true_gev` reproduces a generator's own W exactly
only where `p_N` is the nucleon the generator solved the vertex against**, which
holds for GENIE and (by inference from the hadrons) NuWro. For NEUT and GiBUU it
is displaced by tens of MeV.

### Conventions

- The beam axis is taken **per event** from the incoming neutrino three-momentum,
  never assumed to be +z, so the parallel/transverse split and `cos θ` stay
  correct for off-axis or divergent beams.
- Bjorken-x and `w_gev` use a **fixed** isoscalar nucleon mass `NUCLEON_MASS_GEV`
  ((m_p + m_n)/2 = 0.9389187543 GeV), not the per-event struck-nucleon mass, so
  these two variables are identically defined everywhere.
- `Q² = -(p_ν - p_l)²` is clamped at 0, because it is non-negative up to
  floating-point noise near forward scattering.
- **Negative `W²` is blanked, not clamped.** It is a real high-Q², low-ν corner
  (`w_gev`) or a real off-shell threshold effect (`w_true_gev`). Clamping would
  pile those events at exactly 0, indistinguishable from a measured value.
- `inelasticity_y` can come out slightly **negative** when Fermi motion pushes
  the outgoing lepton above the beam energy. That is physical and passed through.
- The lepton variables are filled for NC as well as CC events; for NC the
  "lepton" is the scattered neutrino.

**Placeholders.** A blank value is NaN (`kinematics.MISSING`). NaN never lands
in a histogram bin and turns any sum it enters into NaN, so a forgotten mask
shows up instead of quietly biasing a result. Files written before
2026-10 used `-1.0` for the non-negative-definite columns and `-999.0` for
`lepton_p_parallel_gev` and `lepton_costheta` (a backward-scattered lepton
genuinely has `cos θ = -1`). Such files lack the `missing_float = "nan"`
metadata attribute, and `common_output` maps their placeholders to NaN on read
(see the [common output format](../README.md#common-output-format)). Blanking rules:

| Column(s) | Blank when |
|---|---|
| `bjorken_x`, `w_gev`, `w_true_gev` | coherent events (no struck nucleon to define them against) |
| `bjorken_x` | additionally, energy transfer ν ≤ 0 (it divides by ν; the W columns do not, so a slightly sub-nucleon W is kept) |
| `w_true_gev` | additionally, the generator supplied no struck system |
| `w_gev`, `w_true_gev` | `W² < 0` |
| `lepton_costheta` | lepton momentum is zero |
| directional variables | beam momentum is zero |
| the whole block | no outgoing lepton (NuWro's `e/out` can be empty) or non-finite four-vectors |

## Final-state content

Fifteen columns summarize the particles that leave the nucleus, derived by one
shared function, `final_state.summarize_final_state`, from each generator's
post-FSI particle list:

- counts: `n_proton`, `n_neutron`, `n_pi_plus`, `n_pi_minus`, `n_pi_zero`,
  `n_other_mesons`;
- energy sums: `hadronic_energy_gev` (Σ E), `hadronic_kinetic_energy_gev`
  (Σ (E − m));
- the leading charged pion: `leading_pi_charged_kinetic_energy_gev`,
  `leading_pi_charged_costheta`;
- the leading π⁰: `leading_pi_zero_kinetic_energy_gev`, `leading_pi_zero_costheta`;
- the leading proton: `leading_proton_kinetic_energy_gev`,
  `leading_proton_costheta`, `leading_proton_lepton_costheta`.

A sixteenth, `native_interaction_code`, carries the generator's own channel code.

**Summaries, not particle lists.** The common format is strictly rectangular, one
1-D dataset per column (see `common_output.py`). Multiplicities, energy sums and
leading particles are what generator-level signal definitions are made of. They
suffice, for example, for MINERvA's CC1π⁺ (Granados 2026, arXiv:2605.24224, §5),
MicroBooNE's νe CC0π with its 1eNp0π / 1e0p0π split (arXiv:2603.13593, §3.2), and
NOvA's ν̄_μ CCπ⁰ (arXiv:2511.05807, §2, §3.4), where W_exp is `w_gev`.
Comparing to MicroBooNE's Wiener-SVD unfolded results additionally takes the
paper's published A_c regularization matrices, applied to the binned prediction.

### The particle list is post-FSI, and FSI is on in every generator

| Generator | List read | FSI switch |
|---|---|---|
| GENIE | `gst` `pdgf`/`Ef`/`pxf`/`pyf`/`pzf` (the `f` family; `i` is pre-FSI) | the tune's own hadron-transport setting; not overridden ([genie.md](generators/genie.md#tunes-config_version)) |
| NuWro | `e/post` ("particles leaving the nucleus"), not `e/out` | `FSI_on = 1`, pinned ([nuwro.md](generators/nuwro.md#run-stage-and-paramstxt)) |
| NEUT | the `NeutVect` particles with `fIsAlive && fStatus == 0` | `NEUT-NEFF 0`, `NUCRES-RESCAT 1`, pinned ([neut.md](generators/neut.md#flattening-nf_flattenc)) |
| GiBUU | the perturbative particles after transport, minus nucleons still bound in the nucleus | 150 × 0.2 fm of transport ([gibuu.md](generators/gibuu.md#fsi-transport)) |

GiBUU's list is a snapshot at the end of transport rather than a list of escaped
particles. Its normalizer drops nucleons that are still bound and puts the rest
on shell ([gibuu.md](generators/gibuu.md#the-particle-list-is-a-snapshot)). The
other three generators' final-state nucleons and pions are on shell at their
vacuum masses.

### Conventions

All enforced in `summarize_final_state`, so a pion count from GENIE means what
one from NuWro means:

* **Counts are by exact, signed PDG code**: π⁺ and π⁻ are separate columns, and
  `n_proton` does not count antiprotons.
* **`n_other_mesons` is every meson except π⁺, π⁻ and π⁰**, of either sign, by the
  PDG numbering scheme's meson digits (`final_state.is_meson`: first quark digit
  0, the other two nonzero). That takes in K±, K⁰/K̄⁰, K⁰_L (130), K⁰_S, η, η′,
  ω, charmed mesons and excited states, and no baryon, lepton, photon or nucleus.
* **The leading charged pion, the leading π⁰ and the leading proton are each
  the highest-kinetic-energy particle of their species.** Charged and neutral
  pions lead separately because selections treat them separately: in 11–26% of
  π⁰ events (by generator) a charged pion carries more kinetic energy than the
  leading π⁰. All `*_costheta` columns are measured against the event's own
  incoming-neutrino three-momentum, the same axis as `lepton_costheta`;
  `leading_proton_lepton_costheta` against the outgoing lepton's. It cannot be
  rebuilt from the two beam angles, which lack the azimuth between the proton
  and the lepton.
* **"Hadronic" means non-leptonic**: every particle except `|pdg|` in 11..16.
  Photons and kaons therefore count toward the energy sums, a slight abuse of
  the name, chosen over silently dropping species that carry energy out of the
  interaction.
* **Nuclear remnants are excluded** (`|pdg| > 1e9`, the `10LZZZAAAI` ion codes).
  GENIE's list carries the residual nucleus, whose rest mass (~37 GeV for argon)
  would otherwise dominate `hadronic_energy_gev`.
* **Mass from the four-vector**, `m = sqrt(E² − |p|²)`, never a lookup table, so
  the summary cannot disagree with the generator about what it produced. The one
  exception is GiBUU's in-medium nucleons, put on shell at GiBUU's own `m_N`.
* `hadronic_kinetic_energy_gev` is **not** the energy transfer ν. It runs ~85% of
  ν on average and exceeds it for a minority of events, because FSI-ejected
  nucleons carry Fermi motion that did not come from the neutrino. Nor is it
  GENIE's `sumKEf`.

**Placeholders.** The counts are integers and default to `-1`; every float
column (energies, leading-particle kinetic energies and cosines) defaults to NaN.
An event with nothing hadronic out is a real zero. An event without a pion (or
proton) has no leading one, so its leading-particle columns stay NaN, as does
the angle of a particle at rest, or of an event without a lepton momentum. Stub
mode leaves every column at its placeholder. Older files used `-1.0` for the
energies and `-999.0` for the cosines; they are mapped to NaN on read.

### `native_interaction_code` is the one non-universal column

Its meaning depends on `generator`. It exists so the exact channel split can be
recovered without the raw files, and nothing in the framework interprets it.

| Generator | Source | Notes |
| --- | --- | --- |
| `genie` | `gst` branch `neut_code` | GENIE has no native integer of its own in `gst`; `gntpc` re-encodes its scattering type into NEUT's mode scheme |
| `neut` | `mode` | signed: negative for antineutrino channels |
| `gibuu` | `evType` | 1 = QE, 2..31 resonances, 32/33/37 background, 34 = DIS, 35/36 = 2p2h |
| `nuwro` | `e/dyn` | distinguishes the CC and NC variant of each dynamics |

Its placeholder is `-2**31`: not `0`, because NuWro's `dyn = 0` is CC
quasi-elastic, and not a small negative number, because NEUT negates its mode
for antineutrinos.

## Weighted statistics and weight efficiency

`analyze-kinematics` (`kinematics_report.py`) reports every mean and median
**weighted by `xsec_weight`**, never raw. Unweighted event distributions are not
the physical ones for a cross-section-weighted generator, and the failure is
silent: a 100k-event GiBUU run reports a healthy-looking quasi-elastic sample
whose effective size is 35 events.

It therefore prints a weight-efficiency table alongside: Kish
`n_eff = (Σw)² / Σw²` per interaction channel, plus the share of weight in the
heaviest 1% of events. The efficiency is computed on `|w|` so GiBUU's negative
interference weights cannot cancel into a meaningless ratio, while `Σw` is
reported signed. **Negative weights are physical and must be summed as they
are.** For GiBUU channel statistics, quote bootstrap intervals rather than point
estimates, and check the efficiency table before trusting a number.

Placeholders are excluded per variable and counted, so `bjorken_x` for a coherent
selection reports "0 used, 127 blank" rather than silently averaging in `-1`.
The weighted quantile uses the **midpoint convention**: cumulative weight
evaluated at the centre of each point's weight, not its upper edge. The naive
cumulative sum biases the median low by half a bin.

## Reweight universes

A **universe** is one setting of a generator's physics parameters, drawn from
Gaussian priors, independent unless a correlation group ties some of them
together. For an event *i* and universe *k*,
`events/universe_weights[i, k]` is the ratio of the differential cross section
under universe *k* to the one under the generation's own parameters (the
central values), both evaluated at the event's stored kinematics. A universe's
cross section in a bin is therefore `Σ xsec_weight · universe_weights[:, k]` over
the bin. The weights are ratios, so merging chunks concatenates them and never
averages or rescales them.

NuWro and GENIE support them; NEUT and GiBUU do not. The configuration schema is
in [configuration.md](configuration.md#reweight-universes-and-variations). What
each generator can reweight is listed in
[generators/nuwro.md](generators/nuwro.md#reweight-universes) and
[generators/genie.md](generators/genie.md#reweight-universes-and-variations).
The sampling below is the same for both; only the parameter tables and the
binary that computes the ratios differ.

**Sampling.** Each parameter has `z ~ N(0, 1)`, drawn from its own stream seeded
by `(universes.seed, crc32(name))`. Adding a parameter therefore leaves the
others' throws unchanged, and a larger `count` extends the existing universes
instead of redrawing them. The seed is explicit, never derived from the run seed
or the job label, so universe *k* means the same parameter values in every job
that uses it. That shared meaning is what makes bin-to-bin correlations
*between* jobs (targets, flavours, fluxes) estimable. The value depends on the
parameter's flag:

| Mode | Value in universe *k* | What `sigma` is |
|---|---|---|
| linear (default) | `central + sigma·z` | Absolute error, in the units of `central` |
| `log: true` | `central · exp(sigma·z)` | Standard deviation of `ln(value)`; for small sigma about the fractional error (0.15 → roughly ±15 %) |

A linear throw at or below zero for a physically positive parameter is an
error, not clipped. In log mode the median is `central`, every value is
positive, and `ln(value/central) = sigma·z` exactly, so a covariance for such a
parameter can be formed in log space, which avoids Peelle's Pertinent Puzzle.

**Correlated parameters.** A block may list correlation groups, each a set of
its parameters with a correlation matrix ρ. The group's independent throws are
mixed by the Cholesky factor `L` of ρ (`ρ = L Lᵀ`): `z_group ← L · z_group`.
The mixed throws keep unit variance and have correlation ρ, and each parameter's
mapping above then applies unchanged. Linear parameters are therefore correlated
in their values, log parameters in `ln(value)`. `L` is lower triangular with
`L₀₀ = 1`, so the group's first parameter keeps its own stream; the others now
depend on every earlier member. Parameters outside any group are unaffected, so
adding a group leaves their throws as they were.

The matrix is the correlation *between the parameters as thrown*. Where a fit
publishes the correlation of quantities that a scale multiplies, a negative
central value flips the sign: for a scale `s` on a coefficient `a` (`a' = a·s`),
`corr(s_i, s_j) = sign(a_i a_j) · corr(a_i, a_j)`. GENIE's z-expansion dials
are the case in point
([generators/genie.md](generators/genie.md#z-expansion-dials)).

**What `central` means is per generator.**

| Generator | `central` | `sigma` |
|---|---|---|
| NuWro, physics parameters | the generation's own value, read from the output, in NuWro units | absolute in linear mode (MeV for an axial mass, not a fraction) |
| NuWro, channel norms | 1 | fractional |
| GENIE | 1: every dial is a scale on the generation tune's own value | fractional in either mode: linear gives `1 + sigma·z`, log gives `exp(sigma·z)` |

Either way the spread is exactly the configured `sigma`. Neither generator's
built-in uncertainty table enters the throws. Those tables are a starting point
for choosing `sigma`, listed per parameter in
[generators/genie.md](generators/genie.md#supported-dials) and
[generators/nuwro.md](generators/nuwro.md#supported-parameters).

The `universes` metadata stores everything needed to rebuild or extend the
throws: the seed, the count, each parameter's central value, sigma, log flag
and optional source, the `z` throws (after correlation), the values, and the
correlation groups if there are any.

**Variations.** Some knobs are not parameters with an uncertainty but switches
between two models, interpolated on [0, 1]. Examples are GENIE's `RPA_CCQE`
(RPA on → off) and `DecayAngMEC` (isotropic → 3 cos²θ). A Gaussian throw has no
meaning for them. Each is therefore stored as one deterministic column of
`events/variation_weights`: that knob at its configured value, everything else
nominal. The `variations` metadata gives the column order, each value, and what
0 and 1 mean. Such a column is a model alternative to compare against, or to
add as a separate one-sided systematic. It is never a universe, and folding it
into the universe covariance would treat a two-point model choice as if it
were Gaussian.

**What the spread means.**

- The covariance from `count` universes has rank at most `count − 1`, so it
  cannot be inverted for more bins than that.
- Its correlation coefficients carry a statistical precision of roughly
  `1/√count`.
- Every universe reweights the same events, so the universe covariance holds
  almost no MC statistical noise. The central sample's own statistical
  covariance, from `Σ xsec_weight²` per bin, must be added separately.
- The spread covers only the parameters that can be reweighted, so it is a lower
  bound on the generator's model uncertainty.

## Design notes

### `xsec_norm_count` has no default

`ConfigTranslator.xsec_norm_count` is abstract rather than defaulting to
`len(events)` so that the GiBUU case, where `D` is the number of runs, cannot be
got wrong silently (`translators/base.py:40-61`).

### The taxonomy is pinned to GENIE

Some reference is needed for labels to mean the same across generators. GENIE is
the reference because its gst flags already name the five categories, and NuWro
uses the same names. Keeping the coarse GENIE categories, even where another
generator can make a finer distinction (GiBUU's own NuHepMC exporter separates
SIS from DIS), keeps cross-generator comparability.

### `resonant_primary` instead of relabelling

Moving NuWro's non-resonant `res` events into `dis` would make one generator's
`interaction` column mean something different from the others', which is the
property the column exists to guarantee. It could not be done for NEUT at all, so
it would trade a visible inconsistency for a hidden partial one. The mechanism is
therefore a separate column. Dated entry:
[design_decisions.md](design_decisions.md#resonant_primary-as-a-column-rather-than-relabelling-nuwros-res-2026-08-05).

### Kinematics are recomputed, not read

GENIE's `gst` tree already precomputes `Q2`, `x`, `y` and `cthl`. They are
recomputed from the four-vectors for every generator anyway, because the promise
of the common format is that a Q² histogram from GENIE means exactly the same
thing as one from GiBUU, which only holds if one formula produces all of them.
GENIE also ships *two* variants (`Q2`/`x`/`y` and `Q2s`/`xs`/`ys`), so "use the
native branch" is not even unambiguous within one generator.
`tests/test_genie_normalizer.py` pins these definitions against GENIE's own
branches for a reference scatter.

### A fixed nucleon mass for `bjorken_x` and `w_gev`

All four generators expose the hit nucleon, but each with its own off-shell and
binding treatment. A fixed mass makes `bjorken_x` and `w_gev` identically defined
everywhere; `w_true_gev` is the column that carries the per-event system.

## Validation

Measurements backing the conventions above. Unless stated otherwise the samples
are numu CC on C12 with a γ = −2 power-law flux over 0.5–5 GeV.

### Chunk merging

Before the fix, a 2-chunk NEUT run reported 2.06× the cross section of its
correct single-chunk equivalent. After it (local Docker, same physics at
different chunk counts):

| Generator | Events | 1 chunk | 2 chunks | 4 chunks |
|---|---|---|---|---|
| NEUT (vs its own evtrt/flux per bin) | 4000 | 1.015 | 1.018 | 1.011 |
| GiBUU (relative to 1 chunk) | 40000 | 1.000 | 0.929 | 1.005 |
| NuWro (relative to 1 chunk) | 4000 | 1.000 | 0.956 | — |

The residual spread is Monte-Carlo noise. GiBUU needs the larger sample because
its weights are heavy-tailed: in a 1000-event run a single event carried 72% of
one chunk's total.

### Weak current

Local Docker, numu on C12; 400 events for GENIE, 2000 for the rest:

| Generator | CC fraction, `cc` run | `nc` run | `inclusive` run | NC share of σ, inclusive |
|---|---|---|---|---|
| GENIE | 1.000 | 0.000 | 0.750 | 0.204 |
| NuWro | 1.000 | 0.000 | 0.744 | 0.278 |
| NEUT | 1.000 | 0.000 | 0.735 | 0.261 |
| GiBUU | 1.000 | 0.000 | 0.449 | 0.308 |

GiBUU's inclusive *event* fraction is near half because it samples phase space;
its NC share of σ is the number to compare. The exact reference is GENIE's
spline decomposition: the staged numu/C12 splines filtered on `Weak[CC]` and
`Weak[NC]` give 1.03736 and 0.39137, an NC share of 0.274, and their sum
reproduces the unfiltered total to all six printed digits.

### Derived kinematics (2026-07-28)

20k-event GENIE, NuWro and NEUT runs plus an 83k-event GiBUU run pass the
structural invariants event by event: Q² ≥ 0, exact energy-transfer closure,
|cos θ| ≤ 1, p_∥² + p_T² = |p|², coherent events blanked and only those. The
cross-section-weighted distributions agree. Quasi-elastic Bjorken-x peaks at
0.75–0.85 (weighted medians 0.825 GENIE, 0.815 NuWro, 0.825 NEUT, 0.809 GiBUU
with bootstrap CI [0.797, 0.851]): a broad peak below 1, because Fermi motion and
binding smear it and the fixed `M_N` does not absorb that.

### The two W columns (2026-08-05)

20k-event GENIE, NuWro and NEUT runs plus a 16.6k-event GiBUU run; figures from
`scripts/plot_w_validation.py`.

- **Against GENIE's own branches.** `w_gev` reproduces gst `W` (the same
  lepton-only formula) to 5.1e-8 GeV over 19 862 events. `w_true_gev` reproduces
  gst `Ws`, GENIE's selected W, to 2.2e-13 GeV over the 18 251 non-MEC events.
  MEC events differ by design: `Ws` is not the pair mass. On MEC, gst `W` and
  `w_gev` both have median 1.161; gst `Ws` 2.184 and `w_true_gev` 2.183.
- **Blanking.** GENIE blanks `w_true_gev` for 138 of 20 000 events: the 136
  coherent events (exactly the `hitnuc == 0` set) and 2 genuine negative-`W²`
  events. NuWro blanks 131, NEUT 105, GiBUU none.
- **Thresholds the code was never told about.** NEUT modes 21/41: all 2770 events
  inside [1.301, 2.000] GeV, NEUT's own 1.3 < W < 2.0 window. GENIE `res` ends
  1.2e-4 GeV below the tune's `Wcut`
  ([genie.md](generators/genie.md#wcut-is-a-sharp-cut-on-w_true_gev)). NuWro
  `res`/`dis` split exactly at `res_dis_cut`
  ([nuwro.md](generators/nuwro.md#w_true_gev-reproduces-res_dis_cut)).
- **MEC is a pair mass.** 100% of MEC `w_true_gev` lies above 2 `M_N`, 0% of MEC
  `w_gev` does. MEC `w_true_gev` minima: 1.928 (GENIE), 1.908 (NEUT), 1.877
  (NuWro) GeV; medians 2.18–2.25 GeV. MEC `w_gev` medians 0.99–1.18 in all four.
- **NEUT's `n_nuc`** is 1 for 18 365 events, 2 for 1531 (all of them mode 2) and
  0 for 104, confirming the 2p2h layout the flattener assumes.

### Final-state content

**With FSI on (2026-10-05).** numu CC on Ar40, local Docker: 2000 events each
from GENIE (G18_10a_02_11b), NuWro and NEUT, and 3435 GiBUU events. FSI
demonstrably ran in each: GENIE's pre- and post-FSI lists differ in 70% of
events, NuWro's `e/post` differs from `e/out` in 47%, 1251 of 2000 NEUT events
carry FSI status codes, and GiBUU's particles per event rose from 1.87 to 4.57
with transport. The GENIE multiplicities agree *exactly*, event by event, with
`gst`'s own `nfp`, `nfn`, `nfpip`, `nfpim` and `nfpi0`. Per quasi-elastic event:

| | GENIE | NuWro | NEUT | GiBUU |
|---|---|---|---|---|
| 0π fraction | 0.981 | 0.969 | 0.978 | 0.990 |
| ⟨n_proton⟩ | 2.04 | 1.35 | 1.52 | 1.03 |
| ⟨n_neutron⟩ | 1.43 | 0.37 | 0.80 | 0.45 |
| no nucleon out | 0.3% | 2.2% | 0% | 9.1% |

The spread is model physics: GENIE's hA cascade knocks out the most nucleons,
and GiBUU's potential captures the most slow ones.

**Leading particles and other mesons (2026-10-06), same runs.** An independent
per-event loop over GENIE's `gst` reproduces the leading charged-pion, π⁰ and
proton columns to ≤ 3.4 × 10⁻⁶ GeV. `n_other_mesons` equals `gst`'s kaon
counters in all 2000 events. Events with a non-pion meson: GENIE 1.2%, NuWro
1.2%, NEUT 3.8%, GiBUU 0.3%. Fraction of events with a proton above MicroBooNE's
50 MeV threshold: GENIE 0.78, NuWro 0.77, NEUT 0.80, GiBUU 0.65.

**Before FSI was enabled in GiBUU (2026-07-30).** 300-event Docker runs of all
four generators: the GENIE multiplicities and `native_interaction_code` agreed
exactly with `gst`, and NuWro's `dyn` maps one-to-one onto the common labels
(0→qel, 2→res, 4→dis, 6→coh, 8→mec).

## Open questions

- **NEUT's and GiBUU's QE vertex offsets** (+53 MeV and −43 MeV in `w_true_gev`)
  have not been traced to their binding treatments. That NEUT is the only
  generator storing an on-shell nucleon and has the only upward offset is
  suggestive but not established.
