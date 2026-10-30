from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import h5py
import numpy as np

from . import final_state, kinematics

LOGGER = logging.getLogger(__name__)

# How many paths to name before collapsing the rest into a count. A cluster run
# can skip dozens of chunks; listing them all buries the message.
_MAX_LISTED_PATHS = 3


# Values of the ``resonant_primary`` column: was the primary hadronic system
# produced by the generator's *resonant* term, or by its non-resonant one?
#
# This is deliberately a column of its own rather than a correction to
# ``interaction``. The two answer different questions: ``interaction`` says which
# channel the generator assigned the event, ``resonant_primary`` says how the
# hadronic system was actually made. For GENIE and GiBUU they agree by
# construction, because their channels *are* their mechanisms. For NuWro they do
# not -- its ``res`` channel blends non-resonant background into itself from
# W = 1.08 GeV up, so a large part of what it calls ``res`` is what GENIE would
# have called ``dis``. Keeping that in a separate column lets an analysis re-cut
# on the mechanism without ``interaction`` silently meaning something different
# for one generator.
#
# An integer with an "unknown" state rather than a bool, because "this generator
# does not expose the distinction" (NEUT) and "not applicable to this channel"
# (quasi-elastic, coherent, 2p2h) are real states a bool would have to fake.
RESONANT_PRIMARY_UNKNOWN = -1
RESONANT_PRIMARY_NO = 0
RESONANT_PRIMARY_YES = 1

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
    # Charged current (True) vs neutral current (False). Required, with no
    # default: the `interaction` label merges the two currents by construction
    # (GENIE's `qel` flag and NEUT's modes 1 and 51/52 all map to "qel"), so a
    # silently defaulted value here would be indistinguishable from a real one
    # and would misclassify half the events of an inclusive run.
    "is_cc": (np.bool_, None),
    # See RESONANT_PRIMARY_* above. Defaulting to "unknown" is correct for every
    # generator and channel that does not expose the distinction, which is why
    # this one does have a default where `is_cc` does not.
    "resonant_primary": (np.int8, RESONANT_PRIMARY_UNKNOWN),
    **{name: (np.float64, default) for name, default in kinematics.FIELD_DEFAULTS.items()},
    # Final-state multiplicities, hadronic energy sums, and the generator's own
    # channel code. All default to placeholders: stub output has no particle
    # list, and files written before these columns existed must stay readable.
    **final_state.NUMERIC_FIELD_SPECS,
}

EVENT_NUMERIC_FIELDS = tuple(NUMERIC_FIELD_SPECS)

# On-disk dtypes of the columns stored narrower than their NUMERIC_FIELD_SPECS
# dtype. Readers cast back to the spec dtype, so this only changes file size
# (a quarter of a production run's chunks, with compression). float32 keeps 7
# significant digits, far below any binning resolution. Kept at full width:
# ``energy_gev``, because float32(0.1) != 0.1 and an event at a flux edge must
# not fall outside the simulated range; ``weight`` and ``xsec_weight``, which
# are summed over up to ~1e8 events; ``event_id`` and ``seed``, which compress
# to almost nothing. ``native_interaction_code`` needs int32 for its -2**31
# placeholder. Writing a value the narrow dtype cannot hold raises.
STORAGE_DTYPES: dict[str, type] = {
    **{name: np.float32 for name in kinematics.FIELD_DEFAULTS},
    **{
        name: np.int16 if name in final_state.COUNT_FIELDS else np.float32
        for name in final_state.FIELD_DEFAULTS
    },
    final_state.NATIVE_CODE_FIELD: np.int32,
}

# Compression of every ``events`` dataset. gzip rather than lzf: every HDF5
# tool can read it, and on real chunks it is ~10% smaller at a negligible cost
# (~3 s per 1e6 events to write, 0.4 s to read). Shuffle groups the bytes of
# each value, which is what lets the float columns compress at all.
DATASET_COMPRESSION: dict[str, Any] = {"compression": "gzip", "compression_opts": 4, "shuffle": True}
# Rows per HDF5 chunk: large enough for gzip to work well, small enough that
# reading a slice does not decompress much more than it needs.
CHUNK_ROWS = 1 << 18

# Metadata stamp of files whose missing float values are NaN. Files without it
# predate the switch and carry LEGACY_PLACEHOLDERS instead, which
# ``_read_event_columns`` maps to NaN.
MISSING_FLOAT_KEY = "missing_float"
MISSING_FLOAT_NAN = "nan"
LEGACY_PLACEHOLDERS: dict[str, float] = {
    **kinematics.LEGACY_FIELD_DEFAULTS,
    **final_state.LEGACY_FIELD_DEFAULTS,
}

# The common interaction labels and their fixed on-disk codes. ``interaction`` is
# stored as an HDF5 enum: one byte per event, with the label of each code carried
# in the dataset's own type, so the file stays self-describing. Variable-length
# strings made reading this one column take ~10x longer than all numeric columns
# together. The codes are part of the format -- append new labels, never renumber.
INTERACTION_LABELS = ("qel", "res", "dis", "coh", "mec", "other")
INTERACTION_CODES = {label: code for code, label in enumerate(INTERACTION_LABELS)}
INTERACTION_DTYPE = h5py.enum_dtype(INTERACTION_CODES, basetype="i1")

# Fields that are constant within a file -- one file holds one job, i.e. one
# generator on one initial state -- and are stored once as ``metadata`` attrs
# instead of as per-event columns. Readers still hand them out per event.
FILE_LEVEL_FIELDS = ("probe", "target", "generator")

EVENT_STRING_FIELDS = ("interaction", *FILE_LEVEL_FIELDS)
EVENT_FIELDS = (*EVENT_NUMERIC_FIELDS, *EVENT_STRING_FIELDS)
# The datasets an ``events`` group carries (besides the optional weight matrices).
EVENT_DATASETS = (*EVENT_NUMERIC_FIELDS, "interaction")

# Metadata keys that uniquely identify a generator version. Files that differ on
# any of these describe different physics and must never be merged together.
VERSION_IDENTITY_KEYS = ("generator", "code_version", "config_version")


# Metadata key recording the denominator a chunk's ``xsec_weight`` was divided
# by (see ``ConfigTranslator.xsec_norm_count``). Present on real generator
# output; absent on stub output, whose weights are placeholders.
XSEC_NORM_COUNT_KEY = "xsec_norm_count"


# Optional 2D event datasets of reweight weights, shape (n_events, n_columns):
# sigma_variant / sigma_central per event (see universes.py). Ratios, so merging
# concatenates them and never rescales them. ``universe_weights`` holds the
# Gaussian-sampled universes; ``variation_weights`` one column per switch-type
# knob at its alternate setting, which follows no Gaussian prior and so is kept
# apart. The metadata key paired with each dataset describes its columns.
UNIVERSE_WEIGHTS_KEY = "universe_weights"
UNIVERSES_METADATA_KEY = "universes"
VARIATION_WEIGHTS_KEY = "variation_weights"
VARIATIONS_METADATA_KEY = "variations"
WEIGHT_MATRICES = {
    UNIVERSE_WEIGHTS_KEY: UNIVERSES_METADATA_KEY,
    VARIATION_WEIGHTS_KEY: VARIATIONS_METADATA_KEY,
}


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


def read_interaction_column(dataset: h5py.Dataset) -> np.ndarray:
    """Read an ``interaction`` dataset as an array of ``str`` labels.

    Decodes the enum codes through the file's own mapping rather than
    ``INTERACTION_CODES``, and still reads the variable-length strings of files
    written before the column became an enum.
    """
    mapping = h5py.check_enum_dtype(dataset.dtype)
    if mapping is None:
        return _decode_text_array(np.asarray(dataset[()]))
    labels = np.empty(max(mapping.values()) + 1, dtype=object)
    for label, code in mapping.items():
        labels[code] = label
    return labels[np.asarray(dataset[()])]


def read_file_level_field(handle: h5py.File, key: str) -> str:
    """Read one of ``FILE_LEVEL_FIELDS`` from a file, whichever layout it uses."""
    events = handle["events"]
    if key in events:
        values = set(_decode_text_array(np.asarray(events[key][()])).tolist())
        if len(values) != 1:
            raise ValueError(f"Expected one '{key}' value per file, found {sorted(values)}")
        return values.pop()
    value = handle["metadata"].attrs[key]
    return value.decode() if isinstance(value, bytes) else str(value)


def _read_event_columns(handle: h5py.File) -> dict[str, np.ndarray]:
    """Read the ``events`` group into per-column arrays.

    Columns a file does not carry are synthesized from their default. That keeps
    files written before a column was introduced readable and mergeable; the
    kinematic columns come back as placeholders rather than raising. Every
    column comes back in its ``NUMERIC_FIELD_SPECS`` dtype whatever its on-disk
    dtype, and the -1/-999 placeholders of files from before the NaN switch come
    back as NaN.
    """
    event_group = handle["events"]
    count = int(len(event_group["event_id"]))
    stamp = handle["metadata"].attrs.get(MISSING_FLOAT_KEY) if "metadata" in handle else None
    if isinstance(stamp, bytes):
        stamp = stamp.decode()
    legacy = stamp != MISSING_FLOAT_NAN

    columns: dict[str, np.ndarray] = {}
    for key, (dtype, default) in NUMERIC_FIELD_SPECS.items():
        if key in event_group:
            columns[key] = np.asarray(event_group[key][()], dtype=dtype)
            if legacy and key in LEGACY_PLACEHOLDERS:
                columns[key][columns[key] == LEGACY_PLACEHOLDERS[key]] = np.nan
        elif default is None:
            raise KeyError(f"Required event column '{key}' is missing from the file")
        else:
            columns[key] = np.full(count, default, dtype=dtype)

    columns["interaction"] = read_interaction_column(event_group["interaction"])
    for key in FILE_LEVEL_FIELDS:
        # Files written before these moved to metadata carry them per event.
        if key in event_group:
            columns[key] = _decode_text_array(np.asarray(event_group[key][()]))
        elif count == 0:
            columns[key] = np.array([], dtype=object)
        else:
            columns[key] = np.full(count, read_file_level_field(handle, key), dtype=object)
    for key in WEIGHT_MATRICES:
        if key in event_group:
            columns[key] = np.asarray(event_group[key][()])
    return columns


def _rows_from_event_columns(columns: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    count = len(columns["event_id"])
    numeric_casts: dict[type, Any] = {np.int64: int, np.bool_: bool}
    casts: list[tuple[str, Any]] = [
        (key, numeric_casts.get(NUMERIC_FIELD_SPECS[key][0], float))
        for key in EVENT_NUMERIC_FIELDS
    ]
    casts += [(key, str) for key in EVENT_STRING_FIELDS]
    return [
        {key: cast(columns[key][index]) for key, cast in casts}
        for index in range(count)
    ]


def _dataset_options(n_rows: int, n_columns: int | None = None) -> dict[str, Any]:
    """Chunking and compression keywords for an events dataset of ``n_rows`` rows.

    HDF5 cannot chunk a zero-length dimension, so empty datasets are stored
    contiguous and uncompressed: there is nothing to compress.
    """
    if n_rows == 0 or n_columns == 0:
        return {}
    if n_columns is None:
        return {**DATASET_COMPRESSION, "chunks": (min(n_rows, CHUNK_ROWS),)}
    rows = max(1, min(n_rows, CHUNK_ROWS // n_columns))
    return {**DATASET_COMPRESSION, "chunks": (rows, n_columns)}


def _storage_array(key: str, values: np.ndarray) -> np.ndarray:
    """``values`` in the column's on-disk dtype; raises rather than wrap or overflow."""
    storage = STORAGE_DTYPES.get(key)
    if storage is None or values.size == 0:
        return values
    if np.issubdtype(storage, np.integer):
        info = np.iinfo(storage)
        low, high = values.min(), values.max()
        if low < info.min or high > info.max:
            raise ValueError(
                f"Column '{key}' spans [{low}, {high}], outside the range of its "
                f"on-disk dtype {np.dtype(storage).name}; widen STORAGE_DTYPES."
            )
        return values.astype(storage)
    with np.errstate(over="ignore"):  # detected and reported just below
        narrow = values.astype(storage)
    overflow = np.isfinite(values) & ~np.isfinite(narrow)
    if overflow.any():
        raise ValueError(
            f"Column '{key}' holds {values[overflow][0]}, which overflows its on-disk "
            f"dtype {np.dtype(storage).name}; widen STORAGE_DTYPES."
        )
    return narrow


def _write_header(
    handle: h5py.File, metadata: dict[str, Any], event_count: int
) -> None:
    """Write the ``metadata`` attrs (with the NaN stamp) and ``run/event_count``."""
    meta_group = handle.create_group("metadata")
    for key, value in {**metadata, MISSING_FLOAT_KEY: MISSING_FLOAT_NAN}.items():
        meta_group.attrs[key] = _serialize_attr(value)
    run_group = handle.create_group("run")
    run_group.attrs["event_count"] = int(event_count)


def _create_event_datasets(
    handle: h5py.File, event_count: int, matrix_columns: dict[str, int]
) -> h5py.Group:
    """Create the full-length, compressed ``events`` datasets, to be filled in slices."""
    group = handle.create_group("events")
    options = _dataset_options(event_count)
    for key in EVENT_NUMERIC_FIELDS:
        dtype = STORAGE_DTYPES.get(key, NUMERIC_FIELD_SPECS[key][0])
        group.create_dataset(key, shape=(event_count,), dtype=dtype, **options)
    group.create_dataset("interaction", shape=(event_count,), dtype=INTERACTION_DTYPE, **options)
    for key, n_columns in matrix_columns.items():
        group.create_dataset(
            key,
            shape=(event_count, n_columns),
            dtype=np.float32,
            **_dataset_options(event_count, n_columns),
        )
    return group


def _write_event_slice(
    group: h5py.Group,
    start: int,
    numeric_columns: dict[str, np.ndarray],
    interaction_codes: np.ndarray,
    matrices: dict[str, np.ndarray],
) -> int:
    """Write one block of events at row ``start``; returns the row after it."""
    stop = start + len(interaction_codes)
    if stop == start:
        return stop
    for key in EVENT_NUMERIC_FIELDS:
        group[key][start:stop] = _storage_array(key, numeric_columns[key])
    group["interaction"][start:stop] = interaction_codes
    for key, matrix in matrices.items():
        group[key][start:stop] = np.asarray(matrix, dtype=np.float32)
    return stop


def _encode_interaction_labels(labels: np.ndarray) -> np.ndarray:
    """Vectorized ``_encode_interactions`` for an array of labels."""
    unique, inverse = np.unique(np.asarray(labels, dtype=str), return_inverse=True)
    unknown = [label for label in unique.tolist() if label not in INTERACTION_CODES]
    if unknown:
        raise ValueError(
            f"Unknown interaction label(s) {unknown}; expected one of "
            f"{', '.join(INTERACTION_LABELS)}"
        )
    codes = np.array([INTERACTION_CODES[label] for label in unique.tolist()], dtype=np.int8)
    return codes[inverse.reshape(-1)] if len(unique) else np.array([], dtype=np.int8)


def _encode_interactions(events: list[dict[str, Any]]) -> np.ndarray:
    """Map each event's ``interaction`` label to its enum code.

    Required, and restricted to ``INTERACTION_LABELS``: the enum has no slot for
    anything else, and a defaulted label would be indistinguishable from a real one.
    """
    codes = np.empty(len(events), dtype=np.int8)
    for index, event in enumerate(events):
        if "interaction" not in event:
            raise KeyError(f"Event {index} is missing the required column 'interaction'")
        label = str(event["interaction"])
        if label not in INTERACTION_CODES:
            raise ValueError(
                f"Event {index} has interaction '{label}'; expected one of "
                f"{', '.join(INTERACTION_LABELS)}"
            )
        codes[index] = INTERACTION_CODES[label]
    return codes


def _file_level_values(metadata: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, str]:
    """Collapse the per-event ``FILE_LEVEL_FIELDS`` into one value each.

    Every event must carry them and agree, and so must ``metadata`` where it
    already names one: a file mixing initial states (e.g. a merge of unlike jobs)
    has no single value to record. Without events, whatever ``metadata`` says is kept.
    """
    values: dict[str, str] = {}
    for key in FILE_LEVEL_FIELDS:
        missing = next((index for index, e in enumerate(events) if key not in e), None)
        if missing is not None:
            raise KeyError(f"Event {missing} is missing the required column '{key}'")
        seen = {str(event[key]) for event in events}
        if key in metadata:
            seen.add(str(metadata[key]))
        if len(seen) > 1:
            raise ValueError(
                f"Events and metadata disagree on '{key}': {sorted(seen)}. A common-output "
                "file holds one generator on one initial state."
            )
        if seen:
            values[key] = seen.pop()
    return values


def write_common_hdf5(
    output_path: str | Path,
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
    universe_weights: np.ndarray | None = None,
    variation_weights: np.ndarray | None = None,
) -> str:
    matrices = {UNIVERSE_WEIGHTS_KEY: universe_weights, VARIATION_WEIGHTS_KEY: variation_weights}
    for key, matrix in matrices.items():
        if matrix is not None and (matrix.ndim != 2 or len(matrix) != len(events)):
            raise ValueError(
                f"{key} has shape {matrix.shape}; expected ({len(events)}, n_columns), "
                "one row per event"
            )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    numeric_columns: dict[str, np.ndarray] = {}
    for key, (dtype, default) in NUMERIC_FIELD_SPECS.items():
        if default is None:
            missing = next((index for index, e in enumerate(events) if key not in e), None)
            if missing is not None:
                raise KeyError(
                    f"Event {missing} is missing the required column '{key}'. It has "
                    "no default: writing a placeholder would put an unphysical value "
                    "into the output where a real one is expected."
                )
            values = [dtype(event[key]) for event in events]
        else:
            values = [dtype(event.get(key, default)) for event in events]
        numeric_columns[key] = np.array(values, dtype=dtype) if events else np.array([], dtype=dtype)

    interaction_codes = _encode_interactions(events)
    metadata = {**metadata, **_file_level_values(metadata, events)}

    present = {key: matrix for key, matrix in matrices.items() if matrix is not None}
    with h5py.File(output, "w") as handle:
        _write_header(handle, metadata, len(events))
        event_group = _create_event_datasets(
            handle, len(events), {key: matrix.shape[1] for key, matrix in present.items()}
        )
        _write_event_slice(event_group, 0, numeric_columns, interaction_codes, present)

    return str(output)


def read_events(input_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}

    with h5py.File(input_path, "r") as handle:
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value

        columns = _read_event_columns(handle)

    return metadata, _rows_from_event_columns(columns)


def _summarize_paths(paths: list[str]) -> str:
    """Name the first few paths, then say how many more there are."""
    if len(paths) <= _MAX_LISTED_PATHS:
        return ", ".join(paths)
    listed = ", ".join(paths[:_MAX_LISTED_PATHS])
    return f"{listed}, and {len(paths) - _MAX_LISTED_PATHS} more"


def _xsec_norm_count(metadata: dict[str, Any]) -> float | None:
    """Read a chunk's declared normalization denominator, or None if absent."""
    if XSEC_NORM_COUNT_KEY not in metadata:
        return None
    try:
        value = float(metadata[XSEC_NORM_COUNT_KEY])
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


# Relative tolerance on the parameter ``values`` of a weight matrix's metadata.
# ``exp`` of a log-mode throw may differ in the last bit between CPUs (NumPy
# picks a SIMD kernel per machine), so chunks of one draw run on different
# nodes can disagree by ~1 ulp there. Everything else, ``z`` included, must
# still match exactly: that is what defines the draw.
DRAW_VALUES_RTOL = 1e-12


def _same_draw(a: Any, b: Any) -> bool:
    """Whether two weight-matrix metadata blocks (JSON attrs) describe the same draw."""
    if a == b:
        return True
    try:
        a, b = json.loads(a), json.loads(b)
    except (TypeError, ValueError):
        return False
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return a == b
    if a.keys() != b.keys() or any(a[k] != b[k] for k in a if k != "values"):
        return False
    values_a, values_b = a.get("values"), b.get("values")
    if not (isinstance(values_a, dict) and isinstance(values_b, dict)):
        return values_a == values_b
    if values_a.keys() != values_b.keys():
        return False
    for name in values_a:
        x, y = np.asarray(values_a[name], dtype=float), np.asarray(values_b[name], dtype=float)
        if x.shape != y.shape or not np.allclose(x, y, rtol=DRAW_VALUES_RTOL, atol=0.0):
            return False
    return True


class _MergeInput(NamedTuple):
    """What the first, metadata-only pass of a merge learns about one input."""

    path: str
    metadata: dict[str, Any]
    event_count: int
    # Column count of each weight matrix the input carries.
    matrix_columns: dict[str, int]
    # The input's FILE_LEVEL_FIELDS values; absent for an empty legacy file
    # that carries them nowhere.
    file_level: dict[str, str]
    norm_count: float | None


def _inspect_merge_input(path: str) -> _MergeInput:
    """Read everything a merge needs from one input except its event columns.

    Raises ``OSError``/``KeyError`` for a file that cannot be read or lacks a
    required column, exactly as a full read would, so the merge can skip it
    before writing anything.
    """
    with h5py.File(path, "r") as handle:
        metadata: dict[str, Any] = {}
        for key, value in handle["metadata"].attrs.items():
            metadata[key] = value.decode() if isinstance(value, bytes) else value
        events = handle["events"]
        for key, (_, default) in NUMERIC_FIELD_SPECS.items():
            if default is None and key not in events:
                raise KeyError(f"Required event column '{key}' is missing from the file")
        if "interaction" not in events:
            raise KeyError("Required event column 'interaction' is missing from the file")
        count = int(len(events["event_id"]))
        file_level: dict[str, str] = {}
        for key in FILE_LEVEL_FIELDS:
            try:
                file_level[key] = read_file_level_field(handle, key)
            except KeyError:
                if count:
                    raise
        matrix_columns = {
            key: int(events[key].shape[1]) for key in WEIGHT_MATRICES if key in events
        }
    return _MergeInput(path, metadata, count, matrix_columns, file_level, _xsec_norm_count(metadata))


def _merge_weight_matrix(inputs: list[_MergeInput], key: str) -> int | None:
    """Column count of the merged ``key`` weight matrix, or None if no input carries it.

    Column ``k`` must mean the same parameter values in every input, so the
    inputs must agree on the paired metadata exactly; and either all of them
    carry the matrix or none does, since a merged column with holes could not be
    told apart from a real one.
    """
    meta_key = WEIGHT_MATRICES[key]
    carrying = [entry.path for entry in inputs if key in entry.matrix_columns]
    if not carrying:
        return None
    if len(carrying) != len(inputs):
        missing = [entry.path for entry in inputs if key not in entry.matrix_columns]
        raise MergeError(
            f"Only {len(carrying)} of {len(inputs)} inputs carry {key}; "
            f"missing from {_summarize_paths(missing)}"
        )
    reference = inputs[0]
    for entry in inputs[1:]:
        if not _same_draw(entry.metadata.get(meta_key), reference.metadata.get(meta_key)):
            raise MergeError(
                f"Refusing to merge {key} drawn differently: the '{meta_key}' "
                f"metadata of {entry.path} differs from {reference.path}"
            )
        if entry.matrix_columns[key] != reference.matrix_columns[key]:
            raise MergeError(
                f"{key} of {entry.path} has {entry.matrix_columns[key]} columns, "
                f"{reference.path} has {reference.matrix_columns[key]}"
            )
    return reference.matrix_columns[key]


def _merged_file_level_values(
    metadata: dict[str, Any], inputs: list[_MergeInput]
) -> dict[str, str]:
    """The merged file's ``FILE_LEVEL_FIELDS``; every input and ``metadata`` must agree.

    A file mixing initial states (a merge of unlike jobs) has no single value
    to record.
    """
    values: dict[str, str] = {}
    for key in FILE_LEVEL_FIELDS:
        seen = {entry.file_level[key] for entry in inputs if key in entry.file_level}
        if key in metadata:
            seen.add(str(metadata[key]))
        if len(seen) > 1:
            raise ValueError(
                f"Events and metadata disagree on '{key}': {sorted(seen)}. A common-output "
                "file holds one generator on one initial state."
            )
        if seen:
            values[key] = seen.pop()
    return values


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
    are left untouched: their weights are placeholders, not cross sections. If
    only *some* inputs declare it, the rest are skipped with a warning rather
    than merged on an incompatible normalization — a large cluster run routinely
    has a few chunks that are unfinished, stale or dead, and losing the other 98
    to them is worse than proceeding without them.

    A generator-version mismatch stays fatal: that is a physics error, not a
    partial run.

    The merge streams: a first pass reads only each input's metadata and shapes,
    a second copies one input at a time into the output, so memory stays at
    about one chunk however many events the merged file holds. The output is
    written under a temporary name and renamed into place only when complete; an
    input that passed the first pass but fails to read in the second is an
    error, not a skip, since half of it may already have been written.
    """
    normalized_inputs = [str(Path(path)) for path in input_files]
    if not normalized_inputs:
        raise MergeError("No input files provided to merge")

    identity: tuple[str, ...] | None = None
    identity_source: str | None = None
    inputs: list[_MergeInput] = []
    unreadable: list[tuple[str, str]] = []

    for input_file in normalized_inputs:
        # A chunk still being written, or left truncated by a killed job, must
        # not take the whole merge down with it.
        try:
            entry = _inspect_merge_input(input_file)
        except (OSError, KeyError) as exc:
            unreadable.append((input_file, f"{type(exc).__name__}: {exc}"))
            continue

        current = _version_identity(entry.metadata)
        if identity is None:
            identity = current
            identity_source = input_file
        elif current != identity:
            raise MergeError(
                "Refusing to merge HDF5 files from different generator versions: "
                f"{identity_source} is {dict(zip(VERSION_IDENTITY_KEYS, identity))} "
                f"but {input_file} is {dict(zip(VERSION_IDENTITY_KEYS, current))}"
            )
        inputs.append(entry)

    if unreadable:
        LOGGER.warning(
            "Skipping %d of %d input(s) that could not be read (still being "
            "written, or left incomplete by a failed job): %s",
            len(unreadable),
            len(normalized_inputs),
            _summarize_paths([path for path, _ in unreadable]),
        )

    declared_count = sum(1 for entry in inputs if entry.norm_count is not None)
    skipped_undeclared: list[str] = []
    if declared_count and declared_count != len(inputs):
        # Their xsec_weight cannot be put on a common normalization with the
        # rest, so they are dropped rather than silently biasing the result.
        skipped_undeclared = [entry.path for entry in inputs if entry.norm_count is None]
        LOGGER.warning(
            "Skipping %d of %d readable input(s) that do not declare '%s' "
            "(stub output, or written before it was recorded); merging the "
            "remaining %d: %s",
            len(skipped_undeclared),
            len(inputs),
            XSEC_NORM_COUNT_KEY,
            declared_count,
            _summarize_paths(skipped_undeclared),
        )
        inputs = [entry for entry in inputs if entry.norm_count is not None]

    if not inputs:
        raise MergeError(
            f"No usable input files out of {len(normalized_inputs)}: "
            f"{len(unreadable)} could not be read and "
            f"{len(skipped_undeclared)} lacked '{XSEC_NORM_COUNT_KEY}'."
        )

    first_file_metadata = inputs[0].metadata
    expected_events_total = sum(
        int(entry.metadata.get("expected_events", 0)) for entry in inputs
    )
    merged_inputs = [entry.path for entry in inputs]
    event_total = sum(entry.event_count for entry in inputs)

    declared = [entry.norm_count for entry in inputs if entry.norm_count is not None]
    norm_count_total = float(sum(declared)) if declared else None

    matrix_columns = {
        key: n_columns
        for key in WEIGHT_MATRICES
        if (n_columns := _merge_weight_matrix(inputs, key)) is not None
    }

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

    metadata.setdefault("merged_inputs", merged_inputs)
    metadata["merged_file_count"] = len(merged_inputs)
    metadata["merged_event_count"] = event_total
    # Make a partial merge auditable from the file itself, so a cross section
    # computed from it can be traced back to how many chunks it actually covers.
    if len(merged_inputs) != len(normalized_inputs):
        metadata["requested_file_count"] = len(normalized_inputs)
        metadata["skipped_inputs"] = [path for path, _ in unreadable] + skipped_undeclared
    # Carry the combined denominator so a merged file can itself be merged.
    if norm_count_total:
        metadata[XSEC_NORM_COUNT_KEY] = norm_count_total
    for key in matrix_columns:
        metadata[WEIGHT_MATRICES[key]] = first_file_metadata[WEIGHT_MATRICES[key]]
    metadata = {**metadata, **_merged_file_level_values(metadata, inputs)}

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    try:
        with h5py.File(partial, "w") as handle:
            _write_header(handle, metadata, event_total)
            group = _create_event_datasets(handle, event_total, matrix_columns)
            row = 0
            for entry in inputs:
                with h5py.File(entry.path, "r") as source:
                    columns = _read_event_columns(source)
                if len(columns["event_id"]) != entry.event_count:
                    raise MergeError(
                        f"{entry.path} holds {len(columns['event_id'])} events now, "
                        f"{entry.event_count} when the merge started"
                    )
                if norm_count_total:
                    # Rescale each chunk to its share of the combined estimate.
                    # With a single input this is a no-op (share == 1), so
                    # one-chunk runs are unaffected.
                    assert entry.norm_count is not None
                    columns["xsec_weight"] = columns["xsec_weight"] * (
                        entry.norm_count / norm_count_total
                    )
                row = _write_event_slice(
                    group,
                    row,
                    columns,
                    _encode_interaction_labels(columns["interaction"]),
                    {key: columns[key] for key in matrix_columns},
                )
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return str(output)
