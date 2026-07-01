from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ConfigTranslator
from ..flux import HistogramFlux, PowerLawFlux, build_flux

# PDG codes for gevgen's -p (probe) flag. gevgen expects a numeric PDG code,
# not a flavour name. Mirrors the mapping used by the NuWro translator.
PARTICLE_PDG = {
    "numu": 14,
    "nue": 12,
    "numubar": -14,
    "nuebar": -12,
    "nutau": 16,
    "nutaubar": -16,
}


class GenieTranslator(ConfigTranslator):
    name = "genie"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux_config = config["flux"]
        target = config["target"]

        config_path = config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        flux = build_flux(flux_config, base_dir=base_dir)

        particle = flux_config["particle"]
        if particle not in PARTICLE_PDG:
            raise KeyError(
                f"Unknown neutrino particle '{particle}' for GENIE probe. "
                f"Known: {', '.join(PARTICLE_PDG)}"
            )

        return {
            "generator": self.name,
            "command": "gevgen",
            "probe": particle,
            "probe_pdg": PARTICLE_PDG[particle],
            "target": target["nucleus"],
            "target_pdg": target.get("pdg", target["nucleus"]),
            "energy_range_gev": [flux.emin_gev, flux.emax_gev],
            "events": int(task["event_count"]),
            "seed": int(task["seed"]),
            "flux_model": flux_config["type"],
            "genie_flux": self._genie_flux_descriptor(flux),
            "event_generator_list": config["physics"].get("event_generator_list"),
            "physics_mode": config["physics"].get("mode", "inclusive"),
            # For GENIE, config_version is the tune and code_version is the git tag.
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
        }

    @staticmethod
    def _genie_flux_descriptor(flux: Any) -> dict[str, Any]:
        """Translate the framework flux into gevgen's -f argument spec.

        Power law -> a ROOT TF1 function string ``x^(gamma)`` where ``x`` is the
        neutrino energy in GeV. Histogram -> the ROOT file + TH1 name, consumed
        directly by gevgen's TH1 flux driver.
        """
        if isinstance(flux, PowerLawFlux):
            return {"kind": "function", "expr": f"x^({flux.gamma})"}
        if isinstance(flux, HistogramFlux):
            # HistogramFlux was built from a ROOT file; carry the source so the
            # adapter can hand the same file to gevgen. build_flux resolved the
            # path to absolute already.
            return {
                "kind": "histogram",
                "file": str(flux.source_path),
                "name": flux.source_name,
            }
        raise TypeError(f"Unsupported flux type for GENIE: {type(flux).__name__}")
