from __future__ import annotations

from pathlib import Path

from .base import OutputNormalizer


class GiBUUNormalizer(OutputNormalizer):
    name = "gibuu"

    def normalize(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        # TODO: read GiBUU's RootTuple ROOT output and write the common HDF5
        # format (see NuWroNormalizer / GenieNormalizer for the uproot pattern).
        # The exact ROOT filename and tree/branch schema still need to be read
        # off a real run before this can be implemented (see STUBS.md).
        raise NotImplementedError(
            "GiBUU ROOT->HDF5 normalization is not implemented yet. The build "
            "produces a valid ROOT event file; translating it into the common "
            "HDF5 format is a pending TODO (see .claude/STUBS.md)."
        )
