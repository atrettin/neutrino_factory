# GENIE

Generator-specific domain knowledge. The conventions this is normalized onto are
in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

Files: `generators/genie.py`, `translators/genie.py`, `normalizers/genie.py`,
`setup/Dockerfile.genie`, `setup/apptainer/genie.def`,
`setup/download_genie_xsec.sh`, `setup/genie/nf_genie_reweight.cc`.

## Versions and provenance

Built from source off the GENIE-MC/Generator GitHub tags. `CODE_VERSIONS` carries
`R-3_06_00` (the working, verified build) and `R-3_04_00`.

GENIE is the one generator whose `config_version` is a real physics parameter set:
the **tune** (e.g. `G18_10a_02_11a`). Tunes are **not statically enumerated** —
`available_config_versions` globs the cross-section splines staged on disk,
`<software_root>/genie/genie_xsec/<tag-safe-code-version>/<tune>/xsecs.xml`, so a
tune is "available" exactly when its splines are present. `list-generators` shows
it only then, and `ensure_compatible(require_available=True)` (real, non-stub
runs) refuses a tune with no staged spline.

The staged spline is a *necessary* but not *sufficient* condition: `gevgen` also
needs the tune's physics-configuration directory inside the **code payload**
(`$GENIE/config/<family>/<tune>`), and nothing checks that at config time.
Observed (2026-09, ODSL): `N18_10j_02_11a` had its spline staged under
R-3_04_00, passed validation, yet `gevgen` aborted at run start with
`FATAL TuneId: No valid tune directory associated with N18_10j_02_11a` because
the R-3_04_00 payload ships no `N18_10j` directory.

The FNAL SciSoft tarballs name tunes with the underscores stripped
(`G1810a0211a` for `G18_10a_02_11a`), so lookup falls back to a
separator-insensitive match (`_normalize_tune`). `setup/download_genie_xsec.sh`
scrapes the available tune list rather than hard-coding it, downloads
`genie_xsec-<dotver>-noarch-<TUNEKEY>-k250-e1000.tar.bz2` (~428 MB) and extracts
only `gxspl-NUsmall.xml` (~543 MB staged). Tunes that SciSoft does not publish
for a code version (HTTP 404) are warned about and skipped, not treated as
errors.

**The SciSoft directory is derived from the code version**, not configured:
`R-3_06_00` → ups version `v3_06_00` (and dotted `3.06.00` for the tarball
name), under `https://scisoft.fnal.gov/scisoft/packages/genie_xsec/<upsver>/`.

### Setup commands

```bash
# build the image for a catalogued code version (default R-3_06_00)
setup/setup_genie.sh R-3_06_00
setup/setup_genie.sh --code-version R-3_06_00 --download-xsec   # …and stage splines

# stage splines on their own
setup/download_genie_xsec.sh --code-version R-3_06_00 --tune G18_10a_02_11a
```

`setup_genie.sh` takes the code version positionally or as
`--code-version`/`--tag`, plus `--jobs N` for the parallel make jobs inside the
image; it targets `linux/amd64` so it runs on Apple Silicon via Rosetta 2.
`download_genie_xsec.sh` takes `--tune` (repeatable; default is every tune
published for the code version), `--software-root`, and `--force` to re-stage
over an existing `xsecs.xml`. Distinct code versions produce distinct image tags,
so multiple GENIE versions coexist.

If `neutrino-factory list-generators --generator genie` shows no available config
version, the usual cause is that no tune's `xsecs.xml` has been staged yet — the
image alone is not enough.

## How it is run

Two stages, the same shape as NEUT's:

1. `gevgen` → `events.ghep.root` (GENIE's native GHEP format).
2. `gntpc -f gst` → `events.gst.root`, the flat analysis tree the normalizer
   reads. `normalize_output` runs stage 2 lazily if only the GHEP file exists.

The command is assembled in `GenieAdapter.build_run_command`. Two details that
are load-bearing:

- **Flux-driven runs require precomputed splines.** Given a spectrum via `-f`,
  `gevgen` aborts without `--cross-sections`. This is the same file the
  normalization later reads, so a run that generates at all can be normalized.
  The staged spline is normally guaranteed by config validation, which calls
  `ensure_compatible(require_available=not stub_mode)` (`config.py`) and so
  rejects a non-stub run whose tune has no `xsecs.xml`. The adapter's own
  `_resolve_xml_path` is a weaker second line: if the file is missing it logs a
  warning and simply omits `--cross-sections`. That path is reachable only when
  validation was bypassed or the file disappeared afterwards — and a flux-driven
  run then fails inside `gevgen` rather than at config time.
- **The `-e` range is padded by a relative `1e-12`.** `gevgen` zeroes every flux
  histogram bin not strictly inside `[emin, emax]`, and reconstructs the upper
  bound as `emin + (emax - emin)` in floating point. Without the padding,
  round-off can silently drop the first or last bin. Physically a no-op.

`run.log_level` maps onto `--message-thresholds` via `MESSENGER_PRESETS`
(`verbose` → `Messenger_rambling.xml`, `quiet` → `Messenger_laconic.xml`,
`essential` → laconic plus a project-written overlay raising the `Ntp` stream to
INFO). GENIE is currently the **only** adapter that honours `run.log_level`.

Two quirks of that mapping: the framed "gevgen job configuration" banner survives
even at `quiet`, because `main()` calls `GetCommandLineArgs()` before
`Initialize()`, and the custom thresholds are applied in `Initialize()`. And
`essential` stays *linear* in event count (the `Ntp` stream emits one "Adding
event N" line per event, sharing stream and priority with the file-write
messages it exists to show); `quiet` is the constant-size option for large
production arrays. The overlay XML is written into the work directory and
resolves through `GetXMLFilePath`'s bare-basename fallback from the CWD — no bind
mount or path remapping needed.

**FSI is the tune's.** Hadron transport is part of each tune's model
configuration (`HadronTransp-Enable` and `HadronTransp-Model`, e.g.
`genie::HAIntranuke2018/Default`, in `config/<tune>/ModelConfiguration.xml`),
and the framework passes no override, so the cascade model is whatever `--tune`
selects. All 33 tunes in the R-3_06_00 source tree set it on.

**Native binary first.** `build_run_command` checks `shutil.which(gevgen)` before
considering a container. On the cluster the Slurm task already runs inside the
Apptainer image (which cannot nest), so the binary must be executed directly
whenever it is on `$PATH`. The branch order is load-bearing in both pathways.

## Flux handling

`gevgen` **never samples a continuous function.** Every `-f` input ends up as a
`TH1D` that `GCylindTH1Flux::GenerateNext` draws from with `TH1::GetRandom` —
uniform within a bin — so the generated flux density is always piecewise
constant. The three input branches differ only in how the TH1D is built
(`Apps/gEvGen.cxx`, `TH1FluxDriver`, R-3_06_00):

| `-f` input | What happens |
|---|---|
| TF1 string (`"x^(-2.0)"`) | `new TH1D("spectrum", …, 300, emin, emax)` then `FillRandom("input_func", 100000)` — Monte-Carlo *estimated* from 100k entries |
| ROOT file (`file.root,hist[,WIDTH]`) | `Clone()`d verbatim; arbitrary (including log) binning preserved; out-of-range bins zeroed; contents multiplied by bin width for a variable-width axis or an explicit `WIDTH` |
| Text file | Rejection-sampled into the same 300-bin histogram |

The TF1 branch is unusable in practice: for E^-2 over 0.1–50 GeV about 63% of
those 100k entries land in the first bin and bins above ~15 GeV hold single-digit
entries (>40% Poisson noise), and `RandomGen::InitRandomGenerators` seeds
`gRandom`, so the noise realization differs per chunk.

**Decision (2026-07): write the histogram ourselves.** A power-law flux is
materialized into the task work directory as `nf_flux.root` —
`GENIE_FLUX_NBINS = 1000` log-spaced bins holding the flux *density* — and passed
as `-f nf_flux.root,nf_flux,WIDTH`, taking the clone branch. Log spacing gives
constant relative resolution (~0.27%/bin over 0.1–50 GeV); the explicit `WIDTH`
field converts our density into the per-bin sampling probability regardless of
binning. Contents are written as float64 so uproot emits a TH1D, which is what
gevgen casts to. A user-supplied ROOT flux is passed through *without* `WIDTH`,
since its content convention is unknown.

**Normalize against `input-flux.root`.** `GenieNormalizer` does not rebuild the
flux from the run config. It reads `input-flux.root` — which gevgen writes into
its working directory on every run — and uses that histogram's **native** binning
as the denominator (`translators/genie._flux_grid`). That file is the only
authoritative record of what was sampled: it reflects the clipping, the width
multiplication, and (for legacy TF1-driven runs) the per-chunk noise realization.
Its contents are per-bin integrals, so they are divided by the bin widths on load
(`HistogramFlux.from_root_file(..., contents_are_counts=True)`).

A missing `input-flux.root` is a hard error. Falling back to the config flux would
silently restore the original bug and produce a wrong normalization that still
looks physical.

<details>
<summary>The bug this fixed (2026-07)</summary>

A 1M-event run over 0.1–50 GeV showed a sawtooth in both the raw and the
`xsec_weight`-weighted event rate below ~1 GeV: a slow rise followed by a sharp
drop, repeating, with a second interfering period. The framework was passing the
power-law flux as a TF1 string on the assumption that GENIE samples the function
continuously. The sawtooth was the 300-bin generated flux beating against the
*different* grid the weight divided by — `flux.to_histogram(nbins=500)`, rebuilt
from the run config. Events were being divided by a flux they were never drawn
from.

Only the normalization half is required for correctness; writing the histogram is
a quality fix. Reading `input-flux.root` alone yields unbiased weights even
against the noisy 300-bin histogram, but the *variance* at high energy would stay
bounded by the flux histogram's 100k entries rather than by the event count.

**Verified** (local Docker, R-3_06_00/G18_10a_02_11a, numu on C12, 0.1–50 GeV):
gevgen's `input-flux.root` came back with edges bit-identical to `nf_flux.root`,
contents equal to our density × bin width, and no zeroed bins — i.e. cloned, not
resampled. Across the resulting events `xsec_weight · E^-2` is constant to 1.25%
(the residual expected from 0.27%/bin log binning), with no step structure.
</details>

## Cross-section weight

GENIE is **unweighted**: `gOptWeighted = false` in `Apps/gEvGen.cxx`, so the gst
`wght` branch is always 1.0 and carries no normalization information. Events land
in energy with density proportional to `flux(E) · σ_total(E)`, so

```
xsec_weight_i = C / (n_events · phi_hat(E_i))
```

where `C` is the run's flux-averaged total cross section per nucleon. GENIE,
unlike NuWro, does not stamp `C` onto its events, so it is reconstructed.

**The gst `XSec`/`DXSec` branches cannot be used.** Both trace back to
`EventRecord::XSec()`, set in `PhysInteractionSelector::SelectInteraction` to the
cross section of only the *one selected channel* at that event's kinematics —
never the summed total over all channels and nucleons that actually governed
accept/reject (`xsec_sum` in that same function, never persisted to any output).
Averaging a per-channel value over events does not converge to the total, because
channel selection is itself correlated with that channel's cross section.

**`C` is reconstructed from the staged spline XML.** Summing every `<spline>`
whose name matches `nu:<probe_pdg>;tgt:<target_pdg>;` — regardless of struck
nucleon or process — reproduces the same `xsec_sum` GENIE computes internally for
that initial state. Verified against the real staged file for
R-3_06_00/G18_10a_02_11a: 103 matching splines for numu/Ar40, covering
QEL/RES/DIS/COH/MEC including the dummy 2p2h pair codes. The XML is streamed line
by line (ISO-8859-1) rather than parsed as a DOM, since only a small fraction of
its splines match any given pair, and knots are interpolated with `np.interp`
(`right=knot_x[-1]`, i.e. flat above the last knot).

Three conversions apply:

- **Current filter.** For `physics.current: cc`/`nc` the run is restricted with
  `--event-generator-list`, so the spline sum must be filtered on the matching
  `proc:Weak[CC]` / `proc:Weak[NC]` tag. Omitting the filter would fold the other
  current in and overstate the result — for numu on carbon, by roughly a third.
  Both settings derive from the same `physics.current` value in the translator, so
  they cannot drift apart.
- **Units.** Raw `<xsec>` knot values are in GENIE's internal natural units
  (GeV = 1; `Framework/Conventions/Units.h`), not literal cm². `GENIE_UNITS_CM2`
  is built from `hbarc = 1.973269804e-16 GeV·m`, and the conversion
  `× 1e38 / GENIE_UNITS_CM2` is exactly what `gNtpConv.cxx` applies
  (`brXSec = event.XSec()*(1E+38/units::cm2)`).
- **Per nucleon.** The spline `tgt:` tag is the **whole-nucleus** PDG code (e.g.
  1000180400 for Ar40), so the reconstructed total is a whole-nucleus quantity —
  the opposite of NuWro. Divide by the mass number, read straight off the target
  PDG code.

`xsec_norm_count` is the chunk's event count.

**A probe with no staged spline is an error, not a zero weight (2026-07).** When
nothing matched, the translator used to return the zero-filled array, so every
event came back with `xsec_weight = 0` — physical-looking output that is silently
unnormalized. It now raises, naming the probe, target, current and XML path. This
is reachable in ordinary use: the shipped `gxspl-NUsmall.xml` carries
nue/nuebar/numu/numubar splines only, so a ν_τ run has no cross section to
reconstruct and must say so.

## Output and interaction taxonomy

Tree `gst`, produced by `gntpc`. Branches read: `Ev`, `wght`, `cc`, the mode flags
`qel`/`res`/`dis`/`coh`/`mec`, the neutrino momentum `(pxv, pyv, pzv)`, the
lepton four-vector `(El, pxl, pyl, pzl)` and the struck nucleon
`(En, pxn, pyn, pzn)` with its PDG code `hitnuc`, the post-FSI particle list
`pdgf`/`Ef`/`pxf`/`pyf`/`pzf` (jagged; it includes the residual nucleus) and
`neut_code` — **all in GeV**, so no unit
conversion is applied. For NC events the "outgoing lepton" branches hold the
scattered neutrino.

Two properties of the struck-nucleon branches, both established from GENIE's
source and both load-bearing for `w_true_gev`:

- **When there is no hit nucleon, GENIE writes literal zeros** into
  `En/pxn/pyn/pzn` and sets `hitnuc = 0` (`gNtpConv.cxx`), rather than a NaN. A
  zero four-vector is finite, so `hitnuc` is what the normalizer masks on.
- **For MEC, `hitnuc` is a two-nucleon *cluster* code** (2000000200/201/202) and
  the four-momentum is the *pair's*: `GHepRecord::HitNucleon` returns the cluster
  when `pdg::Is2NucleonCluster` matches. This is what makes "the struck
  initial-state hadronic system" the right definition of `p_N` across all four
  generators — see [physics.md](../physics.md#the-two-hadronic-mass-columns).

The mode flags go through the shared `normalizers/base.interaction_from_flags`
priority chain (qel → res → dis → coh → mec → other), which NuWro's `treeout`
also uses since it exposes branches with the same names.

`is_cc` comes from the gst `cc` branch. `xsec_norm_count` is recorded on the real
path only.

### GENIE interaction codes

The gst tree has 99 branches, and the five booleans above are the coarsest view
of the interaction it offers. Unused, but present:

| Branch | What it is |
|---|---|
| `neut_code` | **NEUT-equivalent mode**, in NEUT's own signed numbering — produced by `genie::utils::ghep::NeutReactionCode` (`Framework/GHEP/GHepUtils.cxx`) |
| `nuance_code` | NUANCE-equivalent mode |
| `resid` | Resonance ID for RES events (0 = P33(1232), …) |
| `dfr`, `imd`, `imdanh`, `singlek`, `nuel`, `amnugamma`, `charm`, `em`, `hnl` | Process flags outside our five (see the `charm` note below) |
| `hitqrk`, `sea`, `resc` | Struck quark, sea-quark flag, rescattering code |
| `W`, `x`, `y`, `Q2` and the `Ws`/`xs`/`ys`/`Q2s` variants | Selected-vertex vs. hadronic-system-reconstructed kinematics — see [the hadronic-mass branches](#the-hadronic-mass-branches-w-and-ws) below |

`neut_code` is the interesting one: it is computed on the same topological basis
NEUT uses — counting pions and nucleons in the primary hadronic system *before*
intranuclear rescattering — so it would let GENIE and NEUT be compared under one
classification instead of each generator's own. Reading it is tracked in
`.claude/TODOS.md`.

That matters because the two views disagree substantially. Measured on a
25 000-event numu/C12 run (R-3_06_00, G18_10a_02_11a):

- 677 events we label `res` carry `neut_code` 21/41 (multi-π) — GENIE's RES
  generator produced more than one pion.
- 471 events we label `dis` carry single-pion codes 11/12/13/31–34 — GENIE's DIS
  generator produced exactly one pion, i.e. non-resonant background.

Neither is an error: they are two defensible classifications of the same events,
one by *which generator produced it*, one by *what came out*. But only the second
is commensurable with NEUT.

Two further observations from the same run:

- **15 events (0.06%) fall through to `other`** — 12 `imd` (inverse muon decay)
  and 3 `nuel` (ν-e elastic), `neut_code` 9 and 59, which GENIE assigns as
  placeholders since NEUT has no such modes. These are **electron-target**
  processes carrying a per-*nucleon* `xsec_weight`, the same incommensurability
  that makes NuWro's `dyn_lep` switched off (see
  [nuwro.md](nuwro.md#weak-current)). Tracked in `.claude/TODOS.md`.
- 48 MEC events have `neut_code` 0, because `NeutReactionCode` assigns 2/−2 for
  CC MEC only — NEUT has no NC MEC mode.

#### `charm` puts charmed baryons into our `qel` bucket

GENIE simulates **charm quasi-elastic** production, `nu_mu + n -> mu- + Lambda_c+`
(and `Sigma_c`), and flags those events with `qel` *and* `charm`. The common
output reads only the five class flags, so they are labelled `qel` — which is
consistent with the taxonomy's rule (the category GENIE itself assigns) but puts
a charmed baryon in a bucket otherwise made of nucleons.

It is visible in `w_true_gev`, because for a quasi-elastic event that column is
just the mass of the single outgoing baryon. On the 20k-event numu CC C12 run the
GENIE `qel` distribution is therefore three discrete lines rather than one:

| `w_true_gev` | Events | Baryon (PDG mass) |
|---|---|---|
| 0.9383 | 7231 | proton (0.938272) |
| 2.285 | 22 | Λc+ (2.28646) |
| 2.453–2.454 | 10 | Σc (2.4529–2.45397) |

The charm events sit at 2.79–4.71 GeV, above the ~2.6 GeV threshold for Λc
production and inside the run's 0.5–5 GeV flux; they are 32 of 7263 `qel` events,
0.44%. **No other generator produces them** — the largest `qel` `w_true_gev` is
1.141 (NEUT), 1.209 (GiBUU) and 1.470 (NuWro), all consistent with a nucleon. So
a `qel` sample above W ≈ 1.5 GeV is GENIE-specific and entirely charm.

### The hadronic-mass branches, `W` and `Ws`

The `W` branch is **not** a true invariant mass, despite the name pairing with
`Ws`. `gNtpConv.cxx` computes it as `W² = M² + 2 M ν − Q²` with
`M = kNucleonMass` — that is, the lepton-only formula the common output calls
`w_gev`, with a constant differing from ours by 7e-9 GeV. `Ws` is the selected
generator-level W. This makes `W` an exact independent reference for `w_gev` and
*not* a reference for `w_true_gev`. GENIE also fills `W`/`x`/`y` for coherent
events, where the common output blanks all three.

**How exactly the vertex closes.** The charm lines above are sharp for a reason
that doubles as a measurement of this. For a
quasi-elastic event nothing is produced but the lepton and one nucleon, so
`p_nu + p_N(initial) - p_l = p_N(final)` — precisely the four-vector
`w_true_gev` takes the invariant mass of. An escaping nucleon is on shell, so if
the reported `p_N` is the one the vertex used and nothing else absorbs
four-momentum, `w_true_gev` must be a delta function at `m_p` or `m_n`.

GENIE's is: 1st and 99th percentiles both **0.9383**, the proton mass. The others
spread, and the deviation tracks how far the nucleon they report sits from a free
one (medians over `qel`, same 20k run):

| Generator | stored initial `m_N` | `w_true_gev` | offset |
|---|---|---|---|
| GENIE | 0.9006 (off shell) | 0.9383 | exact |
| NuWro | 0.8965 (off shell) | 0.9407 from `e/in`; now 0.9383 (see below) | +1 MeV, 1–99% spread 0.78–1.12 |
| NEUT | 0.9396 (**on shell**, = `m_n`) | 0.9922 | +53 MeV, 0.96–1.05 |
| GiBUU | 0.8878 (bound, potential included) | 0.8963 | −43 MeV, 0.72–1.05 |

GENIE reports the off-shell nucleon that makes its own vertex exactly two-body,
which is why its column is a delta. **NuWro's case has been traced to its
source**: `qelevent1.cc` subtracts a *local-density-dependent* binding energy from
the initial nucleon before solving the kinematics, so the vertex does not balance
against the four-vectors it stores — see
[nuwro.md](nuwro.md#why-the-quasi-elastic-vertex-does-not-close) for the
measured energy and momentum deficits. NuWro's row is the `e/in`-based W. NuWro's
`w_true_gev` is now built from the outgoing nucleon instead (see
[nuwro.md](nuwro.md#w_true_gev-is-nuwros-own-w-taken-from-the-outgoing-hadrons)),
so its column is a delta at `m_p` too, and the spread above describes NuWro's
vertex rather than the column. NEUT's and GiBUU's binding treatments have
**not** been read; that NEUT is the only one storing an on-shell nucleon and has
the only upward offset, the largest, is suggestive but not established
(`.claude/TODOS.md`).

The practical consequence needs no such confirmation: **`w_true_gev` reproduces a
generator's own W exactly only for GENIE** (it matches gst `Ws` to 2e-13); for the
others it is displaced by tens of MeV — the same effect that lets 2.8% of NuWro's
`res` events leak past its `res_dis_cut`.

### The shallow-inelastic region: GENIE splits it, NEUT and GiBUU do not

GENIE has **no SIS category**. The region NEUT calls "multi-π, 1.3 < W < 2.0"
(modes 21/41) and GiBUU calls non-resonant background (`evType` 32/33/37) is in
GENIE shared between two generators, joined at `Wcut`. `config/CommonParam.xml`
states the scheme:

```
For W >  Wcut : RES -> 0,   +  DIS                -> full
For W <= Wcut : RES -> full + `DIS' (non-RES bkg) -> modified by DIS-HMultWgt-* params
```

**`Wcut` is per tune, not a property of the build.** It is read from the tune's
own `config/<tune>/CommonParam.xml` (`NonResBackground` param set), and the
shipped tunes span a wide range:

| Tune | `Wcut` (GeV) |
|---|---|
| `config/CommonParam.xml` (global default), `G21_11*`, `MK19_00a`, `EX00_00a` | 1.7 |
| `G18_10a_02_11b`, `AR23_20i`, `G24_20*`, `N24_20i` | 1.809 |
| `G18_10a_02_11a` and the other `*_02_11a` tunes of the G18_02/G18_10 families | 1.927862 |
| `G18_01a_02_11a`, `G18_01b_02_11a` | 2.2802 |

Quoting 1.7 for a tune that does not use it puts the RES/DIS transition in the
wrong place by up to 0.6 GeV — most of the SIS region. Below `Wcut` the resonant
piece is the full Rein–Sehgal/Berger–Sehgal calculation and the *non-resonant*
piece is produced by the DIS generator with the KNO multiplicity tune applied
(`KNOTunedQPMDISPXSec`, `AGKYLowW2019`); above it, RES is switched off and DIS
runs unmodified.

**The cut is sharp, and it is on the same W the common output derives.** In the
20k-event `G18_10a_02_11a` run (`Wcut = 1.927862`), the largest `w_true_gev`
among `res` events is **1.927742 GeV** and *no* `res` event exceeds `Wcut` — a
1.2e-4 GeV agreement, which is one event's worth of sampling. Note the
asymmetry, visible in `output/plots_w/w_by_interaction.png`: only RES stops at
the line. The non-resonant DIS background carries **51%** of the DIS cross
section below `Wcut`, so `res` and `dis` overlap everywhere beneath it.

**Consequence for the common output.** Both pieces of the SIS region are
generated, nothing is dropped, and they are labelled by their originating
generator: the resonant part gets gst `res` → our `res`, the non-resonant part
gets gst `dis` → our `dis`. In the run above, 1845 `dis` events had W < 2 GeV —
9.3% of all `dis` events.

This is exactly why GiBUU's non-resonant background is labelled `dis` in this
framework (see [gibuu.md](gibuu.md#why-the-non-resonant-background-is-dis)): it
is the category GENIE would have given the same event. NEUT is the one that does
not line up — its modes 11/12/13 lump resonant and non-resonant single-pion
production into one code, which we map to `res`, whereas GENIE's non-resonant
single-π lands in `dis`. That mismatch is inherent to how the generators
decompose the region, and `neut_code` is the route to a like-for-like comparison
rather than something the current mapping can fix.

GENIE's own `NeutReactionCode` makes the same join explicit, handling
`is_res || (is_dis && !W_gt_2)` in a single branch and then classifying by
primary-hadron multiplicity — `npi > 1` → mode 21/41, exactly NEUT's multi-π
codes.

## Reweight universes and variations

A job with a `genie.universes` and/or `genie.variations` block (schema in
[../configuration.md](../configuration.md#reweight-universes-and-variations))
gets a third stage after `gevgen` and `gntpc`. It writes one weight per event
per universe, σ_universe/σ_tune at the event's stored kinematics, into
`events/universe_weights`. It writes one weight per event per switch variation
into `events/variation_weights`. The sampling conventions are in
[../physics.md](../physics.md#reweight-universes). This section is about what
GENIE Reweight can and cannot do, measured on Generator R-3_06_00 with
Reweight R-1_04_02.

### GENIE Reweight and `nf_genie_reweight`

GENIE Reweight ([GENIE-MC/Reweight](https://github.com/GENIE-MC/Reweight)) is a
separate package built against the Generator tree. **R-1_04_02** is used: it is
R-1_04_00, released alongside Generator R-3_06_00, plus a fix that makes the
RES, COH and DIS engines use the phase space stored in the event instead of a
hardcoded one.

It reads the GHEP file (`gtree`/`gmcrec`), not gst, because it needs each
event's stored kinematics, phase-space tag and FSI history. `events.ghep.root`
is kept for this.

Its API:

- `GReWeight` owns one engine per physics area (`GReWeightNuXSecCCQE`,
  `GReWeightNuXSecCCRES`, `GReWeightINuke`, …).
- Dials are set with `Systematics().Set(GSyst_t, value)`.
- `Reconfigure()` pushes them into the engines.
- `CalcWeight(event)` multiplies every engine's weight.

A dial value means `p = p_tune · (1 + dial · err)`, clamped at 0, where `err` is
the 1σ entry in the Generator's `config/GSystUncertaintyTable.xml`.

**Why the shipped apps are not used.**

- `grwght1p` scans a single dial.
- `grwghtnp` throws correlated universes, but adopts engines only for a subset
  of dials. It also hands `NormCCRES` the CCQE engine (upstream issue 2 below).

The framework instead builds **`nf_genie_reweight`**, a driver over the same
engines:

```
nf_genie_reweight <events.ghep.root> <universes.txt> <weights.root>
                  --tune <tune> [--message-thresholds <files>]
```

The first line of `universes.txt` holds one `<GSyst name>:<kind>` per column.
Every further line is one column set. The output is TTree `weights`, branch
`weights[N]/D`, one entry per event in input order.

- **`scale` columns are a multiplier on the tune's own value** (1 = nominal).
  The driver overrides GENIE's 1σ table to 1 for these dials, so GENIE's dial is
  exactly `value − 1`. The config's `sigma` is therefore the only prior: the
  table's own values, including its asymmetric NormCCQE (+20/−15 %) entry, never
  enter.
- **`raw` columns** pass the value to GENIE unchanged. These are the [0, 1]
  switches.
- **It builds the generation tune** (`--tune` = the job's `config_version`),
  because each engine takes its default model from the tune.
- **It adopts only the engines the dials need.** `GReWeightINuke` calls
  `exit(1)` in its constructor unless the tune's FSI model is hA2018.
- **It fails loudly** (exit 2) on a dial its table does not know, or one that no
  adopted engine handles under the tune. An example is `MaCCQE` under
  AR23_20i's z-expansion axial form factor, checked on a 25k-event AR23_20i C12
  sample.
- **It loops universes outer and events inner**, so `Reconfigure()` runs once
  per column, and re-reads each event for every column, because the engines
  modify the interaction while computing a weight.
- **It recomputes the nominal dσ** with the engine's own default model
  (`UseOldWeightFromFile(false)`) instead of reading the dσ stored in the event.
  See upstream issue 1: for G18_10a_02_11a's RES and COH models the stored value
  is unusable as the denominator.

The adapter builds the inputs:

- one row per universe, with every scale dial at its thrown value;
- then one row per variation, with that switch at its value and everything else
  at nominal.

It writes `universes.txt` and `universes.json`, runs the driver, and the
normalizer splits the columns into the two datasets.

**Runtime.** About 1 ms per event per universe with 22 scale dials on (every
dial of the table below except `NormCCQE`, `NormCCRES`, `NormCCCOH` and the two
`FrPiProd` fates) plus `RPA_CCQE`. 10 universes over 20k events took 207 s
(numu C12 CC, G18_10a_02_11a, Docker on the dev Mac). 100 universes per 20k-event chunk
therefore cost ~35 min, against ~25 min to generate the chunk with this tune
(75 ms/event, [../performance.md](../performance.md)). Dials outside the
cross-section engines are cheaper: four dials over 20k events × 6 columns ran in
37 s, including startup.

### Which dials are supported

Every dial below was checked on one sample: 20k numu C12 CC events,
G18_10a_02_11a, power law E⁻² on 0.5–5 GeV. Each was moved alone to scale 1.2
(switches to 1), and accepted only if it met all four conditions:

- the nominal weight is exactly 1;
- every weight is finite;
- it moves only the channel it should;
- its mean weight has the expected sign and size.

Dials outside this table fail config validation. NC and antineutrino variants
of the dials below have not been checked and are not yet accepted.

| Dials (config names) | Engine | Channel moved at +20 % (mean weight over that channel) |
|---|---|---|
| `MaCCQE` | CCQE, `kModeMa` | qel 1.220 (closure below) |
| `NormCCQE` | CCQE, default mode | qel 1.199 |
| `MaCCRES`, `MvCCRES` | CCRES, `kModeMaMv` | res 1.239, 1.456 |
| `NormCCRES` | CCRES, default mode | res 1.200 |
| `NonRESBGv{p,n}CC{1,2}pi` | non-resonant background | DIS events with W < 2 GeV only: 1–29 % of dis events move, dis mean 1.002–1.058 |
| `AhtBY`, `BhtBY`, `CV1uBY`, `CV2uBY` | DIS (Bodek-Yang) | dis 1.025, 1.001, 1.010, 1.020 |
| `MaCOHpi`, `R0COHpi`, `NormCCCOH` | COH | coh 1.055, 0.969, 1.200 |
| `NormCCMEC` | MEC | mec 1.200 |
| `MFP_pi`, `MFP_N` | hA2018 FSI | σ-preserving: all-event mean 0.9992 each |
| `Fr{CEx,Inel,Abs,PiProd}_{pi,N}` | hA2018 FSI fates | σ-preserving: all-event mean 0.9995–1.0002 |

**Switch variations** (`genie.variations`), where 0 is the tune and 1 is the
alternative, read from the engine sources:

| Dial | 1 means | Effect at 1 |
|---|---|---|
| `RPA_CCQE` | Nieves CCQE with RPA off | qel mean 1.063 |
| `XSecShape_CCMEC` | the Empirical MEC model's (T_l, cos θ_l) shape at the tune's MEC total σ | mec mean 1.025, single weights 0–8.5 |
| `DecayAngMEC` | MEC nucleon-cluster decay as 3 cos²θ about q (0: isotropic) | mec mean 0.979 |
| `Theta_Delta2Npi` | isotropic Δ → Nπ decay (0: the Rein-Sehgal distribution the engine assumes) | res mean 1.013 |

**Constraints the config enforces.**

- `MaCCQE` and `NormCCQE` select incompatible CCQE engine modes, so they cannot
  be combined. The same holds for `MaCCRES`/`MvCCRES` with `NormCCRES`.
- Of each fate group, at most three may be varied. `GReWeightINukeParams` needs
  one fate left as the "cushion" that absorbs unitarity, and exits with "There
  must be at least one cushion term" otherwise.
- The fate weights are 0 for an event whose cushion fate's scale factor is
  driven below 0. At +20 % on one nucleon fate this happened for 10–50 of 20k
  events. That is GENIE's own design, not a NaN.

**Rejected.**

- **`FormZone`:** at +20 % it moves the mean DIS weight to 0.854, where a
  formation-zone change should preserve σ. The engine also logs FATAL
  "INTRANUKE didn't set a valid rescattering code" on 224 of 20k events.
- **`AGKYxF1pi`, `AGKYpT1pi`:** not σ-preserving (dis mean 0.986 at +20 %), with
  single weights up to 2.4 and 5.0.
- **`DISNuclMod`:** its `CalcWeight` exits for any non-zero value.
- **`FrElas_*`:** not a fate in this Reweight version.

The z-expansion dials (AR23_20i) and the remaining dials are untested, so they
are not accepted.

**MaCCQE closure.**

- **Setup:** a QEL-Ma +20 % copy of the G18_10a_02_11a tune (QEL-Ma 0.961242 →
  1.1534904, via `GXMLPATH`) gave both the CCQE spline (`gmkspl`, 30 knots) and
  a 10k-event CCQE sample generated directly.
- **σ:** the flux-averaged σ_QE ratio from the splines is 1.2217. The mean
  reweighted QE weight is 1.2203 ± 0.0014. Per energy bin the agreement is
  within 0.1 % up to 2 GeV, and within 0.7 % in 2–5 GeV, which is limited by
  the 30-knot spline.
- **Q² shape:** reweighted against direct gives χ² = 2.7 for 8 degrees of
  freedom over nine bins to 3 GeV². The unweighted nominal against direct gives
  χ² = 46.9.

**Coverage.** The universes cover what GENIE Reweight can reweight. The README
of GENIE Reweight itself states that this is not the full systematic error of
any tune. Nuclear ground state, binding and Pauli blocking have no working
knob for these tunes. The universe spread is therefore a lower bound on GENIE's
model uncertainty.

### Upstream issues (to report to the GENIE team)

1. **The stored dσ does not match the model's own dσ for the RES and COH
   engines.** With G18_10a_02_11a, the RES and COH engines compare
   `event.DiffXSec()` against their default model's `XSec(interaction,
   event.DiffXSecVars())` at the stored kinematics, and warn on each of the first
   20 events: "default dxsec does not match dxsec saved in tree".
   - **Size:** for the first eight events of each, the recomputed value was
     1.05–16× (RES, Berger-Sehgal) and 7–38× (COH) smaller than the stored one.
   - **Effect:** the default `UseOldWeightFromFile(true)` divides by the stored
     value, so every weight is noise. MaCCRES at 1.001 gave RES events a mean
     weight of 0.61; MaCOHpi at 1.2 gave 0.10.
   - **Not the model:** reconfiguring the model copy with the unchanged Ma
     reproduces the default exactly.
   - **Workaround:** `nf_genie_reweight` recomputes the nominal. MaCCRES at 1.001
     then gives 1.0012.
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
   events without a valid INTRANUKE rescattering code (see Rejected above).

## Container build

`setup/apptainer/genie.def` mirrors `setup/Dockerfile.genie` (see
[../containers.md](../containers.md)). Five things in that build are deliberate
and non-obvious:

- **ROOT 6.24.08, not newer.** ROOT 6.26+ changed `TXMLEngine::GetAttr()` to
  return `nullptr` where 6.24 returned an empty string, which crashes GENIE
  R-3_06_00's `AlgConfigPool::LoadRegistries`.
- **`-Dminuit2=ON` is required.** Without Minuit2, every Nieves-MEC (2p2h) event
  segfaults in `MECGenerator::GetXSecMaxTlctl`. This was originally misattributed
  to x86_64 emulation; native cluster builds ruled that out.
- **GENIE resolves its tunes and data files from `$GENIE`**, so relocating a
  `--prefix` install is fragile — the build stays in place.
- **Pythia6 sources are pre-staged**: pythia.org answers with a 302 redirect that
  breaks `curl -f -O`. The shared builder is `setup/lib/build_pythia6.sh` (NuWro
  needs it too).
- **GENIE Reweight and `nf_genie_reweight` are built in the builder stage**
  against `$GENIE`. Reweight has no configure step and reads the Generator's
  `Make.config`. The driver's Makefile rule is `grwght1p`'s with the names
  swapped, so its link line cannot drift. The Reweight libraries, the driver
  and the Reweight headers are then copied into `$GENIE/lib`, `$GENIE/bin` and
  `$GENIE/src`, so the runtime stage, the Apptainer payload and its wrapper need
  no further paths. The headers matter at run time: without them, loading the
  libraries' ROOT dictionaries prints cling "Missing FileEntry" errors for every
  Reweight class. The Apptainer payload exposes the driver as
  `bin/nf_genie_reweight` through the same wrapper body as `gevgen`.

## Known limitations

- The shipped `gxspl-NUsmall.xml` covers nue/nuebar/numu/numubar only; ν_τ runs
  need a spline set that includes it.
- A GENIE array run produces per-chunk HDF5 files; chunk merging after a Slurm
  array is not yet automated (framework-wide, tracked in `.claude/TODOS.md`).
