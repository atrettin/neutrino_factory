"""Generator-agnostic version catalog facade.

Every generator's version knowledge — which *code versions* exist, which *config
versions* (physics parameter sets — GENIE calls these "tunes") are available, the
Docker image tag, and whether a code version is buildable — now lives on the
generator's adapter class (``GeneratorAdapter.CODE_VERSIONS`` and its version-API
classmethods in ``generators/base.py``, with GENIE overriding config-version
discovery to read staged cross-section files off disk). This module is a thin,
generator-agnostic facade that dispatches to the adapters via the registry, plus a
few pure helpers that are not specific to any generator.

A generator instance is uniquely identified by two independent axes:

* ``code_version`` — the code itself (a git tag / release commit, e.g. GENIE
  ``R-3_06_00`` or NuWro ``nuwro_25.11``).
* ``config_version`` — the physics parameter configuration (e.g. the GENIE tune
  ``G18_10a_02_11a``). For generators without a distinct parameter-set concept
  yet, this is ``"default"``.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import containers


class CatalogError(ValueError):
    """Raised when a generator / code_version / config_version is not catalogued."""


def _adapter(generator: str) -> type:
    """Return the adapter class for a generator, or raise ``CatalogError``.

    The registry import is deferred to call time: the adapters import this module
    (for ``CatalogError`` / ``image_built`` / ``_tag_safe``), so importing the
    registry at module top would create a circular import.
    """
    from .generators.registry import REGISTRY

    try:
        return REGISTRY[generator]
    except KeyError as error:
        raise CatalogError(
            f"Unknown generator '{generator}'. Known generators: "
            f"{', '.join(known_generators())}"
        ) from error


def known_generators() -> list[str]:
    from .generators.registry import REGISTRY

    return list(REGISTRY)


def code_versions(generator: str) -> list[str]:
    return _adapter(generator).known_code_versions()


def available_config_versions(
    generator: str, code_version: str, software_root: str | Path | None = None
) -> list[str]:
    return _adapter(generator).available_config_versions(code_version, software_root)


def image_for(generator: str, code_version: str) -> str | None:
    return _adapter(generator).image_for(code_version)


def is_buildable(generator: str, code_version: str) -> bool:
    """True if the adapter records a source ref the setup scripts can build."""
    return _adapter(generator).is_buildable(code_version)


def build_arg(generator: str, code_version: str) -> dict[str, str] | None:
    """Image build argument selecting ``code_version`` (name/value), or ``None``.

    Lets ``build_apptainer_images.sh`` pass ``--build-arg NAME=value`` without a
    hard-coded per-generator mapping.
    """
    return _adapter(generator).build_arg(code_version)


def ensure_known(generator: str, code_version: str) -> None:
    _adapter(generator).ensure_code_version(code_version)


def ensure_compatible(
    generator: str,
    code_version: str,
    config_version: str,
    software_root: str | Path | None = None,
    require_available: bool = False,
) -> None:
    """Raise if the (code_version, config_version) pair is invalid.

    ``require_available`` requests strict availability checking (e.g. GENIE tunes
    must have their cross-section spline staged on disk); it should be set for
    real runs and left off for stub-only runs.
    """
    _adapter(generator).ensure_compatible(
        code_version, config_version, software_root, require_available
    )


def version_identifier(code_version: str, config_version: str) -> str:
    """Uniform, unique version identifier across all generators."""
    return f"{code_version}+{config_version}"


def _tag_safe(value: str) -> str:
    """Filesystem-safe form of a code_version, used as a directory name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def image_built(image: str | None) -> bool:
    """True if the image exists for the active container runtime.

    Delegates to :func:`containers.image_available`: a local Docker image for
    the ``docker`` runtime, a staged SIF file under ``NF_IMAGE_ROOT`` for the
    ``apptainer`` runtime.
    """
    return containers.image_available(image)
