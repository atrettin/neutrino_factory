from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import GeneratorAdapter
from .. import catalog
from ..normalizers.gibuu import GiBUUNormalizer
from ..translators.gibuu import GiBUUTranslator


class GiBUUAdapter(GeneratorAdapter):
    name = "gibuu"
    executable = "GiBUU.x"

    def _docker_image(self, code_version: str | None) -> str | None:
        if not code_version:
            return None
        return catalog.image_for(self.name, code_version)

    def _docker_available(self, code_version: str | None) -> bool:
        return catalog.image_built(self._docker_image(code_version))

    def is_available(self, code_version: str | None = None) -> bool:
        return bool(shutil.which(self.binary_name())) or self._docker_available(code_version)

    def translate_config(self, task: dict) -> dict:
        return GiBUUTranslator().translate(self.config, task)

    @staticmethod
    def _write_jobcard(work_dir: Path, jobcard: str) -> Path:
        jobcard_path = work_dir / "job.job"
        jobcard_path.write_text(jobcard, encoding="utf-8")
        return jobcard_path

    @staticmethod
    def _write_flux_file(work_dir: Path, flux_table: str) -> Path:
        # Written as flux.dat and exposed to GiBUU at /work/flux.dat via the
        # work-dir bind mount, matching the jobcard's FileNameFlux.
        flux_path = work_dir / "flux.dat"
        flux_path.write_text(flux_table, encoding="utf-8")
        return flux_path

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        code_version = translated_config.get("code_version")
        work_dir.mkdir(parents=True, exist_ok=True)
        self._write_jobcard(work_dir, translated_config["gibuu_jobcard"])
        flux_table = translated_config.get("gibuu_flux_table")
        if flux_table:
            self._write_flux_file(work_dir, flux_table)
        (work_dir / "translated_config.json").write_text(
            json.dumps(translated_config), encoding="utf-8"
        )

        # GiBUU reads its jobcard from stdin and writes ROOT output into the CWD.
        if shutil.which(self.binary_name()):
            return ["bash", "-c", f"{self.binary_name()} < job.job"]

        if self._docker_available(code_version):
            return [
                "docker", "run", "--platform", "linux/amd64", "--rm",
                "-v", f"{Path(work_dir).resolve()}:/work",
                "-w", "/work",
                self._docker_image(code_version),
                "bash", "-c", f"{self.binary_name()} < /work/job.job",
            ]

        return ["bash", "-c", f"{self.binary_name()} < job.job"]

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        return GiBUUNormalizer().normalize(
            raw_output_path, normalized_output_path, task, execution_mode
        )
