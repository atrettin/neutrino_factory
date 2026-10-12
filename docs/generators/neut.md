# NEUT

NEUT's source is not public, so the framework runs a NEUT build extracted from a
published image rather than one built from source. It runs in two stages: NEUT's
`neutroot2`, then a project-owned flattener, because NEUT's native output cannot
be read from Python. It has no CC/NC switch; a single current is selected through
its per-channel cross-section scale arrays. The conventions its output is
normalized onto are in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

## At a glance

| | |
|---|---|
| Code versions | `5.7.0-nuint2024` (NEUT 5.7.0 from the `nuisancemc/tutorial:nuint2024` image) |
| `config_version` | `default` only: `NEUT-MDLQE 2002`, `NEUT-MAQE 1.05` |
| Sampling | unweighted (rejection-sampled) |
| Raw σ → per nucleon | already 1e-38 cm² per nucleon, no conversion |
| `physics.current` | `NEUT-MODE -1` plus a `NEUT-CRS` slot mask; `inclusive` is `NEUT-MODE 0` ([Weak current](#weak-current)) |
| FSI | `NEUT-NEFF 0`, `NUCRES-RESCAT 1`, pinned |
| `run.log_level` | ignored |
| Reweight universes | none |

## Setup

`setup/setup_neut.sh` pulls `nuisancemc/tutorial:nuint2024` (~5 GB) and retags it
as `neut:<code_version>` for Docker (`--platform linux/amd64`). For Apptainer,
`build_apptainer_images.sh` builds the NEUT payload SIF from the same image like
any other generator ([Container build](#container-build)). There is no data
staging step: the cross-section tables ship inside the payload.

## Configuring a job

- **`config_version: default`** means `NEUT-MDLQE 2002` and `NEUT-MAQE 1.05`:
  Smith-Moniz + BBBA05, RPA, Nieves 1p1h, matching the NEUT-shipped
  `neut_5.4.0_nd5_*` cards.
- **Any flux works.** NEUT is handed a 1000-bin log-spaced histogram the
  framework writes ([Implementation](#flux)).
- **Targets are pure nuclei** (`NEUT-NUMFREP 0`), so channels that need a free
  proton never fire ([Weak current](#weak-current)).
- **Seeds give independent chunks, but not bit-exact reruns.** A chunk re-run
  with the same seed produces a statistically equivalent, not identical, event
  set ([Known limitations](#known-limitations)).

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

## Reweight universes

NEUT has no reweight support in this framework.

## Known limitations

- **Not bit-reproducible from its seed.** Different seeds give different,
  independent event sets, so chunking is safe; but two runs with the *same* seed
  diverge after a few events. Configs are reproducible in distribution, not in
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
- `config_version` is `"default"` only.
- `run.log_level` is ignored.

---

*Reference: the sections below describe how the adapter works, why, and the
evidence. Users can stop here.*

Files: `generators/neut.py`, `translators/neut.py`, `normalizers/neut.py`,
`setup/neut/nf_flatten.C`, `setup/setup_neut.sh`, `setup/apptainer/neut.def`.

## Implementation

### Run stages

1. `neutroot2 neut.card events.neut.root`: reads a plain-text card of
   `KEY value` lines (`C` in column 1 marks a comment), a flux ROOT file, and a
   RANLUX seed file.
2. `nf-neut-flatten` (project-owned) → `events.flat.root`, the flat tree the
   normalizer reads. `normalize_output` runs this lazily if only the raw file
   exists.

Artifacts in the work directory: `neut.card`, `flux.root`, `ranseed.dat`,
`events.neut.root`, `events.flat.root`.

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

There is **no `setup/Dockerfile.neut`**: the Docker image is the retagged source
image, so the def↔Dockerfile pairing that applies elsewhere does not apply here.
`setup/apptainer/neut.def` bootstraps stage 1 from the source image and stages
only `/opt/neut` (including `share/neut/crsdat` and `include/`; the headers are
needed because the flattener compiles through Cling) plus the ROOT 6.30/04 build
NEUT links against: ~1.5 GB of the ~5 GB image. The def's `CODE_VERSION` must stay
in sync with `NeutAdapter.CODE_VERSIONS`.

- **Catalog integration.** `NeutAdapter` overrides `is_buildable` to treat a
  `source_image` catalog entry as a source (the base class recognizes only
  `git_ref`), so `build_apptainer_images.sh` picks NEUT up with no
  generator-specific code, and overrides `build_arg` to pass
  `NEUT_SOURCE_IMAGE`.
- **Built from the repo root.** `build_apptainer_images.sh` runs `apptainer build`
  from the repo root so `neut.def`'s `%files` can stage the flattener by
  repo-relative path. No other def has a host `%files` block. Changing the macro
  therefore means rebuilding the NEUT payload SIF; under local Docker the macro is
  bind-mounted and takes effect immediately.

Three relocation fixes in `%post`, each of which caused a real failure:

- **Three dangling absolute symlinks are repointed.** ROOT's `bin/root` is an
  absolute symlink to `/opt/root/v6-30-04/bin/root.exe` in the source image and
  dangles once relocated, which bash reports as the confusing `root: not found`.
  `%test` runs `root --version` from the payload, so this fails at build time.
- **`NEUT.pc`'s `prefix=` is repointed**: `neut-config` refuses to run when it
  disagrees with its own location.
- The wrapper exports `NF_NEUT_INCDIR` so Cling finds the NEUT headers, plus
  `$NEUT_CRSPATH` and `$RANFILE`.

`/opt/cern` (CERNLIB) is deliberately **not** staged: `ldd` reported it unused,
and it appears to be statically linked into `libNEUT.a`. `neutroot2` needs only
`libgfortran5`, `libpcre3`, `liblzma5`, `libz` and `libstdc++`, all in
`nf-base.def`'s runtime baseline. If a missing-library error ever appears at
startup, CERNLIB is the first suspect.

Harmless noise from the flatten stage: `Error in cling::AutoLoadingVisitor:
Missing FileEntry for /opt/neut-src/neutclass/*.h`. The image has no NEUT source
tree, so Cling cannot autoload; the macro still compiles because the wrapper
passes `NF_NEUT_INCDIR`.

## Design notes

### A payload extracted from a published image

`code_version` is the NEUT release plus the image tag it came from. Since the
build is not reproducible from source, the image tag is the real pin, and a
re-push of that tag would otherwise be invisible.

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
1..52, CC ≤ 30, NC ≥ 31, negated for antineutrinos (`necard.h`; owned by the
non-public `nemodsel.F`). A **slot** is a position in the dense 30-element
`CRSNEUT(30)` / `CRSNEUTB(30)` arrays, ordered by NEUT's internal enumeration;
the channel per slot is documented only in the shipped cards
(`share/neut/Cards/neut_5.4.0_nd5_O.card`). They cannot line up:

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

### Seed reproducibility

The log confirms `RANLUX INITIALIZED BY RLUXGO FROM SEEDS <seed> 0 0`, different
seeds diverge immediately, and the same seed reproduces the opening events
exactly, but diverges after ~4 events: NEUT's `Ev.# N SEEDS` trace shows it
consuming a different number of random numbers. Reproduced on native aarch64, on
x86_64 under Rosetta, and with ASLR disabled (`setarch -R`).

### Top-bin truncation

Two-bin histogram `[10, 26.858, 50]`, 2000 events: the lower bin is uniform; the
upper is uniform to ~43.7 GeV and empty above (seven slices: `266 272 257 255 265
29 0`). The same `[26.858, 50]` top bin cut at 39.2 GeV in a ten-log-bin run, so
the cut is not a fixed energy. A single bin over 30–80 GeV fills uniformly to
80 GeV, so it is not a cross-section-table limit; ten equal-width bins over
10–50 GeV (top bin 4 GeV wide) reach 49.99 GeV, so it is not simply "the last
bin". The effect tracks the top bin's width.

## Upstream issues

Not reported; the source is not public, so causes are unconfirmed.

1. **Same-seed runs diverge** after ~4 events. Likely uninitialized state inside
   NEUT ([Validation](#seed-reproducibility)).
2. **A very wide topmost flux bin is truncated**, at an energy that depends on the
   bin's width ([Validation](#top-bin-truncation)).
3. **Flavour tokens in histogram names are undocumented** (`numub`, `nueb`, and
   the `fluxhisto`/`ratehisto` fallback).
