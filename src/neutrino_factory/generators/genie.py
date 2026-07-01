from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .base import GeneratorAdapter
from .. import catalog
from ..normalizers.genie import GenieNormalizer
from ..translators.genie import GenieTranslator


LOGGER = logging.getLogger(__name__)


class GenieAdapter(GeneratorAdapter):
    name = "genie"
    executable = "gevgen"

    @staticmethod
    def _tag_safe(value: str) -> str:
        return catalog._tag_safe(value)

    @staticmethod
    def _normalize_tune(value: str) -> str:
        return catalog._normalize_tune(value)

    def _docker_image(self, code_version: str | None) -> str | None:
        if not code_version:
            return None
        return catalog.image_for(self.name, code_version)

    def _docker_available(self, code_version: str | None) -> bool:
        return catalog.image_built(self._docker_image(code_version))

    def is_available(self, code_version: str | None = None) -> bool:
        return bool(shutil.which(self.binary_name())) or self._docker_available(code_version)

    def _xsec_root(self) -> Path:
        return (
            Path(self.config["storage"]["software_root"])
            / "genie"
            / "genie_xsec"
        )

    def _resolve_xml_path(self, translated_config: dict) -> Path | None:
        # For GENIE, config_version is the tune and code_version is the git tag.
        tune = str(translated_config.get("config_version") or "").strip()
        if not tune:
            return None

        code_version = str(translated_config.get("code_version") or "").strip()
        if not code_version:
            LOGGER.warning(
                "GENIE tune '%s' requested but no code_version is configured; skipping --cross-sections",
                tune,
            )
            return None

        software_root = self.config["storage"]["software_root"]
        resolved = catalog.genie_xsecs_xml(software_root, code_version, tune)
        if resolved is not None:
            return resolved

        LOGGER.warning(
            "GENIE precomputed cross sections not found for tune '%s' and code_version '%s'. Looked under %s",
            tune,
            code_version,
            self._xsec_root() / self._tag_safe(code_version),
        )
        return None

    def translate_config(self, task: dict) -> dict:
        return GenieTranslator().translate(self.config, task)

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        energy_min, energy_max = translated_config["energy_range_gev"]
        xml_path = self._resolve_xml_path(translated_config)
        code_version = translated_config.get("code_version")

        work_dir.mkdir(parents=True, exist_ok=True)
        sidecar = dict(translated_config)
        sidecar["docker_image"] = self._docker_image(code_version)
        (work_dir / "translated_config.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )

        flux_spec = translated_config["genie_flux"]
        flux_file = Path(flux_spec["file"]) if flux_spec["kind"] == "histogram" else None

        gevgen_args: list[str] = [
            "gevgen",
            "-n", str(translated_config["events"]),
            "-p", str(translated_config["probe_pdg"]),
            "-t", str(translated_config.get("target_pdg", translated_config["target"])),
            "-e", f"{energy_min},{energy_max}",
            "-f", self._flux_arg(flux_spec, flux_file),
            "--seed", str(translated_config["seed"]),
            "-o", "events.ghep.root",
        ]
        tune = str(translated_config.get("config_version") or "").strip()
        if tune:
            gevgen_args.extend(["--tune", tune])
        event_generator_list = translated_config.get("event_generator_list")
        if event_generator_list:
            gevgen_args.extend(["--event-generator-list", str(event_generator_list)])
        # Flux-driven runs (a spectrum given via -f) require precomputed total
        # cross-section splines; gevgen aborts without them. Load them if present.
        if xml_path is not None:
            gevgen_args.extend(["--cross-sections", str(xml_path)])

        if shutil.which(self.binary_name()):
            return gevgen_args

        if self._docker_available(code_version):
            xsec_root = self._xsec_root()
            # Docker requires absolute host paths for bind mounts.
            docker_args = [
                "docker", "run", "--platform", "linux/amd64", "--rm",
                "-v", f"{xsec_root.resolve()}:/genie_xsec:ro",
                "-v", f"{work_dir.resolve()}:/work",
            ]
            # Mount the histogram flux file's directory so gevgen can read it.
            if flux_file is not None:
                docker_args.extend(["-v", f"{flux_file.parent.resolve()}:/flux:ro"])
            docker_args.extend(["-w", "/work", self._docker_image(code_version)])

            remapped: list[str] = []
            for arg in gevgen_args:
                if xml_path is not None and arg == str(xml_path):
                    rel = Path(arg).relative_to(xsec_root)
                    arg = f"/genie_xsec/{rel}"
                elif flux_file is not None and arg == self._flux_arg(flux_spec, flux_file):
                    arg = f"/flux/{flux_file.name},{flux_spec['name']}"
                remapped.append(arg)
            return docker_args + remapped

        return gevgen_args

    @staticmethod
    def _flux_arg(flux_spec: dict, flux_file: Path | None) -> str:
        """Build gevgen's -f value: a TF1 string, or 'file.root,histname'."""
        if flux_spec["kind"] == "function":
            return flux_spec["expr"]
        return f"{flux_file},{flux_spec['name']}"

    def _run_gntpc(self, work_dir: Path, code_version: str | None) -> None:
        args = ["gntpc", "-i", "events.ghep.root", "-f", "gst", "-o", "events.gst.root"]
        if shutil.which("gntpc"):
            subprocess.run(args, check=True, cwd=work_dir)
            return
        if self._docker_available(code_version):
            subprocess.run(
                [
                    "docker", "run", "--platform", "linux/amd64", "--rm",
                    "-v", f"{Path(work_dir).resolve()}:/work",
                    "-w", "/work",
                    self._docker_image(code_version),
                ] + args,
                check=True,
            )
            return
        raise RuntimeError(
            "gntpc is unavailable: neither a local binary nor a Docker image was found. "
            "Cannot convert GENIE GHEP output to analysis format."
        )

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        work_dir = Path(raw_output_path).parent
        gst = work_dir / "events.gst.root"
        ghep = work_dir / "events.ghep.root"
        if gst.exists():
            actual = gst
        elif ghep.exists():
            self._run_gntpc(work_dir, task.get("code_version"))
            actual = gst
        else:
            actual = Path(raw_output_path)
        return GenieNormalizer().normalize(actual, normalized_output_path, task, execution_mode)
