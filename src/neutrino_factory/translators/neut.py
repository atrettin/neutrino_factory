from __future__ import annotations

from typing import Any

from .base import ConfigTranslator


class NeutTranslator(ConfigTranslator):
    name = "neut"

    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        flux = config["flux"]
        target = config["target"]
        return {
            "generator": self.name,
            "command": "neutmc",
            "neutrino_type": flux["particle"],
            "target_material": target["nucleus"],
            "energy_range_gev": [float(flux["emin_gev"]), float(flux["emax_gev"])],
            "event_total": int(task["event_count"]),
            "random_seed": int(task["seed"]),
            "flux_model": flux["type"],
            "current": config["physics"].get("current", "cc"),
            "code_version": task["code_version"],
            "config_version": task["config_version"],
            "generator_version_id": task.get("generator_version_id"),
        }
