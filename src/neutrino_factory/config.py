from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from . import catalog
from . import flux as flux_module

DEFAULT_CONFIG: Dict[str, Any] = {
    "run": {
        "name": "neutrino_factory_run",
        "events": 100,
        "seed": 12345,
        "executor": "local",
        "stub_mode": True,
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
                    "code_version": "5.x",
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
        "scratch_root": "${NF_SCRATCH_ROOT:-./scratch}",
    },
    "slurm": {
        "partition": "standard",
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


def validate_config(config: Dict[str, Any]) -> None:
    errors = _validate_required_sections(
        config,
        ["run", "flux", "target", "generators", "splitting", "storage", "slurm"],
    )

    run = config.get("run", {})
    flux = config.get("flux", {})
    splitting = config.get("splitting", {})
    generators = config.get("generators", {})

    if int(run.get("events", 0)) < 1:
        errors.append("run.events must be >= 1")
    if int(splitting.get("chunks", 0)) < 1:
        errors.append("splitting.chunks must be >= 1")

    config_path = config.get("config_path")
    flux_base_dir = str(Path(config_path).parent) if config_path else None
    errors.extend(flux_module.validate_flux(flux, base_dir=flux_base_dir))

    if not isinstance(generators, dict):
        errors.append("generators must be a mapping")
        generators = {}

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
                catalog.ensure_compatible(generator_name, code_version, config_version)
            except catalog.CatalogError as error:
                errors.append(str(error))
                continue

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


def resolve_config(config: Dict[str, Any], source_path: str | Path | None = None) -> Dict[str, Any]:
    merged = _deep_merge(DEFAULT_CONFIG, config)
    merged = _expand_env_values(merged)
    merged["run"]["executor"] = os.environ.get(
        "NF_EXECUTION_MODE", merged["run"].get("executor", "local")
    )

    if source_path is not None:
        merged["config_path"] = str(Path(source_path).resolve())

    validate_config(merged)
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
