"""Global catalog of generator versions.

Single source of truth for every generator's *code versions* and, for each code
version, the *config versions* (physics parameter sets — GENIE calls these
"tunes") that are compatible with it. Config validation, the ``list-generators``
CLI command, the generator adapters, and the setup/build scripts all read from
this catalog so that a run always traces back to an exact, catalogued
combination of code and configuration.

A generator instance is uniquely identified by two independent axes:

* ``code_version`` — the code itself (a git tag / release commit, e.g. GENIE
  ``R-3_06_00`` or NuWro ``nuwro_25.11``).
* ``config_version`` — the physics parameter configuration (e.g. the GENIE tune
  ``G18_10a_02_11a``). For generators without a distinct parameter-set concept
  yet, this is ``"default"``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# code_version -> metadata. ``config_versions`` lists the config versions
# compatible with that code version. ``image`` is the Docker image tag that a
# successful build produces (also what the adapters run and what ``--built``
# checks for). ``repo``/``git_ref`` pin the exact source the build script uses.
GENERATOR_CATALOG: dict[str, dict[str, dict[str, Any]]] = {
    "genie": {
        "R-3_06_00": {
            "repo": "https://github.com/GENIE-MC/Generator",
            "git_ref": "R-3_06_00",
            "image": "genie:R-3_06_00",
            "config_versions": ["G18_10a_02_11a", "AR23_20i_00_000"],
        },
    },
    "nuwro": {
        "nuwro_25.11": {
            "repo": "https://github.com/NuWro/nuwro",
            "git_ref": "nuwro_25.11",
            "image": "nuwro:nuwro_25.11",
            # NuWro parameter-set versioning is a non-blocking TODO; only
            # "default" exists for now.
            "config_versions": ["default"],
        },
    },
    "gibuu": {
        # GiBUU is distributed as HEPForge release tarballs, not a git repo
        # (the GitHub mirror is out of date and must not be used). ``git_ref``
        # holds the release tag so ``is_buildable()`` returns True; the setup
        # script downloads the matching release/buuinput/RootTuple tarballs.
        "release2025": {
            "repo": "https://gibuu.hepforge.org/downloads",
            "git_ref": "release2025",
            "image": "gibuu:release2025",
            # GiBUU parameter-set versioning is a non-blocking TODO; only
            # "default" exists for now.
            "config_versions": ["default"],
        },
    },
    "neut": {
        # Catalogued for completeness, but not buildable: NEUT source is not
        # freely available and no image exists. ``repo``/``git_ref`` are None.
        "5.x": {
            "repo": None,
            "git_ref": None,
            "image": "neut:5.x",
            "config_versions": ["default"],
        },
    },
}


class CatalogError(ValueError):
    """Raised when a generator / code_version / config_version is not catalogued."""


def known_generators() -> list[str]:
    return list(GENERATOR_CATALOG)


def code_versions(generator: str) -> list[str]:
    return list(GENERATOR_CATALOG.get(generator, {}))


def config_versions(generator: str, code_version: str) -> list[str]:
    entry = GENERATOR_CATALOG.get(generator, {}).get(code_version, {})
    return list(entry.get("config_versions", []))


def _tag_safe(value: str) -> str:
    """Filesystem-safe form of a code_version, used as a directory name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _normalize_tune(value: str) -> str:
    """Case- and separator-insensitive key for matching tune directory names."""
    return re.sub(r"[^A-Za-z0-9]", "", value).lower()


def _default_software_root() -> str:
    return os.environ.get("NF_SOFTWARE_ROOT", "./software")


def genie_xsecs_xml(
    software_root: str | Path, code_version: str, config_version: str
) -> Path | None:
    """Locate the staged GENIE cross-section spline for a (code, tune) pair.

    Looks for ``<software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml``,
    first by exact tune directory name, then by a separator-insensitive match
    (the FNAL tarballs name tunes with underscores stripped, e.g.
    ``G1810a0211a`` for ``G18_10a_02_11a``). Returns ``None`` if no file exists.
    """
    tune = str(config_version or "").strip()
    code = str(code_version or "").strip()
    if not tune or not code:
        return None

    xsec_root = Path(software_root) / "genie" / "genie_xsec" / _tag_safe(code)

    exact = xsec_root / tune / "xsecs.xml"
    if exact.is_file():
        return exact

    target = _normalize_tune(tune)
    for candidate in sorted(xsec_root.glob("*/xsecs.xml")):
        if _normalize_tune(candidate.parent.name) == target:
            return candidate
    return None


def available_config_versions(
    generator: str, code_version: str, software_root: str | Path | None = None
) -> list[str]:
    """Config versions to surface in bookkeeping.

    For GENIE, a tune is only "available" once its ``xsecs.xml`` spline exists on
    disk (staged by ``setup/download_genie_xsec.sh``). Other generators do not
    yet have a filesystem-backed config-version concept, so all catalogued
    config versions are returned unchanged.
    """
    all_versions = config_versions(generator, code_version)
    if generator != "genie":
        return all_versions
    root = software_root if software_root is not None else _default_software_root()
    return [
        cv
        for cv in all_versions
        if genie_xsecs_xml(root, code_version, cv) is not None
    ]


def image_for(generator: str, code_version: str) -> str | None:
    entry = GENERATOR_CATALOG.get(generator, {}).get(code_version)
    if entry is None:
        return None
    return entry.get("image")


def is_buildable(generator: str, code_version: str) -> bool:
    """True if the catalog records a source ref the setup scripts can build."""
    entry = GENERATOR_CATALOG.get(generator, {}).get(code_version)
    return bool(entry and entry.get("git_ref"))


def version_identifier(code_version: str, config_version: str) -> str:
    """Uniform, unique version identifier across all generators."""
    return f"{code_version}+{config_version}"


def ensure_known(generator: str, code_version: str) -> None:
    if generator not in GENERATOR_CATALOG:
        raise CatalogError(
            f"Unknown generator '{generator}'. Known generators: "
            f"{', '.join(known_generators())}"
        )
    if code_version not in GENERATOR_CATALOG[generator]:
        raise CatalogError(
            f"Unknown code_version '{code_version}' for generator '{generator}'. "
            f"Available code_versions: {', '.join(code_versions(generator))}"
        )


def ensure_compatible(generator: str, code_version: str, config_version: str) -> None:
    """Strict check: raise if the (code_version, config_version) pair is invalid."""
    ensure_known(generator, code_version)
    valid = config_versions(generator, code_version)
    if config_version not in valid:
        raise CatalogError(
            f"config_version '{config_version}' is not compatible with "
            f"{generator} code_version '{code_version}'. "
            f"Compatible config_versions: {', '.join(valid)}"
        )


def image_built(image: str | None) -> bool:
    """True if a Docker image with this tag exists locally.

    Uses ``docker images -q <image>`` (prints the image ID, empty if absent)
    rather than ``docker image inspect``: under Docker Desktop's containerd
    image store, ``inspect <name>`` can spuriously fail with "No such image"
    for an image that plainly exists in ``docker images``.
    """
    if not image or not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "images", "-q", image],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
