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
| GENIE | RES generator (full below `Wcut`, zero above) **plus** the DIS generator supplying non-resonant background with the KNO multiplicity tune, which extends *below* `Wcut` too | `res` / `dis` respectively |
| GiBUU | non-resonant 1π/2π background, `evType` 32/33/37, resonances subtracted | `dis` |
| NEUT | multi-π modes 21/41 (1.3 < W < 2.0); single-π modes 11/12/13 lump resonant *and* non-resonant together | `dis` / `res` |
| NuWro | `dyn_res` / `dyn_dis`, disjoint at a hard `res_dis_cut` = 1.9 GeV; but the RES channel blends non-resonant background into *itself* from W = 1.08 GeV upward, so its `res` is nowhere purely resonant above the Δ | `res` / `dis` |

Labelling GiBUU's background `dis` reproduces what GENIE would have called the
same event. NEUT is the one that cannot be aligned by relabelling, since its
single-pion modes do not separate the resonant and non-resonant pieces at all.

**This is why the common output carries W.** The `w_gev` and `w_true_gev` columns
(see [derived kinematic variables](#derived-kinematic-variables)) are computed by
one formula from four-vectors every generator supplies, so a cut on them is a cut
on the same physical quantity in all four — which no relabelling of the channel
flags can achieve. Every W threshold quoted in the table above is a cut those
columns can now express. `w_true_gev` is the one to compare against a generator's
own threshold; `w_gev` is the one to compare *between* generators.

Classifying by hadron content is the other candidate axis — see the NEUT code
below. Detail in
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

### The `resonant_primary` column

`interaction` says which channel the generator assigned an event.
`resonant_primary` says how the primary hadronic system was actually *made*:
`1` resonant, `0` non-resonant, `-1` unknown or not applicable
(`common_output.RESONANT_PRIMARY_*`). The two are not the same question, and the
difference between them is exactly where the taxonomy stops being comparable.

| Generator | Source | Agrees with `interaction`? |
|---|---|---|
| GENIE | the channel itself — `res` is the Rein–Sehgal/Berger–Sehgal calculation, `dis` the DIS generator that supplies the non-resonant background | yes, by construction |
| GiBUU | `evType` — 2–31 are resonances, 32/33 (1π background), 34 (DIS) and 37 (2π background) are not | yes, by construction |
| NuWro | `e/flag/flag.res_delta` | **no** — see below |
| NEUT | none; modes 11/12/13 lump the two mechanisms and no flag separates them | `-1` for every event |

**NuWro is why this is a column rather than a footnote.** Its RES channel blends
non-resonant background into itself between `res_dis_blending_start` and
`res_dis_blending_end` (see
[nuwro.md](generators/nuwro.md#the-resdis-split-is-a-hard-cut-at-res_dis_cut-plus-a-blend-inside-res)),
so a large part of what it labels `res` is what GENIE would have produced from
its DIS generator and labelled `dis`. Measured on the 20k-event numu CC C12 run:
**10.0% of NuWro's `res` events, but 38.6% of its `res` cross section**, carry
`resonant_primary = 0`. Re-bucketing those into `dis` would move NuWro from
`res` 46.5% / `dis` 23.2% of the total cross section to 28.5% / 41.1% — from
being the clear outlier among the four generators to sitting inside the range the
others span (GENIE 37.2/38.0, NEUT 34.1/41.1, GiBUU 26.1/46.7).

That re-bucketing is deliberately **not** applied to `interaction`. Doing so
would make one generator's `interaction` column mean something different from the
others', which is the property the column exists to guarantee; and it could not
be done for NEUT at all, so it would trade a visible inconsistency for a hidden
partial one. Downstream code that wants the mechanism should cut on
`resonant_primary` directly, and treat `-1` as "this generator cannot say".

**`resonant_primary` is only meaningful for `res` and `dis`.** Quasi-elastic,
coherent and 2p2h have no pion-production mechanism to attribute, so they carry
`-1` everywhere.

## Derived kinematic variables

*Moved here from `design_decisions.md`.*

Ten lab-frame columns are derived in one shared module, `kinematics.py`:
`q2_gev2`, `bjorken_x`, `w_gev`, `w_true_gev`, `inelasticity_y`,
`lepton_energy_gev`, `lepton_momentum_gev`, `lepton_p_parallel_gev`,
`lepton_p_transverse_gev`, `lepton_costheta`.

### The two hadronic-mass columns

W gets **two** columns, because the two useful definitions genuinely differ and
both are wanted — see [the interaction taxonomy](#interaction-taxonomy) for why W
is carried at all.

| Column | Definition | Needs |
|---|---|---|
| `w_gev` | `W² = M_N² + 2 M_N ν − Q²`, the same fixed isoscalar `NUCLEON_MASS_GEV` Bjorken-x uses, with the nucleon taken at rest | the two four-vectors only |
| `w_true_gev` | `W² = (p_ν + p_N − p_l)²`, against the per-event struck system | the struck nucleon, which each generator exposes differently |

`w_gev` is the observable, experimental W: frame-dependent by construction, and
smeared relative to the true one because Fermi motion and binding are not in the
formula. It is identically defined for every generator, present or future,
including any that never exposes a hit nucleon.

**"Smeared" is accurate only for the single-nucleon channels.** Measured on the
20k runs, the median gap `w_true_gev - w_gev` is at most 0.07 GeV for `qel`,
`res` and `dis` — genuine Fermi/binding smearing, with no consistent sign. For
`mec` it is **+1.04 to +1.08 GeV** in all three generators that supply it, which
is not smearing but a *definitional* offset of one nucleon mass: `w_gev` assumes
a single stationary nucleon of mass `M_N`, while `w_true_gev` uses the struck 2N
cluster, whose mass is about 2 `M_N`. 100% of MEC `w_true_gev` lies above
2 `M_N`; 0% of MEC `w_gev` does. The two columns sit on opposite sides of the
pair-mass threshold by construction, so **MEC `w_true_gev` must not be read on
the same axis as the other channels'** — it is the invariant mass of a
two-nucleon final state, not of a single-nucleon one.

Both are nonetheless correct, and GENIE says so itself: on its MEC events gst
`W` (its lepton-only variant) has median 1.161 against our `w_gev`'s 1.161, and
gst `Ws` (its selected W) has 2.184 against our `w_true_gev`'s 2.183. GENIE
carries the same two quantities and gets the same two numbers.

`w_true_gev` is the invariant mass the generators themselves cut on — GENIE's
`Wcut`, NEUT's 1.3 < W < 2.0 multi-π window — so it is the column that
reproduces their thresholds. It is a real Lorentz invariant, which the unit tests
use as its defining property (boosting an event leaves it unchanged and moves
`w_gev`). Being tied to each generator's off-shell and binding treatment, it is
not comparable across generators to the precision `w_gev` is: GiBUU's default
`storeNucleon = 2` stores the *bound* nucleon, whose invariant mass sits below
`M_N`, and GENIE's is likewise a bound nucleon.

**`p_N` is the struck initial-state hadronic *system*, not a single nucleon.**
For 2p2h it is the correlated pair. That choice is forced by GENIE, whose
struck-nucleon branches carry the two-nucleon cluster for MEC events
(`GHepRecord::HitNucleon` returns an `Is2NucleonCluster` code); summing the pair
everywhere else is what keeps the column meaning one thing. The consequence is
worth knowing before plotting all channels together: MEC `w_true_gev` is a *pair*
mass and sits roughly `M_N` above the quasi-elastic peak — measured minima of
1.928 (GENIE), 1.908 (NEUT) and 1.877 (NuWro) GeV, all just above 2 `M_N`.

**GiBUU cannot supply this, and so is blanked for 2p2h.** Its own 2p2h cross
section is built from `eN%boson%mom + eN%nucleon%mom + eN%nucleon2%mom`
(`lepton2p2h.f90`), but `doStoreNeutrinoInfo` passes `neutrinoProdInfo` only
`eN%nucleon`, whose `mom_nuc` field holds a single four-vector; `nucleon2` never
reaches the RootTuple, surviving only in the nuclear-residue bookkeeping. Filling
`w_true_gev` from the one stored nucleon produced a *one-nucleon* invariant mass
— median 0.924 GeV against 2.18–2.25 for the other three, and starting near `M_N`
rather than 2 `M_N` — which is a different physical quantity under the same column
name, exactly the kind of value that is indistinguishable from a real one
downstream. Those events therefore carry the placeholder. `w_gev`, needing no
nucleon, is unaffected and remains comparable across all four (MEC medians
0.99–1.18).

Where each generator's `p_N` comes from:

| Generator | Source | Absent when |
|---|---|---|
| GENIE | gst `En/pxn/pyn/pzn`, flagged by `hitnuc` | `hitnuc == 0`, which on a 20k numu/C12 run is *exactly* the coherent set |
| NuWro | `e/in` beyond index 0, selected by `in.pdg` and summed | coherent (`e/in` holds only the beam); an atomic electron there is not counted |
| NEUT | summed by `nf_flatten.C` into `nuc_*_gev`, with `n_nuc` | `n_nuc == 0` |
| GiBUU | `nuc_E/nuc_Px/nuc_Py/nuc_Pz` from `neutrinoProdInfo` | **2p2h** (`evType` 35/36) — only one of the pair's two nucleons is written, so the defined quantity cannot be formed |

All three ROOT generators write **zeros, not a sentinel**, when there is no
struck nucleon. A zero four-vector is finite, so the finiteness check cannot
catch it and each normalizer passes an explicit validity mask instead.

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
- Bjorken-x and `w_gev` use a **fixed** isoscalar nucleon mass `NUCLEON_MASS_GEV`
  ((m_p + m_n)/2 = 0.9389187543 GeV), not the per-event struck-nucleon mass. All
  four generators do expose the hit nucleon, but each with its own off-shell and
  binding treatment; a fixed mass makes these two variables identically defined
  everywhere, and `w_true_gev` is the column that carries the per-event system.
- `Q² = -(p_ν - p_l)²` is non-negative for this process up to floating-point
  noise near forward scattering, so it is clamped at 0 rather than blanked.
- **Negative `W²` is blanked, not clamped** — the opposite of the Q² rule above,
  and for the opposite reason. The Q² clamp removes rounding near forward
  scattering; negative `W²` is a real high-Q², low-ν corner (`w_gev`) or a real
  off-shell threshold effect (`w_true_gev`). Clamping either would pile those
  events at exactly 0, where nothing downstream could tell them from a measured
  value.
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
`bjorken_x`, `w_gev` and `w_true_gev` for coherent events, which scatter off the
nucleus as a whole and so have no struck nucleon to define any of the three
against; `bjorken_x` additionally for non-positive energy transfer, which it
divides by — the two W columns do not divide by ν and are kept there, so a
slightly sub-nucleon W from Fermi motion is reported rather than hidden;
`w_true_gev` alone wherever the generator supplied no struck nucleon;
`lepton_costheta` when the lepton momentum is zero; the directional variables
when the beam momentum is zero; and the whole block when the generator supplies
no outgoing lepton (NuWro's `e/out` can be empty) or the four-vectors are
non-finite.

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

**The two W columns verified on real generator output (2026-08-05).** 20k-event
GENIE, NuWro and NEUT runs plus a 16.6k-event GiBUU run, all `numu` CC on C12
with a γ=-2 power-law flux over 0.5–5 GeV. Figures from
`scripts/plot_w_validation.py`.

*Against GENIE's own branches.* `w_gev` reproduces gst `W` — which is the same
lepton-only formula, with a nucleon mass differing from ours by 7e-9 GeV — to a
maximum of **5.1e-8 GeV** over 19 862 events (mean 9.8e-9), so the two agree to
the constant. `w_true_gev` reproduces gst `Ws`, GENIE's own selected W, to
**2.2e-13 GeV** over the 18 251 non-MEC events. The MEC events deliberately
differ (up to 0.22 GeV): `Ws` is not the pair mass, whereas `w_true_gev` is built
from the two-nucleon cluster four-vector.

*Where the columns are blank.* GENIE 138 of 20 000, of which 136 are the
coherent events — and `hitnuc == 0` turns out to be **exactly** that same set, so
the two blanking criteria coincide rather than compete. The remaining 2 are
genuine negative-`W²` events outside `coh`, the case the blank-not-clamp rule
exists for. NuWro blanks 131, NEUT 105, GiBUU none (it has no coherent channel
and always writes `nuc_*`).

*Against thresholds the code was never told about* — the sharpest check, since
these are cuts each generator applies internally:

- **NEUT** modes 21/41 (multi-π): all 2770 events land inside
  **[1.301, 2.000] GeV**, reproducing NEUT's own 1.3 < W < 2.0 window to the bin.
  In the by-channel figure NEUT's `dis` switches on abruptly at 1.3 and its `res`
  stops abruptly at 2.0.
- **NuWro** splits at its `res_dis_cut` = **1.9 GeV**: `dis` has *zero* events
  below it (minimum 1.9171), the two dynamics being disjoint by construction.
  2.8% of `res` leaks above it in our variable, because NuWro samples an internal
  W built from a binding-corrected effective mass rather than the struck
  nucleon's four-vector — see
  [nuwro.md](generators/nuwro.md#the-resdis-split-is-a-hard-cut-at-res_dis_cut-plus-a-blend-inside-res).
  **How faithfully `w_true_gev` reproduces a generator's internal W is therefore
  generator-dependent**: exact for GENIE, smeared by tens of MeV for NuWro.
- **GENIE**'s `res` terminates at **1.927742 GeV** with *zero* events above it —
  the tune's `Wcut` of **1.927862** GeV, matched to 1.2e-4 GeV, which is one
  event's sampling granularity. The RES/DIS joining scheme is therefore a hard
  cut on exactly the quantity `w_true_gev` computes, not an approximate one.
  What GENIE does *not* have is a boundary between the two channels: its
  non-resonant DIS background carries 51% of the DIS cross section *below*
  `Wcut`, overlapping RES throughout. So `res` and `dis` coexist on the low side
  and only `res` stops at the line — precisely the structural difference that
  makes the channel labels unalignable with NEUT's.
- **MEC** `w_true_gev` sits entirely above `Wcut` in GENIE (0% below), as the
  pair-mass definition requires — a 2N cluster starts near 2 M_N.

*The struck nucleon each generator hands over*, median invariant mass against
`M_N = 0.9389` GeV: NEUT **0.9396** (on shell), GENIE **0.9045** and GiBUU
**0.8895** (both bound, off shell). This is the quantitative form of the caveat
above — `w_true_gev` is not commensurable across generators at better than
~5%, which is exactly why `w_gev` exists alongside it.

*NEUT's `n_nuc`* came out 1 for 18 365 events, **2 for 1531 — all of them mode 2**,
and 0 for 104. The 2p2h layout the flattener assumes is therefore confirmed per
event rather than asserted.

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
