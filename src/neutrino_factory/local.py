from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .merge import merge_outputs
from .slurm import build_task_manifest, write_manifest
from .generators.registry import get_adapter


def _load_manifest(manifest: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(manifest, dict):
        return manifest
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


def _ensure_layout(config: dict[str, Any]) -> None:
    for key in ("work_root", "output_root", "scratch_root"):
        Path(config["storage"][key]).mkdir(parents=True, exist_ok=True)
    (Path(config["storage"]["work_root"]) / "logs").mkdir(parents=True, exist_ok=True)


def run_task(config: dict[str, Any], task: dict[str, Any], execution_mode: str = "local") -> str:
    _ensure_layout(config)

    adapter = get_adapter(task["generator_name"], config)
    work_root = Path(config["storage"]["work_root"])
    output_root = Path(config["storage"]["output_root"])
    generator_name = task["generator_name"]
    version_token = str(task["generator_version_token"])
    file_stem = f"{task['run_name']}_{generator_name}_{version_token}_chunk{task['chunk_id']:03d}"

    # Each task gets its own work directory (keyed by the unique file_stem, which
    # includes run name and chunk). Generators write fixed-name artifacts
    # (e.g. events.ghep.root, events.gst.root, translated_config.json) into this
    # directory, so tasks that share a (generator, version) must not share it —
    # otherwise a later run reuses an earlier run's stale ROOT output.
    raw_path = (
        work_root
        / "raw"
        / generator_name
        / version_token
        / file_stem
        / f"{file_stem}.json"
    )
    normalized_path = (
        output_root
        / "chunks"
        / generator_name
        / version_token
        / f"{file_stem}.h5"
    )
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

    # Chunks are merged per (generator, code_version, config_version): files from
    # different generators or versions describe different physics and must never
    # be merged together.
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    chunk_outputs: list[str] = []
    for task in manifest["tasks"]:
        output = run_task(config, task, execution_mode="local")
        chunk_outputs.append(output)
        key = (task["generator_name"], task["code_version"], task["config_version"])
        group = groups.setdefault(key, {"task": task, "outputs": []})
        group["outputs"].append(output)

    merged_outputs: list[str] = []
    for (generator_name, code_version, config_version), group in groups.items():
        task = group["task"]
        version_token = str(task["generator_version_token"])
        merged_output = merged_dir / f"{config['run']['name']}_{generator_name}_{version_token}.h5"
        merge_outputs(
            group["outputs"],
            merged_output,
            run_metadata={
                "run_name": config["run"]["name"],
                "executor": "local",
                "generator": generator_name,
                "code_version": code_version,
                "config_version": config_version,
                "generator_version_id": task["generator_version_id"],
                "task_count": len(group["outputs"]),
                "config_path": config.get("config_path", ""),
            },
        )
        merged_outputs.append(str(merged_output))

    return {
        "manifest_path": str(manifest_location),
        "task_count": manifest["task_count"],
        "chunk_outputs": chunk_outputs,
        "merged_outputs": merged_outputs,
    }


def run_task_from_manifest(
    config: dict[str, Any],
    manifest_path: str | Path,
    task_index: int,
    execution_mode: str = "slurm",
) -> str:
    manifest = _load_manifest(manifest_path)
    matching = [task for task in manifest["tasks"] if int(task["task_index"]) == int(task_index)]
    if not matching:
        raise IndexError(f"Task index {task_index} not found in manifest")
    return run_task(config, matching[0], execution_mode=execution_mode)


def plan_and_run_local(config: dict[str, Any]) -> dict[str, Any]:
    manifest = build_task_manifest(config)
    manifest_path = write_manifest(config)
    return run_local(config, manifest_path=manifest_path)
