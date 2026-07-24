from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .base import ConfigTranslator
from ..flux import build_flux

# Number of equal-width bins used to approximate a continuous spectrum as a
# GiBUU user flux file (nuExp=99). GiBUU allocates the flux arrays dynamically.
FLUX_NBINS = 500

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

# (protons, neutrons) per nucleus; A = protons + neutrons. Mirrors the mapping
# used by the NuWro translator.
NUCLEUS_COMPOSITION = {
    "Ar40": (18, 22),
    "C12": (6, 6),
    "O16": (8, 8),
    "Fe56": (26, 30),
    "Ca40": (20, 20),
}


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
        current = str(physics.get("current", "cc")).lower()

        protons, neutrons = NUCLEUS_COMPOSITION[nucleus]
        mass_number = protons + neutrons

        flavor_id = FLAVOR_ID[particle]
        process_id = PROCESS_ID[current]
        if particle.endswith("bar"):
            process_id = -process_id

        # Each GiBUU ensemble simulates the whole nucleus but only a fraction of
        # its A nucleons yields an accepted (cross-section-weighted) interaction,
        # so the perturbative event yield is empirically ~A/2 events per ensemble
        # (measured: ~6 events/ensemble on carbon). Size numEnsembles to land
        # near the requested count. The exact number is stochastic and only
        # approximate. GiBUU warns and aborts for numEnsembles < 100 unless the
        # value is given negative (its documented "enforce anyway" override for
        # small runs), so negate small ensemble counts.
        events_per_ensemble = max(1.0, mass_number / 2.0)
        num_ensembles = max(1, math.ceil(event_count / events_per_ensemble))
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

        gibuu_jobcard = self._render_jobcard(
            version_year=version_year,
            num_ensembles=num_ensembles,
            proton_number=protons,
            mass_number=mass_number,
            process_id=process_id,
            flavor_id=flavor_id,
            enu_gev=enu_gev,
            seed=seed,
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
            "mode": physics.get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
            "gibuu_jobcard": gibuu_jobcard,
            "gibuu_flux_table": gibuu_flux_table,
        }

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
        # Fortran namelist jobcard. path_to_input points at the buuinput data
        # in the staged payload tree used by both the standalone GiBUU payload
        # image and the composed nf-base runtime; EventFormat=4 selects RootTuple ROOT
        # output. numTimeSteps=0 skips FSI transport for a fast, valid event
        # file (sufficient for the ROOT-output smoke test).
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
      numTimeSteps    = 0
      num_runs_SameEnergy = 1
    path_to_input   = '/opt/nf/generators/gibuu/GiBUU/buuinput'
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
      includeQE      = .true.
      includeDELTA   = .true.
      includeRES     = .true.
      includeDIS     = .true.
      include1pi     = .true.
      include2p2hQE  = .true.
/
{sigma_mc_block}&neutrinoAnalysis
      outputEvents = .false.
/
&EventOutput
      WritePerturbativeParticles = .true.
      EventFormat = 4                 ! ROOT (RootTuple)
/
"""
