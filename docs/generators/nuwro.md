# NuWro

Generator-specific domain knowledge. The conventions this is normalized onto are
in [../physics.md](../physics.md); the run pipeline is in
[../architecture.md](../architecture.md).

Files: `generators/nuwro.py`, `translators/nuwro.py`, `normalizers/nuwro.py`,
`setup/Dockerfile.nuwro`, `setup/apptainer/nuwro.def`.

## Versions and provenance

Built from source off the NuWro/nuwro GitHub tags. `CODE_VERSIONS` carries
`nuwro_25.11` only.

`config_version` is `"default"` — NuWro parameter-set versioning has not been
defined yet, so there is no equivalent of a GENIE tune. Adding real parameter-set
versions means extending the `config_versions` list on the adapter and mapping
each to a set of `params.txt` overrides.

## How it is run

Single stage. The adapter writes a `params.txt` of `key = value` lines into the
work directory and invokes `nuwro -o events.root -i params.txt`. Everything the
run needs is in that file — there is no card-plus-flux-file split as in NEUT or
GiBUU, because the spectrum is encoded inline (see below). The file also pins
`FSI_on = 1`, the intranuclear cascade that fills `e/post`; it is NuWro's
default too (`src/params_all.h`, nuwro_25.11), and with it off NuWro copies
`e/out` into `e/post` (`nuwro.cc`), which would make the final-state columns
silently pre-FSI.

**NuWro resolves `data/` relative to its binary.** Under Docker the container
therefore runs with workdir `/opt/nuwro`, and the input/output paths are given
explicitly as `/work/params.txt` and `/work/events.root`. The Apptainer payload
wrapper does the same thing by absolutizing relative `-i`/`-o` arguments before
`cd`-ing into the install tree — this is the one generator whose wrapper must
`cd` (NEUT's deliberately must not).

**Native binary first.** `build_run_command` checks `shutil.which` before
considering a container; on the cluster the Slurm task already runs inside the
Apptainer image, which cannot nest. The branch order is load-bearing.

## Flux handling

NuWro cannot take a continuous function. With `beam_type = 0` the spectrum is
encoded inline in the `beam_energy` parameter:

- a single value → monoenergetic beam;
- `E0 E1 a0 a1 … a(n-1)` → a histogram of `n` **equal-width** bins over
  `[E0, E1]` with unnormalized bin weights `a_i`.

That matches `Flux.to_histogram` exactly (equidistant edges). `FLUX_NBINS = 500`;
NuWro's own parser caps at 5000.

**Energies are in MeV** in this string, so GeV values are scaled by 1000 —
`_beam_energy` is the only place the conversion happens on the input side.

The target is selected with `target_type = 0` plus `nucleus_p` / `nucleus_n`,
derived from the nucleus name via `particles.nucleus_composition`, so any isotope
NuWro itself supports works without a table entry in this framework.

Unlike GENIE and NEUT, NuWro does not stamp the sampled flux into its output, so
the normalizer rebuilds the histogram from the run config with the same
`to_histogram(nbins=FLUX_NBINS)` call the translator used. That is safe here only
because the two use one code path and equal-width binning, with no
generator-side reinterpretation in between.

## Cross-section weight

NuWro rejection-samples, and every accepted event's raw `weight` (the `e/weight`
branch) is **the same constant for the whole run**: the flux-averaged total cross
section in cm². This is set in NuWro's production event loop
(`NuWro::real_events`, `src/nuwro.cc`): `e->weight = _procesy.total();`, where
`chooser::total()` (`src/chooser.h`) sums the flux-averaged mean cross section
over all *active* dynamics channels. The energy dependence of the physics lives
entirely in how many events land in each energy bin, not in the weight value.

```
xsec_weight_i = raw_weight_i · 1e38 / (n_events · phi_hat(E_i))
```

**Already per nucleon — do not divide by A.** Each event's target nucleon is
drawn by `nucleus::get_nucleon()` (`src/nucleus.cc`) as a single representative
nucleon of the whole nucleus, chosen proton vs. neutron with probability equal to
its isotopic fraction (`frac_proton()` / `frac_neutron()`) — i.e. uniformly over
all A nucleons — and no compensating factor of A is applied anywhere in
`qelevent1.cc` / `makeevent()` afterwards. The ensemble average of the raw weight
therefore already is the isospin-weighted per-nucleon cross section. Confirmed
empirically: NuWro's own console total, rescaled by `XSEC_SCALE = 1e38`, comes
out around 1 near 1 GeV, exactly the expected per-nucleon magnitude. Dividing by
the nucleon count again would double-count the normalization. This is the
opposite of GENIE, whose splines are whole-nucleus.

`xsec_norm_count` is the chunk's event count.

## Weak current

The current is applied through NuWro's `dyn_*` switches, written out **in full**
for every current — including the channels that are off — so the params file
states the complete dynamics set rather than inheriting half of it from NuWro's
own defaults (whose shipped `data/params.txt` is CC-only: all `_cc` on, all `_nc`
off).

The nuclear-target grid is `dyn_<channel>_<current>` over
`DYNAMICS_CHANNELS = (qel, res, dis, coh, mec)`. Three channels sit outside that
grid and are pinned explicitly:

| Switch | Setting | Why |
|---|---|---|
| `dyn_hyp_cc` | follows the CC switch | Quasi-elastic hyperon production — a genuine CC channel (antineutrinos only) |
| `dyn_lep` | **off for every current** | ν-e scattering. On in NuWro's defaults, mixes both currents, and its target is an atomic *electron* — its cross section is not on the per-nucleon normalization `xsec_weight` uses, and the `interaction` label would only mark it `other` |
| `dyn_qel_el` | off | (Quasi-)elastic *electron* scattering, for electron beams; irrelevant to a neutrino run |

No `compute_xsec_weight` change accompanies the current selection: the raw weight
is `chooser::total()` over the *active* channels, so restricting channels
rescales the weight by construction.

## Output and interaction taxonomy

Tree `treeout`. The particle branches are **jagged**:

- `e/in/in.{t,x,y,z}` — components are `(E, px, py, pz)`. Index 0 is the beam
  neutrino, followed by the struck initial-state system, whose size is **not**
  fixed. Measured on a 100k-event numu/Ar40 run: coherent events carry no
  initial-state hadron at all (the beam alone), `qel`/`res`/`dis` exactly one
  nucleon, and `mec` **two or three**. A handful of events (9 in 100k) carry an
  atomic *electron* there instead — the ν-e elastic channel, whose target is not
  a nucleon.
  `e/in` is read only to decide *whether* an event has a struck-system W: it
  must carry a nucleon (`e/in/in.pdg` 2112/2212, selected by PDG rather than
  index), and events without one get the `w_true_gev` placeholder. The W itself
  comes from `e/out`, for the reason given in
  [`w_true_gev` is NuWro's own W](#w_true_gev-is-nuwros-own-w-taken-from-the-outgoing-hadrons).
- `e/out/out.{t,x,y,z}` — index 0 is the primary outgoing lepton, then the
  pre-FSI outgoing hadronic system, whose invariant mass is `w_true_gev`. The
  vector **can be empty** for an event. Short entries are zero-padded and flagged
  by `_has_leading`, which is passed to `derive_kinematics(valid=...)` so the
  whole kinematic block is blanked rather than filled with zeros. With the
  spectral function (`sf_method = 1`, which these runs use) a correlated QE event
  also carries its SRC partner nucleon at index 2 (`sfevent.cc`: `out = [l, N1,
  N2]`; 1642 of 8218 QE events on the 20k run, exactly those with
  `flag.isCorrelated`). The partner is a spectator, not part of the vertex, so
  for `qel` only index 1 is taken. `flag.isCorrelated` itself cannot be used as
  the marker: it is set on 13424 of the 20k events, so outside QE it holds
  garbage.
- `e/post/post.{pdg,t,x,y,z}` — the particles leaving the nucleus after the
  cascade, summarized into the
  [final-state columns](../physics.md#final-state-content). It includes the
  outgoing lepton, which the summary excludes.
- `e/dyn`, NuWro's channel code, carried verbatim as `native_interaction_code`.
- `e/weight`, `e/flag/flag.cc`, and the class flags
  `e/flag/flag.{qel,res,dis,coh,mec}`.

**All momenta are in MeV** (`MEV_PER_GEV = 1000.0`); the normalizer converts on
read, so nothing downstream sees MeV.

The class flags share GENIE's names and go through the same shared priority chain
`normalizers/base.interaction_from_flags` (qel → res → dis → coh → mec → other).
`is_cc` comes from `flag.cc`, which is a separate flag — the class flags span
both currents.

### The RES/DIS split is a hard cut at `res_dis_cut`, plus a blend *inside* RES

Three parameters govern it (`src/params_all.h`, values in MeV, all defaults the
framework does not override):

| Parameter | Default |
|---|---|
| `res_dis_cut` | 1900 |
| `res_dis_blending_start` | 1600 |
| `res_dis_blending_end` | 1900 |

They are not only defaults on paper — NuWro writes them per event into `treeout`
as `e/par/par.res_dis_cut` and `e/par/par.res_dis_blending_{start,end}`, so a run
can be checked against its own output rather than against this table.

**The two dynamics are disjoint at `res_dis_cut`, with no overlap by
construction:**

- RES samples W uniformly from `[Wmin, min(res_dis_cut, kinematic max)]`, with
  `Wmin = 1080` (`src/dis/res_kinematics.cc`), so a `dyn_res` event can never
  exceed 1.9 GeV.
- DIS samples W from `[res_dis_cut, Wmax]` (`src/dis/disevent.cc`, log-uniform in
  `W - 1000`), and returns weight 0 outright if the beam energy cannot reach the
  cut. So a `dyn_dis` event can never fall below 1.9 GeV.

**The blending is a different thing entirely**: it interpolates the *composition
of the RES channel*, not the choice between channels. `resevent2.cc` documents
the model as Δ-resonant × `alfadelta(W)` + DIS-like single-pion × `alfadis(W)` +
non-SPP DIS, "the main idea behind is that DIS contribution simulates
non-resonant part".

**`res_dis_blending_start` is a kink, not the onset.** `src/dis/alfa.cc` makes
the non-resonant fraction piecewise-linear in *three* pieces, and it is non-zero
well below 1600 MeV:

```
W in (1080, W_min):   alfa * (W - 1080) / (W_min - 1080)          0     -> alfa
W in [W_min, W_max):  alfa + (1 - alfa) * (W - W_min)/(W_max - W_min)   alfa -> 1
W >= W_max:           1
```

with `alfa` a channel-dependent base of 0, 0.2 or 0.3. So the background ramps up
from **W = 1080 MeV** (the RES `Wmin`), merely reaching `alfa` at
`res_dis_blending_start` before accelerating to 1 at `res_dis_blending_end`.
Treating 1600 as the onset is contradicted by the events themselves: on the 20k
run the non-resonant part of `res` starts at 1.27 GeV, and 534 of its 2008 events
(26%) lie below 1.6 GeV.

`betadis` additionally *overrides* the parameter, substituting
`W_min = 1300 - 75 * bkgrscaling` for the channels whose base is zero
(`bkgrscaling` defaults to 0, giving 1300 MeV — which is where the observed
distribution does turn on). The blending start is therefore not one number across
channels, which is why plots draw only `res_dis_cut`.

The consequence matters for cross-generator classification: **NuWro's `res` is
not purely resonant near the boundary.** By 1.9 GeV the RES channel's content is
entirely non-resonant background, yet it is still labelled `res` by `flag.res`.
This is the same kind of mixing that makes NEUT's single-pion modes unalignable,
arrived at by a different route.

**Unlike NEUT, NuWro says which it was.** `e/flag/flag.res_delta` marks an event
whose hadronic final state came from the resonant term; when it is false the
final state was hadronized by PYTHIA, i.e. the non-resonant piece. That is what
fills the common output's `resonant_primary` column
([../physics.md](../physics.md#the-resonant_primary-column)). Measured on the 20k
numu CC C12 run, it tracks W exactly as the blending predicts: 100% true below
1.21 GeV, 93.6% at 1.3–1.4, 45.8% at 1.5–1.6, 7.5% at 1.8–1.9, 1.3% at
1.9–2.0. All `dyn_dis` events have it false.

**Its meaning is model-dependent, so `e/par/par.res_kind` is checked, not
assumed.** Under the hybrid model (`res_kind = 2`, NuWro's default and what the
framework runs) every resonant final state is generated through
`gen_final_particles_hybrid`, which sets the flag — hence the clean 100% at low
W. Under `resevent2.cc` (`res_kind != 2`) the below-PYTHIA-threshold branch
(`pythia_threshold = 1210` MeV) pushes the nucleon–pion pair *without* setting
it, so the flag would read false across the entire Δ peak and invert the
column's meaning. The normalizer raises rather than fill `resonant_primary` from
a run that used another model.

### `w_true_gev` is NuWro's own W, taken from the outgoing hadrons

NuWro's native W is `event::W()` (`src/event1.h`): the invariant mass of
`e/out[1:]`, every pre-FSI outgoing particle except the lepton. It is a method,
not a stored branch, but it is built from four-vectors the tree does store. For
RES it is the W NuWro sampled and cut on: `res_kinematics::set_kinematics` itself
sets `W = e.W()`.

It is **not** `(p_nu + p_N - p_l)` with `p_N` from `e/in`. RES and DIS both solve
the vertex against a *copy* of the struck nucleon with a binding energy subtracted
from its energy alone (`target.t -= get_binding_energy(...)` in
`res_kinematics.cc`; `nuc0.t -= _E_bind` in `disevent.cc`; for `nucleus_target =
2`, `E_b = Ef(local k_F) + kaskada_w`, as for QE below). They then build the
hadrons in that copy's rest frame with invariant mass W and boost them back. `e/in`
keeps the unbound nucleon. Measured on the 20k-event numu CC C12 run, the residual
`p_nu + p_N(e/in) - p_l - Σ e/out[1:]` is:

| channel | ΔE (MeV) p1 / p50 / p99 | \|Δp\| (MeV) |
|---|---|---|
| `res` | 8.8 / 33.4 / 45.6 | ≤ 3e-4 |
| `dis` | 8.5 / 33.4 / 45.6 | ≤ 5e-4 |
| `mec` | 0 / 0 / 6.0 | ≤ 2e-4 |

The three-momentum balances exactly and the energy is short by `E_b`, so the
binding energy is the whole difference. Reconstructing from `e/in` put W a median
42 MeV (`res`) and 54 MeV (`dis`) above NuWro's own, and 236 of 8400 `res` events
(2.8%) above `res_dis_cut`, out to 1.972 GeV.

`E_b` is not stored and depends on the local density at the vertex, so the
normalizer reads the hadrons instead. It passes `derive_kinematics` the nucleon
that balances them, `Σ e/out[1:] - p_nu + p_l` — the binding-corrected nucleon
NuWro actually used — and the shared formula returns the hadrons' mass. On the
same run `w_true_gev` then reproduces the cut exactly: `res` maximum **1.89997**
GeV (0 events above 1.9), `dis` minimum **1.90054** GeV. A fresh 100k-event run under
the same settings (numu CC C12, power law γ = −2 over 0.5–5 GeV) agrees: `res`
maximum 1.89994 GeV and `dis` minimum 1.90005 GeV, with no event on the wrong side. `qel` is exactly the
nucleon mass for every event, and `mec` is unchanged (its vertex already balanced).
Coherent events stay blank: their `e/out[1:]` is the lone pion, and `m_π` is not a
struck-system W.

### Why the quasi-elastic vertex does not close

For a quasi-elastic event `p_nu + p_N(initial) - p_l` should be exactly the
outgoing nucleon. NuWro's own pre-FSI outgoing nucleon *is* exactly on shell
(`e/out`, invariant mass 0.9383 at the 1st, 50th and 99th percentiles), and that
is what `w_true_gev` reports. A reconstruction from `e/in` does not reproduce it:
83% of QE events land within 30 MeV of `m_p`, 47% within 10 MeV.

The reason is that NuWro's QE vertex does not conserve four-momentum between the
particles it stores. Measured over 6576 QE events on the 20k numu CC C12 run,
`(p_nu + p_N) - (p_mu + p_N')` has a median energy deficit of **5.9 MeV** and a
median missing three-momentum of **18 MeV**.

`qelevent1.cc` shows where the energy goes: a binding energy is subtracted from
the initial nucleon before the kinematics are solved (`aa.t -= _E_bind`). Which
binding energy depends on `nucleus_target`, and for the framework's runs
(`nucleus_target = 2`, local Fermi gas) it is

```
_E_bind = nucleus::Ef(N0) + kaskada_w,   Ef = sqrt(k_F(local)^2 + M^2) - M
```

(`nucleus.cc:143-157`, `kaskada_w = 7` MeV). Because `k_F` is the **local** Fermi
momentum, `_E_bind` varies event by event with the density where the interaction
happened — roughly 7 to 32 MeV across carbon. That is the local density
approximation of Juszczak, Nowak & Sobczyk (NuInt04, *Spectrum of recoil nucleons
in quasi-elastic neutrino-nucleus interactions*) carried into the current code. It
is a spread by construction, not a constant offset, which is why a W
reconstructed from `e/in` is a smeared distribution rather than a shifted delta.

Note that the *momentum-dependent optical potential* which is the other half of
that paper is a different option — `nucleus_target = 6`, `momentum_dependent_potential_kinematics`
— and is **not** what these runs use.

**Not fully explained.** Two things do not follow from the above and remain open
(`.claude/TODOS.md`): a 5% tail beyond 100 MeV in the `e/in`-based W (99th
percentile 227 MeV, 99.9th 458 MeV), far larger than any binding energy, and
equally present with and without an SRC partner (4.9% and 5.4%), so the partner
does not cause it; and the missing three-momentum is
not a pure rescale along the outgoing nucleon's direction, which is what the
paper's "exit from the nuclear potential" step would produce. The measured
dependence of the `e/in`-based W on the struck nucleon's momentum is also only ~3 MeV
across 0–300 MeV, much weaker than `_E_bind`'s own ~30 MeV variation, implying
most of `_E_bind` is already reflected in the (off-shell, median mass 0.8965)
nucleon that `e/in` reports.

## Container build

`setup/apptainer/nuwro.def` mirrors `setup/Dockerfile.nuwro` (see
[../containers.md](../containers.md)). Non-obvious points:

- **Pythia6 is required** and built by the shared `setup/lib/build_pythia6.sh`
  (GENIE needs it too).
- ROOT 6.26+ dropped `TPythia6`/`libEGPythia6`, so the build adds the standalone
  **ROOTEGPythia6** package with `BUILTIN=ON`.
- **That build must not happen under `/tmp`.** The build path is baked into the
  ROOT PCM, and Apptainer bind-mounts the host `/tmp` over the image's at
  runtime, which would shadow the recorded dictionary path. It is built under
  `/opt` instead — one of the few deliberate divergences between the def and its
  Dockerfile.
- NuWro's `CMakeLists.txt` hardcodes the install prefix to the source directory,
  so it is built in place rather than relocated.

## Known limitations

- **NuWro 25.11 aborts with a ROOT buffer overflow on `numubar` × W184.**
  The binary dies with `*** buffer overflow detected ***: terminated`
  (SIGABRT) after finishing event generation, at every event count ≥ 3k
  (2k happens to pass). Reproduced twice on the odslserv01 interactive node
  (keystone_v2_sizing). The other three flavors and lighter nuclei are
  unaffected at 10k — `nue`/`nuebar` on W184 and `numubar` on Fe56 all pass.
  The cause is not diagnosed; the production grid therefore drops the
  `nuwro/W184/numubar` combination.
- `config_version` is `"default"` only; real parameter-set versions are not yet
  defined (tracked in `.claude/TODOS.md`).
- `run.log_level` is ignored — only the GENIE adapter maps it (tracked in
  `.claude/TODOS.md`).
- A NuWro array run produces per-chunk HDF5 files; chunk merging after a Slurm
  array is not yet automated (framework-wide).
