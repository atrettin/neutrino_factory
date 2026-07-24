"""Validate HDF5 files produced by the generators against the common output format.

The diagnostic functions (``check_metadata``, ``check_columns``,
``check_event_count``, ``validate_file``, ``summarize``) never print — they return
plain dictionaries so other scripts can consume them programmatically. The
``format_*`` helpers and the ``__main__`` block turn those dicts into console
output for ad-hoc use:

    python -m neutrino_factory.validate_output file1.h5 file2.h5 ...
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from . import catalog
from .common_output import VERSION_IDENTITY_KEYS
from .slurm import build_task_manifest

# Metadata attrs whose absence makes a file invalid: the version identity (what a
# merge keys on), the simulated ``flux`` and the requested ``expected_events``
# (both needed to make a run's output self-describing for plotting).
REQUIRED_METADATA_KEYS = (
    *VERSION_IDENTITY_KEYS,
    "generator_version_id",
    "flux",
    "expected_events",
)

# Metadata attrs we expect but only warn about when missing.
OPTIONAL_METADATA_KEYS = ("execution_mode", "run_name", "chunk_id", "seed")

# Datasets every ``events`` group must carry (see common_output.write_common_hdf5).
REQUIRED_COLUMNS = (
    "event_id",
    "seed",
    "energy_gev",
    "weight",
    "xsec_weight",
    "interaction",
    "probe",
    "target",
    "generator",
)

DEFAULT_TOLERANCE = 0.05


def _coerce_attr(value: Any) -> Any:
    """Convert an HDF5 attribute value to a native, JSON-serializable Python type."""
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, np.generic):
        return value.item()
    return value


def check_metadata(handle: h5py.File) -> dict[str, Any]:
    """Report presence of required/optional metadata attrs on an open HDF5 file."""
    attrs: dict[str, Any] = {}
    if "metadata" in handle:
        for key, value in handle["metadata"].attrs.items():
            attrs[key] = _coerce_attr(value)

    missing_required = [key for key in REQUIRED_METADATA_KEYS if key not in attrs]
    missing_optional = [key for key in OPTIONAL_METADATA_KEYS if key not in attrs]
    return {
        "present": sorted(attrs.keys()),
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "values": attrs,
        "ok": not missing_required,
    }


def check_columns(handle: h5py.File) -> dict[str, Any]:
    """Report presence of the required event datasets and whether they share a length."""
    lengths: dict[str, int] = {}
    present: list[str] = []
    missing: list[str] = []

    events = handle["events"] if "events" in handle else None
    for name in REQUIRED_COLUMNS:
        if events is not None and name in events:
            present.append(name)
            lengths[name] = int(len(events[name]))
        else:
            missing.append(name)

    consistent_length = len(set(lengths.values())) <= 1
    return {
        "present": present,
        "missing": missing,
        "lengths": lengths,
        "consistent_length": consistent_length,
        "ok": not missing and consistent_length,
    }


def check_event_count(
    handle: h5py.File,
    expected_events: int | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Compare the stored event_count against the actual dataset length (and, if given,
    against an expected total within a relative ``tolerance``)."""
    stored = None
    if "run" in handle and "event_count" in handle["run"].attrs:
        stored = int(handle["run"].attrs["event_count"])

    actual = None
    if "events" in handle and "event_id" in handle["events"]:
        actual = int(len(handle["events"]["event_id"]))

    self_consistent = stored is not None and actual is not None and stored == actual

    within_tolerance: bool | None = None
    if expected_events is not None and actual is not None:
        allowed = abs(expected_events) * tolerance
        within_tolerance = abs(actual - expected_events) <= allowed

    ok = self_consistent and (within_tolerance is not False)
    return {
        "stored": stored,
        "actual": actual,
        "self_consistent": self_consistent,
        "expected": expected_events,
        "tolerance": tolerance,
        "within_tolerance": within_tolerance,
        "ok": ok,
    }


def validate_file(
    path: str | Path,
    expected_events: int | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Validate a single HDF5 file. Returns a diagnostics dict; never raises for a
    missing or corrupt file (those are reported as ``exists``/``openable`` = False)."""
    path = Path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "openable": False,
        "metadata": None,
        "columns": None,
        "event_count": None,
        "valid": False,
        "errors": [],
        "warnings": [],
    }

    if not result["exists"]:
        result["errors"].append("File does not exist")
        return result

    try:
        with h5py.File(path, "r") as handle:
            result["openable"] = True
            metadata = check_metadata(handle)
            columns = check_columns(handle)
            event_count = check_event_count(handle, expected_events, tolerance)
    except OSError as error:
        result["errors"].append(f"Could not open HDF5 file: {error}")
        return result

    result["metadata"] = metadata
    result["columns"] = columns
    result["event_count"] = event_count

    if metadata["missing_required"]:
        result["errors"].append(
            "Missing required metadata: " + ", ".join(metadata["missing_required"])
        )
    if metadata["missing_optional"]:
        result["warnings"].append(
            "Missing optional metadata: " + ", ".join(metadata["missing_optional"])
        )

    if columns["missing"]:
        result["errors"].append(
            "Missing event columns: " + ", ".join(columns["missing"])
        )
    if not columns["consistent_length"]:
        result["errors"].append(
            f"Event columns have inconsistent lengths: {columns['lengths']}"
        )

    if not event_count["self_consistent"]:
        result["errors"].append(
            f"Stored event_count ({event_count['stored']}) does not match "
            f"actual number of events ({event_count['actual']})"
        )
    if event_count["within_tolerance"] is False:
        result["errors"].append(
            f"Event count {event_count['actual']} is outside {tolerance:.0%} "
            f"tolerance of expected {event_count['expected']}"
        )

    result["valid"] = not result["errors"]
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up a list of ``validate_file`` results into summary statistics."""
    total = len(results)
    existing = sum(1 for r in results if r["exists"])
    valid = sum(1 for r in results if r["valid"])
    with_errors = sum(1 for r in results if r["errors"])

    def fraction(count: int) -> float:
        return count / total if total else 0.0

    return {
        "total": total,
        "existing": existing,
        "valid": valid,
        "with_errors": with_errors,
        "fraction_existing": fraction(existing),
        "fraction_valid": fraction(valid),
        "fraction_with_errors": fraction(with_errors),
    }


def format_report(result: dict[str, Any]) -> str:
    """Render a single ``validate_file`` result as a human-readable block."""
    status = "OK" if result["valid"] else "FAIL"
    lines = [f"[{status}] {result['path']}"]

    if not result["exists"]:
        lines.append("  - missing file")
        return "\n".join(lines)
    if not result["openable"]:
        for error in result["errors"]:
            lines.append(f"  - {error}")
        return "\n".join(lines)

    event_count = result["event_count"]
    count_note = f"events: {event_count['actual']}"
    if event_count["expected"] is not None:
        count_note += f" (expected ~{event_count['expected']})"
    lines.append(f"  {count_note}")

    metadata = result["metadata"]
    version_id = metadata["values"].get("generator_version_id", "?")
    lines.append(f"  version: {version_id}")

    for error in result["errors"]:
        lines.append(f"  ERROR: {error}")
    for warning in result["warnings"]:
        lines.append(f"  warn: {warning}")

    return "\n".join(lines)


def format_summary(summary: dict[str, Any]) -> str:
    """Render summary statistics from ``summarize`` as a human-readable block."""
    return (
        "Summary:\n"
        f"  files: {summary['total']}\n"
        f"  existing: {summary['existing']}/{summary['total']} "
        f"({summary['fraction_existing']:.0%})\n"
        f"  valid: {summary['valid']}/{summary['total']} "
        f"({summary['fraction_valid']:.0%})\n"
        f"  with errors: {summary['with_errors']}/{summary['total']} "
        f"({summary['fraction_with_errors']:.0%})"
    )


def expected_outputs(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Compute the HDF5 files a run should produce, mirroring the paths that
    ``local.run_task`` / ``local.run_local`` write (kept in lockstep with them).

    Returns ``{"chunks": [...], "merged": [...]}`` where each entry carries the
    ``path``, ``expected_events``, ``generator`` and ``version_id`` (chunk entries
    also carry ``chunk_id``)."""
    manifest = build_task_manifest(config)
    output_root = Path(config["storage"]["output_root"])
    run_name = config["run"]["name"]

    chunks: list[dict[str, Any]] = []
    merged_groups: dict[tuple[str, str, str], dict[str, Any]] = {}

    for task in manifest["tasks"]:
        generator = task["generator_name"]
        version_id = catalog.version_identifier(
            str(task["code_version"]), str(task["config_version"])
        )
        token = re.sub(r"[^A-Za-z0-9._-]", "_", version_id)
        file_stem = f"{run_name}_{generator}_{token}_chunk{task['chunk_id']:03d}"
        chunk_path = output_root / "chunks" / generator / token / f"{file_stem}.h5"
        chunks.append(
            {
                "path": chunk_path,
                "expected_events": int(task["event_count"]),
                "generator": generator,
                "version_id": version_id,
                "chunk_id": int(task["chunk_id"]),
            }
        )

        key = (generator, task["code_version"], task["config_version"])
        group = merged_groups.setdefault(
            key,
            {
                "path": output_root / "merged" / f"{run_name}_{generator}_{token}.h5",
                "expected_events": 0,
                "generator": generator,
                "version_id": version_id,
            },
        )
        group["expected_events"] += int(task["event_count"])

    return {"chunks": chunks, "merged": list(merged_groups.values())}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neutrino_factory.validate_output",
        description="Validate generator HDF5 output files against the common format.",
    )
    parser.add_argument("files", nargs="+", help="HDF5 file(s) to validate")
    parser.add_argument(
        "--expected-events",
        type=int,
        default=None,
        help="Expected number of events per file (compared within --tolerance)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"Relative tolerance for the event-count check (default: {DEFAULT_TOLERANCE})",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    results = [
        validate_file(path, args.expected_events, args.tolerance) for path in args.files
    ]
    for result in results:
        print(format_report(result))

    if len(results) > 1:
        print()
        print(format_summary(summarize(results)))

    return 0 if all(r["valid"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
