from __future__ import annotations

import json
import random
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config import physics_current


class GeneratorAdapter(ABC):
    name = "base"
    executable = ""

    # Per-generator version knowledge (single source of truth, replacing the old
    # global GENERATOR_CATALOG). Maps a code_version to its build/config metadata:
    #   {"repo": str | None, "git_ref": str | None, "config_versions": [str, ...]}
    # ``git_ref`` truthiness marks a code version as buildable; ``config_versions``
    # is optional and defaults to ["default"] for generators without a distinct
    # parameter-set concept. GENIE overrides config-version handling to discover
    # tunes from staged cross-section files on disk.
    CODE_VERSIONS: dict[str, dict[str, Any]] = {}

    # Name of the Apptainer/Docker build argument that selects this generator's
    # code version (e.g. GENIE's ``GENIE_TAG``). Surfaced through the catalog so
    # ``build_apptainer_images.sh`` no longer hard-codes a per-generator mapping.
    # ``None`` for generators that are not buildable.
    build_arg_name: str | None = None

    # Energy range, in GeV, outside which this generator cannot be asked to
    # generate events at all: it crashes, or its output is unusable. Violating
    # it is a hard config error. ``None`` means "unconstrained / not known".
    MAX_ENERGY_RANGE_GEV: tuple[float, float] | None = None
    # Energy range, in GeV, over which the generator's underlying physics
    # assumptions hold. Always treated as a subset of MAX_ENERGY_RANGE_GEV (see
    # ``valid_energy_range_gev``). Violating it is a warning, not an error: an
    # out-of-validity run still produces events, they just should not be
    # trusted without further thought.
    VALID_ENERGY_RANGE_GEV: tuple[float, float] | None = None

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def binary_name(self) -> str:
        return self.executable

    def container_image(self, code_version: str | None) -> str | None:
        if not code_version:
            return None
        return self.image_for(code_version)

    def container_available(self, code_version: str | None) -> bool:
        from .. import catalog

        return catalog.image_built(self.container_image(code_version))

    def ensure_container_wrappable(self) -> None:
        """Raise unless this process may launch the container itself.

        Only Docker can be invoked from Python. Apptainer cannot nest: on the
        cluster the Slurm task enters the generator's SIF *before* Python runs
        (see slurm.render_sbatch_script), so the binary is native on $PATH and
        the adapters' native branch applies. Reaching the container branch
        under the apptainer runtime therefore means this task was launched
        outside its image — a setup error worth a clear message.
        """
        from .. import containers

        if containers.runtime() != "docker":
            raise RuntimeError(
                f"The {self.name} binary is not on $PATH and the active "
                "container runtime is not docker. Apptainer images cannot be "
                "launched from inside Python (nesting is unsupported); run "
                "this task through the rendered sbatch script, which enters "
                "the generator image first."
            )

    def is_available(self, code_version: str | None = None) -> bool:
        return (
            bool(self.binary_name()) and shutil.which(self.binary_name()) is not None
        ) or self.container_available(code_version)

    # --- Version catalog API (exposed per adapter, checked at runtime) --------

    @classmethod
    def known_code_versions(cls) -> list[str]:
        return list(cls.CODE_VERSIONS)

    @classmethod
    def image_for(cls, code_version: str) -> str | None:
        """Docker image tag for a code version (uniformly ``<name>:<code_version>``)."""
        if code_version not in cls.CODE_VERSIONS:
            return None
        return f"{cls.name}:{code_version}"

    @classmethod
    def build_arg(cls, code_version: str) -> dict[str, str] | None:
        """The image build argument selecting ``code_version``.

        Returns ``{"name": ..., "value": ...}`` (consumed by
        ``build_apptainer_images.sh`` as ``--build-arg NAME=value``) or ``None``
        when the generator declares no ``build_arg_name``. Generators whose build
        arg is a transform of the code version (e.g. GiBUU strips the ``release``
        prefix) override this method.
        """
        if not cls.build_arg_name:
            return None
        return {"name": cls.build_arg_name, "value": code_version}

    @classmethod
    def is_buildable(cls, code_version: str) -> bool:
        """True if the adapter records a source ref the setup scripts can build."""
        entry = cls.CODE_VERSIONS.get(code_version)
        return bool(entry and entry.get("git_ref"))

    @classmethod
    def known_config_versions(cls, code_version: str) -> list[str]:
        """Statically declared config versions for a code version."""
        entry = cls.CODE_VERSIONS.get(code_version, {})
        return list(entry.get("config_versions", ["default"]))

    @classmethod
    def available_config_versions(
        cls, code_version: str, software_root: str | Path | None = None
    ) -> list[str]:
        """Config versions to surface in bookkeeping.

        The default is the static ``known_config_versions``. GENIE overrides this
        to discover tunes from staged cross-section files on disk.
        """
        return cls.known_config_versions(code_version)

    @classmethod
    def max_energy_range_gev(
        cls,
        code_version: str,
        config_version: str | None = None,
        software_root: str | Path | None = None,
    ) -> tuple[float, float] | None:
        """Energy range the generator can be run over at all, or ``None``.

        The default is the declared ``MAX_ENERGY_RANGE_GEV``. GENIE overrides
        this to read the real ceiling off the staged cross-section spline, whose
        top knot is a genuine hard limit (above it the reconstructed cross
        section would be a flat extrapolation of the last knot).
        """
        return cls.MAX_ENERGY_RANGE_GEV

    @classmethod
    def valid_energy_range_gev(
        cls,
        code_version: str,
        config_version: str | None = None,
        software_root: str | Path | None = None,
    ) -> tuple[float, float] | None:
        """Energy range over which the generator's physics is trustworthy.

        The declared ``VALID_ENERGY_RANGE_GEV`` intersected with the maximum
        range: physics can never be valid where the generator cannot run, so a
        spline-limited GENIE tune narrows its own validity window automatically.
        """
        valid = cls.VALID_ENERGY_RANGE_GEV
        maximum = cls.max_energy_range_gev(code_version, config_version, software_root)
        if valid is None:
            return maximum
        if maximum is None:
            return valid
        lo = max(valid[0], maximum[0])
        hi = min(valid[1], maximum[1])
        return (lo, hi) if lo <= hi else None

    @classmethod
    def ensure_code_version(cls, code_version: str) -> None:
        from .. import catalog

        if code_version not in cls.CODE_VERSIONS:
            raise catalog.CatalogError(
                f"Unknown code_version '{code_version}' for generator '{cls.name}'. "
                f"Available code_versions: {', '.join(cls.known_code_versions())}"
            )

    @classmethod
    def ensure_compatible(
        cls,
        code_version: str,
        config_version: str,
        software_root: str | Path | None = None,
        require_available: bool = False,
    ) -> None:
        """Raise if the (code_version, config_version) pair is invalid.

        For statically-versioned generators the declared ``config_versions`` set
        *is* the availability, so ``require_available`` makes no difference.
        """
        from .. import catalog

        cls.ensure_code_version(code_version)
        valid = cls.known_config_versions(code_version)
        if config_version not in valid:
            raise catalog.CatalogError(
                f"config_version '{config_version}' is not compatible with "
                f"{cls.name} code_version '{code_version}'. "
                f"Compatible config_versions: {', '.join(valid)}"
            )

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

        # Stub events still have to carry a current, since it is a required
        # column of the common output. "cc"/"nc" are honoured exactly; for
        # "inclusive" the split is a seeded coin flip — synthetic, and not an
        # estimate of the real CC:NC ratio, which no stub can know.
        current = physics_current(self.config)

        events: list[dict[str, Any]] = []

        for offset in range(event_count):
            energy = energies[offset]
            is_cc = rng.random() < 0.5 if current == "inclusive" else current == "cc"
            events.append(
                {
                    "event_id": int(task["start_event"]) + offset,
                    "seed": int(task["seed"]),
                    "energy_gev": round(energy, 6),
                    "weight": 1.0,
                    "is_cc": is_cc,
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
                "generator_version_id": (
                    f"{task.get('code_version', 'unknown')}+"
                    f"{task.get('config_version', 'unknown')}"
                ),
            },
            "events": events,
        }
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(output)
