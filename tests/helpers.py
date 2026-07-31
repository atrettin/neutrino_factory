"""Shared configuration builders for the tests.

Two shapes matter, and they are not the same thing:

* a **run configuration** — what a YAML file describes, with a ``jobs`` list;
* a **job view** — what an adapter, translator or normalizer is actually handed,
  with the job's ``flux``/``target``/``physics`` lifted to the top level.

:func:`view_config` produces the second by running the first through
``local.job_view_config``, so tests exercise the real materialization rather
than a hand-built imitation of it that could drift away from it.
"""

from __future__ import annotations

from typing import Any

from neutrino_factory.config import resolve_config
from neutrino_factory.local import job_view_config

DEFAULT_JOB: dict[str, Any] = {
    "generator": "genie",
    "code_version": "R-3_06_00",
    "config_version": "G18_10a_02_11a",
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


def job(**overrides: Any) -> dict[str, Any]:
    """One job dict, defaulting to GENIE on argon.

    Blocks given as overrides replace the default outright rather than merging
    into it — the same rule the expander applies to ``flux`` — so a test asking
    for a histogram flux does not silently inherit power-law keys.
    """
    entry = {key: dict(value) if isinstance(value, dict) else value
             for key, value in DEFAULT_JOB.items()}
    entry.update(overrides)
    return entry


def run_config(jobs: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    """A resolved run configuration with at least one job."""
    payload: dict[str, Any] = {"jobs": jobs if jobs is not None else [job()]}
    payload.update(overrides)
    return resolve_config(payload)


def view_config(job_overrides: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """The configuration a single job's adapter/translator receives."""
    config = run_config([job(**(job_overrides or {}))], **overrides)
    task = {"task_index": 0, "job_index": 0, "job_label": config["jobs"][0]["label"]}
    return job_view_config(config, task)
