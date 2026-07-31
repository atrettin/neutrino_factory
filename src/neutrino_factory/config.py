from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from . import catalog
from . import flux as flux_module


LOGGER = logging.getLogger(__name__)

# How chatty the generator's own logs should be. Generator-agnostic names; each
# adapter maps them onto its native mechanism (GENIE: messenger thresholds).
# "essential" is the interesting one: initial job configuration, output-file
# writes, and warnings/errors — nothing per-event.
LOG_LEVELS = ("default", "essential", "quiet", "verbose")

# Which weak current the generators may produce. "inclusive" lets a generator
# produce both, in its own relative proportion; "cc"/"nc" restrict generation at
# the generator's own configuration level (not by filtering afterwards), so the
# cross section reconstructed for the run is the cross section of that current
# alone. Every event carries the resulting per-event ``is_cc`` flag in the
# common output.
PHYSICS_CURRENTS = ("cc", "nc", "inclusive")


def physics_current(config: Dict[str, Any]) -> str:
    """The run's weak current, normalized to one of ``PHYSICS_CURRENTS``.

    ``validate_config`` already rejects unknown values, but translators and
    adapters are also handed hand-built configs (tests, ``run_task`` from a
    manifest), so the check is repeated here rather than trusting the caller:
    silently falling back to "cc" for a typo would generate physically different
    events than the config asks for.
    """
    current = str(config.get("physics", {}).get("current", "cc")).lower()
    if current not in PHYSICS_CURRENTS:
        raise ValueError(
            f"Unknown physics.current '{current}'. Known: {', '.join(PHYSICS_CURRENTS)}"
        )
    return current

DEFAULT_CONFIG: Dict[str, Any] = {
    "run": {
        "name": "neutrino_factory_run",
        "events": 100,
        "seed": 12345,
        "executor": "local",
        "stub_mode": True,
        "log_level": "default",
    },
    "flux": {
        "type": "power_law",
        "particle": "numu",
        "emin_gev": 0.5,
        "emax_gev": 10.0,
        "gamma": -2.0,
    },
    "target": {
        "nucleus": "Ar40",
        "pdg": 1000180400,
    },
    "physics": {
        "mode": "inclusive",
        "current": "cc",
    },
    "generators": {
        "genie": {
            "versions": [
                {
                    "enabled": True,
                    "code_version": "R-3_06_00",
                    "config_version": "G18_10a_02_11a",
                }
            ]
        },
        "neut": {
            "versions": [
                {
                    "enabled": False,
                    "code_version": "5.7.0-nuint2024",
                    "config_version": "default",
                }
            ]
        },
        "nuwro": {
            "versions": [
                {
                    "enabled": False,
                    "code_version": "nuwro_25.11",
                    "config_version": "default",
                }
            ]
        },
    },
    "splitting": {
        "strategy": "events",
        "chunks": 1,
    },
    "storage": {
        "software_root": "${NF_SOFTWARE_ROOT:-./software}",
        "output_root": "${NF_OUTPUT_ROOT:-./output}",
        "work_root": "${NF_WORK_ROOT:-./work}",
        # Container image storage (Apptainer SIFs). containers.py reads the env
        # var directly; this key surfaces the resolved value for bookkeeping.
        "image_root": "${NF_IMAGE_ROOT:-./software/images}",
    },
    "slurm": {
        # "alma" targets the *new* MPP Slurm cluster (submit from mppui1/2),
        # which requires --partition=alma; max job duration 1 day. The old
        # cluster's partitions (short, standard, ...) are not valid there.
        "partition": "alma",
        "time": "00:10:00",
        "cpus_per_task": 1,
        "mem": "2G",
    },
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the user configuration is invalid."""


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_env_string(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        variable, default = match.group(1), match.group(2)
        return os.environ.get(variable, default or "")

    return os.path.expanduser(_ENV_PATTERN.sub(replace, value))


def _expand_env_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env_values(item) for item in value]
    if isinstance(value, str):
        return _expand_env_string(value)
    return value


def _validate_required_sections(config: Dict[str, Any], required: Iterable[str]) -> list[str]:
    errors = []
    for section in required:
        if section not in config or config[section] in (None, {}):
            errors.append(f"Missing required section: {section}")
    return errors


def _format_range(bounds: tuple[float, float]) -> str:
    return f"{bounds[0]:g}-{bounds[1]:g} GeV"


def _contains(outer: tuple[float, float], inner: tuple[float, float]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _validate_energy_range(
    generator_name: str,
    code_version: str,
    config_version: str,
    software_root: str | None,
    requested: tuple[float, float],
    warnings: list[str],
) -> list[str]:
    """Check the run's energy range against a generator version's two ranges.

    Outside the *maximum* range the generator cannot be asked to generate events
    at all, so that is an error. Outside the *valid* range it runs but its
    physics assumptions do not hold, so that is a warning appended to
    ``warnings``. A ``None`` range means the adapter declares no limit.
    """
    errors: list[str] = []
    label = f"{generator_name}[{catalog.version_identifier(code_version, config_version)}]"

    maximum = catalog.max_energy_range_gev(
        generator_name, code_version, config_version, software_root
    )
    if maximum is not None and not _contains(maximum, requested):
        errors.append(
            f"Flux energy range {_format_range(requested)} is outside the range "
            f"{label} can generate events over ({_format_range(maximum)})"
        )
        # A range that is already impossible needs no validity warning on top.
        return errors

    valid = catalog.valid_energy_range_gev(
        generator_name, code_version, config_version, software_root
    )
    if valid is not None and not _contains(valid, requested):
        warnings.append(
            f"Flux energy range {_format_range(requested)} extends outside the "
            f"range over which {label}'s physics assumptions hold "
            f"({_format_range(valid)}); events generated outside it may not be "
            f"physically reliable"
        )
    return errors


def validate_config(config: Dict[str, Any]) -> list[str]:
    """Validate a resolved config, returning non-fatal warnings.

    Raises :class:`ConfigError` with every error joined into one message. The
    returned warnings describe configurations that will run but whose output
    deserves scrutiny (out-of-validity energies, unchecked availability in stub
    mode); callers are expected to surface them to the user.
    """
    warnings: list[str] = []
    errors = _validate_required_sections(
        config,
        ["run", "flux", "target", "generators", "splitting", "storage", "slurm"],
    )

    run = config.get("run", {})
    flux = config.get("flux", {})
    physics = config.get("physics", {})
    splitting = config.get("splitting", {})
    generators = config.get("generators", {})

    if int(run.get("events", 0)) < 1:
        errors.append("run.events must be >= 1")
    if int(splitting.get("chunks", 0)) < 1:
        errors.append("splitting.chunks must be >= 1")
    log_level = run.get("log_level", "default")
    if log_level not in LOG_LEVELS:
        errors.append(
            f"run.log_level must be one of {', '.join(LOG_LEVELS)} (got '{log_level}')"
        )
    current = physics.get("current", "cc")
    if not isinstance(current, str) or current.lower() not in PHYSICS_CURRENTS:
        errors.append(
            f"physics.current must be one of {', '.join(PHYSICS_CURRENTS)} (got '{current}')"
        )

    config_path = config.get("config_path")
    flux_base_dir = str(Path(config_path).parent) if config_path else None
    flux_errors = flux_module.validate_flux(flux, base_dir=flux_base_dir)
    errors.extend(flux_errors)

    # The run's energy range is a property of the flux object, not of the config
    # keys: a histogram flux takes its bounds from the ROOT histogram's edges.
    # Only buildable (i.e. already valid) flux blocks are checked against the
    # per-generator ranges below.
    requested_range: tuple[float, float] | None = None
    if not flux_errors:
        try:
            flux_object = flux_module.build_flux(flux, base_dir=flux_base_dir)
        except flux_module.FluxError as error:
            errors.append(str(error))
        else:
            requested_range = (flux_object.emin_gev, flux_object.emax_gev)

    if not isinstance(generators, dict):
        errors.append("generators must be a mapping")
        generators = {}

    # In stub mode we generate synthetic events, so config-version availability
    # (e.g. staged GENIE cross-section splines) is not required. Real runs must
    # validate strictly so a valid config is guaranteed to run.
    stub_mode = bool(run.get("stub_mode", True))
    if stub_mode and generators:
        warnings.append(
            "run.stub_mode is enabled: generator config_versions are NOT checked "
            "for availability (e.g. staged GENIE cross-section splines). Set "
            "run.stub_mode: false to validate that configured versions can "
            "actually run."
        )

    enabled_instance_count = 0
    for generator_name, generator_block in generators.items():
        if not isinstance(generator_block, dict):
            errors.append(f"generators.{generator_name} must be a mapping")
            continue

        versions = generator_block.get("versions")
        if not isinstance(versions, list):
            errors.append(f"generators.{generator_name}.versions must be a list")
            continue

        seen_version_ids: set[str] = set()
        for index, generator_config in enumerate(versions):
            if not isinstance(generator_config, dict):
                errors.append(f"generators.{generator_name}.versions[{index}] must be a mapping")
                continue
            if not bool(generator_config.get("enabled", False)):
                continue
            enabled_instance_count += 1

            field_prefix = f"generators.{generator_name}.versions[{index}]"
            code_version = str(generator_config.get("code_version") or "").strip()
            config_version = str(generator_config.get("config_version") or "").strip()
            if not code_version:
                errors.append(f"Missing required generator field: {field_prefix}.code_version")
            if not config_version:
                errors.append(f"Missing required generator field: {field_prefix}.config_version")
            if not code_version or not config_version:
                continue

            try:
                software_root = config.get("storage", {}).get("software_root")
                catalog.ensure_compatible(
                    generator_name,
                    code_version,
                    config_version,
                    software_root,
                    require_available=not stub_mode,
                )
            except catalog.CatalogError as error:
                errors.append(str(error))
                continue

            if requested_range is not None:
                errors.extend(
                    _validate_energy_range(
                        generator_name,
                        code_version,
                        config_version,
                        software_root,
                        requested_range,
                        warnings,
                    )
                )

            version_id = catalog.version_identifier(code_version, config_version)
            if version_id in seen_version_ids:
                errors.append(
                    f"Duplicate version identifier for generator '{generator_name}': {version_id}"
                )
            seen_version_ids.add(version_id)

    if enabled_instance_count == 0:
        errors.append("At least one generator version entry must be enabled")

    if errors:
        raise ConfigError("; ".join(errors))
    return warnings


def find_repo_root(start: str | Path | None = None) -> Path | None:
    """Walk upward from ``start`` (default: CWD) to the directory containing
    ``pyproject.toml``. Returns ``None`` if no repo root is found."""
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def load_env_file(start: str | Path | None = None) -> Path | None:
    """Load ``KEY=value`` pairs from the repo-root ``.env`` into the environment.

    Values are applied with setdefault semantics: variables already set in the
    real environment always win. Blank lines and ``#`` comments are ignored, as
    are malformed lines. Returns the path of the file that was loaded, if any.
    """
    root = find_repo_root(start)
    if root is None:
        return None
    env_path = root / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        # Drop any trailing inline comment, then surrounding quotes.
        value = value.split("#", 1)[0].strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)
    return env_path


def resolve_config(config: Dict[str, Any], source_path: str | Path | None = None) -> Dict[str, Any]:
    merged = _deep_merge(DEFAULT_CONFIG, config)
    merged = _expand_env_values(merged)
    merged["run"]["executor"] = os.environ.get(
        "NF_EXECUTION_MODE", merged["run"].get("executor", "local")
    )

    if source_path is not None:
        merged["config_path"] = str(Path(source_path).resolve())

    warnings = validate_config(merged)
    for warning in warnings:
        LOGGER.warning("%s", warning)
    merged["validation_warnings"] = warnings
    merged["enabled_generator_instances"] = enabled_generator_instances(merged)
    merged["enabled_generators"] = enabled_generators(merged)
    return merged


def load_config(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    return resolve_config(user_config, source_path=path)


def enabled_generators(config: Dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for instance in enabled_generator_instances(config):
        name = instance["name"]
        if name not in ordered:
            ordered.append(name)
    return ordered


def enabled_generator_instances(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    instances: list[Dict[str, Any]] = []
    for generator_name, generator_block in config.get("generators", {}).items():
        versions = generator_block["versions"]
        for generator_config in versions:
            if not generator_config.get("enabled", False):
                continue
            code_version = str(generator_config["code_version"])
            config_version = str(generator_config["config_version"])
            instances.append(
                {
                    "name": generator_name,
                    "config": dict(generator_config),
                    "code_version": code_version,
                    "config_version": config_version,
                    "version_id": catalog.version_identifier(code_version, config_version),
                    "image": catalog.image_for(generator_name, code_version),
                }
            )
    return instances
