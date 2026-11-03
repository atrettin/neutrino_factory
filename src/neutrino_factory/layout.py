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


# The one file a pruned raw task directory keeps. It records everything the
# task was run with: the jobcards, params files and flux tables are generated
# from it.
PRUNE_KEEP = "translated_config.json"


def prune_raw_outputs(raw_dir: Path) -> tuple[int, int]:
    """Delete everything the generator left in ``raw_dir`` but ``translated_config.json``.

    Returns the bytes and the number of files freed. Besides the event files
    (``*.root``), which are nearly all of the bytes, generators leave
    auxiliary files: GiBUU writes ~16 per pass, ~300 per chunk with
    ``qes_oversampling: 8``, which in a large run would exhaust the
    filesystem's file quota. Only files in a validated chunk's directory are
    deleted, which nothing reads any more; the Slurm logs live elsewhere.
    Emptied subdirectories are removed too.
    """
    freed_bytes = 0
    freed_files = 0
    for path in sorted(raw_dir.rglob("*")):
        if path == raw_dir / PRUNE_KEEP or path.is_dir() and not path.is_symlink():
            continue
        freed_bytes += path.lstat().st_size
        freed_files += 1
        path.unlink()
    for path in sorted(raw_dir.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    return freed_bytes, freed_files


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
