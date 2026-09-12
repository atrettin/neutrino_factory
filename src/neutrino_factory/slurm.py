from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import containers
from .jobs import job_seed
from .layout import chunk_output_path


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def chunk_ranges(total_events: int, chunks: int) -> list[tuple[int, int]]:
    chunks = max(1, int(chunks))
    total_events = int(total_events)
    base, remainder = divmod(total_events, chunks)
    ranges: list[tuple[int, int]] = []
    start = 0

    for index in range(chunks):
        size = base + (1 if index < remainder else 0)
        stop = start + size
        if start != stop:
            ranges.append((start, stop))
        start = stop

    return ranges


def _ensure_unique_seeds(tasks: list[dict[str, Any]]) -> None:
    """Guard against a hash collision in ``job_seed``.

    Astronomically unlikely, and silently catastrophic if it ever happened: two
    tasks would generate the identical events, inflating the statistics of a
    physics sample without any visible sign. Cheap to check, so check.
    """
    seen: dict[int, int] = {}
    for task in tasks:
        seed = int(task["seed"])
        if seed in seen:
            raise ValueError(
                f"Seed collision between task {seen[seed]} and task "
                f"{task['task_index']} (seed {seed}). Change run.seed."
            )
        seen[seed] = int(task["task_index"])


def build_task_manifest(config: dict[str, Any]) -> dict[str, Any]:
    """One task per (job, chunk).

    Jobs are heterogeneous — each carries its own event budget and chunk count —
    so the total task count is the sum over jobs, not a product.
    """
    run = config["run"]
    jobs = config["jobs"]

    tasks: list[dict[str, Any]] = []
    task_index = 0
    for job_index, job in enumerate(jobs):
        ranges = chunk_ranges(int(job["events"]), int(job["chunks"]))
        for chunk_id, (start_event, stop_event) in enumerate(ranges):
            task = {
                "task_index": task_index,
                # The job this task belongs to. The index resolves against the
                # configuration (local.job_view_config), the label is carried
                # alongside so a manifest written against a since-edited config
                # is detected instead of silently running the wrong job.
                "job_index": job_index,
                "job_label": job["label"],
                "generator_name": job["generator"],
                "code_version": job["code_version"],
                "config_version": job["config_version"],
                "chunk_id": chunk_id,
                "start_event": start_event,
                "event_count": stop_event - start_event,
                "seed": job_seed(int(run["seed"]), str(job["label"]), chunk_id),
                "run_name": run["name"],
                # Carry the framework flux block so normalized output is
                # self-describing (rebuildable via flux.build_flux for plots).
                "flux": copy.deepcopy(job["flux"]),
            }
            tasks.append(task)
            task_index += 1

    _ensure_unique_seeds(tasks)

    return {
        "manifest_version": 4,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "run_name": run["name"],
        "config_path": config.get("config_path"),
        "executor": run.get("executor", "local"),
        # The materialized jobs, recorded once rather than repeated on every
        # task: provenance for what this run was planned to be.
        "jobs": copy.deepcopy(jobs),
        "tasks": tasks,
    }


def default_manifest_path(config: dict[str, Any]) -> Path:
    work_root = Path(config["storage"]["work_root"])
    manifest_dir = work_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    return manifest_dir / f"{config['run']['name']}.json"


def write_manifest(config: dict[str, Any], manifest_path: str | Path | None = None) -> str:
    manifest = build_task_manifest(config)
    destination = Path(manifest_path) if manifest_path else default_manifest_path(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return str(destination)


def render_sbatch_script(config: dict[str, Any], manifest_path: str | Path) -> str:
    manifest = build_task_manifest(config)
    array_max = max(0, len(manifest["tasks"]) - 1)
    slurm = config["slurm"]
    # Slurm executes a spool *copy* of the sbatch file, so BASH_SOURCE cannot
    # locate the repo. Embed the absolute repo root at render time instead
    # (valid for this project's editable install / source checkout).
    repo_root = Path(__file__).resolve().parents[2]

    if containers.runtime() == "apptainer":
        # Unified Apptainer runtime: every task runs inside nf-base.sif.
        # Note the closing quote on the last line: writing it as a bare `"` right
        # before the f-string's `"""` terminator silently merges into the
        # delimiter, which is how this shipped an unterminated quote and made
        # every apptainer-runtime submission die with "unexpected EOF while
        # looking for matching". Keep the escape.
        task_launcher = f"""NF_BASE_SIF="${{NF_IMAGE_ROOT:-{repo_root}/software/images}}/nf-base.sif"
if [[ ! -f "$NF_BASE_SIF" ]]; then
    echo "ERROR: unified Apptainer runtime image not found: $NF_BASE_SIF" >&2
    exit 1
fi
apptainer exec "$NF_BASE_SIF" bash "{repo_root}/jobs/run_task.sh" "{config.get('config_path', '')}" "{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}\""""
        python_export = ""
    else:
        task_launcher = (
            f'bash "{repo_root}/jobs/run_task.sh" "{config.get("config_path", "")}" '
            f'"{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}"'
        )
        # Reuse the submitting interpreter (e.g. the project venv) on the
        # compute node. Not set for apptainer: there the task runs inside the
        # generator image, whose own python3 must be used.
        python_export = f"export PYTHON={sys.executable}\n"

    return f"""#!/bin/bash -l
#SBATCH -J nf_{config['run']['name']}
#SBATCH -o {config['storage']['work_root']}/logs/%x_%A_%a.out
#SBATCH -e {config['storage']['work_root']}/logs/%x_%A_%a.err
#SBATCH -D {config['storage']['work_root']}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={slurm['cpus_per_task']}
#SBATCH --mem={slurm['mem']}
#SBATCH --time={slurm['time']}
#SBATCH --partition={slurm['partition']}
#SBATCH --array=0-{array_max}

set -euo pipefail
# Storage roots and container settings persisted by `neutrino-factory setup`.
if [[ -f "{repo_root}/.env" ]]; then set -a; source "{repo_root}/.env"; set +a; fi
export NF_EXECUTION_MODE=slurm
export OMP_NUM_THREADS=${{SLURM_CPUS_PER_TASK:-1}}
{python_export}
mkdir -p "{config['storage']['work_root']}/logs"

{task_launcher}
"""


def write_sbatch_script(config: dict[str, Any], manifest_path: str | Path) -> str:
    work_root = Path(config["storage"]["work_root"])
    slurm_dir = work_root / "slurm"
    slurm_dir.mkdir(parents=True, exist_ok=True)
    destination = slurm_dir / f"{config['run']['name']}.sbatch"
    destination.write_text(render_sbatch_script(config, manifest_path), encoding="utf-8")
    return str(destination)


def format_slurm_array_spec(task_indices: list[int]) -> str:
    """Convert a list of task indices to compact Slurm array format.

    Examples:
        [0, 1, 2, 5, 7, 8, 9] -> "0-2,5,7-9"
        [5] -> "5"
        [0, 2, 4, 6] -> "0,2,4,6"
    """
    if not task_indices:
        return ""

    sorted_indices = sorted(task_indices)
    ranges: list[str] = []
    start = sorted_indices[0]
    end = sorted_indices[0]

    for i in range(1, len(sorted_indices)):
        if sorted_indices[i] == end + 1:
            end = sorted_indices[i]
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = sorted_indices[i]
            end = sorted_indices[i]

    # Add the last range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def identify_missing_chunks(
    config: dict[str, Any], manifest: dict[str, Any]
) -> list[int]:
    """Identify tasks whose chunk output files are missing.

    Args:
        config: The run configuration
        manifest: The task manifest (loaded from JSON)

    Returns:
        List of task indices (integers) where the chunk file does not exist
    """
    missing_indices: list[int] = []

    for task in manifest["tasks"]:
        chunk_path = chunk_output_path(config, task)
        if not chunk_path.exists():
            missing_indices.append(int(task["task_index"]))

    return missing_indices


def render_retry_sbatch_script(
    config: dict[str, Any],
    manifest_path: str | Path,
    missing_task_indices: list[int],
    time_override: str,
) -> str:
    """Render an sbatch script for retrying failed tasks.

    Like render_sbatch_script but with a restricted array specification
    and overridden time parameter.

    Args:
        config: The run configuration
        manifest_path: Path to the existing manifest JSON
        missing_task_indices: List of task indices to retry
        time_override: Slurm time string (e.g., "01:00:00")

    Returns:
        The rendered sbatch script as a string
    """
    array_spec = format_slurm_array_spec(missing_task_indices)
    slurm = config["slurm"]
    repo_root = Path(__file__).resolve().parents[2]

    if containers.runtime() == "apptainer":
        task_launcher = f"""NF_BASE_SIF="${{NF_IMAGE_ROOT:-{repo_root}/software/images}}/nf-base.sif"
if [[ ! -f "$NF_BASE_SIF" ]]; then
    echo "ERROR: unified Apptainer runtime image not found: $NF_BASE_SIF" >&2
    exit 1
fi
apptainer exec "$NF_BASE_SIF" bash "{repo_root}/jobs/run_task.sh" "{config.get('config_path', '')}" "{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}\""""
        python_export = ""
    else:
        task_launcher = (
            f'bash "{repo_root}/jobs/run_task.sh" "{config.get("config_path", "")}" '
            f'"{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}"'
        )
        python_export = f"export PYTHON={sys.executable}\n"

    return f"""#!/bin/bash -l
#SBATCH -J nf_{config['run']['name']}_retry
#SBATCH -o {config['storage']['work_root']}/logs/%x_%A_%a.out
#SBATCH -e {config['storage']['work_root']}/logs/%x_%A_%a.err
#SBATCH -D {config['storage']['work_root']}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={slurm['cpus_per_task']}
#SBATCH --mem={slurm['mem']}
#SBATCH --time={time_override}
#SBATCH --partition={slurm['partition']}
#SBATCH --array={array_spec}

set -euo pipefail
# Storage roots and container settings persisted by `neutrino-factory setup`.
if [[ -f "{repo_root}/.env" ]]; then set -a; source "{repo_root}/.env"; set +a; fi
export NF_EXECUTION_MODE=slurm
export OMP_NUM_THREADS=${{SLURM_CPUS_PER_TASK:-1}}
{python_export}
mkdir -p "{config['storage']['work_root']}/logs"

{task_launcher}
"""
