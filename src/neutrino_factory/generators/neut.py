from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import GeneratorAdapter
from .. import containers
from ..flux import build_flux
from ..normalizers.neut import NeutNormalizer
from ..translators.neut import FLUX_FILE, FLUX_HIST, FLUX_NBINS, NeutTranslator

# Fixed artifact names inside the task work directory.
CARD_FILE = "neut.card"
SEED_FILE = "ranseed.dat"
RAW_OUTPUT = "events.neut.root"
FLAT_OUTPUT = "events.flat.root"

# Second-stage flattener, shipped in setup/neut/ (see nf_flatten.C for why it is
# needed). Staged into the Apptainer payload's bin/; bind-mounted under Docker.
FLATTEN_BINARY = "nf-neut-flatten"
FLATTEN_SOURCE_DIR = Path("setup") / "neut"
FLATTEN_DOCKER_MOUNT = "/nf_neut"

# NEUT seeds RANLUX by reading the first integer out of the file named by
# $RANFILE, which Fortran reads with a (5X,5I12) format: five rows of five
# 12-column integers, preceded by five spaces. Only the first value is used as
# the RLUXGO seed, but all 25 must be present or the read hits end-of-file.
SEED_FILE_ROWS = 5
SEED_FILE_COLUMNS = 5


class NeutAdapter(GeneratorAdapter):
    name = "neut"
    executable = "neutroot2"
    build_arg_name = "NEUT_SOURCE_IMAGE"

    # NEUT's source code is not publicly available, so unlike every other
    # generator this payload cannot be built from a git ref. It is extracted
    # instead from the NUISANCE collaboration's published tutorial image, which
    # ships a working NEUT build (`neut-config --version` -> 5.7.0). The code
    # version therefore names both the NEUT release and the image tag it came
    # from: the image tag is the real pin, since we cannot reproduce the build.
    CODE_VERSIONS = {
        "5.7.0-nuint2024": {
            "repo": None,
            "git_ref": None,
            "source_image": "nuisancemc/tutorial:nuint2024",
            "config_versions": ["default"],
        },
    }

    @classmethod
    def is_buildable(cls, code_version: str) -> bool:
        """A published container image is a buildable source, like a git ref.

        The base implementation treats only ``git_ref`` as a source. NEUT's
        Apptainer payload bootstraps from ``source_image`` instead, so it is
        just as buildable — ``build_apptainer_images.sh`` is catalog-driven and
        would otherwise skip it.
        """
        entry = cls.CODE_VERSIONS.get(code_version) or {}
        return bool(entry.get("git_ref") or entry.get("source_image"))

    @classmethod
    def build_arg(cls, code_version: str) -> dict[str, str] | None:
        """Pass the source image (not the code version) to the build."""
        entry = cls.CODE_VERSIONS.get(code_version) or {}
        source_image = entry.get("source_image")
        if not cls.build_arg_name or not source_image:
            return None
        return {"name": cls.build_arg_name, "value": str(source_image)}

    def translate_config(self, task: dict) -> dict:
        return NeutTranslator().translate(self.config, task)

    def _flux(self):
        config_path = self.config.get("config_path")
        base_dir = str(Path(config_path).parent) if config_path else None
        return build_flux(self.config["flux"], base_dir=base_dir)

    def _write_flux_file(self, work_dir: Path) -> Path:
        """Write the flux as a TH1D for NEUT's EVCT-MPV 3 histogram driver.

        NEUT has no function-flux driver, so power-law and histogram fluxes both
        go through ``Flux.to_histogram``. Writing the histogram here (rather
        than handing NEUT the user's original ROOT file, as GENIE does) keeps the
        binning used to generate identical to the binning
        ``NeutTranslator.compute_xsec_weight`` reweights with.
        """
        try:
            import uproot
        except ImportError as exc:
            raise RuntimeError(
                "uproot is required to write the NEUT flux histogram. "
                "Install it with: pip install uproot"
            ) from exc

        edges, contents = self._flux().to_histogram(nbins=FLUX_NBINS)
        flux_path = work_dir / FLUX_FILE
        with uproot.recreate(flux_path) as handle:
            handle[FLUX_HIST] = (contents, edges)
        return flux_path

    @staticmethod
    def _write_seed_file(work_dir: Path, seed: int) -> Path:
        """Write the RANLUX seed file NEUT reads when NEUT-RAND is 0."""
        values = [int(seed)] + [0] * (SEED_FILE_ROWS * SEED_FILE_COLUMNS - 1)
        lines = [
            "     " + "".join(f"{value:12d}" for value in values[i : i + SEED_FILE_COLUMNS])
            for i in range(0, len(values), SEED_FILE_COLUMNS)
        ]
        seed_path = work_dir / SEED_FILE
        seed_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return seed_path

    @staticmethod
    def _write_card_file(work_dir: Path, card: dict[str, Any]) -> Path:
        """Render the NEUT card: ``KEY value`` lines, ``C`` for comments."""
        lines = [
            # Fortran card: 'C' in column 1 marks a comment. Keep it ASCII.
            "C Generated by neutrino-factory - do not edit.",
            *(f"{key} {value}" for key, value in card.items()),
        ]
        card_path = work_dir / CARD_FILE
        card_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return card_path

    @staticmethod
    def _flatten_source_dir() -> Path:
        """Host directory holding nf-neut-flatten and nf_flatten.C."""
        from ..config import find_repo_root

        repo_root = find_repo_root()
        if repo_root is None:
            raise RuntimeError(
                "Cannot locate the repository root to mount "
                f"{FLATTEN_SOURCE_DIR} into the NEUT container. Run from within "
                "the checkout, or use the Apptainer pathway, which stages the "
                "flattener into the image."
            )
        source_dir = repo_root / FLATTEN_SOURCE_DIR
        if not (source_dir / FLATTEN_BINARY).is_file():
            raise RuntimeError(f"{FLATTEN_BINARY} not found under {source_dir}")
        return source_dir

    def build_run_command(self, translated_config: dict, work_dir: Path) -> list[str]:
        code_version = translated_config.get("code_version")
        work_dir.mkdir(parents=True, exist_ok=True)

        self._write_flux_file(work_dir)
        self._write_seed_file(work_dir, int(translated_config["seed"]))
        self._write_card_file(work_dir, translated_config["neut_card"])
        (work_dir / "translated_config.json").write_text(
            json.dumps(translated_config), encoding="utf-8"
        )

        neut_args = [self.binary_name(), CARD_FILE, RAW_OUTPUT]

        # Native binary first: on the cluster the Slurm task already runs inside
        # the composed Apptainer image (which cannot nest), so neutroot2 must be
        # executed directly whenever it is on $PATH. Do not reorder these branches.
        if shutil.which(self.binary_name()):
            # subprocess inherits this process's environment, and NEUT reads its
            # RANLUX seed from the file named here (NEUT-RAND 0).
            os.environ["RANFILE"] = str((work_dir / SEED_FILE).resolve())
            return containers.apptainer_dispatch(self.name, code_version, neut_args)

        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            return containers.docker_wrap(
                image,
                neut_args,
                [(work_dir, "/work")],
                "/work",
                env={"RANFILE": f"/work/{SEED_FILE}"},
            )

        return neut_args

    def _run_flatten(self, work_dir: Path, code_version: str | None) -> None:
        """Convert NEUT's NeutVect output into the flat tree the normalizer reads.

        The NEUT counterpart of ``GenieAdapter._run_gntpc``: a second stage that
        must run inside the generator's environment, dispatched through the same
        native/container branches as the generation step.
        """
        if shutil.which(FLATTEN_BINARY):
            subprocess.run(
                containers.apptainer_dispatch(
                    self.name, code_version, [FLATTEN_BINARY, RAW_OUTPUT, FLAT_OUTPUT]
                ),
                check=True,
                cwd=work_dir,
            )
            return
        if self.container_available(code_version):
            self.ensure_container_wrappable()
            image = self.container_image(code_version)
            assert image is not None
            subprocess.run(
                containers.docker_wrap(
                    image,
                    [f"{FLATTEN_DOCKER_MOUNT}/{FLATTEN_BINARY}", RAW_OUTPUT, FLAT_OUTPUT],
                    [
                        (work_dir, "/work"),
                        (self._flatten_source_dir(), FLATTEN_DOCKER_MOUNT, "ro"),
                    ],
                    "/work",
                ),
                check=True,
            )
            return
        raise RuntimeError(
            f"{FLATTEN_BINARY} is unavailable: neither a local copy nor a container "
            "image was found. Cannot convert NEUT's NeutVect output to a readable "
            "format."
        )

    def normalize_output(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        work_dir = Path(raw_output_path).parent
        flat = work_dir / FLAT_OUTPUT
        raw = work_dir / RAW_OUTPUT
        if flat.exists():
            actual = flat
        elif raw.exists():
            self._run_flatten(work_dir, task.get("code_version"))
            actual = flat
        else:
            actual = Path(raw_output_path)
        return NeutNormalizer().normalize(actual, normalized_output_path, task, execution_mode)
