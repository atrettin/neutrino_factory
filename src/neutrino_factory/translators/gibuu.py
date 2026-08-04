from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from .base import ConfigTranslator, physics_current
from ..flux import Flux, build_flux
from ..particles import is_antineutrino, nucleus_composition

# Number of equal-width bins used to approximate a continuous spectrum as a
# GiBUU user flux file (nuExp=99). GiBUU allocates the flux arrays dynamically.
FLUX_NBINS = 500

# num_runs_SameEnergy in the jobcard: the number of independent GiBUU runs at the
# same flux/energy, each writing its own EventOutput.Pert.<run>.root. GiBUU
# normalizes perweight so that the sum over one run reproduces the cross section,
# so compute_xsec_weight must divide by this. Kept in one place so the jobcard
# and the xsec-weight normalization can never drift.
#
# Raising this above 1 is safe but only because the normalizer reads *every*
# part (normalizers.gibuu.pert_output_parts) — reading one while dividing by N
# would report sigma/N. Prefer numEnsembles for more statistics anyway: GiBUU
# divides it out of perweight itself (initNeutrino.f90 normalizes by the nucleon
# test-particle count summed over ensembles), so scaling it leaves each chunk an
# unbiased estimate of sigma, which is what makes merging chunks well-defined.
NUM_RUNS_SAME_ENERGY = 1

# Jobcard path of the flux table the adapter writes into the work directory.
# Deliberately CWD-relative: every pathway runs GiBUU with the work dir as its
# CWD (local subprocess cwd=, docker -w /work, apptainer natively inside the
# SIF at the real work dir), so an absolute /work path would only be valid
# under docker. The leading './' matters: GiBUU's ExpandPath uses a filename
# verbatim only if it contains a '/'.
FLUX_FILE_JOBCARD_PATH = "./flux.dat"

# GiBUU neutrino flavour_ID (independent of neutrino vs antineutrino, which is
# carried by the sign of process_ID).
FLAVOR_ID = {
    "nue": 1,
    "nuebar": 1,
    "numu": 2,
    "numubar": 2,
    "nutau": 3,
    "nutaubar": 3,
}

# GiBUU process_ID magnitude: CC=2, NC=3. Antineutrinos use the negative value.
PROCESS_ID = {"cc": 2, "nc": 3}

# A GiBUU jobcard selects exactly one process_ID, so there is no inclusive run:
# "inclusive" is generated as two passes, one per current, whose events are
# concatenated. That is exact rather than an approximation because GiBUU is
# cross-section-weighted: each event carries an absolute per-nucleon weight and
# each pass's weights already sum to that current's cross section, so the union
# sums to sigma_CC + sigma_NC. (A rejection-sampled generator could not be
# combined this way without reweighting by the relative cross sections.)
CURRENT_PASSES = {"cc": ("cc",), "nc": ("nc",), "inclusive": ("cc", "nc")}

# Seed offset applied to the second (NC) pass of an inclusive run, so the two
# passes do not draw the identical random sequence. Task seeds are hashed from
# (run.seed, job label, chunk) into [1, jobs.SEED_MODULUS] rather than laid out
# arithmetically, so this offset can only collide with another chunk's seed by
# the same negligible hash coincidence that build_task_manifest already checks
# for; the sum also stays inside the int32 range Fortran seeds need.
PASS_SEED_OFFSET = 1_000_000


class GiBUUTranslator(ConfigTranslator):
    name = "gibuu"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]
        physics = config["physics"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        nucleus = target["nucleus"]
        energy_range_gev = [flux.emin_gev, flux.emax_gev]
        seed = int(task["seed"])
        event_count = int(task["event_count"])
        current = physics_current(config)

        protons, neutrons = nucleus_composition(nucleus)
        mass_number = protons + neutrons

        if particle not in FLAVOR_ID:
            raise KeyError(
                f"Unknown neutrino particle '{particle}' for GiBUU probe. "
                f"Known: {', '.join(FLAVOR_ID)}"
            )
        flavor_id = FLAVOR_ID[particle]
        pass_currents = CURRENT_PASSES[current]

        # Each GiBUU ensemble simulates the whole nucleus but only a fraction of
        # its A nucleons yields an accepted (cross-section-weighted) interaction,
        # so the perturbative event yield is empirically ~A/2 events per ensemble
        # (measured: ~6 events/ensemble on carbon). Size numEnsembles to land
        # near the requested count. The exact number is stochastic and only
        # approximate. GiBUU warns and aborts for numEnsembles < 100 unless the
        # value is given negative (its documented "enforce anyway" override for
        # small runs), so negate small ensemble counts.
        #
        # An inclusive run splits those ensembles evenly over its two passes, so
        # the requested event count is the budget for the run as a whole rather
        # than per current. The resulting CC:NC split is not 50:50: each pass
        # yields whatever its own cross section produces from half the ensembles.
        events_per_ensemble = max(1.0, mass_number / 2.0)
        num_ensembles = max(
            1, math.ceil(event_count / (events_per_ensemble * len(pass_currents)))
        )
        if num_ensembles < 100:
            num_ensembles = -num_ensembles

        # Drive event generation from the framework flux via GiBUU's user-flux
        # path (nuExp=99, nuXsectionMode=16); the equidistant (energy, flux)
        # table is written to a file the adapter stages at FileNameFlux. A
        # degenerate range falls back to fixed-energy dSigmaMC (mode 6).
        monoenergetic = flux.emax_gev <= flux.emin_gev
        if monoenergetic:
            gibuu_flux_table = None
            enu_gev = flux.emin_gev
        else:
            gibuu_flux_table = self._flux_table(flux)
            enu_gev = None

        # GiBUU refuses to run unless the jobcard's version matches the code
        # release (e.g. "release2025" -> version=2025).
        version_year = int("".join(c for c in str(task["code_version"]) if c.isdigit()))

        # One rendered jobcard per pass; a single-current run has exactly one.
        gibuu_passes = []
        for index, pass_current in enumerate(pass_currents):
            process_id = PROCESS_ID[pass_current]
            # The beam sign comes from the probe's PDG code, not from the "bar"
            # suffix of its name: the name is a framework label, the sign is the
            # physics.
            if is_antineutrino(particle, "GiBUU"):
                process_id = -process_id
            gibuu_passes.append(
                {
                    "current": pass_current,
                    "jobcard": self._render_jobcard(
                        version_year=version_year,
                        num_ensembles=num_ensembles,
                        proton_number=protons,
                        mass_number=mass_number,
                        process_id=process_id,
                        flavor_id=flavor_id,
                        enu_gev=enu_gev,
                        seed=seed + index * PASS_SEED_OFFSET,
                    ),
                }
            )

        return {
            "generator": self.name,
            "command": "GiBUU.x",
            "beam_particle": particle,
            "nucleus": nucleus,
            "energy_range_gev": energy_range_gev,
            "number_of_events": event_count,
            "seed": seed,
            "flux_model": flux_config["type"],
            "flux_config": flux_config,
            "num_runs": NUM_RUNS_SAME_ENERGY,
            "mode": physics.get("mode", "inclusive"),
            "current": current,
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
            "gibuu_passes": gibuu_passes,
            "gibuu_flux_table": gibuu_flux_table,
        }

    def compute_xsec_weight(
        self,
        energies_gev: np.ndarray,
        raw_weights: np.ndarray,
        translated_config: dict[str, Any],
        flux: Flux,
    ) -> np.ndarray:
        """Recover the energy-resolved cross section from GiBUU perturbative weights.

        GiBUU is phase-space sampled and *weighted by cross section* (not
        rejection-sampled like NuWro): every generated interaction is written out
        carrying a perturbative weight (the ``weight`` branch of ``RootTuple``).
        Two properties of that weight, established from GiBUU's own source
        (``code/analysis/neutrinoAnalysis.f90`` header) and cross-checked against
        the production KM3NeT ``km3buu`` wrapper (which reads the identical
        branch):

        * **Units** are already ``1e-38 cm^2`` - exactly the common convention -
          so no ``XSEC_SCALE`` factor is applied (contrast the NuWro translator,
          whose raw weight is in bare cm^2 and needs a ``1e38`` rescale).
        * **Per nucleon**: the weight is already a per-target-nucleon quantity
          (km3buu multiplies by the mass number ``A`` to recover the whole-nucleus
          cross section, confirming the raw value is per nucleon), so no division
          by ``A`` is applied.

        GiBUU defines the weight so that, within a single run, the sum over all
        events reproduces the flux-folded total cross section
        ``sigma_avg = int sigma(E) * phi_hat(E) dE`` (numEnsembles is already
        folded into the weight; the only run-multiplicity factor is
        ``num_runs_SameEnergy``). Because a spectrum run's weights are therefore
        already flux-folded, recovering the differential ``sigma(E)`` requires
        dividing out the unit-normalized flux density at each event's energy -
        structurally the same flux division NuWro uses, but with a different
        constant:

            xsec_weight_i = raw_weight_i / (num_runs * phi_hat(E_i))

        so that ``sum_bin(xsec_weight) / bin_width -> sigma(E)`` in ``1e-38 cm^2``
        per nucleon. The flux histogram uses the same ``FLUX_NBINS`` binning the
        translator wrote into GiBUU's user-flux file, so the reconstruction
        matches the distribution GiBUU actually sampled.

        Negative weights (interference terms) are passed through unchanged - they
        are physical and must be summed as-is.
        """
        num_runs = max(1, int(translated_config.get("num_runs", NUM_RUNS_SAME_ENERGY)))

        # Monoenergetic run (GiBUU fixed-energy mode 6): there is no flux to
        # divide out, and the differential "/ bin_width" convention is degenerate
        # for a single energy. Sum over the run then reproduces sigma at that
        # energy, so the per-event weight is simply raw / num_runs.
        if flux.emax_gev <= flux.emin_gev:
            return np.asarray(raw_weights, dtype=np.float64) / num_runs

        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        widths = np.diff(edges)
        clipped = np.clip(contents, 0.0, None)
        integral = float(np.sum(clipped * widths))

        xsec_weight = np.zeros_like(raw_weights, dtype=np.float64)
        if integral <= 0.0:
            return xsec_weight

        bin_index = np.clip(
            np.searchsorted(edges, energies_gev, side="right") - 1, 0, len(clipped) - 1
        )
        flux_density = clipped[bin_index]
        nonzero = flux_density > 0.0
        flux_hat = flux_density[nonzero] / integral
        xsec_weight[nonzero] = raw_weights[nonzero] / (num_runs * flux_hat)
        return xsec_weight

    def xsec_norm_count(
        self, translated_config: dict[str, Any], event_count: int
    ) -> float:
        """The chunk's run multiplicity — ``num_runs``, *not* its event count.

        GiBUU is the one generator where these differ. Its per-event weights
        already sum to the flux-folded cross section within a single run
        (numEnsembles is folded into the weight), so a chunk's contribution to a
        merged estimate is measured in generator runs, not in events. Using the
        event count here would weight chunks by how many interactions GiBUU
        happened to produce — which varies with the cross section itself — and
        skew the merged average.
        """
        return float(max(1, int(translated_config.get("num_runs", NUM_RUNS_SAME_ENERGY))))

    @staticmethod
    def _flux_table(flux: Any) -> str:
        """Render GiBUU's user-flux file: two columns ``energy[GeV] flux``.

        Energies are equidistant bin centers (GiBUU requires equidistant bins);
        the flux column is a relative weight, normalized internally by GiBUU.
        Matches ``read_fluxfile`` (energy = middle of bin), which allows leading
        ``#`` comment lines.
        """
        edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        centers = 0.5 * (edges[:-1] + edges[1:])
        lines = ["# GiBUU user flux (nuExp=99) generated by neutrino-factory",
                 "# energy[GeV]  flux[arb. units]"]
        lines += [f"{float(e)} {float(w)}" for e, w in zip(centers, contents)]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_jobcard(
        *,
        version_year: int,
        num_ensembles: int,
        proton_number: int,
        mass_number: int,
        process_id: int,
        flavor_id: int,
        enu_gev: float | None,
        seed: int,
    ) -> str:
        # Fortran namelist jobcard. path_to_input is a placeholder resolved by
        # the adapter (GiBUUAdapter._buuinput_dir) because the buuinput location
        # is runtime- and version-dependent: the Docker image stages it at
        # /opt/GiBUU/buuinput while the version-namespaced Apptainer payload uses
        # /opt/nf/generators/gibuu/<code_version>/GiBUU/buuinput. EventFormat=4
        # selects RootTuple ROOT output.
        #
        # numTimeSteps=0 disables the FSI transport loop (time_max =
        # numTimeSteps*delta_T, so GiBUU.f90's PhaseSpaceEvolution loop never
        # runs). This is GiBUU's own documented setting for *inclusive* cross
        # sections -- every shipped neutrino jobcard carries the comment "for
        # inclusive cross sections set numTimeSteps = 0" -- and not a shortcut.
        # The cross section is fixed at the initial vertex: perweight, evType
        # and both lepton four-vectors are written from neutrinoProdInfo, a
        # write-once record of the initial event, and transport only inherits
        # perweight into the final states it produces. Verified by running one
        # jobcard both ways (numTimeSteps 0 vs. 150, same seed, 1000-event C12
        # CC): the hadron multiplicity rose 1.87 -> 3.08 per event, while
        # weight, evType and lepIn/lepOut were bit-identical and sum(weight)
        # agreed exactly. See docs/generators/gibuu.md.
        #
        # This holds only because the common output records no hadronic
        # observables. Adding any (pion multiplicity, knocked-out nucleons,
        # calorimetric energy, CCQE-like topology) requires numTimeSteps>0 with
        # numTimeSteps*delta_T comfortably exceeding the nuclear radius.
        #
        # Every reaction channel GiBUU makes available is switched on, because
        # the output is meant as an inclusive cross-section estimate; all but
        # includeQE default to .false. include2pi is easy to forget and biases
        # the total low: with new_eN=.true. (the default) GiBUU scales the
        # 1-pion background *down* above W=1.267 "to allow for 2pi contribution"
        # (neutrinoXsection.f90), and leaving 2pi off never adds that strength
        # back. include2p2hDelta is the one switch left off, and not by choice:
        # release2025 aborts on it via notInRelease("2p2p Delta")
        # (initNeutrino.f90:689) because the feature is unpublished. MEC is
        # therefore 2p2h-QE only, and evType 36 cannot occur; revisit when a
        # release enables it.
        #
        # Flux mode (enu_gev is None): nuExp=99 (user flux) + nuXsectionMode=16
        # (EXP_dSigmaMC) reads the equidistant (energy, flux) table at
        # FileNameFlux. Monoenergetic fallback (enu_gev set): nuExp=0 +
        # nuXsectionMode=6 (dSigmaMC) at the fixed energy in &nl_SigmaMC.
        if enu_gev is None:
            xsection_block = f"""      nuXsectionMode = 16             ! EXP_dSigmaMC (flux-integrated)
      nuExp          = 99             ! user-defined flux from file
      FileNameFlux   = '{FLUX_FILE_JOBCARD_PATH}'"""
            sigma_mc_block = ""
        else:
            xsection_block = """      nuXsectionMode = 6              ! dSigmaMC (fixed energy)
      nuExp          = 0              ! no experimental flux"""
            sigma_mc_block = f"""&nl_SigmaMC
      enu = {enu_gev:.4f}
/
"""
        return f"""! GiBUU neutrino jobcard generated by neutrino-factory
&input
      version         = {version_year}   ! must match the GiBUU code release
      eventtype       = 5          ! neutrino induced
      numEnsembles    = {num_ensembles}
      numTimeSteps    = 0          ! no FSI transport: GiBUU's documented setting
                                   ! for inclusive cross sections (see above)
      num_runs_SameEnergy = {NUM_RUNS_SAME_ENERGY}
    path_to_input   = '@NF_GIBUU_INPUT@'
      localEnsemble   = .true.
/
&initRandom
      SEED = {seed}
/
&target
      Z = {proton_number}
      A = {mass_number}
/
&initDensity
      densitySwitch = 2            ! static density (fixed target)
/
&initPauli
      pauliSwitch = 2             ! static Pauli blocking
/
&neutrino_induced
      process_ID     = {process_id}   ! CC=2, NC=3 (negative = antineutrino)
      flavor_ID      = {flavor_id}    ! 1=e, 2=mu, 3=tau
{xsection_block}
      includeQE        = .true.
      includeDELTA     = .true.
      includeRES       = .true.
      includeDIS       = .true.
      include1pi       = .true.    ! non-resonant 1pi background
      include2pi       = .true.    ! non-resonant 2pi background
      include2p2hQE    = .true.
      include2p2hDelta = .false.   ! not released yet (see comment above)
/
{sigma_mc_block}&neutrinoAnalysis
      outputEvents = .false.
/
&EventOutput
      WritePerturbativeParticles = .true.
      EventFormat = 4                 ! ROOT (RootTuple)
/
"""
