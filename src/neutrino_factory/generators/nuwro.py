from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .base import GeneratorAdapter
from .. import containers, universes
from ..normalizers.nuwro import NuWroNormalizer
from ..translators.nuwro import NuWroTranslator


# The single-pass universe reweighter built into the NuWro tree from
# setup/nuwro/nf_reweight.cc, and the files it reads and writes in the task's
# work directory.
REWEIGHT_BINARY = "nf_reweight"
UNIVERSES_SPEC = "universes.txt"
UNIVERSES_RESOLVED = "universes.json"
UNIVERSE_WEIGHTS = "universe_weights.root"


def generation_parameters(root_path: Path, names: list[str]) -> dict[str, float]:
    """The run's own values of NuWro parameters, read from ``e/par``.

    These are what NuWro generated with, and what ``nf_reweight`` takes as each
    event's nominal; a value that varies across the file is an error, because
    the universes would then have no single central value.
    """
    import uproot

    values: dict[str, float] = {}
    with uproot.open(root_path) as f:
        tree = f["treeout"]
        for name in names:
            column = np.asarray(tree[f"e/par/par.{name}"].array(library="np"), dtype=np.float64)
            if column.size and np.any(column != column[0]):
                raise RuntimeError(
                    f"NuWro parameter {name} varies across {root_path}; universes need one "
                    "central value per sample"
                )
            values[name] = float(column[0]) if column.size else float("nan")
    return values


class NuWroAdapter(GeneratorAdapter):
    name = "nuwro"
    executable = "nuwro"
    build_arg_name = "NUWRO_TAG"

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
            return containers.apptainer_dispatch(self.name, code_version, nuwro_args)

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

    def _run_reweight(self, work_dir: Path, code_version: str | None) -> None:
        """Resolve the job's universes and compute their weights, if it has any.

        Writes ``universes.json`` (the resolved metadata the normalizer stores)
        and, when a NuWro-reweighted parameter is present, runs ``nf_reweight``
        over ``events.root``. Norm-only universes need no NuWro call: the
        normalizer applies those factors from the event flags. Always re-run, so
        a changed universes block can never pick up stale weights; it takes
        seconds.
        """
        translated = json.loads((work_dir / "translated_config.json").read_text(encoding="utf-8"))
        block = translated.get("universes")
        for stale in (UNIVERSES_RESOLVED, UNIVERSE_WEIGHTS):
            (work_dir / stale).unlink(missing_ok=True)
        if not block:
            return

        reweighted = [name for name in block["parameters"] if name in universes.REWEIGHT_PARAMS]
        generation = generation_parameters(
            work_dir / "events.root", [*reweighted, "qel_axial_ff_set"]
        )
        resolved = universes.resolve_universes(block, generation)
        if reweighted:
            rows = zip(*(resolved["values"][name] for name in reweighted))
            (work_dir / UNIVERSES_SPEC).write_text(
                " ".join(reweighted) + "\n"
                + "".join(" ".join(repr(v) for v in row) + "\n" for row in rows),
                encoding="utf-8",
            )
            self._run_reweight_binary(work_dir, code_version)
        (work_dir / UNIVERSES_RESOLVED).write_text(json.dumps(resolved), encoding="utf-8")

    def _run_reweight_binary(self, work_dir: Path, code_version: str | None) -> None:
        # Same native-first branch order as generation (see build_run_command).
        args = ["events.root", UNIVERSES_SPEC, UNIVERSE_WEIGHTS]
        if shutil.which(REWEIGHT_BINARY):
            command = containers.apptainer_dispatch(
                self.name, code_version, [REWEIGHT_BINARY, *args]
            )
        elif self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            command = containers.docker_wrap(
                image,
                [REWEIGHT_BINARY, *(f"/work/{arg}" for arg in args)],
                [(work_dir, "/work")],
                "/work",
            )
        else:
            raise RuntimeError(
                f"{REWEIGHT_BINARY} is unavailable: neither a local copy nor a container "
                "image was found, so the job's universe weights cannot be computed."
            )
        subprocess.run(command, check=True, cwd=work_dir)

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        root_path = Path(raw_output_path).parent / "events.root"
        actual_path = root_path if root_path.exists() else Path(raw_output_path)
        if root_path.exists():
            self._run_reweight(root_path.parent, task.get("code_version"))
        return NuWroNormalizer().normalize(actual_path, normalized_output_path, task, execution_mode)
