from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from .base import GeneratorAdapter
from .. import catalog, containers
from ..normalizers.genie import GenieNormalizer
from ..translators.genie import GenieTranslator


LOGGER = logging.getLogger(__name__)


class GenieAdapter(GeneratorAdapter):
    name = "genie"
    executable = "gevgen"

    # GENIE tunes are not statically enumerated: availability is discovered from
    # the cross-section splines staged on disk (see ``available_config_versions``).
    CODE_VERSIONS = {
        "R-3_06_00": {
            "repo": "https://github.com/GENIE-MC/Generator",
            "git_ref": "R-3_06_00",
        },
    }

    @staticmethod
    def _tag_safe(value: str) -> str:
        return catalog._tag_safe(value)

    @staticmethod
    def _normalize_tune(value: str) -> str:
        """Case- and separator-insensitive key for matching tune directory names."""
        return re.sub(r"[^A-Za-z0-9]", "", value).lower()

    @classmethod
    def _default_software_root(cls) -> str:
        return os.environ.get("NF_SOFTWARE_ROOT", "./software")

    @classmethod
    def _xsec_dir(cls, software_root: str | Path, code_version: str) -> Path:
        return Path(software_root) / "genie" / "genie_xsec" / cls._tag_safe(code_version)

    @classmethod
    def genie_xsecs_xml(
        cls, software_root: str | Path, code_version: str, config_version: str
    ) -> Path | None:
        """Locate the staged GENIE cross-section spline for a (code, tune) pair.

        Looks for ``<software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml``,
        first by exact tune directory name, then by a separator-insensitive match
        (the FNAL tarballs name tunes with underscores stripped, e.g.
        ``G1810a0211a`` for ``G18_10a_02_11a``). Returns ``None`` if none exists.
        """
        tune = str(config_version or "").strip()
        code = str(code_version or "").strip()
        if not tune or not code:
            return None

        xsec_root = cls._xsec_dir(software_root, code)

        exact = xsec_root / tune / "xsecs.xml"
        if exact.is_file():
            return exact

        target = cls._normalize_tune(tune)
        for candidate in sorted(xsec_root.glob("*/xsecs.xml")):
            if cls._normalize_tune(candidate.parent.name) == target:
                return candidate
        return None

    @classmethod
    def available_config_versions(
        cls, code_version: str, software_root: str | Path | None = None
    ) -> list[str]:
        """Discover tunes from staged ``xsecs.xml`` files (the literal dir names)."""
        root = software_root if software_root is not None else cls._default_software_root()
        xsec_root = cls._xsec_dir(root, code_version)
        return sorted(p.parent.name for p in xsec_root.glob("*/xsecs.xml"))

    @classmethod
    def ensure_compatible(
        cls,
        code_version: str,
        config_version: str,
        software_root: str | Path | None = None,
        require_available: bool = False,
    ) -> None:
        """Validate a GENIE (code_version, tune) pair.

        GENIE tunes are not enumerated in advance, so there is no static list to
        be "incompatible" with. Baseline check: known code version + non-empty
        tune. When ``require_available`` is set (real, non-stub runs), the tune's
        cross-section spline must also be staged on disk, so a valid config is
        guaranteed to actually run.
        """
        cls.ensure_code_version(code_version)
        tune = str(config_version or "").strip()
        if not tune:
            raise catalog.CatalogError(
                f"config_version must be non-empty for {cls.name} "
                f"code_version '{code_version}'"
            )
        if require_available:
            root = software_root if software_root is not None else cls._default_software_root()
            if cls.genie_xsecs_xml(root, code_version, tune) is None:
                available = cls.available_config_versions(code_version, root)
                raise catalog.CatalogError(
                    f"GENIE tune '{tune}' has no staged cross-section spline for "
                    f"code_version '{code_version}' under "
                    f"{cls._xsec_dir(root, code_version)}. Stage it with "
                    f"setup/download_genie_xsec.sh --tune {tune}, or enable "
                    f"run.stub_mode. Available tunes: "
                    f"{', '.join(available) or 'none'}"
                )

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
        resolved = self.genie_xsecs_xml(software_root, code_version, tune)
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
        sidecar["image"] = self.container_image(code_version)
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

        # Native binary first: on the cluster the Slurm task already runs inside
        # the generator's Apptainer image (which cannot nest), so gevgen must be
        # executed directly whenever it is on $PATH. Do not reorder these branches.
        if shutil.which(self.binary_name()):
            return gevgen_args

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            xsec_root = self._xsec_root()
            binds: list[tuple] = [
                (xsec_root, "/genie_xsec", "ro"),
                (work_dir, "/work"),
            ]
            # Mount the histogram flux file's directory so gevgen can read it.
            if flux_file is not None:
                binds.append((flux_file.parent, "/flux", "ro"))
            image = self.container_image(code_version)
            assert image is not None

            remapped: list[str] = []
            for arg in gevgen_args:
                if xml_path is not None and arg == str(xml_path):
                    rel = Path(arg).relative_to(xsec_root)
                    arg = f"/genie_xsec/{rel}"
                elif flux_file is not None and arg == self._flux_arg(flux_spec, flux_file):
                    arg = f"/flux/{flux_file.name},{flux_spec['name']}"
                remapped.append(arg)
            return containers.docker_wrap(image, remapped, binds, "/work")

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
        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            subprocess.run(
                containers.docker_wrap(image, args, [(work_dir, "/work")], "/work"),
                check=True,
            )
            return
        raise RuntimeError(
            "gntpc is unavailable: neither a local binary nor a container image was found. "
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
