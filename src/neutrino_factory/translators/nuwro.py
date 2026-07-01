from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ConfigTranslator
from ..flux import build_flux

# Number of equal-width bins used to approximate a continuous spectrum as a
# NuWro `beam_energy` histogram. NuWro's parser caps at 5000 bins.
FLUX_NBINS = 500

# PDG codes for NuWro's beam_particle parameter.
PARTICLE_PDG = {
    "numu": 14,
    "nue": 12,
    "numubar": -14,
    "nuebar": -12,
    "nutau": 16,
    "nutaubar": -16,
}

# (protons, neutrons) for NuWro's nucleus_p / nucleus_n parameters.
NUCLEUS_COMPOSITION = {
    "Ar40": (18, 22),
    "C12": (6, 6),
    "O16": (8, 8),
    "Fe56": (26, 30),
    "Ca40": (20, 20),
}


class NuWroTranslator(ConfigTranslator):
    name = "nuwro"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        nucleus = target["nucleus"]
        energy_range_gev = [flux.emin_gev, flux.emax_gev]
        seed = int(task["seed"])

        protons, neutrons = NUCLEUS_COMPOSITION[nucleus]

        # NuWro (beam_type=0) reads the spectrum straight from `beam_energy`.
        # target_type=0 selects a single nucleus via nucleus_p / nucleus_n.
        nuwro_params = {
            "number_of_events": int(task["event_count"]),
            "random_seed": seed,
            "beam_particle": PARTICLE_PDG[particle],
            "beam_type": 0,
            "beam_energy": self._beam_energy(flux),
            "nucleus_p": protons,
            "nucleus_n": neutrons,
            "target_type": 0,
        }

        return {
            "generator": self.name,
            "command": "nuwro",
            "beam_particle": particle,
            "nucleus": nucleus,
            "energy_range_gev": energy_range_gev,
            "number_of_events": int(task["event_count"]),
            "seed": seed,
            "flux_model": flux_config["type"],
            "mode": config["physics"].get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
            "nuwro_params": nuwro_params,
        }

    @staticmethod
    def _beam_energy(flux: Any) -> str:
        """Render NuWro's `beam_energy` value (MeV) from the framework flux.

        NuWro (beam_type=0) encodes the spectrum inline: a single value is
        monoenergetic, while ``E0 E1 a0 a1 ... a(n-1)`` is a histogram with
        ``n`` equal-width bins over ``[E0, E1]`` and (unnormalized) bin weights
        ``a_i``. This matches ``Flux.to_histogram`` exactly (equidistant edges).
        Energies are in MeV, so GeV values are scaled by 1000.
        """
        emin_mev = flux.emin_gev * 1000.0
        emax_mev = flux.emax_gev * 1000.0
        if flux.emax_gev <= flux.emin_gev:
            # Degenerate range -> monoenergetic beam.
            return f"{emin_mev}"
        _edges, contents = flux.to_histogram(nbins=FLUX_NBINS)
        weights = " ".join(str(float(w)) for w in contents)
        return f"{emin_mev} {emax_mev} {weights}"
