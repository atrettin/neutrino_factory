"""Job expansion: macros, matrix products, labels and seeds.

A run configuration is a list of **jobs**. A job is one generator version on one
initial state (flux x target x physics) with its own event budget and chunking —
self-contained, because the generators do not agree on what a "run" is: GiBUU
needs a different spectral index and far more statistics than the
rejection-sampling generators to reach the same statistical uncertainty, so a
single global ``flux`` and ``events`` cannot describe a production run.

``macros`` and ``matrix`` are pure sugar on top of that list. They introduce no
concept the expanded configuration does not already have: ``expand_jobs`` turns
them into plain job dictionaries before anything else in the framework looks at
the configuration, and ``neutrino-factory expand`` prints exactly what they
produced. Nothing downstream of this module knows macros exist.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
from typing import Any, Iterable, Mapping

from . import catalog
from .particles import nucleus_pdg


# ``{{name}}``, optionally padded. Deliberately distinct from the ``${VAR:-x}``
# environment syntax expanded by config.py, so a macro body can use both.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Filled in for any key a job leaves out. These are per-job, not global: the
# whole point of the schema is that two jobs in one run may disagree about every
# one of them.
JOB_DEFAULTS: dict[str, Any] = {
    "events": 100,
    "chunks": 1,
    "flux": {
        "type": "power_law",
        "particle": "numu",
        "emin_gev": 0.5,
        "emax_gev": 10.0,
        "gamma": -2.0,
    },
    "target": {"nucleus": "Ar40"},
    "physics": {"mode": "inclusive", "current": "cc"},
}

# Keys of a ``jobs:`` entry that drive expansion rather than describing the job.
_CONTROL_KEYS = ("use", "with", "matrix")

# Task seeds are drawn from [1, SEED_MODULUS]. The bound keeps a seed (plus
# GiBUU's PASS_SEED_OFFSET of 1e6) inside the signed 32-bit range that Fortran
# generators require.
SEED_MODULUS = 2_000_000_000


class JobExpansionError(ValueError):
    """Raised when a ``macros``/``jobs`` block cannot be expanded.

    Subclasses ``ValueError`` so that ``config.ConfigError`` handling in the CLI
    catches it; ``config`` re-exports it for that reason.
    """


def safe_token(value: str) -> str:
    """``value`` reduced to characters that are safe in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))


def substitute(node: Any, params: Mapping[str, Any], where: str) -> Any:
    """Replace ``{{name}}`` placeholders in ``node`` from ``params``.

    A string that is *only* a placeholder yields the parameter's value with its
    type intact — ``"{{events}}"`` with ``events: 100000`` gives the integer
    100000, not the string ``"100000"``, so a macro can template numeric fields.
    A placeholder embedded in a longer string interpolates as text and the
    result stays a string; it is never re-parsed as YAML, so an interpolated
    value cannot change type by accident.
    """
    if isinstance(node, dict):
        return {
            substitute(key, params, where): substitute(value, params, where)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [substitute(item, params, where) for item in node]
    if not isinstance(node, str):
        return node

    def lookup(name: str) -> Any:
        if name not in params:
            known = ", ".join(sorted(params)) or "(none)"
            raise JobExpansionError(
                f"{where}: unknown placeholder '{{{{{name}}}}}'. Declared parameters: {known}"
            )
        return params[name]

    whole = PLACEHOLDER.fullmatch(node)
    if whole is not None:
        return copy.deepcopy(lookup(whole.group(1)))
    return PLACEHOLDER.sub(lambda match: str(lookup(match.group(1))), node)


def matrix_combinations(matrix: Mapping[str, Any], where: str) -> list[dict[str, Any]]:
    """Every parameter combination a ``matrix:`` block describes.

    Plain keys are independent axes, crossed with ``itertools.product``. The
    ``include`` key is a *coupled* axis: a list of mappings whose keys vary
    together, cross-multiplied with the other axes — that is how the
    (generator, code_version, config_version) triple travels as one unit, and it
    is deliberately not GitHub Actions' append-and-patch ``include``. ``exclude``
    drops combinations matching all of an entry's key/value pairs.
    """
    if not isinstance(matrix, Mapping):
        raise JobExpansionError(f"{where}: 'matrix' must be a mapping")

    axes: list[tuple[str, list[Any]]] = []
    for key, values in matrix.items():
        if key in ("include", "exclude"):
            continue
        if not isinstance(values, list) or not values:
            raise JobExpansionError(
                f"{where}: matrix axis '{key}' must be a non-empty list of values"
            )
        axes.append((key, values))

    names = [name for name, _ in axes]
    combinations = [
        dict(zip(names, values)) for values in itertools.product(*(v for _, v in axes))
    ]

    include = matrix.get("include")
    if include is not None:
        if not isinstance(include, list) or not include:
            raise JobExpansionError(
                f"{where}: matrix 'include' must be a non-empty list of mappings"
            )
        for group in include:
            if not isinstance(group, Mapping):
                raise JobExpansionError(
                    f"{where}: every matrix 'include' entry must be a mapping"
                )
        combinations = [
            {**combination, **dict(group)}
            for combination in combinations
            for group in include
        ]

    exclude = matrix.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            raise JobExpansionError(
                f"{where}: matrix 'exclude' must be a list of mappings"
            )
        known_keys = {key for combination in combinations for key in combination}
        for entry in exclude:
            if not isinstance(entry, Mapping):
                raise JobExpansionError(
                    f"{where}: every matrix 'exclude' entry must be a mapping"
                )
            # A typo here would silently generate jobs the user believes are
            # excluded, so an exclude that can never match is an error.
            for key in entry:
                if key not in known_keys:
                    raise JobExpansionError(
                        f"{where}: matrix 'exclude' references unknown key '{key}'. "
                        f"Matrix keys: {', '.join(sorted(known_keys)) or '(none)'}"
                    )
        combinations = [
            combination
            for combination in combinations
            if not any(
                all(combination.get(key) == value for key, value in entry.items())
                for entry in exclude
            )
        ]

    if not combinations:
        raise JobExpansionError(
            f"{where}: matrix expands to zero jobs after applying 'exclude'"
        )
    return combinations


def resolve_params(
    declared: Mapping[str, Any],
    supplied: Mapping[str, Any],
    where: str,
    macro_name: str | None = None,
) -> dict[str, Any]:
    """Merge declared defaults with supplied values.

    A declared value of ``None`` marks the parameter as required. Supplying a
    parameter the macro does not declare is an error rather than a no-op: it is
    almost always a typo that would otherwise leave the intended field at its
    default.
    """
    owner = f"macro '{macro_name}'" if macro_name else "this job entry"
    for name in supplied:
        if name not in declared:
            known = ", ".join(sorted(declared)) or "(none)"
            raise JobExpansionError(
                f"{where}: unknown parameter '{name}' for {owner}. Declared: {known}"
            )

    params = {name: value for name, value in declared.items() if value is not None}
    params.update(supplied)

    missing = [name for name, value in declared.items() if value is None and name not in supplied]
    if missing:
        raise JobExpansionError(
            f"{where}: {owner} requires parameter(s) {', '.join(sorted(missing))} "
            "(supply them via 'with:' or a matrix axis)"
        )
    return params


def deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``updates`` onto a copy of ``base``; scalars and lists
    are replaced wholesale, nested mappings are merged. Shared with ``config``,
    which merges user configuration onto its defaults the same way."""
    merged = copy.deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _apply_defaults(job: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in whatever a job left out, from :data:`JOB_DEFAULTS`.

    ``flux`` gets the default energy range and spectral index only when the job
    describes a power law. The keys of a power-law flux and a histogram flux
    describe different things, so merging the defaults under a histogram flux
    would leave a block claiming to be both. Every other default is additive and
    merges normally.
    """
    defaults = copy.deepcopy(JOB_DEFAULTS)
    flux = job.get("flux")
    if flux and str(flux.get("type", JOB_DEFAULTS["flux"]["type"])) != "power_law":
        defaults.pop("flux")
    return deep_merge(defaults, job)


def expand_entry(
    entry: Mapping[str, Any], macros: Mapping[str, Any], index: int
) -> list[dict[str, Any]]:
    """Expand one ``jobs:`` entry into the concrete jobs it describes."""
    where = f"jobs[{index}]"
    if not isinstance(entry, Mapping):
        raise JobExpansionError(f"{where} must be a mapping")

    macro_name = entry.get("use")
    supplied = entry.get("with") or {}
    matrix = entry.get("matrix")
    inline = {key: value for key, value in entry.items() if key not in _CONTROL_KEYS}

    if not isinstance(supplied, Mapping):
        raise JobExpansionError(f"{where}: 'with' must be a mapping")

    if macro_name is not None:
        if macro_name not in macros:
            known = ", ".join(sorted(macros)) or "(none defined)"
            raise JobExpansionError(
                f"{where}: unknown macro '{macro_name}'. Defined macros: {known}"
            )
        macro = macros[macro_name]
        if not isinstance(macro, Mapping) or not isinstance(macro.get("job"), Mapping):
            raise JobExpansionError(
                f"macros.{macro_name} must be a mapping with a 'job' body"
            )
        declared = macro.get("params") or {}
        if not isinstance(declared, Mapping):
            raise JobExpansionError(f"macros.{macro_name}.params must be a mapping")
        body = macro["job"]
    else:
        # No macro: the entry itself is the body, and its parameters are exactly
        # whatever `with`/`matrix` supply. This keeps a one-off matrix from
        # needing a macro declaration just to name its axes.
        declared = {
            name: None
            for name in itertools.chain(
                supplied,
                (key for key in (matrix or {}) if key not in ("include", "exclude")),
                *(dict(group) for group in (matrix or {}).get("include", [])),
            )
        }
        body = inline
        inline = {}

    combinations = (
        matrix_combinations(matrix, where) if matrix is not None else [{}]
    )

    jobs: list[dict[str, Any]] = []
    for combination in combinations:
        params = resolve_params(
            declared, {**dict(supplied), **combination}, where, macro_name
        )
        job = _apply_defaults(substitute(copy.deepcopy(dict(body)), params, where))
        if inline:
            job = _apply_defaults(
                deep_merge(job, substitute(copy.deepcopy(inline), params, where))
            )
        # Provenance: which entry and which parameter set produced this job.
        # Surfaced by `neutrino-factory expand --json` so a materialized run can
        # be traced back to the line of YAML that asked for it.
        job["source"] = {
            "entry": index,
            "macro": macro_name,
            "params": copy.deepcopy(params),
        }
        jobs.append(job)
    return jobs


def job_label(job: Mapping[str, Any]) -> str:
    """The job's identity, used in every output path it produces.

    A label is a pure function of the job itself and is never disambiguated
    against its neighbours: the label ends up in HDF5 filenames, and adding an
    unrelated job to a configuration must not silently rename another job's
    outputs. Two jobs that collide are therefore an error, not a rename.
    """
    name = job.get("name")
    if name:
        return safe_token(str(name))
    version_id = catalog.version_identifier(
        str(job.get("code_version", "")), str(job.get("config_version", ""))
    )
    return safe_token(
        "_".join(
            [
                str(job.get("generator", "unknown")),
                version_id,
                str(job.get("flux", {}).get("particle", "unknown")),
                str(job.get("target", {}).get("nucleus", "unknown")),
                str(job.get("physics", {}).get("current", "unknown")),
            ]
        )
    )


def job_seed(run_seed: int, label: str, chunk_id: int) -> int:
    """The random seed for one chunk of one job.

    Hashed rather than laid out arithmetically so that a seed depends only on
    the run seed and the chunk's own identity: inserting or removing a job never
    changes another job's random stream, which an offset scheme (``run.seed +
    generator_index * 1000 + chunk``) could not promise. The result is always
    positive and inside the signed 32-bit range Fortran generators need.
    """
    digest = hashlib.blake2b(
        f"{int(run_seed)}|{label}|{int(chunk_id)}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % SEED_MODULUS + 1


def expand_jobs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Materialize ``config['jobs']`` into concrete, labelled job dictionaries.

    Runs before validation, so that everything downstream — validation included —
    only ever sees plain jobs.
    """
    entries = config.get("jobs")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise JobExpansionError("jobs must be a list of job entries")

    macros = config.get("macros") or {}
    if not isinstance(macros, Mapping):
        raise JobExpansionError("macros must be a mapping of name to macro definition")

    jobs: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        jobs.extend(expand_entry(entry, macros, index))

    labels: dict[str, int] = {}
    for index, job in enumerate(jobs):
        target = job.setdefault("target", {})
        if "pdg" not in target:
            # Derived rather than configured: a hand-written PDG that disagrees
            # with the nucleus name would generate events on the wrong target
            # without any visible sign. An unparsable name raises here, at
            # expansion time, rather than inside a running Slurm task.
            try:
                target["pdg"] = nucleus_pdg(str(target.get("nucleus", "")))
            except ValueError as error:
                raise JobExpansionError(f"jobs[{job['source']['entry']}]: {error}") from None

        job["index"] = index
        label = job_label(job)
        if label in labels:
            raise JobExpansionError(
                f"Duplicate job label '{label}' from jobs entries "
                f"{jobs[labels[label]]['source']['entry']} and {job['source']['entry']}. "
                "Job labels are built from the generator, its versions, flux.particle, "
                "target.nucleus and physics.current; these jobs differ only in other "
                "fields. Give at least one of them an explicit 'name:' — it becomes "
                "the label verbatim."
            )
        labels[label] = index
        job["label"] = label

    return jobs


def job_summaries(jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One flat row per job, for ``neutrino-factory expand`` and log lines."""
    return [
        {
            "index": job.get("index"),
            "label": job.get("label"),
            "generator": job.get("generator"),
            "code_version": job.get("code_version"),
            "config_version": job.get("config_version"),
            "particle": job.get("flux", {}).get("particle"),
            "nucleus": job.get("target", {}).get("nucleus"),
            "current": job.get("physics", {}).get("current"),
            "events": job.get("events"),
            "chunks": job.get("chunks"),
        }
        for job in jobs
    ]
