from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import containers
from .config import enabled_generator_instances


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


def build_task_manifest(config: dict[str, Any]) -> dict[str, Any]:
    run = config["run"]
    splitting = config["splitting"]
    generator_instances = enabled_generator_instances(config)
    ranges = chunk_ranges(int(run["events"]), int(splitting["chunks"]))

    tasks: list[dict[str, Any]] = []
    task_index = 0
    for generator_offset, generator_instance in enumerate(generator_instances):
        generator_name = generator_instance["name"]
        generator_version_id = generator_instance["version_id"]
        generator_version_token = _safe_token(generator_version_id)
        for chunk_id, (start_event, stop_event) in enumerate(ranges):
            task = {
                "task_index": task_index,
                "generator_name": generator_name,
                "generator_config": dict(generator_instance["config"]),
                "code_version": generator_instance["code_version"],
                "config_version": generator_instance["config_version"],
                "image": generator_instance["image"],
                "generator_version_id": generator_version_id,
                "generator_version_token": generator_version_token,
                "chunk_id": chunk_id,
                "start_event": start_event,
                "stop_event": stop_event,
                "event_count": stop_event - start_event,
                "seed": int(run["seed"]) + generator_offset * 1000 + chunk_id,
                "run_name": run["name"],
                # Carry the framework flux block so normalized output is
                # self-describing (rebuildable via flux.build_flux for plots).
                "flux": dict(config.get("flux", {})),
            }
            tasks.append(task)
            task_index += 1

    return {
        "manifest_version": 2,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "run_name": run["name"],
        "config_path": config.get("config_path"),
        "executor": run.get("executor", "local"),
        "task_count": len(tasks),
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
    array_max = max(0, int(manifest["task_count"]) - 1)
    slurm = config["slurm"]
    # Slurm executes a spool *copy* of the sbatch file, so BASH_SOURCE cannot
    # locate the repo. Embed the absolute repo root at render time instead
    # (valid for this project's editable install / source checkout).
    repo_root = Path(__file__).resolve().parents[2]

    if containers.runtime() == "apptainer":
        # Inverted container layering: the array task enters the generator's
        # SIF first and runs the CLI inside it, where the generator binary is
        # native on $PATH (Apptainer cannot nest). One SIF path per task index;
        # an empty entry (no catalogued image) runs on the host, e.g. stubs.
        sif_entries = "\n".join(
            f'  "{containers.sif_path(task["image"]) if task["image"] else ""}"'
            for task in manifest["tasks"]
        )
        task_launcher = f"""TASK_SIFS=(
{sif_entries}
)
SIF="${{TASK_SIFS[$SLURM_ARRAY_TASK_ID]}}"
if [[ -n "$SIF" ]]; then
  apptainer exec "$SIF" bash "{repo_root}/jobs/run_task.sh" "{config.get('config_path', '')}" "{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}"
else
  bash "{repo_root}/jobs/run_task.sh" "{config.get('config_path', '')}" "{manifest_path}" "${{SLURM_ARRAY_TASK_ID}}"
fi"""
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
