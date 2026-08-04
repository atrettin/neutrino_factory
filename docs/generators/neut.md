# NEUT

Generator-specific domain knowledge. The conventions this is normalized onto are
in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

Files: `generators/neut.py`, `translators/neut.py`, `normalizers/neut.py`,
`setup/neut/nf_flatten.C`, `setup/setup_neut.sh`, `setup/apptainer/neut.def`.

## Versions and provenance

**NEUT's source code is not publicly available**, so unlike every other generator
this payload cannot be built from a git ref. It is extracted instead from the
NUISANCE collaboration's published tutorial image `nuisancemc/tutorial:nuint2024`
(~5 GB), which ships a working NEUT build (`neut-config --version` → 5.7.0).

Consequences, each a deliberate departure from the rules elsewhere:

- `code_version` is **`5.7.0-nuint2024`**: the NEUT release plus the image tag it
  came from. Since the build is not reproducible from source, the image tag is
  the real pin, and a re-push of that tag would otherwise be invisible.
- **There is no `setup/Dockerfile.neut`**, so the def↔Dockerfile pairing that
  applies to the other three generators does not apply here.
  `setup/setup_neut.sh` pulls and retags the image as `neut:<code_version>` for
  Docker (`--platform linux/amd64`; the source image is multi-arch).
  `setup/apptainer/neut.def` bootstraps stage 1 from it and stages only
  `/opt/neut` plus the ROOT build it links against (~1.5 GB of the ~5 GB image).
- `NeutAdapter` overrides `is_buildable` to treat a `source_image` catalog entry
  as a source (the base class recognizes only `git_ref`), so the catalog-driven
  `build_apptainer_images.sh` picks NEUT up with no generator-specific code, and
  overrides `build_arg` to pass `NEUT_SOURCE_IMAGE` rather than the code version.
- `build_apptainer_images.sh` runs `apptainer build` from the repo root so
  `neut.def`'s `%files` can stage the flattener by repo-relative path. No other
  def has a host `%files` block.

`config_version` is `"default"`, which means `NEUT-MDLQE 2002` and
`NEUT-MAQE 1.05` — Smith-Moniz + BBBA05, RPA, Nieves 1p1h, matching the
NEUT-shipped `neut_5.4.0_nd5_*` cards. `CONFIG_VERSION_CARDS` in
`translators/neut.py` is the extension point: a new parameter set is a new entry
there plus a new `config_versions` entry on the adapter.

## How it is run

Two stages, the same shape as GENIE's:

1. `neutroot2 neut.card events.neut.root` — reads a plain-text card of
   `KEY value` lines (`C` in column 1 marks a comment), a flux ROOT file, and a
   RANLUX seed file.
2. `nf-neut-flatten` (project-owned) → `events.flat.root`, the flat tree the
   normalizer reads. `normalize_output` runs stage 2 lazily if only the raw file
   exists.

Artifacts in the work directory: `neut.card`, `flux.root`, `ranseed.dat`,
`events.neut.root`, `events.flat.root`.

### Every path NEUT sees must be a bare filename

**NEUT reads filenames into 80-character Fortran buffers and truncates anything
longer without complaining** — the failure surfaces much later as
`Fortran runtime error: End of file` on a path that has silently lost its tail. A
real cluster work directory overruns this easily
(`/ptmp/.../work/raw/neut/<version>/<run>_<gen>_<version>_chunk000` is ~90
characters before the filename).

Everything NEUT is handed is therefore a **bare filename** resolved against the
working directory: the card, its `EVCT-FILENM 'flux.root'`, the output, and
`$RANFILE` (`ranseed.dat`). `local.run_task` launches with `cwd=work_dir`, which
is the same assumption the other adapters' relative output paths already make,
and the Apptainer payload wrapper deliberately does **not** `cd` (unlike NuWro's,
which must). An absolute path anywhere in this chain is a latent bug that only
appears on deep work roots.

The one absolute path NEUT still receives is `$NEUT_CRSPATH`, set by the payload
wrapper to `/opt/nf/generators/neut/<code_version>/neut/share/neut/crsdat` (~62
characters). That fits, but a longer `code_version` would break it.

### Seeding

`NEUT-RAND 0` makes NEUT seed RANLUX from the file named by `$RANFILE`
(`1` would seed from the clock and destroy chunk reproducibility). That file is
read with a Fortran `(5X,5I12)` format: **five rows of five 12-column integers,
each preceded by five spaces**. Only the first value is used as the RLUXGO seed,
but all 25 must be present or the read hits end-of-file.

`NEUT-CRSPATH` is deliberately omitted from the card so the cross-section tables
resolve from `$NEUT_CRSPATH`, which both container images already set.

**NEUT is not bit-reproducible from its seed.** Seeding works — the log confirms
`RANLUX INITIALIZED BY RLUXGO FROM SEEDS <seed> 0 0`, different seeds diverge
immediately, and the same seed reproduces the opening events exactly. But two
runs with the *same* seed diverge after ~4 events: NEUT's own `Ev.# N SEEDS`
trace shows it consuming a different number of random numbers from there on. This
is NEUT, not the harness — it reproduces on native aarch64 and on x86_64 under
Rosetta, and with ASLR disabled (`setarch -R`), which rules out emulation and
address-layout effects. The likely cause is uninitialized state inside NEUT; the
source is unavailable to confirm.

Consequence: chunking is still safe, since that requires only that different
chunk seeds give different, independent event sets. What is lost is bit-exact
re-running of a given chunk. Configs remain reproducible in distribution, not in
individual events.

## Flux handling

NEUT has no function-flux driver, so power-law and histogram fluxes both go
through `Flux.to_histogram` and NEUT is handed a histogram *we* wrote
(`EVCT-MPV 3`, `EVCT-FILENM 'flux.root'`, `EVCT-HISTNM 'nf_flux'`).
`FLUX_NBINS = 1000`, `FLUX_SPACING = "log"`.

**Bin contents are per-bin integrals (density × width), not densities.** NEUT
picks a bin in proportion to its raw content and ignores the widths, so on a
log-spaced grid densities would sample a spectrum tilted by one power of the bin
width. This is GENIE's `WIDTH` flux field applied ahead of time, because NEUT has
no equivalent switch. It also makes the `evtrt`/`flux` integral ratio a correctly
flux-weighted cross-section average on *any* binning, so
`NeutNormalizer._flux_averaged_xsec` needs no width factor.

**Two unit traps on the card**, both confirmed against NEUT 5.7.0:

- `EVCT-PV` (the fixed/uniform energy range, unused here) is in **MeV**, whereas
  the flux histogram is read in the unit declared by `EVCT-INMEV` — `0` for GeV,
  which is what `to_histogram` produces.
- `EVCT-MPV 3` (histogram flux) is used for *every* framework flux type.

**Normalize against what NEUT stamped out.** `NeutNormalizer` does not rebuild
the flux from the run config; it loads the `flux_<flavour>` histogram NEUT copied
into its own output on its **native** binning (`contents_are_counts=True`), and
`translators/neut._flux_grid` keeps that binning intact. There is no fallback to
the configured flux — it would restore the bug below and still look physical.

### What NEUT actually samples

Established from the NEUT 5.7.0 binary plus dedicated runs, since the source is
not distributed. `neutroot2`'s `rndenuevtrt_` calls `Ufm2TH1dist::GetValue`,
whose `Init` calls `TH1::ComputeIntegral`/`GetIntegral` and whose `GetValue`
inverts that cumulative — `TH1::GetRandom` semantics applied to the `evtrt`
(flux × σ) histogram. Two consequences, both confirmed by generation:

- *A bin is chosen in proportion to its raw content; the widths are ignored.* On
  10 log-spaced bins over 0.1–50 GeV, 30k events: χ²/ndf **0.90** against
  `p_b ∝ evtrt_b`, **1.7 × 10⁴** against `p_b ∝ evtrt_b · width_b`.
- *Within the chosen bin the energy is uniform in E* — not in log E, and with no
  σ(E) dependence. A single flux bin spanning 0.2–2.0 GeV, where σ(E) rises by a
  factor 56, gave a flat `dN/dE`: χ²/ndf **0.48**, mean energy
  **1.0961 ± 0.0030 GeV** against 1.1000 predicted. The alternatives are excluded
  by hundreds of σ (uniform-in-log-E predicts 0.7830; `dN/dE ∝ σ(E)` predicts
  1.3676).

So the generated flux density is piecewise constant on exactly the input bin
edges, and the reconstructed σ(E) can have no structure finer than those bins.
`FLUX_NBINS` is therefore a **resolution setting**, not just a sampling aid:
1000 log bins give ~0.62%/bin over 0.1–50 GeV, where the previous 500 equal-width
bins gave 0.1 GeV steps that swallowed the whole region in which σ(E) rises by
orders of magnitude.

<details>
<summary>The staircase this fixed (2026-07), with the validation numbers</summary>

A 1M-event 0.1–50 GeV run showed, below ~1 GeV, a raw `dN/dE` that was a
staircase with ~0.1 GeV steps — the width of the 500 equal-width flux bins — and
a σ(E)/E plot with an inverse sawtooth. NuWro and GiBUU on the same config looked
smooth. The "sawtooth" was the σ/E plotting convention drawn over a staircase,
not a separate defect.

**Full chain on real NEUT output.** Generated with a deliberately coarse version
of the new convention — 10 log-spaced bins over 0.1–50 GeV holding per-bin
integrals of an E^-2 density, 100× coarser than production — 15k events on C12,
then normalized. `Σ xsec_weight / bin_width` per flux bin against NEUT's own
`evtrt/flux`, over a cross section spanning a factor 6700:

| E (GeV) | N | σ recovered | σ NEUT | pull |
|---|---|---|---|---|
| 0.100–0.186 | 70 | 0.00575 ± 0.00069 | 0.00517 | +0.85 |
| 0.347–0.645 | 1620 | 0.4613 ± 0.0115 | 0.4571 | +0.37 |
| 2.236–4.163 | 1796 | 3.299 ± 0.078 | 3.426 | −1.62 |
| 26.858–50.0 | 1588 | 35.04 ± 0.88 | 34.44 | +0.69 |

Over all ten bins the pull is mean **+0.06**, rms **0.88** — Monte-Carlo noise
with no systematic tilt. The unequal widths are the point: a divisor that ignored
the bin widths, or one resampled onto a different grid, is invisible on the
equal-width flat-flux fixtures the unit tests otherwise use.

At production binning (40k events, E^-2 over 0.1–50 GeV on C12, 40 log analysis
bins) the same comparison gives ratio **1.006 ± 0.044** with pull rms 1.2, and
the staircase is gone where it used to be worst: the four analysis bins between
0.1 and 0.2 GeV, all of which fell inside the *single* first bin of the old
500-bin linear grid and therefore had to report one identical σ, now return
0.0026, 0.0033, 0.0077 and 0.0184 — tracking NEUT's own σ(E) across a factor 7
within what used to be one flat step.
</details>

## Cross-section weight

NEUT generates **unweighted** events, so the `weight` column is 1.0 and carries
no normalization information. Events are distributed in energy with density
proportional to `flux(E) · σ_total(E)`, structurally identical to GENIE:

```
xsec_weight_i = sigma_avg / (n_events · phi_hat(E_i))
```

**σ_avg needs no external spline file.** When sampling a flux histogram
(`EVCT-MPV 3`), NEUT writes both that histogram (`flux_<flavour>`) and the
resulting event rate (`evtrt_<flavour>`, = flux × σ) into its own output, and the
ratio of their integrals *is* the flux-averaged total cross section.
`NeutNormalizer` reads them and injects the ratio into the translated config as
`flux_averaged_xsec_1e38`; `compute_xsec_weight` raises if it is absent.

**Already per nucleon and already in 1e-38 cm²** — no `XSEC_SCALE`, no division
by mass number, unlike GENIE. Two independent checks:

- *It does not scale with A.* Regenerating the same flux on C12, O16, Ar40 and CH
  gives 0.655, 0.663, 0.675 and 0.636 — the small isospin-driven spread of a
  per-nucleon quantity, not the factor ~3.3 a whole-nucleus quantity would show
  between C12 and Ar40.
- *Absolute value.* NUISANCE, reading the same file through its own NEUT input
  handler, reports `Event/Flux : 6.55288e-39 cm2/nucleon` where this ratio is
  0.6552880 — agreement to every printed digit. (An earlier run of the same check
  on a different flux gave 1.4677402686 against `1.46774e-38 cm2/nucleon`.)
- *Sampling law.* The generated energy spectrum tracks `evtrt`, not `flux`: over
  ten coarse bins the summed absolute difference in normalized shape is 0.034
  against `evtrt` versus 0.95 against `flux`. This is what licenses the formula.
- *End to end.* For a 4000-event single-chunk C12 run, `Σ xsec_weight / bin_width`
  per energy bin reproduces NEUT's own `evtrt/flux` ratio per bin to 1.03 ± 0.05.

`xsec_norm_count` is the chunk's event count.

**Why the histogram pair is found by prefix, not by name.** The names carry the
beam flavour: `neutroot2` formats them as `flux_%s` / `evtrt_%s` with its *own*
short token — `numu`, **`numub`** (not `numubar`), `nue`, `nueb`, read off the
NEUT 5.7.0 binary — and falls back to `fluxhisto` / `ratehisto` for a beam it has
no token for, e.g. ν_τ. None of that is a documented contract, so the framework
does not encode the mapping: `nf_flatten.C` copies *every* TH1 out of NEUT's
output verbatim, and `_find_histogram` picks the unique `flux*` / `evtrt*` (or
generic) pair, erroring if there is none or more than one rather than guessing.
NUISANCE solves the same problem the same way (`PlotUtils::GetObjectWithName`).
Hardcoding `flux_numu` made every non-numu NEUT run fail at normalization.

## Weak current: `NEUT-CRS` slot tables

NEUT has **no CC/NC switch**. `NEUT-MODE 0` is its normal mode (every channel in
proportion to its cross section = `inclusive`); `NEUT-MODE n > 0` would pin a
single channel. A single current is selected with `NEUT-MODE -1` ("input cross
section by CRSNEUT"), which multiplies each channel's cross section by its slot
in the 30-element `NEUT-CRS` (neutrino) / `NEUT-CRSB` (antineutrino) arrays.
Zeroing every slot of the unwanted current restricts generation to one current.

### Slot numbers are not mode numbers

These are two unrelated numbering schemes, and neither is derivable from the
other:

- A **mode** is the physics-channel identifier written per event (the `Mode`
  member of `NeutVect`, our `mode` column). It is *sparse* over 1..52 and
  semantically grouped — CC channels are ≤ 30, NC channels ≥ 31 — and negated
  for antineutrinos. `necard.h` documents the scheme, and `nemodsel.F` (not
  public) owns it.
- A **slot** is a position in the *dense* 30-element `CRSNEUT(30)` /
  `CRSNEUTB(30)` scaling array, ordered by NEUT's internal enumeration of
  generatable channels. `necard.h` describes the arrays only as "Multiplied
  factor to cross section on each mode. See nemodsel.F", and the channel list
  per slot is documented in the shipped cards
  (`share/neut/Cards/neut_5.4.0_nd5_O.card`).

Four structural reasons the two cannot line up:

1. **Slots resolve sub-channels that share a mode.** NEUT splits some channels
   by whether the target nucleon is free or bound, which the mode numbering does
   not distinguish: ν slots 11 and 12 both produce mode 51, and ν̄ slots 12 and 13
   both produce mode 51.
2. **The slot array is a dense array index**; the mode space is sparse and
   carries meaning in its ranges.
3. **The two arrays disagree with each other.** The ν̄ list has a free/bound CCQE
   split at slots 1 and 11, which pushes NC elastic and the coherent pair one
   slot later than in the ν list.
4. **A slot can be empty for one beam sign** — ν slot 22 has no channel at all.

**The full mapping, measured** (NEUT 5.7.0, C12 + 1 free proton, numu/numubar,
E^-2 flux over 0.5–8 GeV; each slot enabled alone with `NEUT-MODE -1` and 400
events generated, then the observed `mode` values read off the flattened tree).
Every slot produced exactly one mode, 400/400 events:

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

Note the card's naming: "CC DIS 1320" is the 1.3 < W < 2.0 region and maps to
NEUT's *multi-π* mode 21, while "CC DIS" (W > 2.0) maps to mode 26 — NUISANCE
titles them "Multi π (1.3 < W < 2.0)" and "DIS (W > 2.0)" respectively.

**This measurement validates both shipped masks**: deriving each slot's current
from its observed mode (CC ≤ 30, NC ≥ 31) reproduces `CRS_SLOT_CURRENTS` for all
30 ν slots and all 30 ν̄ slots, with zero mismatches. It also settles the one
entry that had previously only been inferred — the card labels slots 14/15
(ν) and 15/16 (ν̄) merely as "coherent", and they are confirmed CC-then-NC.

Both rows are written on every card, each masked with its own table, so the run
does not depend on which array NEUT consults for a given beam sign. Slot 22 stays
zero in both masks.

A separate run confirmed that NEUT's `evtrt` histogram — the basis of the
`xsec_weight` normalization — *does* follow the mask, so a masked run is
normalized to its own current and not the inclusive total.

**A slot with no cross section aborts the run**, loudly and helpfully. Enabling
ν slot 22, or the diffractive slots on a target with no free protons, gives:

```
Calculated an empty event rate histogram
But the flux histogram seems to make sense
Looks like there's no cross-section in your provided energy range!
```

That is how the four unreachable slots above were identified: slots 29/30
(diffractive) need free protons *and* `NEUT-DIFPI 1`, and ν slot 11 (NC elastic
on a free proton) needs free protons. The framework's targets are always pure
nuclei (`NEUT-NUMFREP 0`) and it never sets `NEUT-DIFPI`, which defaults to 0, so
modes 15 and 35 cannot occur in our runs.

## Output flattening (`nf_flatten.C`)

`neutroot2` writes a `neuttree` whose `vectorbranch` holds `NeutVect` objects.
uproot deserializes their scalar members from the file's streamers but fails on
the nested `TObjArray` of `NeutPart` ("invalid class-tag reference"), so **NEUT's
native output cannot be read from Python at all.**

`setup/neut/nf_flatten.C`, run through `setup/neut/nf-neut-flatten`, is the
project-owned second stage — the NEUT analogue of GENIE's `gntpc`, dispatched by
`NeutAdapter._run_flatten` through the same native/container branches as
generation. It writes an `nf_neut` tree of plain scalars and copies NEUT's
normalization histograms across verbatim.

**Four-momenta are converted to GeV in the macro** (`NeutPart` stores MeV), so
the flat tree is single-unit.

**Do not read the outgoing lepton at `PartInfo(2)`.** The usual layout is
[0] beam neutrino, [1] struck nucleon, [2] outgoing lepton, and 49 of 50 events
in a real numu-CC C12 run follow it. The exception is **2p2h (Mode 2), which has
two initial-state nucleons at [1] and [2], putting the lepton at [3]** — a fixed
index would have read a neutron as the outgoing lepton and silently corrupted
Q²/x/y for the entire MEC channel. The flattener scans from index 1 for the first
particle with |PDG| in 11..16 (the charged lepton for CC, the scattered neutrino
for NC; leptons do not rescatter, so there is no FSI copy to confuse it), and
reports `pdglep = 0` when it finds none, which blanks the kinematics for that
event. Verified by dumping every NeutVect entry of a real run against the
flattened tree: 50/50 events agree.

**Alternatives rejected.** NEUT ships `neutclass_to_tree`, but its `nework`
branch is a Fortran leaf-list containing `pne[100][3]`, which uproot mis-parses
(it reads the dtype as `(3,)` rather than `(100,3)`); reading it would mean
hand-maintaining a 2808-byte numpy dtype that must track NEUT's common block.
NUISANCE's `nuisflat` produces a genuinely flat tree, but `ldd` shows it linking
GENIE, NuWro, LHAPDF and Pythia — the payload would grow from ~1.5 GB to
essentially the whole image.

## Output and interaction taxonomy

Tree `nf_neut`. Branches: `enu_gev`, `mode`, `nu_p{x,y,z}_gev`,
`lep_{e,px,py,pz}_gev`, `pdglep` (0 = no lepton found → kinematics blanked). The
`weight` column is 1.0.

`INTERACTION_BY_MODE` is keyed on the **absolute** mode (NEUT negates the mode
for antineutrinos) and spelled out explicitly rather than as ranges, so a mode
NEUT adds later falls through to `other` instead of being silently absorbed into
a neighbouring category:

| Modes | Category |
|---|---|
| 1, 51, 52 | `qel` — CCQE plus NC elastic (p, n) |
| 2 | `mec` — CC 2p2h |
| 11–13, 17, 22, 23, 31–34, 38, 39, 42–45 | `res` — resonant 1π, 1γ, 1η, 1K, both currents |
| 21, 26, 41, 46 | `dis` — multi-π and DIS, both currents |
| 16, 36 | `coh` — coherent π |

**NC elastic (51/52) is grouped with CCQE under `qel`** to match GENIE, whose gst
`qel` flag likewise covers both. This is why `is_cc` cannot be inferred from
`interaction`.

Modes **15 and 35** (CC/NC diffractive π) have no entry and would fall through to
`other`. They are unreachable in this framework — diffractive production needs
free protons and `NEUT-DIFPI 1`, and the framework sets `NEUT-NUMFREP 0` and
never enables `DIFPI` — so the gap is latent rather than active; see
`.claude/TODOS.md`.

`is_cc` comes from the mode number itself: NEUT numbers charged-current channels
1..30 and neutral-current channels 31 and up (`MAX_CC_MODE = 30`), compared on
the absolute value.

## Container payload

`setup/apptainer/neut.def` stages `/opt/neut` (including
`share/neut/crsdat` and `include/` — the headers are needed because the flattener
compiles through Cling) plus the ROOT 6.30/04 build NEUT links against. The def's
`CODE_VERSION` must stay in sync with `NeutAdapter.CODE_VERSIONS`.

Three relocation fixes in `%post`, each of which caused a real failure:

- **Three dangling absolute symlinks are repointed.** ROOT's `bin/root` is an
  absolute symlink to `/opt/root/v6-30-04/bin/root.exe` in the source image and
  dangles once relocated, which bash reports as the confusing `root: not found`.
  The `%test` section runs `root --version` from the payload, so a build that
  gets this wrong now fails at build time.
- **`NEUT.pc`'s `prefix=` is repointed**: it bakes in the original install prefix
  and `neut-config` refuses to run when that disagrees with its own location.
- The wrapper exports `NF_NEUT_INCDIR` so Cling finds the NEUT headers, plus
  `$NEUT_CRSPATH` and `$RANFILE`.

`/opt/cern` (CERNLIB) is deliberately **not** staged: `ldd` reported it unused,
and it appears to be statically linked into `libNEUT.a`. `neutroot2` needs only
`libgfortran5`, `libpcre3`, `liblzma5`, `libz` and `libstdc++`, all already in
`nf-base.def`'s runtime baseline. If a missing-library error ever appears at
startup, CERNLIB is the first suspect.

Harmless noise to expect from the flatten stage:
`Error in cling::AutoLoadingVisitor: Missing FileEntry for
/opt/neut-src/neutclass/*.h` — the image has no NEUT source tree, so Cling cannot
autoload; the macro still compiles because the wrapper passes `NF_NEUT_INCDIR`.

## Known limitations

- **A very wide topmost flux bin gets truncated.** When the last bin of the flux
  histogram is *very* wide, energies in it are drawn uniformly over only part of
  the bin and then stop dead. Two-bin histogram `[10, 26.858, 50]`, 2000 events:
  the lower bin is uniform across all seven slices, the upper is uniform to
  ~43.7 GeV and empty above (slice counts `266 272 257 255 265 29 0`). The same
  `[26.858, 50]` top bin cut at 39.2 GeV in a ten-log-bin run over 0.1–50 GeV, so
  the cut is not a fixed energy; nor is it a cross-section-table limit (a single
  bin spanning 30–80 GeV fills uniformly to 80 GeV); nor is it simply "the last
  bin" (ten equal-width bins over 10–50 GeV, top bin 4 GeV wide, reach 49.99 GeV
  uniformly). The effect tracks how wide that last bin is; the cause is
  unidentified, since NEUT's source is not distributed. With 1000 log bins the
  last bin spans ~0.17 GeV at a 50 GeV endpoint — two orders of magnitude
  narrower than the widths that showed the effect — so the production binning
  stays clear of it. Not worked around: any workaround would distort the
  requested flux. Tracked in `.claude/TODOS.md`.
- Not bit-reproducible from its seed (see above).
- `config_version` is `"default"` only.
- `run.log_level` is ignored — only the GENIE adapter maps it.
- `MAX_ENERGY_RANGE_GEV` / `VALID_ENERGY_RANGE_GEV` are provisional guesses
  pending verification.
