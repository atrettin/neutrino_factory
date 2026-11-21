from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .common_output import merge_hdf5_files


def merge_outputs(
    input_files: Iterable[str | Path],
    output_file: str | Path,
    run_metadata: dict[str, Any] | None = None,
    unweight_events: int | None = None,
    unweight_energy_power: float = -2.0,
) -> str:
    return merge_hdf5_files(
        input_files,
        output_file,
        run_metadata=run_metadata,
        unweight_events=unweight_events,
        unweight_energy_power=unweight_energy_power,
    )


def merged_output_is_current(
    output_file: str | Path,
    input_files: Iterable[str | Path],
    unweight_events: int | None = None,
    unweight_energy_power: float = -2.0,
) -> bool:
    """Whether ``output_file`` is a finished merge of exactly ``input_files``.

    ``merge_hdf5_files`` renames its output into place only when complete, so an
    existing file is never half-written. It is current when the inputs it
    accounts for (merged, plus the ones it skipped as unreadable) are the given
    ones and none of them was modified after it was written, which makes an
    interrupted ``merge --config`` resumable and a re-merge after retries
    replace only the targets whose chunks changed. A file unweighted to a
    different event count or energy power (or weighted when unweighting is
    asked, or vice versa) is not current either.
    """
    import h5py

    output = Path(output_file)
    inputs = [Path(path) for path in input_files]
    try:
        with h5py.File(output, "r") as handle:
            attrs = handle["metadata"].attrs
            merged = json.loads(attrs["merged_inputs"])
            skipped = json.loads(attrs["skipped_inputs"]) if "skipped_inputs" in attrs else []
            unweighted = int(attrs["unweight_events"]) if "unweight_events" in attrs else None
            power = float(attrs.get("unweight_energy_power", 0.0))
        if unweighted != unweight_events:
            return False
        if unweight_events is not None and power != unweight_energy_power:
            return False
        written = output.stat().st_mtime
        return {str(p) for p in inputs} == {*merged, *skipped} and all(
            p.stat().st_mtime <= written for p in inputs
        )
    except (OSError, KeyError, ValueError):
        return False
