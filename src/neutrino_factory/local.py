from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

from . import catalog, layout
from .merge import merge_outputs
from .slurm import build_task_manifest, write_manifest
from .generators.registry import get_adapter


def _load_manifest(manifest: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(manifest, dict):
        return manifest
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


def _ensure_layout(config: dict[str, Any]) -> None:
    for key in ("work_root", "output_root"):
        Path(config["storage"][key]).mkdir(parents=True, exist_ok=True)
    (Path(config["storage"]["work_root"]) / "logs").mkdir(parents=True, exist_ok=True)


def job_view_config(config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The configuration as this task's job sees it.

    Adapters, translators, normalizers and the stub generator all read
    ``config["flux"]``, ``config["target"]`` and ``config["physics"]``. Rather
    than teaching every one of them about jobs, the job's own blocks are grafted
    onto the global ``run``/``storage``/``slurm`` configuration here, at the one
    point where a task is turned into work.

    The blocks are *replaced*, never merged: a histogram-flux job merged over a
    power-law base would keep the base's ``gamma``/``emin_gev`` keys and hand
    ``build_flux`` a block describing two different fluxes at once.
    """
    jobs = config.get("jobs") or []
    index = int(task["job_index"])
    if index >= len(jobs):
        raise IndexError(
            f"Manifest task {task.get('task_index')} refers to job {index}, but this "
            f"configuration expands to {len(jobs)} job(s). Re-run `neutrino-factory plan`."
        )
    job = jobs[index]

    expected_label = task.get("job_label")
    if expected_label and expected_label != job.get("label"):
        raise ValueError(
            f"Manifest task {task.get('task_index')} refers to job {index} "
            f"('{expected_label}'), but this configuration expands job {index} to "
            f"'{job.get('label')}'. The configuration changed after the manifest was "
            "written; re-run `neutrino-factory plan`."
        )

    view = copy.deepcopy(config)
    view["flux"] = copy.deepcopy(job["flux"])
    view["target"] = copy.deepcopy(job["target"])
    view["physics"] = copy.deepcopy(job["physics"])
    view["run"] = {
        **config["run"],
        "log_level": job.get("log_level", config["run"].get("log_level", "default")),
    }
    view["job"] = copy.deepcopy(job)
    return view


def run_task(config: dict[str, Any], task: dict[str, Any], execution_mode: str = "local") -> str:
    config = job_view_config(config, task)
    _ensure_layout(config)

    adapter = get_adapter(task["generator_name"], config)

    # Each task gets its own work directory: generators write fixed-name
    # artifacts (events.ghep.root, events.gst.root, translated_config.json) into
    # it, so two tasks sharing a directory would make a later run read an
    # earlier run's stale ROOT output. See layout.py for the path scheme.
    raw_path = layout.raw_output_path(config, task)
    normalized_path = layout.chunk_output_path(config, task)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)

    translated_config = adapter.translate_config(task)
    stub_mode = bool(config["run"].get("stub_mode", True))

    if stub_mode or not adapter.is_available(task.get("code_version")):
        adapter.run_stub(translated_config, raw_path, task)
    else:
        command = adapter.build_run_command(translated_config, raw_path.parent)
        subprocess.run(command, check=True, cwd=raw_path.parent)

    return adapter.normalize_output(raw_path, normalized_path, task, execution_mode)


def run_local(config: dict[str, Any], manifest_path: str | Path | None = None) -> dict[str, Any]:
    manifest_location = manifest_path or write_manifest(config)
    manifest = _load_manifest(manifest_location)

    merged_dir = Path(config["storage"]["output_root"]) / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)

    # Chunks are merged per job. Two jobs may share a generator and version and
    # still differ in flux particle, target nucleus or weak current, so the job
    # — not the generator version — is what decides which files describe the
    # same physics and may be merged.
    groups: dict[int, list[str]] = {}
    chunk_outputs: list[str] = []
    for task in manifest["tasks"]:
        output = run_task(config, task, execution_mode="local")
        chunk_outputs.append(output)
        groups.setdefault(int(task["job_index"]), []).append(output)

    merged_outputs: list[str] = []
    for job_index, outputs in groups.items():
        job = config["jobs"][job_index]
        merged_output = layout.merged_output_path(config, job)
        merge_outputs(
            outputs,
            merged_output,
            run_metadata={
                "run_name": config["run"]["name"],
                "executor": "local",
                "job_label": job["label"],
                "generator": job["generator"],
                "code_version": job["code_version"],
                "config_version": job["config_version"],
                "generator_version_id": catalog.version_identifier(
                    str(job["code_version"]), str(job["config_version"])
                ),
                # The initial state, so a merged file says what physics it holds
                # without anyone having to re-read the configuration.
                "probe": job["flux"]["particle"],
                "target_nucleus": job["target"]["nucleus"],
                "task_count": len(outputs),
                "config_path": config.get("config_path", ""),
            },
        )
        merged_outputs.append(str(merged_output))

    return {
        "manifest_path": str(manifest_location),
        "task_count": len(manifest["tasks"]),
        "chunk_outputs": chunk_outputs,
        "merged_outputs": merged_outputs,
    }


def run_task_from_manifest(
    config: dict[str, Any],
    manifest_path: str | Path | dict[str, Any],
    task_index: int,
    execution_mode: str | None = None,
) -> str:
    manifest = _load_manifest(manifest_path)
    matching = [task for task in manifest["tasks"] if int(task["task_index"]) == int(task_index)]
    if not matching:
        raise IndexError(f"Task index {task_index} not found in manifest")
    resolved_execution_mode = execution_mode or str(
        manifest.get("executor") or config.get("run", {}).get("executor", "slurm")
    )
    return run_task(config, matching[0], execution_mode=resolved_execution_mode)


def plan_and_run_local(config: dict[str, Any]) -> dict[str, Any]:
    manifest = build_task_manifest(config)
    manifest_path = write_manifest(config)
    return run_local(config, manifest_path=manifest_path)
