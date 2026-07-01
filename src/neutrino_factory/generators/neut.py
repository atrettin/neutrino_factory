from __future__ import annotations

from pathlib import Path

from .base import GeneratorAdapter
from ..normalizers.neut import NeutNormalizer
from ..translators.neut import NeutTranslator


class NeutAdapter(GeneratorAdapter):
    name = "neut"
    executable = "neutmc"

    def translate_config(self, task: dict) -> dict:
        return NeutTranslator().translate(self.config, task)

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        energy_min, energy_max = translated_config["energy_range_gev"]
        return [
            self.binary_name(),
            "--events",
            str(translated_config["event_total"]),
            "--neutrino",
            str(translated_config["neutrino_type"]),
            "--target",
            str(translated_config["target_material"]),
            "--energy-range",
            f"{energy_min}:{energy_max}",
            "--seed",
            str(translated_config["random_seed"]),
        ]

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        return NeutNormalizer().normalize(raw_output_path, normalized_output_path, task, execution_mode)
