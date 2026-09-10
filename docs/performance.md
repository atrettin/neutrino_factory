# Generator performance

Measured wall-clock runtimes of the four generators as a function of event
count, generator version/tune, and the local execution pipeline. This is a
single-point measurement on one machine (facts below); treat the absolute
numbers as machine-specific and use them for relative comparisons and for
sizing Slurm time limits, not as portable constants.

## Scope of this measurement

- Probe: **μ neutrino (PDG 14)** only; target: **Fe56** (PDG 1000260560) only.
  No flavour or target-size comparison is included in this round.
- Flux: power law, 0.5–10 GeV, γ = −2, for every job (the production
  convention of giving GiBUU a flatter γ = −1 spectrum was *not* used, so its
  numbers are at a non-production flux).
- `physics.mode: inclusive`, `current: cc` (single generation pass for every
  generator — GiBUU's `inclusive` two-pass mode is not exercised).
- One chunk per job, so each measured run is one generator invocation.
- Executed 2026-09-08 (UTC) on the machine described below.

## Machine

| Fact | Value |
|---|---|
| Hostname | `odslserv01` (interactive node of the MPCDF/ODSL cluster) |
| Host kernel | Linux 5.14.0-687.17.1.el9_8.x86_64 (AlmaLinux 9) |
| Shell environment | `nf-dev` Apptainer container (Debian 12 bookworm); no nested containers — generators run as native payloads from `/opt/nf/generators/<gen>/<code_version>/bin/` via the `nf-run` wrapper |
| CPU | AMD EPYC 7662 (Rome), 128 threads visible from the container, AVX2 (no AVX-512 flags exposed) |
| Memory | 1007 GB total; no cgroup CPU or memory limit |
| Python | 3.13.15 (numpy 2.5.3, h5py 3.16.0, uproot 5.7.6) |
| Framework | `neutrino-factory` v0.1.0, git `5ca28ec` (branch `main`) |
| Output storage | gpfsu `/u` (28 TB filesystem, 14 % used at run time) |

Generator versions, as printed by the binaries themselves: GENIE **3.06.00**
(`R-3_06_00` payload) and **3.04.00** (`R-3_04_00` payload), GiBUU
**"Release 2025, patch 5 (April 24, 2026)"**, NEUT **5.7.0** (`nuint2024`
payload), NuWro **`nuwro_25.11`** (catalog git tag; the binary prints no
version string).

## Generators and tunes benchmarked

`neutrino-factory list-generators` at run time:

| generator | code_version | config versions available |
|---|---|---|
| genie | R-3_06_00 | AR2320i00000, G1802a00000, G1810a0211a, G1810a0211b, N2420i0211b |
| genie | R-3_04_00 | AR2320i00000, G1801a00000, G1802a00000, G1810a0211a, G1810a0211b, G2111a00000, N1810j0211a |
| gibuu | release2025 | default |
| neut | 5.7.0-nuint2024 | default |
| nuwro | nuwro_25.11 | default |

GENIE tunes are deduplicated across code versions (a tune staged under both
payloads is benchmarked once, under the code version that serves it), giving
**8 distinct tunes**. Seven of them ran; one did not:

- **N18_10j_02_11a (R-3_04_00) could not be run.** Its cross-section spline is
  staged on disk (so the catalog lists the tune as available), but the
  R-3_04_00 payload ships no `genie/config/N18_10j` directory, and `gevgen`
  aborts: `FATAL TuneId: No valid tune directory associated with N18_10j_02_11a`.
  Availability of the spline and of the tune's physics configuration are two
  independent conditions; only the former is checked at configuration time.

Each GENIE tune ran under the code version its staged spline lives under:
R-3_06_00 served AR23_20i, G18_02a, G18_10a_02_11a, G18_10a_02_11b and
N24_20i; R-3_04_00 served G18_01a, G21_11a and (attempted) N18_10j.

## Method

Each benchmark point is one config with one job (one chunk), timed as the
end-to-end wall clock of

```
neutrino-factory submit --config <cfg> --executor local
```

which covers manifest planning, the generator invocation itself, output
normalization to the common HDF5, and the (trivial) merge of the single
chunk. All four generators are single-threaded; points within a phase ran up
to 8 in parallel on the 128-thread node, so individual runtimes are not
meaningfully affected by each other.

Event-count ladder per combo: **1,000 → 10,000 → one "big" point** sized from
the measured 1k→10k slope to predict ≤ 7 min, chosen from
{20k, 50k, 100k} (cap 2 M events, not reached). Phases (UTC): 1k at
12:12–12:15, 10k at 12:38–12:51, big at 13:25–13:31.

## Results

Wall-clock runtimes (seconds, end-to-end pipeline). "big" is the largest
point actually run for that combo; the requested event count is in
parentheses. All rows: numu, Fe56, CC, power law 0.5–10 GeV γ=−2.

| generator (code_version) | tune / config | 1k events | 10k events | big |
|---|---|---:|---:|---|
| genie (R-3_04_00) | G18_01a_00_000 | 31.6 | 64.6 | 322.7 (100k) |
| genie (R-3_06_00) | G18_02a_00_000 | 31.9 | 65.6 | 322.7 (100k) |
| genie (R-3_04_00) | G21_11a_00_000 | 33.4 | 79.4 | 238.4 (50k) |
| genie (R-3_06_00) | AR23_20i_00_000 | 37.2 | 106.6 | 391.0 (50k) |
| genie (R-3_06_00) | N24_20i_02_11b | 36.8 | 108.2 | 167.4 (20k) |
| genie (R-3_06_00) | G18_10a_02_11a | 144.7 | 786.8 | — ¹ |
| genie (R-3_06_00) | G18_10a_02_11b | 147.2 | 780.1 | — ¹ |
| nuwro (nuwro_25.11) | default | 65.0 | 103.8 | 235.4 (50k) |
| neut (5.7.0-nuint2024) | default | 145.9 | 182.5 | 346.5 (50k) |
| gibuu (release2025) | default | 7.4 | 47.8 | 192.2 (50k → 42,298 events) |

¹ The 10 k-point already exceeds the 10-minute budget (13.1 min); no larger
point was run.

All requested event counts were met exactly by GENIE, NuWro and NEUT
(rejection-sampled generators). GiBUU's event count is an ensemble budget, and
it yielded 42,298 events from a 50,000 budget on Fe56 CC (85 %), consistent
with its ~A/2 events-per-ensemble yield.

### GENIE generation rate, from the per-event log timestamps

GENIE logs one line per event with a timestamp, so the generation loop alone
(first → last event, excluding spline load, `gntpc`, normalization) can be
isolated:

| tune | 1k | 10k | big |
|---|---:|---:|---:|
| G18_01a_00_000 (R-3_04_00) | 4.0 | 2.9 | 2.8 ms/event (100k) |
| G18_02a_00_000 (R-3_06_00) | 4.0 | 3.0 | 2.8 ms/event (100k) |
| G21_11a_00_000 (R-3_04_00) | 5.0 | 4.4 | 4.5 ms/event (50k) |
| AR23_20i_00_000 (R-3_06_00) | 9.0 | 7.2 | 7.9 ms/event (50k) |
| N24_20i_02_11b (R-3_06_00) | 9.0 | 7.3 | 7.7 ms/event (20k) |
| G18_10a_02_11a (R-3_06_00) | 117.0 | 75.2 | — |
| G18_10a_02_11b (R-3_06_00) | 119.0 | 74.6 | — |

### Fixed cost and per-event cost, all generators

Fitting the measured points as `t = fixed + rate × N`:

| generator | fixed cost | per-event rate |
|---|---:|---:|
| genie (G18_01a / G18_02a) | ~30 s | ~2.8 ms/event |
| genie (G21_11a) | ~30 s | ~4.4 ms/event |
| genie (AR23_20i / N24_20i) | ~30 s | ~7.7 ms/event |
| genie (G18_10a_02_11a / 11b) | ~30 s | ~75 ms/event |
| nuwro | ~62–71 s | ~3.3 ms/event |
| neut | ~142 s | ~4.1 ms/event |
| gibuu | ~4–12 s | ~3.6 ms/event (per ensemble-budget event) |

For GENIE the ~30 s fixed cost decomposes (measured from the log
timestamps of the 1k runs, identical across all seven runnable tunes):
~1–2 s of CLI/planning, **~23 s loading the staged cross-section spline**
(the 519 MB `xsecs.xml`, parsed on every run), the generation loop, then
~5 s of `gntpc` conversion, HDF5 normalization and merge.

## Findings

- **Runtime is linear in the event count** for every combo; the intercept is
  a per-run fixed cost and the slope is a per-event (per-tune) rate.
- **GENIE's fixed cost is dominated by the spline load (~23 s)**, which is paid
  once per chunk. Small runs (≲ 10k events) are mostly fixed cost: a 1k-event
  GENIE job takes ~30–50 s regardless of tune family.
- **The per-event rate is tune-dependent by a factor of ~27** on Fe56 CC:
  2.8 ms/event for G18_01a/G18_02a, ~4.4 for G21_11a, ~7.7 for
  AR23_20i/N24_20i, and ~75 for the G18_10a_02_11a/11b pair.
- The G18_10a_02_11x slowness is the model construction, not the channel mix.
  G18_01[a-d] are the adiabatic update of the GENIE v2 default
  (Llewellyn-Smith QEL, Rein-Sehgal RES, Bodek-Yang DIS, relativistic Fermi
  gas, empirical multinucleon model); G18_02[a-d] swap in Berger-Sehgal for
  RES/COH; **G18_10[a-d] additionally replace the QEL and multinucleon models
  with the Valencia (Nieves) models on a local Fermi gas with tabulated
  hadron tensors** (Tena Vidal 2021, arXiv:2104.09179, §4.2.1–4.2.3). The
  The R-3_06_00 runtime confirms the heavier machinery is active: the two
  G18_10a_02_11x runs load four 120×120 Nieves hadron-tensor tables for Fe56
  (`TabulatedHadronTensorModelI`, `HadTensor120-Nieves-1000280560-*.dat`)
  that none of the other five runnable tunes loads.
  Measured at 10k events, the interaction mix is comparable across all seven
  runnable tunes (~29–33 % each of qel/res/dis, 7–11 % mec), so the rate gap
  is per-event model cost.
- **The 02_11a tunes are preliminary versions of the 02_11b tuning**
  (Tena Vidal 2021, arXiv:2104.09179, §4.2); the a/b pair was benchmarked
  separately because both are staged, and they are equally slow (75.2 vs
  74.6 ms/event at 10k). That equality is a **ν_μ result only**: on ν̄_μ the
  pair splits sharply. Measured at 10k events on Fe56, inclusive current,
  power law 0.1–500 GeV γ=−2, run serially on odslserv01 (keystone_v2_sizing):
  `02_11a` took **1533 s (~150 ms/event)** vs `02_11b`'s **410 s (~40 ms/event)**
  — a ~3.7× gap. `02_11a`'s ν̄_μ sampling is far more expensive than its own
  ν_μ, which is why chunk sizing for that tune keys off the antineutrino rate.
- **NEUT carries the largest non-GENIE fixed cost (~142 s)** — cross-section
  data loading plus its two-stage `neutroot2` → `nf_flatten` processing — but a
  modest ~4.1 ms/event slope.
- **GiBUU is the fastest to small event counts** (7.4 s for 1k) thanks to a
  small fixed cost, at ~3.6 ms per ensemble-budget event.
- For Slurm sizing: budget ≈ 30 s + (rate × events) for GENIE (use the
  per-tune rate), ~145 s + 4.1 ms×events for NEUT, ~65 s + 3.3 ms×events for
  NuWro, ~10 s + 3.6 ms×events for GiBUU, plus the per-chunk overhead this
  measurement includes (it is one chunk; Slurm jobs here are also one task per
  chunk).

## Caveats

- Single probe (μν), single nucleus (Fe56), CC only in the main table. The
  effect of target size (e.g. H1 vs Fe56) and of antineutrinos is not measured
  there. The one ν̄_μ point that was run (G18_10a pair, above) shows a large
  flavor asymmetry, so ν̄_μ rates must not be assumed to match the ν_μ table.
- The 8 runs per phase overlapped on a shared interactive node; the jobs are
  single-threaded and the node had 128 threads, so CPU contention is
  negligible, but the filesystem (gpfs) is shared.
- The 1k points for G18_10a_02_11x show 117–119 ms/event, higher than their
  10k rate (75 ms/event): the first events of a run pay one-off setup
  (hadron-tensor and hadronizer initialization), which a 1k sample cannot
  amortize.
- `run.log_level: essential` was used for GENIE (one short stdout line per
  event); `quiet` would shave a small amount of the per-event cost at the
  largest runs.
- Artifacts (configs, driver scripts, per-run logs, `results.csv`, merged
  HDF5) live in the gitignored `scratch/bench/` directory.
