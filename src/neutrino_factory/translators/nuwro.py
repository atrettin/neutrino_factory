from __future__ import annotations

from typing import Any

from .base import ConfigTranslator

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
        flux = config["flux"]
        target = config["target"]

        particle = flux["particle"]
        nucleus = target["nucleus"]
        energy_range_gev = [float(flux["emin_gev"]), float(flux["emax_gev"])]
        seed = int(task["seed"])

        protons, neutrons = NUCLEUS_COMPOSITION[nucleus]

        # NuWro uses beam_type=0 for monoenergetic; use midpoint of energy range.
        # target_type=0 selects a single nucleus via nucleus_p / nucleus_n.
        energy_mev = (energy_range_gev[0] + energy_range_gev[1]) / 2.0 * 1000.0
        nuwro_params = {
            "number_of_events": int(task["event_count"]),
            "random_seed": seed,
            "beam_particle": PARTICLE_PDG[particle],
            "beam_type": 0,
            "beam_energy": energy_mev,
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
            "flux_model": flux["type"],
            "mode": config["physics"].get("mode", "inclusive"),
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
            "nuwro_params": nuwro_params,
        }
