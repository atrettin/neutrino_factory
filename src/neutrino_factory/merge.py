from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .common_output import merge_hdf5_files


def merge_outputs(
    input_files: Iterable[str | Path],
    output_file: str | Path,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    return merge_hdf5_files(input_files, output_file, run_metadata=run_metadata)
