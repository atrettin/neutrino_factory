from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import GeneratorAdapter
from .. import containers
from ..normalizers.gibuu import GiBUUNormalizer
from ..translators.gibuu import GiBUUTranslator


class GiBUUAdapter(GeneratorAdapter):
    name = "gibuu"
    executable = "GiBUU.x"
    build_arg_name = "GIBUU_RELEASE"

    CODE_VERSIONS = {
        # GiBUU is distributed as HEPForge release tarballs; ``git_ref`` holds the
        # release tag so it is treated as buildable (the GitHub mirror is stale).
        "release2025": {
            "repo": "https://gibuu.hepforge.org/downloads",
            "git_ref": "release2025",
            # GiBUU parameter-set versioning is a non-blocking TODO; only
            # "default" exists for now.
            "config_versions": ["default"],
        },
    }

    @classmethod
    def build_arg(cls, code_version: str) -> dict[str, str] | None:
        # GiBUU release tarballs are keyed by the bare year; the code_version
        # carries the ``release`` prefix (``release2025``) but the build arg does
        # not (``GIBUU_RELEASE=2025``).
        assert cls.build_arg_name is not None
        return {"name": cls.build_arg_name, "value": code_version.removeprefix("release")}

    def translate_config(self, task: dict) -> dict:
        return GiBUUTranslator().translate(self.config, task)

    @staticmethod
    def _write_jobcard(work_dir: Path, jobcard: str) -> Path:
        jobcard_path = work_dir / "job.job"
        jobcard_path.write_text(jobcard, encoding="utf-8")
        return jobcard_path

    @staticmethod
    def _write_flux_file(work_dir: Path, flux_table: str) -> Path:
        # Written as flux.dat in the work dir; the jobcard's FileNameFlux
        # references it CWD-relatively ('./flux.dat'), and GiBUU always runs
        # with the work dir as CWD in every pathway.
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
        # Native binary first: on the cluster the Slurm task already runs inside
        # the generator's Apptainer image (which cannot nest). Do not reorder.
        if shutil.which(self.binary_name()):
            launcher = containers.apptainer_dispatch_prefix(
                self.name, code_version, self.binary_name()
            )
            return ["bash", "-c", f"{launcher} < job.job"]

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            return containers.docker_wrap(
                image,
                ["bash", "-c", f"{self.binary_name()} < /work/job.job"],
                [(work_dir, "/work")],
                "/work",
            )

        return ["bash", "-c", f"{self.binary_name()} < job.job"]

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        # GiBUU writes its RootTuple output into the run CWD under a fixed name,
        # not to raw_output_path; hand the real ROOT file to the normalizer.
        root_path = Path(raw_output_path).parent / "EventOutput.Pert.00000001.root"
        actual_path = root_path if root_path.exists() else Path(raw_output_path)
        return GiBUUNormalizer().normalize(
            actual_path, normalized_output_path, task, execution_mode
        )
