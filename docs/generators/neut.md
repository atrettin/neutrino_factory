# NEUT

NEUT's source is access-restricted (`github.com/neut-devel/neut`, granted on
request). The framework runs three NEUT versions: 5.7.0, extracted from a published
image, whose DIS mode distorts high-energy kinematics
([Known limitations](#known-limitations)); 5.4.2 and 6.2.4, both built from source
on top of that same image. NEUT 6 changed the build system and the card format,
but not the output, the mode numbers or the seeding, so it is a `code_version` of
the same generator ([NEUT 6](#neut-6)). It runs in two stages: NEUT's
`neutroot2`, then a project-owned flattener, because NEUT's native output cannot
be read from Python. It has no CC/NC switch; a single current is selected through
its per-channel cross-section scale arrays. The conventions its output is
normalized onto are in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

## At a glance

| | |
|---|---|
| Code versions | `5.7.0-nuint2024` (NEUT 5.7.0 from the `nuisancemc/tutorial:nuint2024` image, pinned by digest); `5.4.2` (built from the `5.4.2` tag); `6.2.4` (built from the `6.2.4` tag, TOML card) |
| `config_version` | NEUT 5: `default` only (`NEUT-MDLQE 2002`, `NEUT-MAQE 1.05`). NEUT 6: `b25` only (shipped `NEUT.Defaults.toml` + hybrid FSI Pauli blocking; [model sets](#neut-6-model-sets)) |
| Sampling | unweighted (rejection-sampled) |
| Raw σ → per nucleon | already 1e-38 cm² per nucleon, no conversion |
| `physics.current` | `NEUT-MODE -1` plus a `NEUT-CRS` slot mask (NEUT 6: `mode_choice_type = "rescaled"` with a named `mode_rescaling` table); `inclusive` is `NEUT-MODE 0` / `"normal"` ([Weak current](#weak-current)) |
| FSI | `NEUT-NEFF 0`, `NUCRES-RESCAT 1`, pinned |
| `run.log_level` | ignored |
| Reweight universes | none |

## Setup

`setup/setup_neut.sh [--jobs N] <code_version>` makes `neut:<code_version>` for
Docker (`--platform linux/amd64`). For `5.7.0-nuint2024` it pulls the source image,
`nuisancemc/tutorial:nuint2024` pinned by digest ([Container build](#container-build)),
~5 GB, and retags it. For `5.4.2` and `6.2.4` it builds
`setup/Dockerfile.neut` on top of that image from the source tarball
`setup/neut/src/neut-<tag>.tar.gz` (git-ignored), which it downloads first with
`gh auth token` when absent; the token needs read access to `neut-devel/neut`.
For Apptainer, `build_apptainer_images.sh` builds the NEUT payload SIF from the
same image like any other generator; a source build needs the tarball placed in
`setup/neut/src/` by hand ([Container build](#container-build)). There is no data
staging step: the cross-section tables ship inside the payload.

## Configuring a job

- **`config_version: default`** means, for NEUT 5, `NEUT-MDLQE 2002` and
  `NEUT-MAQE 1.05`: Smith-Moniz + BBBA05, RPA, Nieves 1p1h, matching the
  NEUT-shipped `neut_5.4.0_nd5_*` cards. NEUT 6 has no `default`; its config
  versions are named after T2K's model sets, and the one available is **`b25`**:
  Benhar's spectral function, Rein–Sehgal, NEUT cascade with hybrid FSI Pauli
  blocking ([NEUT 6 model sets](#neut-6-model-sets)). NEUT 5 `default` and NEUT 6
  `b25` are therefore different physics models.
- **Any flux works.** NEUT is handed a 1000-bin log-spaced histogram the
  framework writes ([Implementation](#flux)).
- **Targets are pure nuclei** (`NEUT-NUMFREP 0`), so channels that need a free
  proton never fire ([Weak current](#weak-current)).
- **Seeds give independent chunks; only NEUT 6 gives bit-exact reruns.** Under
  5.7.0 a chunk re-run with the same seed produces a statistically equivalent,
  not identical, event set ([Known limitations](#known-limitations)). 6.2.4
  reproduces it exactly ([Validation](#seed-reproducibility)).

## Reading the output

### Interaction labels and `is_cc`

`native_interaction_code` is NEUT's signed `mode` (negative for
antineutrinos). The label comes from `INTERACTION_BY_MODE`, keyed on the absolute
mode:

| Modes | Category |
|---|---|
| 1, 51, 52 | `qel`: CCQE plus NC elastic (p, n) |
| 2 | `mec`: CC 2p2h |
| 11–13, 17, 22, 23, 31–34, 38, 39, 42–45 | `res`: resonant 1π, 1γ, 1η, 1K, both currents |
| 21, 26, 41, 46 | `dis`: multi-π (1.3 < W < 2.0) and DIS (W > 2.0), both currents |
| 16, 36 | `coh`: coherent π |

- **NC elastic (51/52) is in `qel`**, matching GENIE, so `is_cc` cannot be
  inferred from `interaction`.
- **`is_cc` comes from the mode**: CC channels are 1..30, NC 31 and up
  (`MAX_CC_MODE = 30`), compared on the absolute value.
- **Single-π modes 11/12/13 lump resonant and non-resonant production**, and no
  flag separates them. They are labelled `res`, whereas GENIE's non-resonant
  single-π lands in `dis`; this is the one SIS mismatch relabelling cannot fix
  ([physics.md](../physics.md#the-shallow-inelastic-region)).
  `resonant_primary` is `-1` for every NEUT event.
- Modes 15/35 (diffractive π) have no entry and would fall through to `other`;
  they cannot occur in this framework's runs.

### Struck nucleon and `w_true_gev`

NEUT emits no invariant mass of its own; both W columns are derived. `p_N` is the
sum of the struck initial-state nucleons (`nuc_*_gev`): one normally, the
correlated pair for 2p2h, matching GENIE's two-nucleon cluster. NEUT stores the
nucleon **on shell**, unlike the other generators, so `qel` `w_true_gev` sits
~50 MeV above the nucleon mass rather than on it
([physics.md](../physics.md#how-each-generators-qe-vertex-closes)).

### Final-state content

Read from the `NeutVect` particles with `fIsAlive && fStatus == 0` ("Normal"),
selected by flag rather than index: the array also holds the initial-state
nucleons (status −1) and particles killed or replaced during the cascade (e.g.
3 = absorbed).

## Model notes

- **Multi-π vs DIS naming.** The card's "CC DIS 1320" is the 1.3 < W < 2.0
  region and is NEUT's *multi-π* mode 21; "CC DIS" (W > 2.0) is mode 26. NUISANCE
  titles them "Multi π (1.3 < W < 2.0)" and "DIS (W > 2.0)".
- **The flux binning is a resolution setting.** NEUT draws energies uniformly
  within a flux bin, so the reconstructed σ(E) has no structure finer than the
  1000 log bins (~0.62%/bin over 0.1–50 GeV) the framework hands over.

## NEUT 6 model sets

NEUT 6 ships one defaults card but T2K names two model sets, B25 and A25. The
source for this section is the release-validation talk linked from the 6.2.4
release notes: L. Pickering, *NEUT 6.2 Status & Validations*
([slides](https://github.com/user-attachments/files/34908823/NEUT6.2Validations.3.pdf),
login to GitHub required), cited below by slide number. The talk validates 6.2.2.
Each claim marked *(source)* was checked against the 6.2.4 source.

| Model set | Slides | QE | Single π | DIS / multi-π | FSI | `config_version` |
|---|---|---|---|---|---|---|
| **B25**, "T2K legacy OA model" | 3 | Benhar SF | Rein–Sehgal | custom + PYTHIA 5.7 | NEUT cascade + hybrid Pauli blocking | `b25` |
| **A25**, "T2K state-of-the-art model" | 17 | ED-RMF | DCC, with binding energy (Eb) on | not listed | not listed | `a25` — blocked |
| A25 with DCC Eb off | 18 | as A25 | DCC, Eb off | — | — | `a25_Eb_off` — blocked |

**`b25` is NEUT 6's shipped defaults plus hybrid FSI Pauli blocking.**
`NEUT.Defaults.toml` already selects QE `model = "rome"` (Benhar SF),
`ResPP.model = "rein_sehgal"` and `cascade_model = "default"`, but sets
`pauli_blocking_fsi = "on"`. B25 lists hybrid blocking, so the card adds
`[neut.target.nuclear_effects] pauli_blocking_fsi = "hybrid"` *(source)*. Hybrid
blocking scales the nucleon cascade's Pauli blocking by a table,
`crsdat/PB_scale_factor_table.root`. The defaults card describes it as reducing
blocking for nucleons below 150 MeV kinetic energy (`GetScaleFactor.cc`)
*(source)*. Slide 3 lists only four components, so any other B25 setting that
differs from `NEUT.Defaults.toml` is not captured. No B25 or A25 card ships in
the 6.2.4 source.

**A25 is blocked on ED-RMF**, whose tables the project cannot obtain
([Known limitations](#known-limitations)). DCC itself is not blocked: its tables
(`crsdat/DCC1pi`, 86 MB in the source tree) ship with NEUT *(source)*.

**DCC Eb is the binding energy in DCC single-pion production.** The card key is
`[neut.target.nuclear_effects] spp_binding_energy = "on"|"off"`, and the shipped
default is `"off"` *(source)*. With `"on"`, the DCC branch makes two changes
(`ResPP/fnspicrs.F`, `ResPP/dcc1pi/dcc_vecgen.F`) *(source)*:
- it reads bound-nucleon rather than free-nucleon DCC cross-section tables;
- it gives the struck nucleon an effective mass lowered by a binding energy: 34
  MeV under RFG, or the Fermi energy plus 7 MeV under LFG (the default), and not
  for ν_τ.

Nothing outside the DCC branch reads the switch, so it has no effect under
Rein–Sehgal, and is irrelevant to `b25`. Slide 18 shows its size for CC1π⁺
(mode 11), compared with NEUT 6.1.4 A25:
- with Eb on, 6.2.2 is 0.89× in total;
- the deficit is concentrated below ~1.5 GeV, with a ratio near 0.6 at 0.5 GeV;
- with Eb off, 6.2.2 reproduces 6.1.4 exactly (1.00×, χ² = 46/47).

Eb is on by default in A25 (slide 18).

**What changed from NEUT 5 to B25** (6.2.2 against t2k/5.6.4.6, slides 4–15). The
authors mark every item "understood/expected":

| Observable | Change | Attributed to |
|---|---|---|
| Final-state nucleon momenta, all modes | Mode-1 proton spectrum differs below ~0.3 GeV/c; it agrees with FSI off | Hybrid Pauli blocking in FSI (slide 5) |
| NC elastic Q² (modes 51, 52) | Shape change, normalization 0.99× | Updated SF for NC scattering (slide 7) |
| CC1π (modes ±11, 12, 13) | Small, at low lepton momentum | neut PR #72 (slide 9) |
| ν̄ single-γ (modes −17, −38) | 1.10× and 1.12×; cross section now starts at ~0.5 GeV, where NEUT 5's was zero | neut PR #214, a fix — the tables have cross section at 0.5 GeV (slide 12) |
| 2p2h (mode 2) | Number of nucleons leaving the nucleus shifts slightly | Possibly neut PR #172; accepted as small (slide 14) |

**What changed in A25 from 6.1.4 to 6.2.2** (slides 17–20):
- ED-RMF and DCC received significant fixes, but only single-π production is
  expected to move, through Eb.
- A bug in the Nieves 1p1h code was fixed: it used the neutron density where
  the proton density belongs. Relative to 6.1.4 A25 on C, the ν̄_μ CCQE cross
  section on Ar went from 1.07× to 0.86×, while ν_μ on Ar moved only from 1.09×
  to 1.11×. The bug matters for non-isoscalar nuclei in antineutrino mode
  (slide 20).

## Reweight universes

NEUT has no reweight support in this framework.

## Known limitations

- **DIS kinematics are wrong above ~10 GeV in 5.7.0.** NEUT 5.7.0 rejects every mode-26/46
  (W > 2 GeV) event whose lepton is within θ < 2.6° of the neutrino in the
  struck nucleon's rest frame (cos θ > 0.999), and generates a replacement. This
  removes Q² ≲ 0.002·E²(1−y), about 30(1−y) GeV² at 125 GeV, so the low-x, low-y
  corner is empty. The DIS cross section is roughly preserved, but its events pile
  up at high y and high x; the light-cone w (≈ y for DIS) of an inclusive sample
  shows the W < 2 GeV channels as an isolated spike at w ≈ 0, then a gap, then a
  distribution rising towards 1. Negligible below ~5 GeV, clearly biased by
  10–20 GeV; QE, 2p2h, RES and multi-π are unaffected. Fixed in NEUT 5.9.0
  ([upstream issue 4](#upstream-issues), [Validation](#dis-lepton-angle-cut)).
  5.4.2's `nejet.F` has no forward cut, only a backward one (`COSTH < -0.75`);
  its high-energy DIS kinematics have not been measured.
- **NEUT 5.4.2 reports itself as 5.4.1** (`neut-config --version`, library
  suffix `.541`): the tag's `configure.ac` was not bumped. Likewise **6.2.4
  reports itself as 6.2.0**: its `CMakeLists.txt` says `project(NEUT VERSION 6.2.0)`.
- **5.7.0 is not bit-reproducible from its seed.** Different seeds give different,
  independent event sets, so chunking is safe; but two runs with the *same* seed
  diverge after a few events. 6.2.4 does not show this; 5.4.2 was not tested. Configs are reproducible in distribution, not in
  individual events. This is inside NEUT ([upstream issue 1](#upstream-issues)).
- **A very wide topmost flux bin is truncated**: energies are drawn over only
  part of the bin. The production binning (top bin ~0.17 GeV wide at 50 GeV) is
  two orders of magnitude narrower than the widths that showed it
  ([upstream issue 2](#upstream-issues)). Not worked around, since any workaround
  would distort the requested flux.
- **Paths are limited to 80 characters** inside NEUT. The framework hands NEUT
  only bare filenames, so deep work roots are fine; the one absolute path,
  `$NEUT_CRSPATH` (~62 characters), would break with a much longer
  `code_version` ([Implementation](#paths-must-be-bare-filenames)).
- **A channel with no cross section in the energy range aborts the run** with
  "Looks like there's no cross-section in your provided energy range!". The
  framework's masks never enable such a channel.
- **NEUT 6's ED-RMF QE model (`model = "rmf"`) cannot run.** The model is
  compiled, but it reads hadron-tensor tables (`Hmunu_*.bin.gz`) that NEUT does
  not ship: its installer fetches them from `neut-devel/neut-data`, a separate
  private repository this project has no access to. The 6.2.4 image therefore
  has only the small `rmfData/{maxDiff,totXsec}` tables (828 KB), and a card
  selecting `rmf` would stop with a `runtime_error` when it opens the first
  tensor file (`src/models/contrib/rmf/RMFTensor.cpp`). This blocks T2K's A25
  model set. Access to `neut-data` would unblock it ([NEUT 6](#neut-6)).
- `config_version` is `"default"` only for NEUT 5 and `"b25"` only for NEUT 6;
  `a25` and `a25_Eb_off` wait on the ED-RMF tables above
  ([NEUT 6 model sets](#neut-6-model-sets)).
- `run.log_level` is ignored.

---

*Reference: the sections below describe how the adapter works, why, and the
evidence. Users can stop here.*

Files: `generators/neut.py`, `translators/neut.py`, `normalizers/neut.py`,
`setup/neut/nf_flatten.C`, `setup/setup_neut.sh`, `setup/apptainer/neut.def`,
`setup/Dockerfile.neut`, `setup/neut/build_neut.sh`.

## Implementation

### Run stages

1. `neutroot2 neut.card events.neut.root`: reads a plain-text card of
   `KEY value` lines (`C` in column 1 marks a comment), a flux ROOT file, and a
   RANLUX seed file.
2. `nf-neut-flatten` (project-owned) → `events.flat.root`, the flat tree the
   normalizer reads. `normalize_output` runs this lazily if only the raw file
   exists.

Artifacts in the work directory: `neut.card` (NEUT 6: `neut.toml`), `flux.root`,
`ranseed.dat`, `events.neut.root`, `events.flat.root`. NEUT 6 also leaves
`nf_flux_o.root`. It writes the flux and event-rate histograms there first and
then copies them into `events.neut.root` (`loadflx_`, `NEUTROOT::AddFlux`).

Like every adapter, `build_run_command` runs a binary found on `$PATH` before considering a container, since a cluster task already runs inside the Apptainer image, which cannot nest ([../apptainer_image.md](../apptainer_image.md)).

`CONFIG_VERSION_CARDS` in `translators/neut.py` maps `config_version` to card
keys. A new parameter set is a new entry there plus a new `config_versions` entry
on the adapter.

### Paths must be bare filenames

**NEUT reads filenames into 80-character Fortran buffers and truncates anything
longer without complaining.** The failure surfaces much later as `Fortran runtime
error: End of file`. A cluster chunk work directory alone is ~90 characters.

Everything NEUT is handed is therefore a **bare filename** resolved against the
working directory: the card, its `EVCT-FILENM 'flux.root'`, the output, and
`$RANFILE` (`ranseed.dat`). `local.run_task` launches with `cwd=work_dir`, and
the Apptainer payload wrapper deliberately does **not** `cd`. An absolute path
anywhere in this chain is a latent bug that only appears on deep work roots.

The one absolute path NEUT still receives is `$NEUT_CRSPATH`, set by the payload
wrapper to `/opt/nf/generators/neut/<code_version>/neut/share/neut/crsdat` (~62
characters). `NEUT-CRSPATH` is omitted from the card so the tables resolve from
that variable, which both container images set.

### Seeding

`NEUT-RAND 0` makes NEUT seed RANLUX from the file named by `$RANFILE` (`1`
would seed from the clock). That file is read with a Fortran `(5X,5I12)` format:
**five rows of five 12-column integers, each preceded by five spaces**. Only the
first value is used as the RLUXGO seed, but all 25 must be present or the read
hits end-of-file.

### Flux

NEUT has no function-flux driver, so every flux goes through `Flux.to_histogram`
and NEUT is handed a histogram the framework wrote (`EVCT-MPV 3`,
`EVCT-FILENM 'flux.root'`, `EVCT-HISTNM 'nf_flux'`). `FLUX_NBINS = 1000`,
`FLUX_SPACING = "log"`.

- **Bin contents are per-bin integrals (density × width).** NEUT picks a bin in
  proportion to its raw content and ignores the widths, and has no equivalent of
  GENIE's `WIDTH` flag. This also makes the `evtrt`/`flux` integral ratio a
  correctly flux-weighted average on any binning, so
  `NeutNormalizer._flux_averaged_xsec` needs no width factor.
- **Unit traps on the card.** `EVCT-PV` (the fixed/uniform energy range, unused
  here) is in **MeV**, whereas the flux histogram is read in the unit declared by
  `EVCT-INMEV`, `0` for GeV.
- **Normalization reads NEUT's own copy.** `NeutNormalizer` loads the
  `flux_<flavour>` histogram NEUT copied into its output, on its native binning
  (`contents_are_counts=True`); `translators/neut._flux_grid` keeps that binning.
  There is no fallback to the configured flux.

How NEUT samples, established from the 5.7.0 binary since the source is not
distributed: `neutroot2`'s `rndenuevtrt_` calls `Ufm2TH1dist::GetValue`, which
inverts the cumulative of the `evtrt` (flux × σ) histogram, i.e. `TH1::GetRandom`
semantics. A bin is chosen in proportion to its raw content, and within it the
energy is uniform in E, with no σ(E) dependence ([Validation](#flux-sampling)).

### Cross-section weight

NEUT generates unweighted events (`weight` = 1.0). Events are distributed with
density ∝ `flux(E) · σ_total(E)`:

```
xsec_weight_i = sigma_avg / (n_events · phi_hat(E_i))
```

`xsec_norm_count` is the chunk's event count. **σ_avg needs no external spline
file.** When sampling a flux histogram, NEUT writes both that histogram
(`flux_<flavour>`) and the resulting event rate (`evtrt_<flavour>` = flux × σ)
into its output; the ratio of their integrals *is* the flux-averaged total cross
section, already per nucleon in 1e-38 cm². `NeutNormalizer` reads them and injects
the ratio into the translated config as `flux_averaged_xsec_1e38`;
`compute_xsec_weight` raises if it is absent.

The histogram pair is found by prefix: `_find_histogram` picks the unique
`flux*` / `evtrt*` pair (or the generic `fluxhisto` / `ratehisto`), erroring if
there is none or more than one ([Design notes](#histograms-are-found-by-prefix)).

### Weak current

NEUT has **no CC/NC switch**. `NEUT-MODE 0` is its normal mode (every channel in
proportion to its cross section, i.e. `inclusive`). A single current is selected
with `NEUT-MODE -1` ("input cross section by CRSNEUT"), which multiplies each
channel's cross section by its slot in the 30-element `NEUT-CRS` (neutrino) /
`NEUT-CRSB` (antineutrino) arrays. Zeroing every slot of the unwanted current
restricts generation to the other one. `evtrt` follows the mask, so a masked run
is normalized to its own current.

NEUT 6 fills the same arrays from the card's `[neut.interaction_model]`.
`mode_choice_type = "rescaled"` sets NEMODFLG −1 and reads a factor for each of
59 named channels from `mode_rescaling` (`CC_QE_nu`, `NC_DIS_nubar`, …;
`readcard.cc`, `CardSwitchValues.cc`). The current is the name's prefix, so the
translator needs no slot table (`NEUT6_MODE_NAMES`). NEUT aborts on a missing
name, so all 59 are written.

Both arrays are written on every card, each masked with its own table
(`CRS_SLOT_CURRENTS`), so the run does not depend on which array NEUT consults
for a given beam sign. Slot numbers are not mode numbers
([Design notes](#slot-numbers-are-not-mode-numbers)); the measured mapping is:

| Slot | `NEUT-CRS` (ν) label → mode | `NEUT-CRSB` (ν̄) label → mode |
|---|---|---|
| 1 | CC Q.E. → **1** | CC Q.E. (free) → **1** |
| 2–4 | CC 1π → **11, 12, 13** | CC 1π → **11, 12, 13** |
| 5 | CC DIS 1320 → **21** | CC DIS 1320 → **21** |
| 6–9 | NC 1π → **31, 32, 33, 34** | NC 1π → **31, 32, 33, 34** |
| 10 | NC DIS 1320 → **41** | NC DIS 1320 → **41** |
| 11 | NC elastic → **51** | CC Q.E. (bound) → **1** |
| 12 | NC elastic → **51** | NC elastic → **51** |
| 13 | NC elastic → **52** | NC elastic → **51** |
| 14 | coherent → **16** (CC) | NC elastic → **52** |
| 15 | coherent → **36** (NC) | coherent → **16** (CC) |
| 16 | CC η → **22** | coherent → **36** (NC) |
| 17 | NC η → **42** | CC η → **22** |
| 18 | NC η → **43** | NC η → **42** |
| 19 | CC K → **23** | NC η → **43** |
| 20 | NC K → **44** | CC K → **23** |
| 21 | NC K → **45** | NC K → **44** |
| 22 | N/A → *(no channel)* | NC K → **45** |
| 23 | CC DIS → **26** | CC DIS → **26** |
| 24 | NC DIS → **46** | NC DIS → **46** |
| 25 | CC 1γ → **17** | CC 1γ → **17** |
| 26–27 | NC 1γ → **38, 39** | NC 1γ → **38, 39** |
| 28 | CC 2p2h → **2** | CC 2p2h → **2** |
| 29 | CC diffractive → **15** | CC diffractive → **15** |
| 30 | NC diffractive → **35** | NC diffractive → **35** |

**Slots that never fire in production.** ν slot 22 has no channel. ν slot 11
(NC elastic on a free proton) needs free protons. Slots 29/30 (diffractive) need
free protons *and* `NEUT-DIFPI 1`. The framework's targets are pure nuclei
(`NEUT-NUMFREP 0`) and it never sets `NEUT-DIFPI` (default 0), so modes 15 and 35
cannot occur. Enabling any of these slots alone aborts the run with "Calculated
an empty event rate histogram … Looks like there's no cross-section in your
provided energy range!".

### Flattening (`nf_flatten.C`)

`neutroot2` writes a `neuttree` whose `vectorbranch` holds `NeutVect` objects.
uproot reads their scalar members but fails on the nested `TObjArray` of
`NeutPart` ("invalid class-tag reference"), so NEUT's native output cannot be
read from Python. `setup/neut/nf_flatten.C`, run through
`setup/neut/nf-neut-flatten` and dispatched by `NeutAdapter._run_flatten` through
the same native/container branches as generation, writes an `nf_neut` tree of
plain scalars and copies every TH1 from NEUT's output verbatim. Four-momenta are
converted from MeV to GeV in the macro.

- **The outgoing lepton is found by scan, not at `PartInfo(2)`.** The usual
  layout is [0] beam, [1] struck nucleon, [2] lepton, but 2p2h (mode 2) has two
  initial-state nucleons at [1] and [2], putting the lepton at [3]. The flattener
  scans from index 1 for the first particle with |PDG| in 11..16 and writes
  `pdglep = 0` when there is none, which blanks the kinematics.
- **The struck nucleons are the nucleons before that lepton**: one normally, two
  for 2p2h. They are summed into `nuc_{e,px,py,pz}_gev`, with `n_nuc` recording
  how many. If no lepton is found, the collected nucleons are discarded, since the
  scan then ran past the vertex.
- **The post-FSI list** is written as variable-length branches (`n_fs`,
  `fs_pdg[n_fs]`, `fs_{e,px,py,pz}_gev[n_fs]`), selected on
  `fIsAlive && fStatus == 0`. The buffers hold 200 particles; an event with more
  makes the macro exit with an error rather than truncate. The cascade is on: the
  card pins `NEUT-NEFF 0` (pion FSI; `necard.h` calls it "nuclear effect in O16",
  but it is the general switch) and `NUCRES-RESCAT 1` (nucleon rescattering),
  both NEUT's defaults.
- **The macro frees each entry's particle arrays itself.** NEUT's dictionary
  gives `NeutVect` an old-style generated `Streamer` (its LinkDef has no `+`),
  which reads every `TObjArray` member with `R__b >> fPartInfo`: a new array per
  entry, with the pointer overwritten and the old array and its particles never
  deleted, although `neutvect.h` marks the members `//->`. Before each
  `GetEntry` the macro therefore calls `SetNpart(0)`, `SetNfsiPart(0)`,
  `SetNfsiVert(0)`, `SetNnucFsiVert(0)` and `SetNnucFsiStep(0)`, the calls
  `~NeutVect` makes, which delete the arrays. It also disables `vertexbranch`
  (`NeutVtx`, unused here, same leak). Resident memory then peaks at about
  270 MiB for any chunk size; without the calls it grows by 7.6–14 KiB per entry,
  to 7.4–11.1 GiB for 1e6 entries ([Validation](#flattener)).
- **Progress lines.** Every 100 000 entries, and once after the last, the macro
  prints `nf_flatten: read <i>/<n> entries, RSS <r> MiB`, so a flatten that
  slows down or stops shows in the task's `.out` log, and how far it got.
- **A new attempt starts without event files.** `build_run_command`, and
  `run_stub` in stub mode, delete `events.flat.root` and `events.neut.root` left
  in the work dir, and log a warning for each. `run_task` reuses the work dir on
  retry, and `normalize_output` prefers those files to the stub's JSON, so
  otherwise a keyless flat file from a killed flatten would crash the retry, a
  complete one would be paired with the new events of a `--new-seed` retry, and
  a stub run would normalize an earlier real run's events.
- **Stale flat files are re-flattened.** `normalize_output` reuses an existing
  `events.flat.root`, so a file from an older macro would serve a stale schema.
  The normalizer raises `FlatSchemaError` naming the missing branches, and the
  adapter re-runs the flattener over the still-present `events.neut.root` and
  retries once. Only a deleted native file makes the error final.

`nf_neut` branches: `enu_gev`, `mode`, `nu_p{x,y,z}_gev`, `lep_{e,px,py,pz}_gev`,
`pdglep` (0 → kinematics blanked), `nuc_{e,px,py,pz}_gev`, `n_nuc` (0 →
`w_true_gev` blanked), `pdgnuc` (the first nucleon's PDG, informational), and the
`n_fs`/`fs_*` final-state branches. `pdgnu` and `totcrs` are written but not
read.

## Container build

Both code versions start from `nuisancemc/tutorial:nuint2024`, pinned by digest.
`setup/apptainer/neut.def` bootstraps stage 1 from it and stages only
`/opt/neut` (including `share/neut/crsdat` and `include/`; the headers are needed
because the flattener compiles through Cling) plus the ROOT 6.30/04 build NEUT
links against: ~1.5 GB of the ~5 GB image. `build_apptainer_images.sh` passes
`CODE_VERSION`, which names the payload directory.

For `5.7.0-nuint2024` the image's NEUT is used as is, and the Docker image is the
retagged source image. For `5.4.2`, `setup/neut/build_neut.sh` compiles NEUT in
the image and installs it over `/opt/neut`, so the image's environment
(`PATH`, `LD_LIBRARY_PATH`, `NEUT_CRSPATH`, `neut-config`) and everything
downstream apply unchanged. `setup/Dockerfile.neut` runs it for Docker; stage 1
of `neut.def` runs it when `setup/neut/src/neut-<CODE_VERSION>.tar.gz` exists,
and fails the build when it is missing for any version but `5.7.0-nuint2024`.
The source build needs four adjustments, each of which caused a real failure:

- **CERNLIB layout shim.** NEUT 5.4.2's configure requires the CERNLIB 2005
  layout, `$CERN/$CERN_LEVEL/{bin/cernlib,lib,include/pdf804/pilot.h}`, and links
  `jetset74 pdflib804 photos202 mathlib packlib kernlib`. The image's CERNLIB
  2023 has all six libraries but is installed flat at `/opt/cern` with the PDF
  headers under `include/pdf`; a symlink tree at `/opt/cernshim/2005` presents it
  in the expected layout.
- **Autotools timestamps.** A GitHub tarball gives every file one mtime, so make
  tries to regenerate `aclocal.m4` with `aclocal-1.13`, which the image lacks.
  The generated files are touched in dependency order instead.
- **`-lEve`.** `neutgeom`'s `genev`, `event_rate` and `dumptotpau_nd280` link
  ROOT's Eve, which this ROOT lacks; the flag is removed from the generated
  Makefile. None of the three is used.
- **Missing `return` statements.** `AddNuclei` (`n1p1h/src/N1p1hFortranInterface.C`)
  and `NeutrootTreeSingleton::Destroy` are declared non-void and fall off their
  end. That is undefined behaviour, and g++ 11 at `-O2` compiled
  `AddNuclei` into a fall-through: `neutroot2` aborted with `free(): double free
  detected in tcache 2` right after the Nieves 1p1h setup, before the first
  event (backtrace `AddNuclei` ← `fn_1p1h_n_` ← `fntotpau_` ← `loadflx_`, via
  `addr2line`). The script adds the returns and fails if the patterns no longer
  match.

NEUT is installed directly at `/opt/neut` rather than built elsewhere and moved:
`neut-config` bakes in its install prefix (`INSTALL_ROOT`), which the
flattener's include-directory lookup relies on.

- **The source image is pinned by digest.** `NeutAdapter.CODE_VERSIONS` and the
  def's defaults name `nuisancemc/tutorial@sha256:24239bd216fed290eb60057addbe6df91605eb3bb6f3dd99b023bafb6d766ead`,
  the linux/amd64 manifest that the tag `nuint2024` resolved to when the
  production payload was built ([Validation](#source-image)). A rebuild that
  only ships a new flattener therefore keeps the NEUT and ROOT binaries.
- **Catalog integration.** `NeutAdapter` overrides `is_buildable` to treat a
  `source_image` catalog entry as a source (the base class recognizes only
  `git_ref`), so `build_apptainer_images.sh` picks NEUT up with no
  generator-specific code, and overrides `build_arg` to pass
  `NEUT_SOURCE_IMAGE`.
- **Built from the repo root.** `build_apptainer_images.sh` runs `apptainer build`
  from the repo root so `neut.def`'s `%files` can stage the flattener by
  repo-relative path. No other def has a host `%files` block. Changing the macro
  therefore means rebuilding the NEUT payload SIF and recomposing `nf-base.sif`,
  from a host shell: `bash setup/build_apptainer_images.sh --only neut --force`.
  `nf-dev.sif` is built on `nf-base.sif` and keeps the old macro until it is
  rebuilt with `--dev-tools --force` (adding `--dev-tools` to that command does
  all three). Under local Docker the macro is bind-mounted and takes effect
  immediately.

Three relocation fixes in `%post`, each of which caused a real failure:

- **Three dangling absolute symlinks are repointed.** ROOT's `bin/root` is an
  absolute symlink to `/opt/root/v6-30-04/bin/root.exe` in the source image and
  dangles once relocated, which bash reports as the confusing `root: not found`.
  `%test` runs `root --version` from the payload, so this fails at build time.
- **`neut-config`'s install prefix is repointed**: in 5.7.0 through `NEUT.pc`'s
  `prefix=` (`neut-config` refuses to run when it disagrees with its own
  location), in 5.4.2 through `INSTALL_ROOT` in the script itself.
- The wrapper exports `NF_NEUT_INCDIR` so Cling finds the NEUT headers, plus
  `$NEUT_CRSPATH` and `$RANFILE`.

`/opt/cern` (CERNLIB) is deliberately **not** staged: `ldd` reported it unused,
and it is static (`/opt/cern/lib` holds only `.a` archives), so it is linked
into the binaries. `neutroot2` needs only
`libgfortran5`, `libpcre3`, `liblzma5`, `libz` and `libstdc++`, all in
`nf-base.def`'s runtime baseline. If a missing-library error ever appears at
startup, CERNLIB is the first suspect.

Harmless noise from the flatten stage: `Error in cling::AutoLoadingVisitor:
Missing FileEntry for /opt/neut-src/neutclass/*.h`. The image has no NEUT source
tree, so Cling cannot autoload; the macro still compiles because the wrapper
passes `NF_NEUT_INCDIR`.

## NEUT 6

NEUT 6 is a `code_version` of this generator, not a separate one
([../design_decisions.md](../design_decisions.md), 2026-11). Compared against the
5.4.2 source:

| Area | NEUT 6.2.4 | Effect here |
|---|---|---|
| Build | CMake ≥ 3.17 from the repository root. CERNLIB 2005 is bundled (`ext/cernlib2005`); toml11, FortModGen, NucDeEx and Eigen are fetched at configure time. Installs to the same `bin`, `lib`, `include`, `share/neut/{crsdat,Cards}` layout, except that the Bodek-Yang tables moved to `crsdat/BY/`. | `build_neut.sh` takes a CMake branch when the tree has a top-level `CMakeLists.txt`. It turns off `NEUT_DOWNLOAD_DATA`, `NEUT_WERROR_ENABLED` and NuHepMC. `NEUT_DOWNLOAD_DATA` adds only an install step that runs `src/utils/CLI/neut-fetch-data --model ED-RMF`, which clones the private `neut-devel/neut-data` repository (or fetches from it with a `GITHUB_TOKEN`) into `crsdat/rmfData/tensors/`; the RMF code is compiled either way, but without the tables QE `model = "rmf"` cannot run ([Known limitations](#known-limitations)). `neut-fetch-data` is not installed into `bin/`. NEUT raises its C++ standard to ROOT's (17). |
| Card | TOML (`neutroot2 card.toml out.root`), merged key by key onto `$NEUT_CARDS/NEUT.Defaults.toml` (`toml_h::update_table`). | `NeutTranslator._toml_card` writes only the framework's keys, rendered by `render_toml`. Each mirrors a NEUT 5 card key: probe `species`, `energy_histogram = ["flux.root", "nf_flux", "gev"]`, target nucleon counts, `rng_seed = "file"`, FSI on. |
| Output | Defaults to NuHepMC; `output_format = "neutroot"` gives the same `neuttree` of NeutVect (ClassDef 6 → 8, members only added). The dictionary library is now `libNEUTROOTClass`. | The card pins `neutroot`. `nf_flatten.C` loads whichever of the two libraries exists. |
| Modes, flux, seed | Same `nemodsel` mode table, same `Ufm2TH1dist` sampling and histogram names, same `nerdseed` reading `$RANFILE`. | Normalizer, `compute_xsec_weight` and the seed file are unchanged. |
| Physics defaults | QE: Benhar spectral function (`"rome"`), plus new FSI and de-excitation options. | `config_version: b25` adopts them and overrides only `pauli_blocking_fsi` ([NEUT 6 model sets](#neut-6-model-sets)). |

## Design notes

### A payload extracted from a published image

`code_version` is the NEUT release plus the image tag it came from. Since the
build is not reproducible from source, the source image is the real pin. It is
pinned by digest, not by the tag, because a re-push of the tag would be invisible
and would change NEUT in any rebuild of the payload, including one meant only to
ship a new flattener.

### Building 5.4.2 in the same image

NEUT 5.4.2 needs CERNLIB, which no current distribution packages, and building
CERNLIB 2005 from source is a project of its own. The tutorial image already
carries a CERNLIB with every library NEUT links, together with the ROOT and
gfortran a source build needs, and its NEUT 5.7.0 payload already relies on it.
Building on it reuses that toolchain and the image's runtime environment, at the
cost of tying 5.4.2's build to the same pinned image.

### Histograms are found by prefix

`neutroot2` names the histograms `flux_%s` / `evtrt_%s` with its *own* flavour
token (`numu`, **`numub`** not `numubar`, `nue`, `nueb`, read off the binary), and
falls back to `fluxhisto` / `ratehisto` for a beam it has no token for, e.g. ν_τ.
None of that is a documented contract, so the framework does not encode the
mapping. NUISANCE solves the same problem the same way
(`PlotUtils::GetObjectWithName`). Hardcoding `flux_numu` made every non-numu NEUT
run fail at normalization.

### Slot numbers are not mode numbers

A **mode** is the physics-channel identifier written per event: sparse over
1..52, CC ≤ 30, NC ≥ 31, negated for antineutrinos (`necard.h`). A **slot** is a
position in the dense 30-element `CRSNEUT(30)` / `CRSNEUTB(30)` arrays, ordered
by NEUT's internal enumeration; the shipped cards
(`share/neut/Cards/neut_5.4.0_nd5_O.card`) label it, and in the 5.4.2 source
`neutcore/necrsmode.F` spells out the mode → slot map. They cannot line up:

1. Slots resolve sub-channels that share a mode (free vs bound target): ν slots
   11 and 12 both produce mode 51, ν̄ slots 12 and 13 both produce mode 51.
2. The slot array is dense; the mode space is sparse and meaningful in its ranges.
3. The two arrays disagree with each other: the ν̄ list has a free/bound CCQE
   split at slots 1 and 11, which shifts NC elastic and coherent one slot later.
4. A slot can be empty for one beam sign (ν slot 22).

So the masks are derived from a measurement, not from the mode numbering.

### Why a project-owned flattener

NEUT ships `neutclass_to_tree`, but its `nework` branch is a Fortran leaf-list
containing `pne[100][3]`, which uproot mis-parses (dtype `(3,)` instead of
`(100,3)`); reading it would mean hand-maintaining a 2808-byte numpy dtype that
tracks NEUT's common block. NUISANCE's `nuisflat` produces a flat tree, but links
GENIE, NuWro, LHAPDF and Pythia; the payload would grow from ~1.5 GB to
essentially the whole image.

The 2p2h layout is the reason for the lepton scan: a fixed index would have read
a neutron as the outgoing lepton and silently corrupted Q²/x/y for the entire MEC
channel. Summing the 2p2h pair matches GENIE's two-nucleon cluster, so
`w_true_gev` is one quantity across generators. `n_nuc` is written alongside the
sum so a later change of that policy is a Python change, not a container rebuild.

A stale flat schema raises instead of blanking the missing columns, because a run
of placeholders is indistinguishable from a run of genuinely undefined events.

`INTERACTION_BY_MODE` lists modes explicitly rather than as ranges, so a mode
NEUT adds later falls through to `other` instead of being silently absorbed into
a neighbouring category.

### Log-spaced per-bin integrals (2026-07)

A 1M-event 0.1–50 GeV run with the previous 500 equal-width flux bins showed, below
~1 GeV, a raw `dN/dE` staircase with ~0.1 GeV steps (the bin width) and a σ(E)/E
plot with an inverse sawtooth; NuWro and GiBUU on the same config looked smooth.
The 0.1 GeV steps swallowed the region where σ(E) rises by orders of magnitude.
The fix is 1000 log bins of per-bin integrals, normalized against NEUT's own
stamped flux. Falling back to the configured flux would restore the bug and still
look physical.

## Validation

### Per-nucleon normalization

- **No A scaling.** The same flux on C12, O16, Ar40 and CH gives σ_avg 0.655,
  0.663, 0.675 and 0.636, an isospin-size spread, not the factor ~3.3 a
  whole-nucleus quantity would show between C12 and Ar40.
- **Absolute value.** NUISANCE, reading the same file, reports `Event/Flux :
  6.55288e-39 cm2/nucleon` where the ratio is 0.6552880 (and 1.46774e-38 vs
  1.4677402686 on another flux).
- **Sampling law.** The generated spectrum tracks `evtrt`, not `flux`: summed
  absolute shape difference 0.034 against `evtrt`, 0.95 against `flux`.
- **End to end.** 4000 events on C12: `Σ xsec_weight / bin_width` per energy bin
  reproduces NEUT's own `evtrt/flux` to 1.03 ± 0.05.

### Flux sampling

10 log-spaced bins over 0.1–50 GeV, 30k events: χ²/ndf 0.90 against
`p_b ∝ evtrt_b`, 1.7 × 10⁴ against `p_b ∝ evtrt_b · width_b`. A single flux bin
over 0.2–2.0 GeV, where σ(E) rises by a factor 56, gave a flat `dN/dE`
(χ²/ndf 0.48), mean energy 1.0961 ± 0.0030 GeV against 1.1000 predicted
(uniform-in-log-E predicts 0.7830; `dN/dE ∝ σ(E)` predicts 1.3676).

### Flux binning fix

With 10 log bins of per-bin integrals over 0.1–50 GeV (15k events on C12), the
recovered σ per bin against NEUT's own `evtrt/flux`, over a factor 6700 in σ,
has pull mean +0.06 and rms 0.88. At production binning (40k events, 40 log
analysis bins) the ratio is 1.006 ± 0.044, and the four analysis bins between
0.1 and 0.2 GeV, which all fell inside one bin of the old grid, return 0.0026,
0.0033, 0.0077 and 0.0184, tracking NEUT's σ(E).

### NEUT 5.4.2 against 5.7.0

NEUT 5.4.2 (Docker, `config_version: default`), ν_μ CC on C12, E^-2 over
0.5–5 GeV, 2000 events, against the 20k-event 5.7.0 run on the same config.
The CC mask uses the 5.7.0 slot table unchanged, and every event came out in a CC
mode: 1 (685), 2 (163), 11 (390), 12 (151), 13 (172), 16 (7), 17 (1), 21 (276),
22 (31), 23 (2), 26 (122). σ per nucleon from the `xsec_weight` sum, in 1e-38 cm²:

| E (GeV) | 0.5–1 | 1–2 | 2–3 | 3–5 |
|---|---|---|---|---|
| 5.4.2 | 0.644 | 1.414 | 1.929 | 3.276 |
| 5.7.0 | 0.646 | 1.370 | 2.145 | 3.194 |

The differences are within the 2000-event statistics.

NC (`physics.current: nc`), same flux and target, 2000 events per beam sign,
against 2000-event 5.7.0 runs on the same config. Every event came out in an NC
mode: ν_μ 31 (209), 32 (215), 33 (154), 34 (140), 36 (22), 38 (1), 41 (244),
42 (35), 43 (22), 44 (5), 45 (6), 46 (116), 51 (338), 52 (493); ν̄_μ likewise
only −31…−52. The slot table needs no 5.4.2 variant: `neutcore/necrsmode.F` in the
5.4.2 source maps every CC mode to a slot `CRS_SLOT_CURRENTS` marks CC and every
NC mode to one it marks NC, for both arrays, and leaves ν slot 22 unassigned.
σ per nucleon, in 1e-38 cm² (statistical errors ~4% at low E, ~5% at high E):

| E (GeV) | 0.5–1 | 1–2 | 2–3 | 3–5 |
|---|---|---|---|---|
| ν_μ 5.4.2 | 0.226 | 0.417 | 0.660 | 0.968 |
| ν_μ 5.7.0 | 0.223 | 0.464 | 0.739 | 0.990 |
| ν̄_μ 5.4.2 | 0.068 | 0.173 | 0.279 | 0.497 |
| ν̄_μ 5.7.0 | 0.073 | 0.180 | 0.316 | 0.488 |

The largest gap, ν_μ at 1–2 GeV, is 1.9σ; 5.4.2 sits low at 1–3 GeV in both
currents. Startup time is dominated,
as in 5.7.0, by the Nieves 1p1h tables built for all six flavours before the
first event.

### NEUT 6.2.4

NEUT 6.2.4 (Docker), C12, E^-2 over 0.5–5 GeV, 2000 events per job
(2026-11-23). These runs predate `b25` and used the plain `NEUT.Defaults.toml`,
i.e. `b25` with `pauli_blocking_fsi = "on"`. That setting acts only in the
nucleon cascade, after the cross section is drawn: a `b25` ν_μ CC rerun gave
the same flux-averaged σ to all printed digits (1.16515), and NEUT's parameter
dump showed `fefsipau(integer): 1` (hybrid) with no warnings from the hybrid
scale-factor table.

**Current selection.** Every event of the ν_μ CC job came out in a CC mode: 1 (788),
2 (148), 11 (355), 12 (126), 13 (159), 16 (11), 17 (1), 21 (237), 22 (57), 23 (8),
26 (110). The NC job gave only modes 31–52, and ν̄_μ CC only −1…−26. The
flux-averaged σ, i.e. the `evtrt`/`flux` integral ratio without event statistics,
is 1.16515 (CC) + 0.38271 (NC) = 1.54786, which equals the inclusive job's 1.54786.
So `mode_rescaling` is folded into `evtrt` exactly as `NEUT-CRS` is in NEUT 5.

σ per nucleon from the `xsec_weight` sum, in 1e-38 cm², with statistical errors:

| E (GeV) | 0.5–1 | 1–2 | 2–3 | 3–5 |
|---|---|---|---|---|
| ν_μ CC 6.2.4 | 0.720 ± 0.031 | 1.479 ± 0.062 | 2.198 ± 0.122 | 3.130 ± 0.166 |
| ν_μ CC 5.7.0 | 0.646 | 1.370 | 2.145 | 3.194 |
| ν_μ NC 6.2.4 | 0.239 ± 0.010 | 0.476 ± 0.020 | 0.717 ± 0.040 | 1.071 ± 0.056 |
| ν_μ NC 5.7.0 | 0.223 | 0.464 | 0.739 | 0.990 |
| ν̄_μ CC 6.2.4 | 0.158 ± 0.008 | 0.466 ± 0.020 | 0.922 ± 0.046 | 1.453 ± 0.065 |

The CC cross section at 0.5–1 GeV is 11% (2.4σ) above 5.7.0. Mode 1 (CCQE) makes
up 39% of CC events, against 34% in 5.4.2. The QE model differs between NEUT 5's
`default` and NEUT 6's defaults ([NEUT 6](#neut-6)), but no run has isolated it
as the cause.

### Slot mapping

NEUT 5.7.0, C12 plus 1 free proton, numu/numubar, E^-2 over 0.5–8 GeV. Each slot
was enabled alone with `NEUT-MODE -1`, 400 events generated, and the observed
`mode` read off the flattened tree; every slot that has a channel produced exactly
one mode. Deriving each slot's current from its mode reproduces
`CRS_SLOT_CURRENTS` for all 30 ν and 30 ν̄ slots, and confirms the slots the card
labels only "coherent" (ν 14/15, ν̄ 15/16) as CC-then-NC. A separate run
confirmed that `evtrt` follows the mask.

> **Unverified:** the table lists slots 29/30 producing modes 15/35, which needs
> `NEUT-DIFPI 1`; the measurement setup presumably enabled it, but this is not
> recorded.

### Flattener

Every `NeutVect` entry of a real numu-CC C12 run dumped against the flattened
tree: 50/50 events agree; 49 of 50 had the lepton at index 2. On a 20k-event run,
`n_nuc` is 2 for all 1531 mode-2 events
([physics.md](../physics.md#the-two-w-columns-2026-08-05)).

**Memory.** Peak resident memory of the flattener's ROOT process on numu
inclusive chunk000 files (2026-11): C12, O16 and Fe56 from
`keystone_prod_v3_rejection`, Ar40 from `keystone_prod_v3_smoke`. Peaks are
`VmHWM` from `/proc`, sampled every second; Fe56, whose 17 s run ended between
samples with the leaking macro, is read from `gSystem->GetProcInfo` at the end of
the read loop.

| Target | Entries | Macro without the array cleanup | Current macro |
|---|---|---|---|
| C12 | 1e6 | 7.43 GiB | 269 MiB |
| O16 | 1e6 | 8.45 GiB | 268 MiB |
| Ar40 | 1e6 | 11.08 GiB | 270 MiB |
| Fe56 | 153 847 | 2.19 GiB | 258 MiB |

Without the cleanup, RSS grows linearly with the entry index, by 7.6 (C12), 8.7
(O16), 11.4 (Ar40) and 13.8 (Fe56) KiB per entry, the slope of `GetProcInfo`
between entry 5e4 and the last progress line (O16: 0.61 GiB at 5e4, 4.37 GiB at
5e5, 8.47 GiB at 1e6). A variant that reads without calling `Fill` grows the
same (O16: 8.6 KiB per entry), so the growth is in reading `NeutVect`. The
`SetN*(0)` calls alone leave 0.4 KiB per entry; disabling `vertexbranch` as well
leaves under 0.1 KiB. The current macro's C12 and Ar40 outputs are identical
branch by branch and histogram by histogram to the files the leaking macro wrote.

These peaks match `keystone_prod_v3_rejection` on the cluster, which ran the
leaking macro at `slurm.mem: 8G` (8 GiB); outcomes from `check-status`. Its
1e6-event C12 chunks finished, except three that crashed in `neutroot2`. Every 1e6-event O16 and Ar40 chunk
that did not crash earlier in `neutroot2` hit the time limit in the flatten step,
where the process stalls at the limit instead of being killed. NEUT Fe56 had
1e6-event chunks in three jobs, numubar, nue and nuebar (10 chunks each), and
none of the 30 finished: 26 hit the time limit (those inspected after
`neutroot2` had written all 1e6 events, with a partial `events.flat.root`), 3
crashed earlier in `neutroot2` (SIGFPE) and 1 lost its node. The numu Fe56 job,
65 chunks of 153 847 events, finished. In `keystone_prod_v3_smoke`, Ar40 chunks
of 1e6 events finished at 12G.

### Source image

The pinned digest, `sha256:24239bd2…` (linux/amd64 manifest of
`nuisancemc/tutorial:nuint2024`), is the image the production payload
`neut_5.7.0-nuint2024.sif` (built 2026-10-09) was built from, established
2026-11-17:

- The SIF does not record it: its labels name only the tag
  (`org.neutrino-factory.source`) and the final stage's
  `python:3.13-slim-bookworm` digest, and its embedded def names the tag.
- The build's Apptainer cache (the `APPTAINER_CACHEDIR` that `.env` sets; its
  index was last written by the 2026-10-23 build) holds exactly one
  `nuisancemc/tutorial` manifest, `sha256:24239bd2…`, image config
  `sha256:d1bc8f56…`, created 2024-04-11.
- All 1715 regular files under that image's `/opt/neut` are byte-identical to
  the deployed payload's `neut/` tree, except `NEUT.pc`, which `%post` edits.
- Docker Hub still served it for the tag that day, as the amd64 entry of the
  multi-arch index `sha256:9c779bec…`.

### Seed reproducibility

The log confirms `RANLUX INITIALIZED BY RLUXGO FROM SEEDS <seed> 0 0`, different
seeds diverge immediately, and the same seed reproduces the opening events
exactly, but diverges after ~4 events: NEUT's `Ev.# N SEEDS` trace shows it
consuming a different number of random numbers. Reproduced on native aarch64, on
x86_64 under Rosetta, and with ASLR disabled (`setarch -R`).

NEUT 6.2.4 (Docker under Rosetta, ν_μ CC on C12, 50 events) reproduces all 50
event energies exactly when the same seed file is run twice. A different seed
shares none of them.

### Top-bin truncation

Two-bin histogram `[10, 26.858, 50]`, 2000 events: the lower bin is uniform; the
upper is uniform to ~43.7 GeV and empty above (seven slices: `266 272 257 255 265
29 0`). The same `[26.858, 50]` top bin cut at 39.2 GeV in a ten-log-bin run, so
the cut is not a fixed energy. A single bin over 30–80 GeV fills uniformly to
80 GeV, so it is not a cross-section-table limit; ten equal-width bins over
10–50 GeV (top bin 4 GeV wide) reach 49.99 GeV, so it is not simply "the last
bin". The effect tracks the top bin's width.

### DIS lepton-angle cut

Found in `keystone_prod_v2.1` (NEUT 5.7.0, ν_μ on Fe56, E^-2 over 0.1–500 GeV),
where NEUT's y, Bjorken x and light-cone w disagreed with every other generator
at 100–150 GeV. Measured on the raw `events.flat.root` files, i.e. NEUT's own
four-vectors, bypassing the normalizer:

- **The minimum W of mode 26 grows with energy**: its 1st percentile is
  1.75 GeV at 5–10 GeV, 1.99 at 20–40, 4.18 at 100–150 and 9.4 at 300–500, where
  DIS should start at W ≈ 2 GeV throughout. Median y rises to 0.70 and 0.83 in the
  two highest bands.
- **The missing region is low Q².** The 2% Q² quantile of DIS events
  (W > 2.5 GeV), NEUT over GENIE `G18_01a_00_000` on the same config, is 1.0 at
  3–5 GeV, 1.35 at 10–20, 2.4 at 20–40 and 4.7 at 40–80.
- **The edge is a hard cut.** Boosting each mode-26 event into its struck
  nucleon's rest frame (single-nucleon events, 150k), the largest lepton
  cos θ is 0.999000 in every energy band from 5 to 500 GeV, and no event lies
  above it.

The cut is in NEUT 5.7.0's `src/neutcore/nejet.F`, which generates modes 26 and
46 by running PYTHIA 5.7 (CERNLIB `jetset74`) on the neutrino–nucleon pair. It
rejects events with `RFCOSTH < -0.999 .or. RFCOSTH > 0.999` and calls `PYEVNT`
again. The comment says "center of mass", but the boost is by the struck
nucleon's velocity, so the angle is in the nucleon rest frame (≈ the lab), where
high-energy DIS leptons are mostly forward. With Q² = 2EE′(1−cos θ), the
forward cut removes Q² < 0.002·E²(1−y).

## Upstream issues

Issues 1–3 are not reported, and their causes are unconfirmed.

1. **Same-seed runs diverge** after ~4 events. Likely uninitialized state inside
   NEUT ([Validation](#seed-reproducibility)).
2. **A very wide topmost flux bin is truncated**, at an energy that depends on the
   bin's width ([Validation](#top-bin-truncation)).
3. **Flavour tokens in histogram names are undocumented** (`numub`, `nueb`, and
   the `fluxhisto`/`ratehisto` fallback).
4. **DIS rejects forward leptons** (cos θ > 0.999 in the nucleon rest frame),
   distorting kinematics above ~10 GeV ([Validation](#dis-lepton-angle-cut)).
   Fixed upstream by commit `f7cf2d04` (2024-10-09, PR #80
   `feature/dis_clep_cut`), which keeps only the backward cut
   `RFCOSTH < -0.999`; it is the only change to `nejet.F` after 5.7.0. First
   released in 5.9.0, not in 5.8.0. The framework needs a newer NEUT, built from
   source, to pick it up.
5. **Reading `NeutVect` or `NeutVtx` from a tree leaks every entry's particle
   arrays.** The LinkDef declares both classes without `+`
   (`src/neutclass/neutclassLinkDef.h` in 5.7.0, still so in
   `src/output/ROOT/NEUTROOTClassLinkDef.h` on `develop`, 2026-11), so the
   generated `Streamer` reads each `TObjArray*` with `operator>>` (seen in the
   disassembly of `libNEUTClass.so`) instead of in place. `nf_flatten.C` works
   around it ([Flattening](#flattening-nf_flattenc)). Not reported.
