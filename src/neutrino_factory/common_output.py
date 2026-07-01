from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np


STRING_DTYPE = h5py.string_dtype(encoding="utf-8")

# Metadata keys that uniquely identify a generator version. Files that differ on
# any of these describe different physics and must never be merged together.
VERSION_IDENTITY_KEYS = ("generator", "code_version", "config_version")


class MergeError(ValueError):
    """Raised when attempting to merge HDF5 files with inconsistent version metadata."""


def _version_identity(metadata: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(metadata.get(key, "unknown")) for key in VERSION_IDENTITY_KEYS)


def version_metadata(generator_name: str, task: dict[str, Any], execution_mode: str) -> dict[str, Any]:
    """Build the common metadata block carrying generator version info.

    Version info lives in metadata only (not per event): the two independent
    axes ``code_version`` and ``config_version`` plus the combined
    ``generator_version_id`` for convenience.
    """
    return {
        "generator": generator_name,
        "code_version": str(task.get("code_version", "unknown")),
        "config_version": str(task.get("config_version", "unknown")),
        "generator_version_id": str(task.get("generator_version_id", "unknown")),
        "execution_mode": execution_mode,
        "run_name": task["run_name"],
        "chunk_id": int(task["chunk_id"]),
        "seed": int(task["seed"]),
    }


def _serialize_attr(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_common_hdf5(output_path: str | Path, metadata: dict[str, Any], events: list[dict[str, Any]]) -> str:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    event_ids = np.array([int(event["event_id"]) for event in events], dtype=np.int64) if events else np.array([], dtype=np.int64)
    seeds = np.array([int(event["seed"]) for event in events], dtype=np.int64) if events else np.array([], dtype=np.int64)
    energies = np.array([float(event["energy_gev"]) for event in events], dtype=np.float64) if events else np.array([], dtype=np.float64)
    weights = np.array([float(event.get("weight", 1.0)) for event in events], dtype=np.float64) if events else np.array([], dtype=np.float64)
    interactions = np.array([str(event.get("interaction", "unknown")) for event in events], dtype=STRING_DTYPE)
    probes = np.array([str(event.get("probe", "unknown")) for event in events], dtype=STRING_DTYPE)
    targets = np.array([str(event.get("target", "unknown")) for event in events], dtype=STRING_DTYPE)
    generators = np.array([str(event.get("generator", metadata.get("generator", "unknown"))) for event in events], dtype=STRING_DTYPE)

    with h5py.File(output, "w") as handle:
        meta_group = handle.create_group("metadata")
        for key, value in metadata.items():
            meta_group.attrs[key] = _serialize_attr(value)

        run_group = handle.create_group("run")
        run_group.attrs["event_count"] = int(len(events))

        event_group = handle.create_group("events")
        event_group.create_dataset("event_id", data=event_ids)
        event_group.create_dataset("seed", data=seeds)
        event_group.create_dataset("energy_gev", data=energies)
        event_group.create_dataset("weight", data=weights)
        event_group.create_dataset("interaction", data=interactions, dtype=STRING_DTYPE)
        event_group.create_dataset("probe", data=probes, dtype=STRING_DTYPE)
        event_group.create_dataset("target", data=targets, dtype=STRING_DTYPE)
        event_group.create_dataset("generator", data=generators, dtype=STRING_DTYPE)

    return str(output)


def read_events(input_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    with h5py.File(input_path, "r") as handle:
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value

        event_group: Any = handle["events"]
        event_id_ds: Any = event_group["event_id"]
        seed_ds: Any = event_group["seed"]
        energy_ds: Any = event_group["energy_gev"]
        weight_ds: Any = event_group["weight"]
        interaction_ds: Any = event_group["interaction"]
        probe_ds: Any = event_group["probe"]
        target_ds: Any = event_group["target"]
        generator_ds: Any = event_group["generator"]

        count = len(event_id_ds)
        for index in range(count):
            event = {
                "event_id": int(event_id_ds[index]),
                "seed": int(seed_ds[index]),
                "energy_gev": float(energy_ds[index]),
                "weight": float(weight_ds[index]),
                "interaction": interaction_ds[index].decode() if isinstance(interaction_ds[index], bytes) else str(interaction_ds[index]),
                "probe": probe_ds[index].decode() if isinstance(probe_ds[index], bytes) else str(probe_ds[index]),
                "target": target_ds[index].decode() if isinstance(target_ds[index], bytes) else str(target_ds[index]),
                "generator": generator_ds[index].decode() if isinstance(generator_ds[index], bytes) else str(generator_ds[index]),
            }
            events.append(event)

    return metadata, events


def merge_hdf5_files(
    input_files: Iterable[str | Path],
    output_path: str | Path,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    merged_events: list[dict[str, Any]] = []
    normalized_inputs = [str(Path(path)) for path in input_files]
    if not normalized_inputs:
        raise MergeError("No input files provided to merge")

    identity: tuple[str, ...] | None = None
    identity_source: str | None = None
    first_file_metadata: dict[str, Any] = {}

    for input_file in normalized_inputs:
        file_metadata, events = read_events(input_file)
        current = _version_identity(file_metadata)
        if identity is None:
            identity = current
            identity_source = input_file
            first_file_metadata = file_metadata
        elif current != identity:
            raise MergeError(
                "Refusing to merge HDF5 files from different generator versions: "
                f"{identity_source} is {dict(zip(VERSION_IDENTITY_KEYS, identity))} "
                f"but {input_file} is {dict(zip(VERSION_IDENTITY_KEYS, current))}"
            )
        merged_events.extend(events)

    metadata = dict(run_metadata or {})
    # Guarantee the merged file records the (single, consistent) version identity,
    # even if the caller did not supply it in run_metadata.
    for key in (*VERSION_IDENTITY_KEYS, "generator_version_id"):
        if key not in metadata and key in first_file_metadata:
            metadata[key] = first_file_metadata[key]

    metadata.setdefault("merged_inputs", normalized_inputs)
    metadata["merged_file_count"] = len(normalized_inputs)
    metadata["merged_event_count"] = len(merged_events)

    return write_common_hdf5(output_path, metadata, merged_events)
