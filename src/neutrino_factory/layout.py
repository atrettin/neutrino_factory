"""Where a run's files go.

The single source of truth for output paths. ``local`` writes them and
``validate_output`` predicts them; before this module the two constructed the
same strings independently and had to be kept in lockstep by hand, so a change
to one silently broke ``check-status``, ``merge --config`` and ``plot-output``.

Everything is keyed by the **job label** (see :mod:`neutrino_factory.jobs`),
which already encodes generator, versions, flux particle, target nucleus and
weak current — so two jobs of one run can never collide, and a file's name says
what physics is in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .jobs import safe_token


def chunk_file_stem(run_name: str, job_label: str, chunk_id: int) -> str:
    """Filename stem shared by one chunk's raw and normalized outputs.

    Carries the run name because ``output_root`` is shared across runs, and the
    chunk id because every chunk of a job gets its own file.
    """
    return f"{safe_token(run_name)}_{job_label}_chunk{int(chunk_id):03d}"


def _stem_for_task(task: Mapping[str, Any]) -> str:
    return chunk_file_stem(str(task["run_name"]), str(task["job_label"]), int(task["chunk_id"]))


def raw_output_path(config: Mapping[str, Any], task: Mapping[str, Any]) -> Path:
    """The generator's raw output for ``task``.

    Each task gets its own directory: generators write fixed-name artifacts
    (``events.ghep.root``, ``translated_config.json``, ...) beside it, so two
    tasks sharing a directory would make a later run read an earlier run's stale
    output.
    """
    stem = _stem_for_task(task)
    return Path(config["storage"]["work_root"]) / "raw" / str(task["job_label"]) / stem / f"{stem}.json"


def chunk_output_path(config: Mapping[str, Any], task: Mapping[str, Any]) -> Path:
    """The normalized (common-format HDF5) output for one chunk."""
    stem = _stem_for_task(task)
    return Path(config["storage"]["output_root"]) / "chunks" / str(task["job_label"]) / f"{stem}.h5"


def chunk_sidecar_path(config: Mapping[str, Any], task: Mapping[str, Any]) -> Path:
    """The per-chunk sidecar recording start/finish times and output validity.

    Sits beside the normalized chunk HDF5 (see :func:`chunk_output_path`), written
    by ``local.run_task`` when a task starts and updated when it finishes, so a run's
    progress and wall-clock cost can be read before (and without) any HDF5 existing.
    """
    stem = _stem_for_task(task)
    return Path(config["storage"]["output_root"]) / "chunks" / str(task["job_label"]) / f"{stem}.sidecar.json"


def merged_output_path(config: Mapping[str, Any], job: Mapping[str, Any]) -> Path:
    """The single HDF5 file all of ``job``'s chunks merge into."""
    run_name = safe_token(str(config["run"]["name"]))
    return Path(config["storage"]["output_root"]) / "merged" / f"{run_name}_{job['label']}.h5"


def plots_dir(config: Mapping[str, Any]) -> Path:
    """Default destination for ``neutrino-factory plot-output``."""
    return Path(config["storage"]["output_root"]) / "plots"
