from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np

from . import kinematics


STRING_DTYPE = h5py.string_dtype(encoding="utf-8")

# The numeric event columns, each with its dtype and the default used when an
# event dict omits it (stub/JSON mode) or when reading a file written before the
# column existed; ``None`` marks a column every event must carry. Everything that
# iterates over columns -- the writer, the reader, the merger -- is driven off
# this single table.
NUMERIC_FIELD_SPECS: dict[str, tuple[type, Any]] = {
    "event_id": (np.int64, None),
    "seed": (np.int64, None),
    "energy_gev": (np.float64, None),
    "weight": (np.float64, 1.0),
    "xsec_weight": (np.float64, 1.0),
    **{name: (np.float64, default) for name, default in kinematics.FIELD_DEFAULTS.items()},
}

EVENT_NUMERIC_FIELDS = tuple(NUMERIC_FIELD_SPECS)
EVENT_STRING_FIELDS = ("interaction", "probe", "target", "generator")
EVENT_FIELDS = (*EVENT_NUMERIC_FIELDS, *EVENT_STRING_FIELDS)

# Metadata keys that uniquely identify a generator version. Files that differ on
# any of these describe different physics and must never be merged together.
VERSION_IDENTITY_KEYS = ("generator", "code_version", "config_version")


# Metadata key recording the denominator a chunk's ``xsec_weight`` was divided
# by (see ``ConfigTranslator.xsec_norm_count``). Present on real generator
# output; absent on stub output, whose weights are placeholders.
XSEC_NORM_COUNT_KEY = "xsec_norm_count"


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
    """Read the ``events`` group into per-column arrays.

    Columns a file does not carry are synthesized from their default. That keeps
    files written before a column was introduced readable and mergeable; the
    kinematic columns come back as placeholders rather than raising.
    """
    event_group = handle["events"]
    count = int(len(event_group["event_id"]))

    columns: dict[str, np.ndarray] = {}
    for key, (dtype, default) in NUMERIC_FIELD_SPECS.items():
        if key in event_group:
            columns[key] = np.asarray(event_group[key][()], dtype=dtype)
        elif default is None:
            raise KeyError(f"Required event column '{key}' is missing from the file")
        else:
            columns[key] = np.full(count, default, dtype=dtype)

    for key in EVENT_STRING_FIELDS:
        columns[key] = _decode_text_array(np.asarray(event_group[key][()]))
    return columns


def _rows_from_event_columns(columns: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    count = len(columns["event_id"])
    casts: list[tuple[str, Any]] = [
        (key, int if NUMERIC_FIELD_SPECS[key][0] is np.int64 else float)
        for key in EVENT_NUMERIC_FIELDS
    ]
    casts += [(key, str) for key in EVENT_STRING_FIELDS]
    return [
        {key: cast(columns[key][index]) for key, cast in casts}
        for index in range(count)
    ]


def _concat_event_columns(chunks: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not chunks:
        empty: dict[str, np.ndarray] = {
            key: np.array([], dtype=dtype) for key, (dtype, _) in NUMERIC_FIELD_SPECS.items()
        }
        empty.update({key: np.array([], dtype="U") for key in EVENT_STRING_FIELDS})
        return empty

    merged: dict[str, np.ndarray] = {}
    for key in EVENT_FIELDS:
        arrays = [chunk[key] for chunk in chunks]
        merged[key] = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
    return merged


def write_common_hdf5(output_path: str | Path, metadata: dict[str, Any], events: list[dict[str, Any]]) -> str:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    numeric_columns: dict[str, np.ndarray] = {}
    for key, (dtype, default) in NUMERIC_FIELD_SPECS.items():
        if default is None:
            values = [dtype(event[key]) for event in events]
        else:
            values = [dtype(event.get(key, default)) for event in events]
        numeric_columns[key] = np.array(values, dtype=dtype) if events else np.array([], dtype=dtype)

    string_defaults = {
        "interaction": "unknown",
        "probe": "unknown",
        "target": "unknown",
        "generator": metadata.get("generator", "unknown"),
    }
    string_columns = {
        key: np.array(
            [str(event.get(key, string_defaults[key])) for event in events], dtype=STRING_DTYPE
        )
        for key in EVENT_STRING_FIELDS
    }

    with h5py.File(output, "w") as handle:
        meta_group = handle.create_group("metadata")
        for key, value in metadata.items():
            meta_group.attrs[key] = _serialize_attr(value)

        run_group = handle.create_group("run")
        run_group.attrs["event_count"] = int(len(events))

        event_group = handle.create_group("events")
        for key, values in numeric_columns.items():
            event_group.create_dataset(key, data=values)
        for key, values in string_columns.items():
            event_group.create_dataset(key, data=values, dtype=STRING_DTYPE)

    return str(output)


def read_events(input_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}

    with h5py.File(input_path, "r") as handle:
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value

        columns = _read_event_columns(handle)

    return metadata, _rows_from_event_columns(columns)


def _xsec_norm_count(metadata: dict[str, Any]) -> float | None:
    """Read a chunk's declared normalization denominator, or None if absent."""
    if XSEC_NORM_COUNT_KEY not in metadata:
        return None
    try:
        value = float(metadata[XSEC_NORM_COUNT_KEY])
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


def merge_hdf5_files(
    input_files: Iterable[str | Path],
    output_path: str | Path,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    """Concatenate common-output files, averaging their cross-section weights.

    Every generator's ``xsec_weight`` column is a *per-chunk* estimate of sigma:
    ``sum(xsec_weight in an energy bin) / bin_width`` converges to sigma(E) for
    each chunk on its own. Plain concatenation therefore reports N times the
    cross section for an N-chunk run — the events add up, but the estimates must
    be *averaged*.

    Each chunk declares its statistical size as ``xsec_norm_count`` (the ``D`` in
    ``compute_xsec_weight``'s ``numerator / (D * phi_hat)``; see
    ``ConfigTranslator.xsec_norm_count``). Scaling chunk *c* by ``D_c / sum(D)``
    turns the concatenation into exactly that weighted average — equivalently,
    the merged weights are what a single run of ``sum(D)`` would have produced.
    Recording the summed count on the output keeps the operation associative, so
    merging merged files stays correct.

    Files without the key (stub output, or output from before this was recorded)
    are left untouched: their weights are placeholders, not cross sections. A set
    that mixes the two is refused rather than silently half-rescaled.
    """
    merged_chunks: list[dict[str, np.ndarray]] = []
    normalized_inputs = [str(Path(path)) for path in input_files]
    if not normalized_inputs:
        raise MergeError("No input files provided to merge")

    identity: tuple[str, ...] | None = None
    identity_source: str | None = None
    first_file_metadata: dict[str, Any] = {}
    expected_events_total = 0
    norm_counts: list[float | None] = []

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
        norm_counts.append(_xsec_norm_count(file_metadata))
        merged_chunks.append(columns)

    declared = [count for count in norm_counts if count is not None]
    if declared and len(declared) != len(norm_counts):
        missing = [
            path for path, count in zip(normalized_inputs, norm_counts) if count is None
        ]
        raise MergeError(
            f"Refusing to merge: {len(declared)} of {len(norm_counts)} inputs declare "
            f"'{XSEC_NORM_COUNT_KEY}' and the rest do not, so their xsec_weight "
            "columns cannot be put on a common normalization. Inputs missing it "
            f"(stub output, or generated before it was recorded): {', '.join(missing)}"
        )

    norm_count_total = float(sum(declared)) if declared else None
    if norm_count_total:
        # Rescale each chunk to its share of the combined estimate. With a single
        # input this is a no-op (share == 1), so one-chunk runs are unaffected.
        for columns, count in zip(merged_chunks, norm_counts):
            assert count is not None
            columns["xsec_weight"] = columns["xsec_weight"] * (count / norm_count_total)

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
    # Carry the combined denominator so a merged file can itself be merged.
    if norm_count_total:
        metadata[XSEC_NORM_COUNT_KEY] = norm_count_total

    return write_common_hdf5(output_path, metadata, merged_events)
