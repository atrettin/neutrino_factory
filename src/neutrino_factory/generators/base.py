from __future__ import annotations

import json
import random
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class GeneratorAdapter(ABC):
    name = "base"
    executable = ""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def binary_name(self) -> str:
        return self.executable

    def is_available(self, code_version: str | None = None) -> bool:
        return bool(self.binary_name()) and shutil.which(self.binary_name()) is not None

    @abstractmethod
    def translate_config(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def build_run_command(self, translated_config: dict[str, Any], work_dir: Path) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict[str, Any],
        execution_mode: str,
    ) -> str:
        raise NotImplementedError

    def run_stub(
        self,
        translated_config: dict[str, Any],
        raw_output_path: str | Path,
        task: dict[str, Any],
    ) -> str:
        output = Path(raw_output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        energy_min, energy_max = translated_config.get("energy_range_gev", [0.5, 10.0])
        rng = random.Random(int(task["seed"]))
        event_count = int(task["event_count"])

        # Draw synthetic energies from the configured flux so stub output honors
        # the same flux abstraction as the real generators. Fall back to a flat
        # draw over the energy range if the flux cannot be built.
        try:
            from ..flux import build_flux

            config_path = self.config.get("config_path")
            base_dir = str(Path(config_path).parent) if config_path else None
            flux = build_flux(self.config["flux"], base_dir=base_dir)
            energies = flux.sample_energies(event_count, rng)
        except Exception:
            energies = [
                energy_min + (energy_max - energy_min) * rng.random()
                for _ in range(event_count)
            ]

        events: list[dict[str, Any]] = []

        for offset in range(event_count):
            energy = energies[offset]
            events.append(
                {
                    "event_id": int(task["start_event"]) + offset,
                    "seed": int(task["seed"]),
                    "energy_gev": round(energy, 6),
                    "weight": 1.0,
                    "interaction": self.config.get("physics", {}).get("mode", "inclusive"),
                    "probe": self.config.get("flux", {}).get("particle", "numu"),
                    "target": self.config.get("target", {}).get("nucleus", "Ar40"),
                    "generator": self.name,
                }
            )

        payload = {
            "generator": self.name,
            "translated_config": translated_config,
            "metadata": {
                "run_name": self.config["run"]["name"],
                "execution_mode": self.config["run"].get("executor", "local"),
                "stub_mode": True,
                "code_version": str(task.get("code_version", "unknown")),
                "config_version": str(task.get("config_version", "unknown")),
                "generator_version_id": str(task.get("generator_version_id", "unknown")),
            },
            "events": events,
        }
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(output)
