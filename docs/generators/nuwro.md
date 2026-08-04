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
GiBUU, because the spectrum is encoded inline (see below).

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
  neutrino, the struck nucleon follows.
- `e/out/out.{t,x,y,z}` — index 0 is the primary outgoing lepton, but the vector
  **can be empty** for an event. Short entries are zero-padded and flagged by
  `_has_leading`, which is passed to `derive_kinematics(valid=...)` so the whole
  kinematic block is blanked rather than filled with zeros.
- `e/weight`, `e/flag/flag.cc`, and the class flags
  `e/flag/flag.{qel,res,dis,coh,mec}`.

**All momenta are in MeV** (`MEV_PER_GEV = 1000.0`); the normalizer converts on
read, so nothing downstream sees MeV.

The class flags share GENIE's names and go through the same shared priority chain
`normalizers/base.interaction_from_flags` (qel → res → dis → coh → mec → other).
`is_cc` comes from `flag.cc`, which is a separate flag — the class flags span
both currents.

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

- `config_version` is `"default"` only; real parameter-set versions are not yet
  defined (tracked in `.claude/TODOS.md`).
- `run.log_level` is ignored — only the GENIE adapter maps it (tracked in
  `.claude/TODOS.md`).
- A NuWro array run produces per-chunk HDF5 files; chunk merging after a Slurm
  array is not yet automated (framework-wide).
