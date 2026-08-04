# GENIE

Generator-specific domain knowledge. The conventions this is normalized onto are
in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

Files: `generators/genie.py`, `translators/genie.py`, `normalizers/genie.py`,
`setup/Dockerfile.genie`, `setup/apptainer/genie.def`,
`setup/download_genie_xsec.sh`.

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
`qel`/`res`/`dis`/`coh`/`mec`, the neutrino momentum `(pxv, pyv, pzv)` and the
lepton four-vector `(El, pxl, pyl, pzl)` — **all in GeV**, so no unit conversion
is applied. For NC events the "outgoing lepton" branches hold the scattered
neutrino.

The mode flags go through the shared `normalizers/base.interaction_from_flags`
priority chain (qel → res → dis → coh → mec → other), which NuWro's `treeout`
also uses since it exposes branches with the same names.

`is_cc` comes from the gst `cc` branch. `xsec_norm_count` is recorded on the real
path only.

### GENIE supplies much finer codes than the five flags we read

The gst tree has 99 branches, and the five booleans above are the coarsest view
of the interaction it offers. Unused, but present:

| Branch | What it is |
|---|---|
| `neut_code` | **NEUT-equivalent mode**, in NEUT's own signed numbering — produced by `genie::utils::ghep::NeutReactionCode` (`Framework/GHEP/GHepUtils.cxx`) |
| `nuance_code` | NUANCE-equivalent mode |
| `resid` | Resonance ID for RES events (0 = P33(1232), …) |
| `dfr`, `imd`, `imdanh`, `singlek`, `nuel`, `amnugamma`, `charm`, `em`, `hnl` | Process flags outside our five |
| `hitnuc`, `hitqrk`, `sea`, `resc` | Struck nucleon / quark, sea-quark flag, rescattering code |
| `W`, `x`, `y`, `Q2` and the `Ws`/`xs`/`ys`/`Q2s` variants | True vs. hadronic-system-reconstructed kinematics |

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

### The shallow-inelastic region: GENIE splits it, NEUT and GiBUU do not

GENIE has **no SIS category**. The region NEUT calls "multi-π, 1.3 < W < 2.0"
(modes 21/41) and GiBUU calls non-resonant background (`evType` 32/33/37) is in
GENIE shared between two generators, joined at `Wcut`. `config/CommonParam.xml`
states the scheme:

```
For W >  Wcut : RES -> 0,   +  DIS                -> full
For W <= Wcut : RES -> full + `DIS' (non-RES bkg) -> modified by DIS-HMultWgt-* params
```

`Wcut = 1.7` GeV in this build (`CommonParam.xml`, `NonResBackground`). So below
1.7 GeV the resonant piece is the full Rein–Sehgal/Berger–Sehgal calculation and
the *non-resonant* piece is produced by the DIS generator with the KNO
multiplicity tune applied (`KNOTunedQPMDISPXSec`, `AGKYLowW2019`); above it, RES
is switched off and DIS runs unmodified.

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

## Container build

`setup/apptainer/genie.def` mirrors `setup/Dockerfile.genie` (see
[../containers.md](../containers.md)). Four things in that build are deliberate
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

## Known limitations

- The shipped `gxspl-NUsmall.xml` covers nue/nuebar/numu/numubar only; ν_τ runs
  need a spline set that includes it.
- A GENIE array run produces per-chunk HDF5 files; chunk merging after a Slurm
  array is not yet automated (framework-wide, tracked in `.claude/TODOS.md`).
