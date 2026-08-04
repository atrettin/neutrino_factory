# Physics conventions

The physics contract every generator backend is normalized onto. Anything that
reads, writes or compares the common HDF5 output depends on the conventions
below; the per-generator half of the story — how each generator's raw output is
turned into these quantities — is in [generators/](generators/).

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
normalization denominator. What varies between generators is the numerator and
`D`, because generators encode cross-section information in fundamentally
different ways:

- **Rejection-sampled / unweighted** (GENIE, NuWro, NEUT). Every event carries
  the same information; the event *density* in energy is what encodes σ(E). The
  numerator is a flux-averaged total cross section obtained per generator, and
  `D` is the chunk's **event count**.
- **Phase-space sampled and cross-section weighted** (GiBUU). The raw per-event
  weight *is* a cross section; the event density carries no normalization. The
  numerator is the raw weight and `D` is the number of generator **runs**.

`ConfigTranslator.xsec_norm_count` is abstract rather than defaulting to
`len(events)` precisely so that the GiBUU case cannot be got wrong silently
(`translators/base.py:40-61`).

### Per-nucleon, always

The column is per target nucleon regardless of the nucleus. Generators differ in
what they hand over, and the conversion is part of each translator's knowledge:

| Generator | Raw scale | Per-nucleon conversion |
|---|---|---|
| GENIE | natural units, whole-nucleus splines | `× 1e38 / GENIE_UNITS_CM2`, then **÷ A** |
| NuWro | bare cm², already per-nucleon | `× 1e38`, **no** `/A` |
| NEUT | already 1e-38 cm² per nucleon | none |
| GiBUU | already 1e-38 cm² per nucleon | none |

The two "already per-nucleon" claims are not assumptions — both were verified by
regenerating the same flux on different nuclei and confirming the weight does not
scale with `A` (see [generators/neut.md](generators/neut.md) and
[generators/gibuu.md](generators/gibuu.md)). GENIE's splines are keyed on the
whole-nucleus PDG code, which is why it is the one generator that divides.

### Merging chunks averages, it does not sum

*Moved here from `design_decisions.md` (2026-07).*

Every generator's `xsec_weight` column is a *per-chunk* estimate of the cross
section: for one chunk on its own,
`sum(xsec_weight in an energy bin) / bin_width` converges to σ(E). Concatenating
N chunks therefore reports roughly N times the true cross section — measured at
2.06× for a 2-chunk NEUT run whose single-chunk equivalent was correct to 1.03.

This affects **all four generators**. GiBUU is not exempt: its weights sum to σ
*within one run* (`num_runs_SameEnergy = 1` per chunk), so two chunks are two
independent estimates and adding them double-counts exactly as elsewhere.

Each chunk declares its own `D` as HDF5 metadata `xsec_norm_count`, and
`merge_hdf5_files` scales chunk *c* by `D_c / Σ D` — turning concatenation into a
weighted average. Equivalently, the merged weights are what a single run of `Σ D`
events would have produced.

Properties:

- A single input is a no-op (share = 1), so one-chunk runs are unchanged.
- The merged file records the summed `Σ D`, so merging merged files stays correct.
- Stub output declares no count and is left untouched — its weights are
  placeholders, not cross sections — and a set mixing declared with undeclared
  inputs is refused rather than half-rescaled.

Validation (local Docker, same physics at different chunk counts):

| Generator | Events | 1 chunk | 2 chunks | 4 chunks |
|---|---|---|---|---|
| NEUT (vs its own evtrt/flux per bin) | 4000 | 1.015 | 1.018 | 1.011 |
| GiBUU (relative to 1 chunk) | 40000 | 1.000 | 0.929 | 1.005 |
| NuWro (relative to 1 chunk) | 4000 | 1.000 | 0.956 | — |

The residual spread is Monte-Carlo noise. GiBUU needs the larger sample for a
meaningful comparison: its weights are heavy-tailed, and in a 1000-event run a
single event carried 72% of one chunk's total.

## Flux

A flux is a callable returning a number proportional to the flux **density**
versus energy in GeV, in arbitrary units (`flux.py:1-18`). Each concrete flux
knows its support `[emin_gev, emax_gev]` and the probe it describes;
`to_histogram` produces the binned form handed to generators and used to draw
synthetic energies in stub mode.

Two facts dominate every generator's flux handling:

- **Density versus per-bin integral.** ROOT-based samplers
  (`TH1::GetRandom` and NEUT's `Ufm2TH1dist`) pick a bin in proportion to its raw
  bin *content*, ignoring the bin width. Feeding a density on an unequal-width
  grid therefore generates a spectrum tilted by one power of the bin width.
  GENIE has a `WIDTH` flag that performs the multiplication for you; NEUT has no
  such switch, so the adapter writes per-bin integrals directly.
  `HistogramFlux.from_root_file(..., contents_are_counts=True)` is the inverse
  conversion on readback.
- **The generated flux is piecewise constant on the input bin edges.** Energy is
  uniform *within* a bin for every histogram-driven generator here, so the
  reconstructed σ(E) can carry no structure finer than the flux binning. This is
  why GENIE and NEUT use 1000 **log-spaced** bins (~0.3–0.6%/bin over
  0.1–50 GeV) rather than equal-width bins: constant relative resolution is the
  only sensible choice for a steeply falling spectrum.

**Normalize against what was actually sampled, never against the config.** GENIE
and NEUT both stamp the flux histogram they used into their own output, and both
normalizers read it back on its *native* binning rather than rebuilding the flux
from the run configuration. There is deliberately no fallback: reconstructing the
flux from the config would silently restore a normalization bug whose output
still looks physical.

`PowerLawFlux` takes `gamma` directly from the config, so an E^-2 spectrum is
`gamma: -2.0`.

## Probes and targets

*Moved here from `design_decisions.md` (2026-07).*

`flux.particle` accepts the six neutrino probes — `nue`, `numu`, `nutau` and
their antineutrinos — and `particles.py` is the single table mapping them to PDG
codes. Every translator derives its generator-native beam setting from it
(GENIE's numeric `-p`, NuWro's `beam_particle`, NEUT's `EVCT-IDPT`, GiBUU's
`flavor_ID` plus the sign of `process_ID`), so a flavour is supported by all four
generators or by none.

- **`validate_flux` rejects an unknown name**, so a typo fails at
  `validate-config` rather than as a `KeyError` deep inside a translator once
  Slurm tasks are already running.
- **The antineutrino sign comes from the PDG code, never from the name.**
  `translators/gibuu.py` used to negate `process_ID` when the flavour string
  ended in `bar`; a name-based test silently produces a *neutrino* run for any
  probe spelled differently. `particles.is_antineutrino` asks the table instead.

Targets are parsed from names (`C12`, `Ar40`) into `Z`, `A` and the PDG
`10LZZZAAAI` nuclear code by `nucleus_z_a` / `nucleus_pdg` /
`nucleus_composition` (`particles.py:77-117`) — there is no nuclide table, so any
element-plus-mass-number name resolves.

## Weak current

*Moved here from `design_decisions.md`.*

`physics.current` (`cc` | `nc` | `inclusive`) is applied through each generator's
**own** configuration rather than by filtering the normalized output. Filtering
after the fact would waste the discarded events against the requested count and,
worse, leave `xsec_weight` normalized to the inclusive cross section while the
surviving sample covered only one current. The native switches are
`--event-generator-list` (GENIE), the `dyn_*` parameters (NuWro), `NEUT-MODE -1`
plus a `NEUT-CRS` mask (NEUT) and `process_ID` (GiBUU);
[configuration.md](configuration.md) tabulates them and each generator doc
explains its own.

**`is_cc` is a required column with no default.** The `interaction` label cannot
stand in for it: `qel` covers CCQE and NC elastic alike (GENIE's `qel` gst flag
does, and NEUT modes 1 and 51/52 both map to it), so an inclusive run without
this column is unusable for anything current-specific. Since a default would be
indistinguishable from a measured value, `write_common_hdf5` raises instead.

Two cross-generator consequences:

- **GENIE's spline sum must track the generator list.** The reconstructed σ has
  to cover exactly the channels `gevgen` was allowed to generate, so it is
  filtered on `proc:Weak[CC]` / `proc:Weak[NC]`. Without the filter a
  CC-restricted run would be normalized to the CC+NC total — for numu on carbon,
  an overstatement of roughly a third. Both settings derive from the same
  `physics.current` value in the translator so they cannot drift apart.
- **GiBUU `inclusive` is two passes, not one run**, concatenated. This is exact
  rather than an approximation only because GiBUU is cross-section weighted; the
  same trick would not work for the other three.

Validation (local Docker, numu on C12, power-law flux over 0.5–5 GeV; 400 events
for GENIE, 2000 for the rest):

| Generator | CC fraction, `cc` run | `nc` run | `inclusive` run | NC share of σ, inclusive |
|---|---|---|---|---|
| GENIE | 1.000 | 0.000 | 0.750 | 0.204 |
| NuWro | 1.000 | 0.000 | 0.744 | 0.278 |
| NEUT | 1.000 | 0.000 | 0.735 | 0.261 |
| GiBUU | 1.000 | 0.000 | 0.449 | 0.308 |

GiBUU's inclusive *event* fraction is near half because it samples phase space
rather than the cross section; its CC:NC ratio lives in the weights, so the NC
share of σ is the number to compare. The exact reference is GENIE's spline
decomposition, which is deterministic: summing the staged splines for numu/C12
filtered on `Weak[CC]` and `Weak[NC]` gives 1.03736 and 0.39137, whose sum
reproduces the unfiltered total to all six printed digits — an NC share of 0.274,
consistent with every measured value above.

## Interaction taxonomy

The common `interaction` column takes one of `qel`, `res`, `dis`, `coh`, `mec`,
`other`. GENIE's `gst` and NuWro's `treeout` expose parallel boolean branches
with these very names and share one priority chain,
`normalizers/base.interaction_from_flags` (qel → res → dis → coh → mec → other).
GiBUU maps an integer `evType` and NEUT a mode number; both use explicit,
closed tables that raise on an unknown code rather than falling through to
`other`.

**The categories are pinned to GENIE**: an event gets the category it would have
had if GENIE had produced it. `qel` deliberately covers NC elastic as well as
CCQE, which is why `is_cc` cannot be inferred from `interaction`.

**The shallow-inelastic region (roughly 1.2 < W < 2.0 GeV) is the one place the
four generators genuinely disagree**, because each decomposes it differently and
none of them exposes a `sis` category:

| Generator | How the region is produced | Our label |
|---|---|---|
| GENIE | RES generator (full below `Wcut` = 1.7 GeV) **plus** the DIS generator supplying non-resonant background with the KNO multiplicity tune | `res` / `dis` respectively |
| GiBUU | non-resonant 1π/2π background, `evType` 32/33/37, resonances subtracted | `dis` |
| NEUT | multi-π modes 21/41 (1.3 < W < 2.0); single-π modes 11/12/13 lump resonant *and* non-resonant together | `dis` / `res` |
| NuWro | `dyn_res` / `dyn_dis` split | `res` / `dis` |

Labelling GiBUU's background `dis` reproduces what GENIE would have called the
same event. NEUT is the one that cannot be aligned by relabelling, since its
single-pion modes do not separate the resonant and non-resonant pieces at all.
Comparing it like-for-like would mean classifying every generator by hadron
content instead — see the NEUT code below. Detail in
[generators/genie.md](generators/genie.md#the-shallow-inelastic-region-genie-splits-it-neut-and-gibuu-do-not)
and [generators/gibuu.md](generators/gibuu.md#why-the-non-resonant-background-is-dis).

### The NEUT code as a candidate cross-generator axis

NEUT's mode numbering is the closest thing the field has to a lingua franca for
fine-grained channels, but **only two of the four generators emit one**:

| Generator | NEUT code | Source |
|---|---|---|
| NEUT | native — `mode` **is** the NEUT code | generated channel |
| GENIE | native — gst `neut_code` | `genie::utils::ghep::NeutReactionCode`, counts primary hadrons |
| NuWro | **none**; only `e/dyn` plus the `flag.*` booleans | derivable: NUISANCE's `ConvertNuwroMode` counts `e/out` |
| GiBUU | **none**; only `evType` | derivable: NUISANCE's `ConvertModeGiBUUtoNEUT`, a code→code switch |

Two properties to be clear about before treating it as a topology label:

- **It classifies the primary hadronic system, not the observable final state.**
  GENIE's routine counts particles "in the primary hadronic system (_before_
  intranuclear rescattering)"; NUISANCE's NuWro converter counts `e/out`, which
  NuWro's own header documents as "outgoing particles (before fsi)" — not
  `e/post`, "particles leaving the nucleus"; and NEUT's `mode` is assigned at
  production. So it is a *pre-FSI topological* classification: generator-agnostic
  and finer than "which module produced the event", but not an observable.
- **The derived mappings are lossy, and the GiBUU one is partly invalid.**
  `ConvertModeGiBUUtoNEUT` collapses all 30 resonance codes (2–31) onto
  single-pion modes regardless of how the resonance decayed, returns **10** (CC)
  and **30** (NC) for the 1π background — neither is a NEUT mode — and maps NC
  2p2h to **42**, which in NEUT means NC 1η. It is not a faithful round trip.

Adding the column is tracked in `.claude/TODOS.md`.

## Derived kinematic variables

*Moved here from `design_decisions.md`.*

Eight lab-frame columns are derived in one shared module, `kinematics.py`:
`q2_gev2`, `bjorken_x`, `inelasticity_y`, `lepton_energy_gev`,
`lepton_momentum_gev`, `lepton_p_parallel_gev`, `lepton_p_transverse_gev`,
`lepton_costheta`.

**Recomputed, not read.** GENIE's `gst` tree already precomputes `Q2`, `x`, `y`
and `cthl`, and it would have been less code to use them. They are recomputed
from the four-vectors for every generator anyway, because the promise of the
common format is that a Q² histogram from GENIE means exactly the same thing as
one from GiBUU — which only holds if one formula produces all of them. GENIE
further ships *two* variants (`Q2`/`x`/`y` true, and `Q2s`/`xs`/`ys`
reconstructed from the hadronic system), so "use the native branch" is not even
unambiguous within one generator. `tests/test_genie_normalizer.py` pins these
definitions against GENIE's own branches for a reference scatter.

Conventions:

- The beam axis is taken **per event** from the incoming neutrino three-momentum,
  never assumed to be +z, so the parallel/transverse split and `cos θ` stay
  correct for off-axis or divergent beams.
- Bjorken-x uses a **fixed** isoscalar nucleon mass `NUCLEON_MASS_GEV`
  ((m_p + m_n)/2 = 0.9389187543 GeV), not the per-event struck-nucleon mass. Only
  GiBUU and NuWro expose the hit nucleon; a fixed mass makes x identically
  defined everywhere.
- `Q² = -(p_ν - p_l)²` is non-negative for this process up to floating-point
  noise near forward scattering, so it is clamped at 0 rather than blanked.
- `inelasticity_y` can come out slightly **negative** when Fermi motion of the
  struck nucleon pushes the outgoing lepton above the beam energy. That is
  physical and is passed through; only `bjorken_x` (which would diverge) is
  blanked when the energy transfer is non-positive.
- The lepton variables are filled for NC as well as CC events — for NC the
  "lepton" is the scattered neutrino. Detector observability is a downstream
  question, not the generator-harmonization layer's to decide.

**Placeholders**, two of them, because `-1` is a physical value for the signed
quantities: `MISSING = -1.0` for the non-negative-definite columns and
`MISSING_SIGNED = -999.0` for `lepton_p_parallel_gev` and `lepton_costheta` (a
backward-scattered lepton genuinely has `cos θ = -1`). Blanking rules:
`bjorken_x` for coherent events (no struck nucleon) and for non-positive energy
transfer; `lepton_costheta` when the lepton momentum is zero; the directional
variables when the beam momentum is zero; and the whole block when the generator
supplies no outgoing lepton (NuWro's `e/out` can be empty) or the four-vectors
are non-finite.

**Verified on real generator output (2026-07-28).** 20k-event GENIE, NuWro and
NEUT runs plus an 83k-event GiBUU run, all `numu` CC on C12 with a γ=-2 power-law
flux over 0.5–5 GeV, pass the structural invariants event by event (Q² ≥ 0, exact
energy-transfer closure, |cos θ| ≤ 1, p_∥² + p_T² = |p|², coherent events blanked
and only those) and agree on the cross-section-weighted distributions. Bjorken-x
for quasi-elastic peaks at 0.75–0.85 with a weighted median of 0.825 (GENIE),
0.815 (NuWro), 0.825 (NEUT) and 0.809 (GiBUU) — a broad peak *below* 1, not the
sharp x=1 of free-nucleon QE, because Fermi motion and binding smear it and the
fixed `M_N` does not absorb that. The GiBUU number is quoted with a bootstrap CI
of [0.797, 0.851] rather than as a point estimate, because its QE weight is
extremely concentrated (see weight efficiency below).

## Weighted statistics and weight efficiency

`analyze-kinematics` (`kinematics_report.py`) reports every mean and median
**weighted by `xsec_weight`**, never raw. Unweighted event distributions are
simply not the physical ones for a cross-section-weighted generator, and the
failure is silent: a 100k-event GiBUU run reports a perfectly healthy-looking
quasi-elastic sample whose effective size is 35 events.

It therefore prints a weight-efficiency table alongside — Kish
`n_eff = (Σw)² / Σw²` per interaction channel, plus the share of weight in the
heaviest 1% of events. The efficiency is computed on `|w|` so GiBUU's negative
interference weights cannot cancel into a meaningless ratio, while `Σw` is
reported signed.

Placeholders are excluded per variable and counted, so `bjorken_x` for a coherent
selection reports "0 used, 127 blank" rather than silently averaging in `-1`.
The weighted quantile uses the **midpoint convention** — cumulative weight
evaluated at the centre of each point's weight, not its upper edge; the naive
cumulative sum biases the median low by half a bin.
