# GENIE

GENIE is built from source off the GENIE-MC/Generator GitHub tags and run in two
stages, `gevgen` → `gntpc`. It is the only generator whose `config_version` is a
real physics parameter set (the **tune**), and the one with the broadest reweight
support, through GENIE Reweight. The conventions its output is normalized onto
are in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

## At a glance

| | |
|---|---|
| Code versions | `R-3_06_00` (verified), `R-3_04_00` |
| `config_version` | a GENIE tune, e.g. `G18_10a_02_11a`; available iff its splines are staged ([Tunes](#tunes-config_version)) |
| Sampling | unweighted (rejection-sampled) |
| Raw σ → per nucleon | splines in natural units, whole nucleus → `× 1e38 / GENIE_UNITS_CM2`, `÷ A` |
| `physics.current` | `--event-generator-list CC`/`NC` ([Weak current](#weak-current)) |
| FSI | the tune's own setting, not overridden; on in every R-3_06_00 tune |
| `run.log_level` | honoured ([Log verbosity](#log-verbosity)) |
| Reweight universes | yes, plus switch variations ([Reweight universes](#reweight-universes-and-variations)) |

## Setup

```bash
# build the image for a catalogued code version (default R-3_06_00)
setup/setup_genie.sh R-3_06_00
setup/setup_genie.sh --code-version R-3_06_00 --download-xsec   # …and stage splines

# stage splines on their own
setup/download_genie_xsec.sh --code-version R-3_06_00 --tune G18_10a_02_11a
```

- `setup_genie.sh` takes the code version positionally or as
  `--code-version`/`--tag`, plus `--jobs N` for parallel make. It targets
  `linux/amd64`, so it runs on Apple Silicon via Rosetta 2. Distinct code
  versions produce distinct image tags, so several GENIE versions coexist.
- `download_genie_xsec.sh` takes `--tune` (repeatable; default: every tune
  published for the code version), `--software-root`, and `--force` to re-stage
  over an existing file. It downloads the FNAL SciSoft tarball (~428 MB per
  tune) and stages only `gxspl-NUsmall.xml` (~543 MB). Tunes SciSoft does not
  publish for a code version are warned about and skipped.

Splines land in `<software_root>/genie/genie_xsec/<tag-safe-code-version>/<tune>/xsecs.xml`.

**"No available config version"** in `neutrino-factory list-generators --generator
genie` almost always means no tune's `xsecs.xml` has been staged yet. The image
alone is not enough.

## Configuring a job

### Tunes (`config_version`)

Tunes are **discovered, not enumerated**: a tune is available exactly when its
spline file is staged. `list-generators` shows it only then, and validation of a
non-stub run refuses a tune with no staged spline. Tune names are matched
separator-insensitively, because SciSoft strips the underscores
(`G1810a0211a` for `G18_10a_02_11a`).

A staged spline is necessary but **not sufficient**. `gevgen` also needs the
tune's physics configuration inside the code payload (`$GENIE/config/<tune>`),
and nothing checks that at config time. Example: `N18_10j_02_11a` has splines
for R-3_04_00 and passes validation, but `gevgen` aborts with `FATAL TuneId: No
valid tune directory associated with N18_10j_02_11a`, because R-3_04_00 ships no
`N18_10j` directory.

Each tune also fixes physics a user might expect to be a framework setting:

- **FSI.** Hadron transport is part of the tune's model configuration
  (`HadronTransp-Enable`, `HadronTransp-Model`, e.g.
  `genie::HAIntranuke2018/Default`). The framework passes no override; all 33
  tunes in the R-3_06_00 source tree switch it on.
- **The RES/DIS boundary `Wcut`** differs by up to 0.6 GeV between tunes
  ([Model notes](#the-resdis-boundary-is-the-tunes-wcut)).
- **Which reweight dials work** ([Which dials a tune allows](#which-dials-a-tune-allows)).

### Probes

The shipped `gxspl-NUsmall.xml` has splines for ν_e, ν̄_e, ν_μ and ν̄_μ only. A ν_τ
job fails at normalization with an error naming the probe, target, current and
spline file, rather than producing zero weights.

### Log verbosity

GENIE is the only generator that honours `run.log_level`. What each level gives,
and how to pick between `essential` and `quiet`, is in
[../configuration.md](../configuration.md#generator-log-verbosity-runlog_level).
The mapping is in [Implementation](#log-level-mapping).

## Reading the output

### Interaction labels and `is_cc`

The gst class flags `qel`/`res`/`dis`/`coh`/`mec` go through the shared priority
chain (qel → res → dis → coh → mec → other); `is_cc` comes from the gst `cc`
branch. Things to know:

- **`qel` includes NC elastic** and charm quasi-elastic production
  ([Model notes](#charm-quasi-elastic-events-are-labelled-qel)).
- **`res` and `dis` overlap below `Wcut`**: the non-resonant background comes
  from GENIE's DIS generator and is labelled `dis`
  ([Model notes](#the-resdis-boundary-is-the-tunes-wcut)).
- **`other` is electron-target processes**: inverse muon decay (`imd`) and ν-e
  elastic (`nuel`), ~0.06% of events. Their `xsec_weight` is per *nucleon*,
  which does not fit an electron target. NuWro switches its equivalent channel
  off for that reason.
- **`resonant_primary`** follows the label: `res` → 1, `dis` → 0.

### Struck nucleon and `w_true_gev`

`p_N` is gst `En/pxn/pyn/pzn`, flagged by `hitnuc`:

- **No hit nucleon → literal zeros** and `hitnuc = 0` (`gNtpConv.cxx`), not a
  NaN. On numu C12 this is exactly the coherent set, whose W columns are blank
  anyway.
- **For MEC, `hitnuc` is a two-nucleon *cluster* code** (2000000200/201/202) and
  the four-momentum is the *pair's*. This is what defines `p_N` as the struck
  *system* across all four generators
  ([physics.md](../physics.md#the-two-hadronic-mass-columns)).

GENIE stores the off-shell nucleon its vertex actually used, so `w_true_gev`
reproduces GENIE's own selected W (gst `Ws`) exactly for every non-MEC event.

### `native_interaction_code` and other gst codes

`native_interaction_code` is gst `neut_code`: GENIE's event re-encoded into NEUT's
signed mode numbering by `genie::utils::ghep::NeutReactionCode`, counting pions
and nucleons in the primary hadronic system *before* FSI. It is the route to a
like-for-like GENIE/NEUT comparison
([physics.md](../physics.md#the-neut-code-as-a-candidate-cross-generator-axis)).
Two quirks: `imd` and `nuel` get placeholder codes 9 and 59, and MEC events have
`neut_code` 0 except CC MEC (±2), since NEUT has no NC MEC mode.

The gst tree has 99 branches; others that are present but unused:

| Branch | What it is |
|---|---|
| `nuance_code` | NUANCE-equivalent mode |
| `resid` | Resonance ID for RES events (0 = P33(1232), …) |
| `dfr`, `imd`, `imdanh`, `singlek`, `nuel`, `amnugamma`, `charm`, `em`, `hnl` | Process flags outside our five |
| `hitqrk`, `sea`, `resc` | Struck quark, sea-quark flag, rescattering code |
| `W`, `x`, `y`, `Q2` / `Ws`, `xs`, `ys`, `Q2s` | Lepton-only vs. selected kinematics (below) |

**gst `W` is not an invariant mass.** `gNtpConv.cxx` computes it as `W² = M² +
2Mν − Q²` with `M = kNucleonMass`, the same lepton-only formula as our `w_gev`.
`Ws` is GENIE's selected W, our `w_true_gev`. GENIE also fills `W`/`x`/`y` for
coherent events, where the common output blanks them.

## Model notes

### The RES/DIS boundary is the tune's `Wcut`

GENIE has **no shallow-inelastic category**. The region is shared between two
generators joined at `Wcut` (`config/CommonParam.xml`):

```
For W >  Wcut : RES -> 0,   +  DIS                -> full
For W <= Wcut : RES -> full + `DIS' (non-RES bkg) -> modified by DIS-HMultWgt-* params
```

Below `Wcut` the resonant piece is the full Rein–Sehgal/Berger–Sehgal
calculation, and the *non-resonant* piece is produced by the DIS generator with
the KNO multiplicity tune applied (`KNOTunedQPMDISPXSec`, `AGKYLowW2019`). Above
it, RES is off and DIS runs unmodified. Consequences for the output:

- `res` stops sharply at `Wcut` in `w_true_gev`; `dis` does not start there. The
  non-resonant background carries about half of the DIS cross section below
  `Wcut`, so `res` and `dis` overlap everywhere beneath it.
- Roughly 9% of `dis` events have W < 2 GeV.

**`Wcut` is per tune, not per build.** It is read from the tune's own
`config/<tune>/CommonParam.xml` (`NonResBackground` parameter set):

| Tune | `Wcut` (GeV) |
|---|---|
| `config/CommonParam.xml` (global default), `G21_11*`, `MK19_00a`, `EX00_00a` | 1.7 |
| `G18_10a_02_11b`, `AR23_20i`, `G24_20*`, `N24_20i` | 1.809 |
| `G18_10a_02_11a` and the other `*_02_11a` tunes of the G18_02/G18_10 families | 1.927862 |
| `G18_01a_02_11a`, `G18_01b_02_11a` | 2.2802 |

Quoting 1.7 for a tune that does not use it puts the boundary in the wrong place
by up to 0.6 GeV.

### Charm quasi-elastic events are labelled `qel`

GENIE simulates charm QE production, `ν_μ n → μ⁻ Λ_c⁺` (and `Σ_c`), flagged
with both `qel` and `charm`. They are labelled `qel`, consistent with the
taxonomy, which puts a charmed baryon in a bucket otherwise made of nucleons.
They appear in `w_true_gev` as discrete lines at the Λ_c⁺ (2.285 GeV) and Σ_c
(2.453 GeV) masses, above the proton peak. They need E_ν above ~2.6 GeV and make
up ~0.4% of `qel` on a 0.5–5 GeV flux. No other generator produces them, so a
`qel` sample above W ≈ 1.5 GeV is GENIE-specific and entirely charm.

## Reweight universes and variations

A job with a `genie.universes` and/or `genie.variations` block (schema in
[../configuration.md](../configuration.md#reweight-universes-and-variations))
gets a third stage after `gevgen` and `gntpc`. It writes one weight per event per
universe, σ_universe/σ_tune at the event's stored kinematics, into
`events/universe_weights`, and one weight per event per switch variation into
`events/variation_weights`. The sampling conventions are in
[../physics.md](../physics.md#reweight-universes). Universes require code version
R-3_06_00.

Every dial is a **scale on the generation tune's own value**: central 1, and
`sigma` is a fractional error. GENIE's own 1σ table does not enter.

### Supported dials

Dials outside this table fail config validation. Every one is listed, at
GENIE's default 1σ and with its incompatibilities as comments, in
`configs/schema/genie_dials.yaml`, a block to copy into a job. Every dial passed
a per-dial check on a real sample ([Validation](#per-dial-checks)): the first
round on G18_10a_02_11a numu CC events, the dials marked † (added 2026-10-23)
on AR23_20i and G18_10a_02_11a samples of the probe and current they act on.

| Dials (config names) | Engine | Moves | GENIE default 1σ |
|---|---|---|---|
| `MaCCQE` | CCQE, `kModeMa` | `qel` (closure-tested) | ±3 % |
| `NormCCQE` | CCQE, default mode | `qel` | +20 / −15 % |
| `ZNormCCQE` † | CCQE, z-expansion mode | `qel` | +20 / −15 % |
| `ZExpA1CCQE`–`ZExpA4CCQE` † | CCQE, z-expansion mode | `qel` (axial form factor coefficients a₁–a₄) | ±14, ±67, ±100, ±75 % |
| `CoulombCCQE` † | CCQE, any mode | `qel` (Nieves Coulomb potential scale) | ±20 % |
| `MaCCRES`, `MvCCRES` | CCRES, `kModeMaMv` | `res` | ±20 %, ±10 % |
| `NormCCRES` | CCRES, default mode | `res` | ±20 % |
| `NonRESBGv{p,n}CC{1,2}pi` | non-resonant background | `dis` events with W < 2 GeV only | ±50 % each |
| `NonRESBGv{p,n}NC{1,2}pi`, `NonRESBGvbar{p,n}{CC,NC}{1,2}pi` † | non-resonant background | as above | ±50 % each |
| `AhtBY`, `BhtBY`, `CV1uBY`, `CV2uBY` | DIS (Bodek-Yang) | `dis` | ±25 %, ±25 %, ±30 %, ±40 % |
| `MaCOHpi`, `R0COHpi`, `NormCCCOH` | COH | `coh` | ±40 %, ±10 %, ±100 % |
| `NormCCMEC` | MEC | `mec` | ±50 % |
| `MFP_pi`, `MFP_N` | hA2018 FSI | final state; σ-preserving | ±20 % each |
| `Fr{CEx,Inel,Abs,PiProd}_{pi,N}` | hA2018 FSI fates | final state; σ-preserving | π: ±50, ±40, ±30, ±20 %; N: ±50, ±40, ±20, ±20 % |

**Choosing `sigma`.** The last column is GENIE's own uncertainty
(`config/GSystUncertaintyTable.xml`, R-3_06_00; one file shared by every tune),
a fractional error on the tune's value. Use it as is in linear mode, or as an
approximate log-space sigma with `log: true`. Two caveats:

- **Asymmetric entries.** `NormCCQE` and `ZNormCCQE` are +20 / −15 %, and a
  universe throw is symmetric, so one value has to be chosen
  (`genie_dials.yaml` takes the larger side).
- **Non-positive throws.** At ±100 %, `NormCCCOH` in linear mode throws a
  non-positive scale in about one universe in six, which fails the run. Use
  `log: true`.

### Switch variations

`genie.variations`, where 0 is the tune and 1 is the alternative:

| Dial | 1 means | Effect at 1 (mean weight) |
|---|---|---|
| `RPA_CCQE` | Nieves CCQE with RPA off | `qel` 1.063 |
| `XSecShape_CCMEC` | the Empirical MEC model's (T_l, cos θ_l) shape at the tune's MEC total σ | `mec` 1.025, single weights 0–8.5 |
| `DecayAngMEC` | MEC nucleon-cluster decay as 3 cos²θ about q (0: isotropic) | `mec` 0.979 |
| `Theta_Delta2Npi` | isotropic Δ → Nπ decay (0: the Rein-Sehgal distribution the engine assumes) | `res` 1.013 |

### Constraints

Validation enforces these:

- `MaCCQE` and `NormCCQE` select incompatible CCQE engine modes, so they cannot
  be combined. The same holds for `MaCCRES`/`MvCCRES` with `NormCCRES`.
- Of each FSI fate group, at most three may be varied. GENIE needs one fate left
  as the "cushion" that absorbs unitarity.

Validation also **warns**, once per flavor and current, about non-resonant
background dials that cannot act on a job. `GReWeightNonResonanceBkg` picks the
dial per event from the interaction type, probe and hit nucleon
(`GSyst::RBkg`), so `NonRESBGv*` gives weight 1 on antineutrino events,
`NonRESBGvbar*` on neutrino events, and the `NC` (`CC`) dials in a CC-only
(NC-only) run. One block holding all 16 can therefore serve every flavor, and
universe *k* is the same throws in all of them.

One effect is expected and not an error: the fate weights are 0 for an event
whose cushion fate's scale factor is driven below 0. At +20 % on one nucleon
fate this hits 10–50 of 20k events.

### Which dials a tune allows

Each engine takes its default model from the generation tune, so whether a dial
works depends on the tune. A tune's models are fixed by its comprehensive model
configuration (CMC), the first two fields of its name (`G18_10a` for
`G18_10a_02_11a`). `validate-config` checks every dial against the job's CMC. It
refuses CMCs that do not set all six models (EM-only, HNL, dark-matter and
high-energy configurations) as unknown.

**Refused.** The last column lists the R-3_06_00 CMCs each rule refuses.

| Dials | Need | Why | Refused CMCs |
|---|---|---|---|
| `MFP_*`, `Fr*` | hA2018 FSI | `GReWeightINuke`'s constructor exits for any other FSI model | hN2018, INCL, Geant4 and the old hA: G00_00a/b/z, G18_01b, G18_02b–d, G18_10b–d, G18_10j, G21_11b–d, G24_20j–l, GDM18_00a/b |
| `MaCCQE`, `NormCCQE` | a dipole axial form factor | the CCQE engine accepts them only with a dipole (or running-M_A) form factor; the SuSAv2 hybrid and linear-combination QE models do not set one | z-expansion: AR23_20i, G18_10i, G18_10j, G24_20i–l, N24_20i; SuSAv2 QE: G21_11a–d; GPRD18_10a |
| `ZNormCCQE`, `ZExpA*CCQE` | a z-expansion axial form factor | the CCQE engine switches itself to `kModeZExp` only when the tune's form factor is `ZExpAxialFormFactorModel`, and handles these dials only there | dipole: G00_00a/b/z, G18_01a/b, G18_02a–d, G18_10a–d, GDM18_00a/b; SuSAv2 QE: G21_11a–d; GPRD18_10a |
| `CoulombCCQE` | the Nieves CCQE model | the engine sets `CoulombScale` on a copy of the tune's CCQE model; only `NievesQELCCPXSec` reads it (in its Coulomb potential), so elsewhere every weight would be 1 | as for `RPA_CCQE` |
| `RPA_CCQE` | the Nieves CCQE model | it switches the `RPA` flag of the tune's CCQE model, which only `NievesQELCCPXSec` has; under any other model every weight would be 1 | Llewellyn Smith: G00_00a/b/z, G18_01a/b, G18_02a–d, GDM18_00a/b; SuSAv2 QE: G21_11a–d; GPRD18_10a |
| `XSecShape_CCMEC` | a MEC model other than Empirical | the alternative shape is the Empirical model's own, so under an Empirical-MEC tune the column equals the nominal | G00_00a/b/z, G18_01a/b, G18_02a–d, GDM18_00a/b |

**Warned.** If the model a dial's engine reads differs from G18_10a's,
validation logs one warning per dial and CMC but accepts the dial. Examples:
`NormCCMEC` under SuSAv2 MEC (AR23_20i, N24_20i, G21_11*, G24_20*), `MaCCRES`
under Rein-Sehgal RES (G18_01*). The DIS dials never warn: every CMC uses the
same DIS model as G18_10a.

### Rejected and untested dials

- **`FormZone`:** not σ-preserving under hA2018, and its engine reports events
  without a valid INTRANUKE rescattering code.
- **`AGKYxF1pi`, `AGKYpT1pi`:** not σ-preserving, with single weights up to 5.
- **`DISNuclMod`:** its `CalcWeight` exits for any non-zero value.
- **`FrElas_*`:** not a fate in this Reweight version.
- **Untested, so not accepted:** every dial not listed above, among them the
  NC dials `MaNCRES`, `MvNCRES`, `NormNCRES`, `MaNCEL`, `EtaNCEL`, `NormNCCOH`,
  `NormNCMEC`, and the z-expansion vector form factor dials `ZExpELFF*`.

### Coverage

The universes cover what GENIE Reweight can reweight. GENIE Reweight's own
README states that this is not the full systematic error of any tune. Nuclear
ground state, binding and Pauli blocking have no working knob for these tunes.
The universe spread is therefore a lower bound on GENIE's model uncertainty.

**Runtime** is about 1 ms per event per universe with 22 scale dials. 100
universes on a 20k-event chunk cost ~35 min, comparable to generating the chunk
with G18_10a_02_11a (~25 min). Dials outside the cross-section engines are
cheaper ([Validation](#reweight-runtime)). The z-expansion and Coulomb dials each
re-evaluate the CCQE cross section with a tweaked copy of the model, on top of
the nominal one, so they add to the cost on `qel` events. Alone they are cheap:
the six CCQE dials over 20k AR23_20i events took 0.07 ms per event per column
including startup ([Validation](#per-dial-checks)).

## Known limitations

- **No ν_τ splines** in the shipped `gxspl-NUsmall.xml`; ν_τ runs need a spline
  set that includes it.
- **A tune can pass validation and still fail at run start** if the code payload
  lacks its configuration directory ([Tunes](#tunes-config_version)).
- **Reweight universes require R-3_06_00.** The R-3_04_00 image has no
  `nf_genie_reweight` ([Container build](#container-build)), and the tune-model
  snapshot covers R-3_06_00 only, so `validate-config` refuses `genie.universes`
  and `genie.variations` on any other code version.

---

*Reference: the sections below describe how the adapter works, why, and the
evidence. Users can stop here.*

Files: `generators/genie.py`, `translators/genie.py`, `normalizers/genie.py`,
`setup/Dockerfile.genie`, `setup/apptainer/genie.def`,
`setup/download_genie_xsec.sh`, `setup/genie/nf_genie_reweight.cc`.

## Implementation

### Run stages

1. `gevgen` → `events.ghep.root` (GENIE's native GHEP format).
2. `gntpc -f gst` → `events.gst.root`, the flat tree the normalizer reads.
   `normalize_output` runs this lazily if only the GHEP file exists.
3. With universes or variations: `nf_genie_reweight` over the GHEP file
   ([below](#nf_genie_reweight)).

The command is assembled in `GenieAdapter.build_run_command`:

- **Flux-driven runs need `--cross-sections`.** Given a spectrum via `-f`,
  `gevgen` aborts without precomputed splines. This is the same file the
  normalization reads. Validation guarantees it exists for non-stub runs
  (`ensure_compatible(require_available=not stub_mode)`); the adapter's
  `_resolve_xml_path` only logs a warning and omits the flag if it is missing,
  so a run that bypassed validation fails inside `gevgen`.
- **The `-e` range is padded by a relative `1e-12`.** `gevgen` zeroes every
  flux bin not strictly inside `[emin, emax]` and reconstructs the upper bound
  in floating point; without padding, round-off can silently drop the first or
  last bin.

Like every adapter, `build_run_command` runs a binary found on `$PATH` before considering a container, since a cluster task already runs inside the Apptainer image, which cannot nest ([../apptainer_image.md](../apptainer_image.md)).

Tune discovery is `available_config_versions`, which globs the staged spline
directories; `_normalize_tune` does the separator-insensitive match. The SciSoft
directory is derived from the code version: `R-3_06_00` → `v3_06_00` (dotted
`3.06.00` in the tarball name) under
`https://scisoft.fnal.gov/scisoft/packages/genie_xsec/<upsver>/`; the tune list
is scraped, not hard-coded.

### Flux

`gevgen` **never samples a continuous function.** Every `-f` input becomes a
`TH1D` that `GCylindTH1Flux::GenerateNext` draws from with `TH1::GetRandom`,
uniform within a bin (`Apps/gEvGen.cxx`, `TH1FluxDriver`, R-3_06_00):

| `-f` input | What happens |
|---|---|
| TF1 string (`"x^(-2.0)"`) | `new TH1D(…, 300, emin, emax)` then `FillRandom("input_func", 100000)`: Monte-Carlo *estimated* from 100k entries |
| ROOT file (`file.root,hist[,WIDTH]`) | `Clone()`d verbatim; arbitrary binning preserved; out-of-range bins zeroed; contents multiplied by bin width for a variable-width axis or an explicit `WIDTH` |
| Text file | Rejection-sampled into the same 300-bin histogram |

The framework uses the ROOT branch. A power-law flux is written to the task work
directory as `nf_flux.root`: `GENIE_FLUX_NBINS = 1000` log-spaced bins holding
the flux *density* (~0.27%/bin over 0.1–50 GeV), passed as
`-f nf_flux.root,nf_flux,WIDTH`. Contents are float64 so uproot emits a TH1D,
which is what `gevgen` casts to. A user-supplied ROOT flux is passed through
*without* `WIDTH`, since its content convention is unknown.

**Normalization reads `input-flux.root`**, which `gevgen` writes into its working
directory on every run, on its native binning (`translators/genie._flux_grid`).
Its contents are per-bin integrals, divided by the bin widths on load
(`contents_are_counts=True`). A missing `input-flux.root` is a hard error.

### Cross-section weight

GENIE is unweighted (`gOptWeighted = false` in `Apps/gEvGen.cxx`), so gst `wght`
is always 1.0. Events land in energy with density ∝ `flux(E) · σ_total(E)`, so

```
xsec_weight_i = C / (n_events · phi_hat(E_i))
```

where `C` is the run's flux-averaged total cross section per nucleon, and
`xsec_norm_count` is the chunk's event count. GENIE does not record `C`, so it is
**reconstructed from the staged spline XML**: every `<spline>` whose name matches
`nu:<probe_pdg>;tgt:<target_pdg>;`, regardless of struck nucleon or process, is
summed. That reproduces the `xsec_sum` GENIE computes internally for the initial
state. The XML is streamed line by line (ISO-8859-1) rather than parsed as a DOM,
and knots are interpolated with `np.interp` (`right=knot_x[-1]`, flat above the
last knot).

Three conversions apply:

- **Current filter.** For `cc`/`nc` the sum is filtered on the matching
  `proc:Weak[CC]` / `proc:Weak[NC]` tag
  ([Weak current](#weak-current)).
- **Units.** Raw `<xsec>` knot values are in GENIE's natural units (GeV = 1).
  `GENIE_UNITS_CM2` is built from `hbarc = 1.973269804e-16 GeV·m`, and
  `× 1e38 / GENIE_UNITS_CM2` is exactly what `gNtpConv.cxx` applies.
- **Per nucleon.** The `tgt:` tag is the whole-nucleus PDG code, so the sum is
  divided by the mass number, read off the target PDG code.

A probe with no matching spline raises, naming probe, target, current and XML
path.

### Weak current

`physics.current: cc`/`nc` restricts generation with `--event-generator-list
CC`/`NC`; `inclusive` omits it. **The spline sum must cover exactly the channels
`gevgen` was allowed to generate**, so it is filtered on the same current.
Without the filter a CC run on carbon would be normalized to the CC+NC total, an
overstatement of roughly a third. Both settings derive from the one
`physics.current` value in the translator, so they cannot drift apart.

### gst branches

Branches read: `Ev`, `wght`, `cc`, the class flags `qel`/`res`/`dis`/`coh`/`mec`,
the neutrino momentum `(pxv, pyv, pzv)`, the lepton `(El, pxl, pyl, pzl)`, the
struck nucleon `(En, pxn, pyn, pzn)` with `hitnuc`, the post-FSI particle list
`pdgf`/`Ef`/`pxf`/`pyf`/`pzf` (jagged; includes the residual nucleus), and
`neut_code`. All in GeV, so no unit conversion. For NC events the "lepton"
branches hold the scattered neutrino. `xsec_norm_count` is recorded on the real
path only.

### Log-level mapping

`run.log_level` maps onto `--message-thresholds` via `MESSENGER_PRESETS`:
`verbose` → `Messenger_rambling.xml`, `quiet` → `Messenger_laconic.xml`,
`essential` → laconic plus a project-written overlay raising the `Ntp` stream to
INFO. The overlay XML is written into the work directory and resolves through
`GetXMLFilePath`'s bare-basename fallback from the CWD, so no bind mount is
needed.

### `nf_genie_reweight`

GENIE Reweight ([GENIE-MC/Reweight](https://github.com/GENIE-MC/Reweight)) is a
separate package built against the Generator tree. **R-1_04_02** is used: the
R-1_04_00 release that goes with Generator R-3_06_00, plus a fix that makes the
RES, COH and DIS engines use the phase space stored in the event. It reads the
GHEP file (`gtree`/`gmcrec`), not gst, because it needs each event's stored
kinematics, phase-space tag and FSI history; `events.ghep.root` is kept for this.

Its API: `GReWeight` owns one engine per physics area; dials are set with
`Systematics().Set(GSyst_t, value)`; `Reconfigure()` pushes them into the
engines; `CalcWeight(event)` multiplies every engine's weight. A dial value means
`p = p_tune · (1 + dial · err)`, clamped at 0, where `err` is the 1σ entry in
`config/GSystUncertaintyTable.xml`.

```
nf_genie_reweight <events.ghep.root> <universes.txt> <weights.root>
                  --tune <tune> [--message-thresholds <files>]
```

The first line of `universes.txt` holds one `<GSyst name>:<kind>` per column;
every further line is one column set. The output is TTree `weights`, branch
`weights[N]/D`, one entry per event in input order.

- **`scale` columns** are a multiplier on the tune's value (1 = nominal). The
  driver overrides the 1σ table to 1 for these dials, so GENIE's dial is exactly
  `value − 1`.
- **`raw` columns** pass the value to GENIE unchanged. These are the [0, 1]
  switches.
- It builds the generation tune (`--tune` = the job's `config_version`), because
  each engine takes its default model from the tune.
- It adopts only the engines the dials need, since `GReWeightINuke` calls
  `exit(1)` in its constructor unless the tune's FSI model is hA2018.
- It exits 2 on a dial its table does not know, or one no adopted engine handles
  under the tune. Validation refuses those dials earlier; this is a backstop.
  `CoulombCCQE` passes this backstop under every tune (the engine reports it
  handled regardless of the CCQE model), and `ZExpA<i>CCQE` with i above the
  tune's `QEL-Kmax` is accepted but ignored by the engine; validation's model
  rules and the per-dial checks cover both. The R-3_06_00
  `ZExpAxialFormFactorModel.xml` sets `QEL-Kmax = 4` (a₁–a₄ = 2.30, −0.6, −3.8,
  2.3) and AR23_20i's `CommonParam.xml` does not override it, so under AR23_20i
  all four coefficients act.
- It sets the CCQE engine to `kModeMa` when `MaCCQE` is present. Otherwise the
  engine picks its mode from the tune's form factor (`kModeZExp` under a
  z-expansion form factor, where the `ZNorm`/`ZExpA` dials act).
- It loops universes outer and events inner, so `Reconfigure()` runs once per
  column, and re-reads each event for every column, because the engines modify
  the interaction while computing a weight.
- It recomputes the nominal dσ with the engine's own default model
  (`UseOldWeightFromFile(false)`) instead of reading the stored one
  ([upstream issue 1](#upstream-issues)).

The adapter writes one row per universe (every scale dial at its thrown value),
then one row per variation (that switch at its value, everything else nominal),
into `universes.txt` and `universes.json`, runs the driver, and the normalizer
splits the columns into the two datasets.

**Batches.** The driver's memory grows with every `Reconfigure()`, by about
10 MB per column under AR23_20i ([upstream issue 6](#upstream-issues)). The
adapter therefore runs it on at most `REWEIGHT_BATCH_ROWS` = 100 rows per
process (`universes.batchNNN.txt` → `universe_weights.batchNNN.root`), logs one
line per batch, and joins the batches' columns in row order into
`universe_weights.root`. The batch files are deleted afterwards. A spec of 100
rows or fewer runs as a single process on `universes.txt` itself. Each extra
batch repeats the driver's startup, ~6 s.

Batching does not change the weights: on 1000 H1 events under AR23_20i, columns
0–199 of a 10-batch run of 1000 universes are bit-identical to one process
running those 200 universes, and the run's peak RSS was 1.2 GiB.

**Tune-model snapshot.** Validation uses `GENIE_TUNE_MODELS` in
`src/neutrino_factory/universes.py`, a snapshot of each R-3_06_00 CMC's FSI,
CCQE, RES, COH, DIS and MEC models from `config/<CMC>/ModelConfiguration.xml`. A
test compares it with the payload wherever the payload is present.

## Container build

`setup/apptainer/genie.def` mirrors `setup/Dockerfile.genie` (see
[../containers.md](../containers.md)). Deliberate and non-obvious:

- **ROOT 6.24.08, not newer.** ROOT 6.26+ changed `TXMLEngine::GetAttr()` to
  return `nullptr` where 6.24 returned an empty string, which crashes R-3_06_00's
  `AlgConfigPool::LoadRegistries`.
- **`-Dminuit2=ON` is required.** Without Minuit2, every Nieves-MEC event
  segfaults in `MECGenerator::GetXSecMaxTlctl`. (This was first misattributed to
  x86_64 emulation; native cluster builds ruled that out.)
- **The build stays in place.** GENIE resolves its tunes and data files from
  `$GENIE`, so relocating a `--prefix` install is fragile.
- **Pythia6 sources are pre-staged**: pythia.org answers with a 302 redirect that
  breaks `curl -f -O`. The shared builder is `setup/lib/build_pythia6.sh` (NuWro
  needs it too).
- **GENIE Reweight and `nf_genie_reweight` are built in the builder stage**
  against `$GENIE`. Reweight has no configure step and reads the Generator's
  `Make.config`. The driver's Makefile rule is `grwght1p`'s with the names
  swapped, so its link line cannot drift. The Reweight libraries, driver and
  headers are copied into `$GENIE/lib`, `$GENIE/bin` and `$GENIE/src`. The
  headers matter at run time: without them, loading the ROOT dictionaries prints
  cling "Missing FileEntry" errors for every Reweight class. The Apptainer
  payload exposes the driver as `bin/nf_genie_reweight` through the same wrapper
  body as `gevgen`.
- **Reweight is built only for `GENIE_TAG` R-3_06_00.** Reweight R-1_04_02 goes
  with Generator R-3_06_00 and fails to compile against R-3_04_00, so the
  R-3_04_00 image is built without it. The Apptainer runtime stage adds
  `nf_genie_reweight` to the wrapper set and to `nf-payload.json`'s `binaries`
  only when the builder produced it, so the payload verification in
  `build_apptainer_images.sh` checks exactly what was built.

## Design notes

### The flux histogram is written by hand (2026-07)

The framework used to pass a power-law flux as a TF1 string, assuming GENIE
samples it continuously. A 1M-event run over 0.1–50 GeV then showed a sawtooth in
the event rate below ~1 GeV. Two problems combined:

- The TF1 branch fills only 300 bins from 100k random entries: for E^-2 over
  0.1–50 GeV, ~63% land in the first bin and bins above ~15 GeV have >40%
  Poisson noise, with a different noise realization per chunk (each chunk reseeds
  `gRandom`).
- The weight divided by a *different* grid rebuilt from the run config
  (`flux.to_histogram(nbins=500)`), so events were divided by a flux they were
  never drawn from. This was the sawtooth.

Only the normalization half is needed for correctness: reading `input-flux.root`
gives unbiased weights even against the noisy 300-bin histogram. Writing the
histogram ourselves is a quality fix: it removes the noise, which otherwise
bounds high-energy variance by the histogram's 100k entries rather than the event
count. Falling back to the config flux when `input-flux.root` is missing would
silently restore the bug, hence the hard error.

### Why not the gst `XSec`/`DXSec` branches

Both trace back to `EventRecord::XSec()`, set in
`PhysInteractionSelector::SelectInteraction` to the cross section of only the
*selected channel* at that event's kinematics, never the sum over all channels
and nucleons that governed accept/reject (`xsec_sum`, never persisted). Averaging
a per-channel value does not converge to the total, because channel selection is
correlated with that channel's cross section.

### A probe with no spline is an error (2026-07)

When no spline matched, the translator used to return zeros, so every event
came out with `xsec_weight = 0`: physical-looking, silently unnormalized output.
It now raises.

### Why the shipped Reweight apps are not used

`grwght1p` scans a single dial. `grwghtnp` throws correlated universes, but
adopts engines for only a subset of dials and hands `NormCCRES` the CCQE engine
([upstream issue 2](#upstream-issues)). `nf_genie_reweight` is a driver over the
same engines that adopts exactly the engines the configured dials need.

### Dials are scales with GENIE's 1σ overridden

With the 1σ table set to 1, a dial equals the fractional change exactly, the
config's `sigma` is the only prior, and `log: true` means the same as for NuWro's
norms. GENIE's table, with its asymmetric NormCCQE entry, never enters, so the
metadata fully describes the prior. Dated entry:
[design_decisions.md](../design_decisions.md#reweight-universes-for-genie-and-switch-variations-2026-10).

### Dials are checked against a snapshot, not the payload

The CLI also runs where no GENIE payload is present (the Docker dev setup), and a
check that silently disappears there would let the same config pass in one place
and fail in another. Dated entry:
[design_decisions.md](../design_decisions.md#genie-dials-checked-against-the-tune-at-validation-2026-10).

## Validation

Unless stated otherwise: R-3_06_00 / G18_10a_02_11a, numu CC on C12, E^-2 power
law over 0.5–5 GeV, 20k events, local Docker.

### Spline sum

Against the staged R-3_06_00/G18_10a_02_11a file, 103 splines match numu/Ar40,
covering QEL/RES/DIS/COH/MEC including the dummy 2p2h pair codes. The CC/NC
split for numu/C12 is in
[physics.md](../physics.md#weak-current-1).

### Flux histogram

Over 0.1–50 GeV, `gevgen`'s `input-flux.root` came back with edges bit-identical
to `nf_flux.root`, contents equal to density × bin width, and no zeroed bins:
cloned, not resampled. `xsec_weight · E^-2` is constant to 1.25% across the
events (the residual expected from 0.27%/bin binning), with no step structure.

### `Wcut` is a sharp cut on `w_true_gev`

With `Wcut = 1.927862`, the largest `res` `w_true_gev` is 1.927742 GeV and no
`res` event exceeds `Wcut`, a 1.2e-4 GeV agreement (one event's sampling
granularity). The non-resonant background carries 51% of the DIS cross section
below `Wcut`; 1845 `dis` events (9.3%) have W < 2 GeV. MEC `w_true_gev` lies
entirely above `Wcut`.

### `neut_code` against our labels

25 000 events: 677 `res` events carry multi-π `neut_code` 21/41 (GENIE's RES
generator produced more than one pion), and 471 `dis` events carry single-π codes
11/12/13/31–34 (non-resonant background with exactly one pion). 15 events (0.06%)
are `other`: 12 `imd`, 3 `nuel`. 48 MEC events have `neut_code` 0.

### Charm lines in `qel`

On a numu CC C12 run (0.5–5 GeV), the `qel` `w_true_gev` distribution has three
lines: 0.9383 GeV (7231 events, proton), 2.285 (22, Λc+), 2.453–2.454 (10, Σc).
The charm events sit at E_ν = 2.79–4.71 GeV; 32 of 7263 `qel` events (0.44%).
The largest `qel` `w_true_gev` in the other generators is 1.141 (NEUT), 1.209
(GiBUU) and 1.470 (NuWro).

### Per-dial checks

Each dial was moved alone to scale 1.2 (switches to 1) and accepted only if the
nominal weight is exactly 1, every weight is finite, it moves only the channel it
should, and its mean weight has the expected sign and size. Mean weight over the
moved channel at +20 %:

| Dial | Mean weight |
|---|---|
| `MaCCQE`, `NormCCQE` | qel 1.220, 1.199 |
| `MaCCRES`, `MvCCRES`, `NormCCRES` | res 1.239, 1.456, 1.200 |
| `NonRESBGv{p,n}CC{1,2}pi` | 1–29 % of dis events move; dis mean 1.002–1.058 |
| `AhtBY`, `BhtBY`, `CV1uBY`, `CV2uBY` | dis 1.025, 1.001, 1.010, 1.020 |
| `MaCOHpi`, `R0COHpi`, `NormCCCOH` | coh 1.055, 0.969, 1.200 |
| `NormCCMEC` | mec 1.200 |
| `MFP_pi`, `MFP_N` | all-event mean 0.9992 each |
| `Fr*` fates | all-event mean 0.9995–1.0002 |

Rejections: `FormZone` moved the mean DIS weight to 0.854 and logged FATAL
"INTRANUKE didn't set a valid rescattering code" on 224 of 20k events.
`AGKYxF1pi`/`AGKYpT1pi` gave a DIS mean of 0.986 with single weights up to 2.4
and 5.0. `MaCCQE` under AR23_20i's z-expansion form factor made the driver exit
(25k-event AR23_20i C12 sample).

**The † dials** (2026-10-26) were checked with the same criteria on four
20k-event C12 samples (0.5–5 GeV, E⁻², R-3_06_00) from
`configs/smoke/genie_dialcheck.yaml`, by `scripts/check_genie_dials.py`. No
event outside the expected channel moved, for any dial. Mean weight over the
expected channel at +20 %:

| Dial | Sample | Mean weight |
|---|---|---|
| `ZNormCCQE` | AR23_20i numu CC | CC qel 1.199 |
| `ZExpA1CCQE`–`ZExpA4CCQE` | AR23_20i numu CC | CC qel 0.800, 0.991, 1.009, 1.005 |
| `CoulombCCQE` | AR23_20i / G18_10a numu CC; G18_10a numubar | CC qel 1.003 / 1.003; 0.997 |
| `NonRESBGvbar{p,n}CC{1,2}pi` | G18_10a numubar inclusive | CC dis, W < 2 GeV: 1.050, 1.004, 1.077, 1.043 (35–620 events move) |
| `NonRESBGvbar{p,n}NC{1,2}pi` | G18_10a numubar inclusive | NC dis, W < 2 GeV: 1.070, 1.016, 1.025, 1.059 (67–292 events move) |
| `NonRESBGv{p,n}NC{1,2}pi` | G18_10a numu NC | NC dis, W < 2 GeV: 1.019, 1.072, 1.050, 1.025 (265–1026 events move) |

`ZExpA1CCQE` responds strongly and with the opposite sign to the other
coefficients: at +20 % the median CC qel weight is 0.85 and 8 % of the moved
events fall below 0.5 (minimum 0.04; 2000-event AR23_20i sample). a₁ = 2.30
is the leading coefficient and the axial form factor is negative, so raising a₁
shrinks |F_A|. GENIE varies the four coefficients independently, although a
z-expansion fit correlates them. `CoulombCCQE` lowers the numubar weight where
it raises numu, as the Coulomb potential's sign follows the lepton charge.
A closure of `ZExpA1CCQE` against a directly generated sample, as for `MaCCQE`,
has not been done.

### MaCCQE closure

A QEL-Ma +20 % copy of the tune (0.961242 → 1.1534904, via `GXMLPATH`) gave both
a CCQE spline (`gmkspl`, 30 knots) and a 10k-event CCQE sample generated
directly. The flux-averaged σ_QE ratio from the splines is 1.2217; the mean
reweighted QE weight is 1.2203 ± 0.0014, within 0.1 % per energy bin up to 2 GeV
and 0.7 % in 2–5 GeV (limited by the 30-knot spline). The Q² shape of reweighted
against direct gives χ² = 2.7 for 8 degrees of freedom (unweighted nominal:
χ² = 46.9).

### Reweight runtime

10 universes with 22 scale dials plus `RPA_CCQE` over 20k events took 207 s on
the dev Mac, ~1 ms per event per universe. Generation with this tune runs at
75 ms/event ([../performance.md](../performance.md)). Four non-cross-section dials
over 20k events × 6 columns ran in 37 s including startup.

## Upstream issues

To report to the GENIE team.

1. **The stored dσ does not match the model's own dσ for the RES and COH
   engines.** With G18_10a_02_11a, both engines warn "default dxsec does not
   match dxsec saved in tree" on each of the first 20 events. The recomputed
   value was 1.05–16× (RES, Berger-Sehgal) and 7–38× (COH) smaller than the
   stored one. With the default `UseOldWeightFromFile(true)`, every weight is
   noise: MaCCRES at 1.001 gave RES events a mean weight of 0.61, MaCOHpi at 1.2
   gave 0.10. Reconfiguring the model copy with the unchanged Ma reproduces the
   default exactly, so the model is not the cause. *Workaround:*
   `nf_genie_reweight` recomputes the nominal; MaCCRES at 1.001 then gives
   1.0012.
2. **`grwghtnp` adopts `GReWeightNuXSecCCQE` under the name `xsec_ccres`** for
   `NormCCRES`/`MaCCRESshape`/`MvCCRESshape` (`gRwghtNCorrelatedParams.cxx`,
   ~line 837), so those dials never reach the RES engine.
3. **`grwght1p`'s `--max-tweak` defaults to −5, not +5**
   (`gRwght1Param.cxx`, ~line 584), so a scan without it runs backwards.
4. **The non-resonant background engine hardcodes W < 2.0 GeV**
   (`GReWeightNonResonanceBkg.cxx`, ~line 198). G18_10a_02_11a's `Wcut` is
   1.927862, so DIS events with 1.93 < W < 2.0 GeV are reweighted as background
   although generation did not treat them as such.
5. **`FormZone` is not σ-preserving under hA2018,** and its engine reports
   events without a valid INTRANUKE rescattering code.
6. **Memory grows with every `Reconfigure()`.** Under AR23_20i with 40 scale
   dials, `nf_genie_reweight` reached the 8 GB Slurm limit (MaxRSS 7.98 GiB)
   near column 843 for every nucleus and event count, so the growth is per
   column, not per event. The cgroup then throttled the process instead of
   killing it: each further column took hours, and the 2026-10-26 pilot of
   `configs/high_stats_genie_universes.yml` stalled in all tasks. Measured on
   odslserv01 over 1000 H1 events: peak RSS 728 / 1198 / 2146 MiB for 50 / 100 /
   200 columns, i.e. ~260 MiB plus 9.4 MiB per column. *Workaround:* the adapter
   runs the driver in batches of 100 columns. Not yet narrowed down to an engine.
