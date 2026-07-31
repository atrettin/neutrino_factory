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
from . import jobs as jobs_module
from . import particles
from .jobs import JobExpansionError, deep_merge


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
        "seed": 12345,
        "executor": "local",
        "stub_mode": True,
        "log_level": "default",
    },
    # Reusable parameterized job templates; see neutrino_factory.jobs. Expanded
    # away before validation, so no other module ever sees them.
    "macros": {},
    # The run itself: one entry per generator version x initial state. Per-job
    # defaults live in jobs.JOB_DEFAULTS, not here, because they are filled in
    # per job rather than once for the whole configuration.
    "jobs": [],
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


def _validate_job(
    job: Dict[str, Any],
    where: str,
    stub_mode: bool,
    software_root: Any,
    flux_base_dir: str | None,
) -> list[str]:
    """Everything that must hold for one expanded job, as a list of messages."""
    errors: list[str] = []

    generator = str(job.get("generator") or "").strip()
    if not generator:
        errors.append(f"{where}: missing required field 'generator'")
    elif generator not in catalog.known_generators():
        errors.append(
            f"{where}: unknown generator '{generator}'. "
            f"Known: {', '.join(catalog.known_generators())}"
        )

    code_version = str(job.get("code_version") or "").strip()
    config_version = str(job.get("config_version") or "").strip()
    if not code_version:
        errors.append(f"{where}: missing required field 'code_version'")
    if not config_version:
        errors.append(f"{where}: missing required field 'config_version'")

    if generator and code_version and config_version and not errors:
        try:
            catalog.ensure_compatible(
                generator,
                code_version,
                config_version,
                software_root,
                require_available=not stub_mode,
            )
        except catalog.CatalogError as error:
            errors.append(f"{where}: {error}")

    try:
        events = int(job.get("events", 0))
    except (TypeError, ValueError):
        events = 0
        errors.append(f"{where}: events must be an integer")
    try:
        chunks = int(job.get("chunks", 0))
    except (TypeError, ValueError):
        chunks = 0
        errors.append(f"{where}: chunks must be an integer")

    if events < 1:
        errors.append(f"{where}: events must be >= 1 (got {job.get('events')})")
    if chunks < 1:
        errors.append(f"{where}: chunks must be >= 1 (got {job.get('chunks')})")
    if events >= 1 and chunks > events:
        # Splitting into more chunks than events would silently produce fewer
        # tasks than asked for, so say so instead.
        errors.append(f"{where}: chunks ({chunks}) exceeds events ({events})")

    for message in flux_module.validate_flux(job.get("flux", {}), base_dir=flux_base_dir):
        errors.append(f"{where}: {message}")

    current = job.get("physics", {}).get("current", "cc")
    if not isinstance(current, str) or current.lower() not in PHYSICS_CURRENTS:
        errors.append(
            f"{where}: physics.current must be one of {', '.join(PHYSICS_CURRENTS)} "
            f"(got '{current}')"
        )

    log_level = job.get("log_level")
    if log_level is not None and log_level not in LOG_LEVELS:
        errors.append(
            f"{where}: log_level must be one of {', '.join(LOG_LEVELS)} (got '{log_level}')"
        )

    nucleus = str(job.get("target", {}).get("nucleus", ""))
    try:
        derived_pdg = particles.nucleus_pdg(nucleus)
    except ValueError as error:
        errors.append(f"{where}: {error}")
    else:
        configured_pdg = job.get("target", {}).get("pdg")
        if configured_pdg is not None and int(configured_pdg) != derived_pdg:
            LOGGER.warning(
                "%s: target.pdg %s does not match the code derived from nucleus "
                "'%s' (%d). The explicit pdg is used.",
                where,
                configured_pdg,
                nucleus,
                derived_pdg,
            )

    return errors


def validate_config(config: Dict[str, Any]) -> None:
    errors = _validate_required_sections(config, ["run", "jobs", "storage", "slurm"])

    run = config.get("run", {})
    job_list = config.get("jobs", [])

    log_level = run.get("log_level", "default")
    if log_level not in LOG_LEVELS:
        errors.append(
            f"run.log_level must be one of {', '.join(LOG_LEVELS)} (got '{log_level}')"
        )

    # In stub mode we generate synthetic events, so config-version availability
    # (e.g. staged GENIE cross-section splines) is not required. Real runs must
    # validate strictly so a valid config is guaranteed to run.
    stub_mode = bool(run.get("stub_mode", True))
    if stub_mode and job_list:
        LOGGER.warning(
            "run.stub_mode is enabled: generator config_versions are NOT checked "
            "for availability (e.g. staged GENIE cross-section splines). Set "
            "run.stub_mode: false to validate that configured versions can "
            "actually run."
        )

    if not isinstance(job_list, list):
        errors.append("jobs must be a list of job entries")
        job_list = []
    elif not job_list:
        errors.append("At least one job must be configured")

    config_path = config.get("config_path")
    flux_base_dir = str(Path(config_path).parent) if config_path else None
    software_root = config.get("storage", {}).get("software_root")

    for index, job in enumerate(job_list):
        if not isinstance(job, dict):
            errors.append(f"jobs[{index}] must be a mapping")
            continue
        where = f"jobs[{index}] ({job.get('label', '?')})"
        errors.extend(
            _validate_job(job, where, stub_mode, software_root, flux_base_dir)
        )

    if errors:
        raise ConfigError("; ".join(errors))


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
    merged = deep_merge(DEFAULT_CONFIG, config)
    merged = _expand_env_values(merged)
    merged["run"]["executor"] = os.environ.get(
        "NF_EXECUTION_MODE", merged["run"].get("executor", "local")
    )

    # Set before expansion and validation: relative flux histogram paths in a
    # job resolve against the configuration file's own directory.
    if source_path is not None:
        merged["config_path"] = str(Path(source_path).resolve())

    # Macros and matrices are expanded away here, before validation, so every
    # later consumer — validation included — only ever sees plain jobs. An
    # expansion failure is a configuration error like any other, so it is
    # re-raised as one rather than leaking a second exception type to callers.
    try:
        merged["jobs"] = jobs_module.expand_jobs(merged)
    except JobExpansionError as error:
        raise ConfigError(str(error)) from None

    validate_config(merged)
    merged["enabled_generators"] = enabled_generators(merged)
    return merged


def load_config(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    return resolve_config(user_config, source_path=path)


def enabled_generators(config: Dict[str, Any]) -> list[str]:
    """The distinct generators the run's jobs use, in first-appearance order."""
    ordered: list[str] = []
    for job in config.get("jobs", []):
        name = job.get("generator")
        if name and name not in ordered:
            ordered.append(name)
    return ordered
