from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np


STRING_DTYPE = h5py.string_dtype(encoding="utf-8")
EVENT_NUMERIC_FIELDS = ("event_id", "seed", "energy_gev", "weight")
EVENT_STRING_FIELDS = ("interaction", "probe", "target", "generator")
EVENT_FIELDS = (*EVENT_NUMERIC_FIELDS, *EVENT_STRING_FIELDS)

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

    Also records the simulated ``flux`` (the framework flux block, so it can be
    rebuilt with ``flux.build_flux`` for plotting) and ``expected_events`` (the
    requested event count for this task, distinct from the actual number
    written to ``run/event_count``).
    """
    code_version = str(task.get("code_version", "unknown"))
    config_version = str(task.get("config_version", "unknown"))
    return {
        "generator": generator_name,
        "code_version": code_version,
        "config_version": config_version,
        "generator_version_id": f"{code_version}+{config_version}",
        "execution_mode": execution_mode,
        "run_name": task["run_name"],
        "chunk_id": int(task["chunk_id"]),
        "seed": int(task["seed"]),
        "flux": dict(task.get("flux", {})),
        "expected_events": int(task.get("event_count", 0)),
    }


def _serialize_attr(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _decode_text_array(values: np.ndarray) -> np.ndarray:
    if values.dtype.kind == "S":
        return np.char.decode(values, "utf-8")
    return np.asarray(values, dtype=object).astype(str)


def _read_event_columns(handle: h5py.File) -> dict[str, np.ndarray]:
    event_group = handle["events"]
    columns: dict[str, np.ndarray] = {
        "event_id": np.asarray(event_group["event_id"][()], dtype=np.int64),
        "seed": np.asarray(event_group["seed"][()], dtype=np.int64),
        "energy_gev": np.asarray(event_group["energy_gev"][()], dtype=np.float64),
        "weight": np.asarray(event_group["weight"][()], dtype=np.float64),
    }
    for key in EVENT_STRING_FIELDS:
        columns[key] = _decode_text_array(np.asarray(event_group[key][()]))
    return columns


def _rows_from_event_columns(columns: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    count = len(columns["event_id"])
    return [
        {
            "event_id": int(columns["event_id"][index]),
            "seed": int(columns["seed"][index]),
            "energy_gev": float(columns["energy_gev"][index]),
            "weight": float(columns["weight"][index]),
            "interaction": str(columns["interaction"][index]),
            "probe": str(columns["probe"][index]),
            "target": str(columns["target"][index]),
            "generator": str(columns["generator"][index]),
        }
        for index in range(count)
    ]


def _concat_event_columns(chunks: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not chunks:
        return {
            "event_id": np.array([], dtype=np.int64),
            "seed": np.array([], dtype=np.int64),
            "energy_gev": np.array([], dtype=np.float64),
            "weight": np.array([], dtype=np.float64),
            "interaction": np.array([], dtype="U"),
            "probe": np.array([], dtype="U"),
            "target": np.array([], dtype="U"),
            "generator": np.array([], dtype="U"),
        }

    merged: dict[str, np.ndarray] = {}
    for key in EVENT_FIELDS:
        arrays = [chunk[key] for chunk in chunks]
        merged[key] = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
    return merged


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

    with h5py.File(input_path, "r") as handle:
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value

        columns = _read_event_columns(handle)

    return metadata, _rows_from_event_columns(columns)


def merge_hdf5_files(
    input_files: Iterable[str | Path],
    output_path: str | Path,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    merged_chunks: list[dict[str, np.ndarray]] = []
    normalized_inputs = [str(Path(path)) for path in input_files]
    if not normalized_inputs:
        raise MergeError("No input files provided to merge")

    identity: tuple[str, ...] | None = None
    identity_source: str | None = None
    first_file_metadata: dict[str, Any] = {}
    expected_events_total = 0

    for input_file in normalized_inputs:
        with h5py.File(input_file, "r") as handle:
            file_metadata: dict[str, Any] = {}
            for key, value in handle["metadata"].attrs.items():
                file_metadata[key] = value.decode() if isinstance(value, bytes) else value
            columns = _read_event_columns(handle)

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
        expected_events_total += int(file_metadata.get("expected_events", 0))
        merged_chunks.append(columns)

    merged_columns = _concat_event_columns(merged_chunks)
    merged_events = _rows_from_event_columns(merged_columns)

    metadata = dict(run_metadata or {})
    # Guarantee the merged file records the (single, consistent) version identity,
    # even if the caller did not supply it in run_metadata.
    for key in (*VERSION_IDENTITY_KEYS, "generator_version_id"):
        if key not in metadata and key in first_file_metadata:
            metadata[key] = first_file_metadata[key]

    # Carry the (identical) flux from the inputs and sum the requested event
    # counts so the merged file stays self-describing for plotting/validation.
    if "flux" not in metadata and "flux" in first_file_metadata:
        metadata["flux"] = first_file_metadata["flux"]
    metadata.setdefault("expected_events", expected_events_total)

    metadata.setdefault("merged_inputs", normalized_inputs)
    metadata["merged_file_count"] = len(normalized_inputs)
    metadata["merged_event_count"] = int(len(merged_columns["event_id"]))

    return write_common_hdf5(output_path, metadata, merged_events)
