from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import GeneratorAdapter
from .. import catalog, containers
from ..normalizers.gibuu import GiBUUNormalizer, pass_output_dirs
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

    # TODO: verify — provisional. GiBUU is a nuclear transport model aimed at
    # the few-GeV region; its neutrino mode is not intended for high energies.
    MAX_ENERGY_RANGE_GEV = (0.01, 50.0)
    VALID_ENERGY_RANGE_GEV = (0.1, 20.0)

    @classmethod
    def build_arg(cls, code_version: str) -> dict[str, str] | None:
        # GiBUU release tarballs are keyed by the bare year; the code_version
        # carries the ``release`` prefix (``release2025``) but the build arg does
        # not (``GIBUU_RELEASE=2025``).
        assert cls.build_arg_name is not None
        return {"name": cls.build_arg_name, "value": code_version.removeprefix("release")}

    def translate_config(self, task: dict) -> dict:
        return GiBUUTranslator().translate(self.config, task)

    # Placeholder the translator writes for the jobcard's path_to_input; resolved
    # here because the buuinput location is runtime- and version-dependent.
    _BUUINPUT_PLACEHOLDER = "@NF_GIBUU_INPUT@"

    @staticmethod
    def _buuinput_dir(code_version: str | None) -> str:
        """Absolute buuinput path GiBUU reads, per the active container runtime.

        Apptainer runs inside the version-namespaced payload tree; Docker uses
        the flat image layout (setup/Dockerfile.gibuu: GIBUU_INPUT=/opt/GiBUU/buuinput).
        """
        if containers.runtime() == "apptainer":
            cv = catalog._tag_safe(str(code_version or ""))
            return f"/opt/nf/generators/gibuu/{cv}/GiBUU/buuinput"
        return "/opt/GiBUU/buuinput"

    @staticmethod
    def _write_pass_inputs(pass_dir: Path, jobcard: str, flux_table: str | None) -> None:
        """Stage one pass's jobcard and flux table into its own directory.

        Every pass runs with its own directory as CWD, since GiBUU writes its
        ROOT output under a fixed name (``EventOutput.Pert.<run>.root``) into the
        CWD: a second pass sharing the directory would overwrite the first. The
        jobcard's ``FileNameFlux`` is CWD-relative ('./flux.dat'), so each pass
        gets its own copy of the same table.
        """
        pass_dir.mkdir(parents=True, exist_ok=True)
        (pass_dir / "job.job").write_text(jobcard, encoding="utf-8")
        if flux_table:
            (pass_dir / "flux.dat").write_text(flux_table, encoding="utf-8")

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        code_version = translated_config.get("code_version")
        work_dir.mkdir(parents=True, exist_ok=True)
        flux_table = translated_config.get("gibuu_flux_table")

        # One subdirectory per weak current: exactly one for a cc/nc run, two for
        # an inclusive one (GiBUU's process_ID selects a single current, see
        # translators.gibuu.CURRENT_PASSES). Uniform for both cases so there is a
        # single layout for the normalizer to read.
        pass_dirs = []
        for gibuu_pass in translated_config["gibuu_passes"]:
            pass_dir = work_dir / str(gibuu_pass["current"])
            jobcard = gibuu_pass["jobcard"].replace(
                self._BUUINPUT_PLACEHOLDER, self._buuinput_dir(code_version)
            )
            self._write_pass_inputs(pass_dir, jobcard, flux_table)
            pass_dirs.append(pass_dir.name)

        (work_dir / "translated_config.json").write_text(
            json.dumps(translated_config), encoding="utf-8"
        )

        def script(binary: str, root: str) -> str:
            # `set -e` so a failing pass fails the task instead of being masked
            # by the exit status of the last one.
            passes = " && ".join(
                f"(cd {root}/{name} && {binary} < job.job)" for name in pass_dirs
            )
            return f"set -e; {passes}"

        # GiBUU reads its jobcard from stdin and writes ROOT output into the CWD.
        # Native binary first: on the cluster the Slurm task already runs inside
        # the generator's Apptainer image (which cannot nest). Do not reorder.
        if shutil.which(self.binary_name()):
            launcher = containers.apptainer_dispatch_prefix(
                self.name, code_version, self.binary_name()
            )
            return ["bash", "-c", script(launcher, ".")]

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            return containers.docker_wrap(
                image,
                ["bash", "-c", script(self.binary_name(), "/work")],
                [(work_dir, "/work")],
                "/work",
            )

        return ["bash", "-c", script(self.binary_name(), ".")]

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        # GiBUU writes its RootTuple output into each pass's CWD, not to
        # raw_output_path, and produces one file per run (num_runs_SameEnergy).
        # Hand the normalizer the work directory: it discovers the per-current
        # pass directories underneath it (see normalizers.gibuu). With no pass
        # output at all the run was a stub, whose JSON lives at raw_output_path.
        work_dir = Path(raw_output_path).parent
        source = work_dir if pass_output_dirs(work_dir) else Path(raw_output_path)
        return GiBUUNormalizer().normalize(
            source, normalized_output_path, task, execution_mode
        )
