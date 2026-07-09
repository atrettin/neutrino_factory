from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import GeneratorAdapter
from .. import containers
from ..normalizers.nuwro import NuWroNormalizer
from ..translators.nuwro import NuWroTranslator


class NuWroAdapter(GeneratorAdapter):
    name = "nuwro"
    executable = "nuwro"

    CODE_VERSIONS = {
        "nuwro_25.11": {
            "repo": "https://github.com/NuWro/nuwro",
            "git_ref": "nuwro_25.11",
            # NuWro parameter-set versioning is a non-blocking TODO; only
            # "default" exists for now.
            "config_versions": ["default"],
        },
    }

    def translate_config(self, task: dict) -> dict:
        return NuWroTranslator().translate(self.config, task)

    @staticmethod
    def _write_params_file(work_dir: Path, nuwro_params: dict) -> Path:
        params_path = work_dir / "params.txt"
        lines = [f"{key} = {value}" for key, value in nuwro_params.items()]
        params_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return params_path

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        code_version = translated_config.get("code_version")
        work_dir.mkdir(parents=True, exist_ok=True)
        self._write_params_file(work_dir, translated_config["nuwro_params"])
        (work_dir / "translated_config.json").write_text(
            json.dumps(translated_config), encoding="utf-8"
        )

        nuwro_args = [
            self.binary_name(),
            "-o", "events.root",
            "-i", "params.txt",
        ]

        # Native binary first: on the cluster the Slurm task already runs inside
        # the generator's Apptainer image (which cannot nest). Do not reorder.
        if shutil.which(self.binary_name()):
            return nuwro_args

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            # NuWro resolves data/ relative to its binary; run from /opt/nuwro
            # and write output explicitly to the mounted work dir.
            return containers.docker_wrap(
                image,
                [self.binary_name(), "-o", "/work/events.root", "-i", "/work/params.txt"],
                [(work_dir, "/work")],
                "/opt/nuwro",
            )

        return nuwro_args

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        root_path = Path(raw_output_path).parent / "events.root"
        actual_path = root_path if root_path.exists() else Path(raw_output_path)
        return NuWroNormalizer().normalize(actual_path, normalized_output_path, task, execution_mode)
