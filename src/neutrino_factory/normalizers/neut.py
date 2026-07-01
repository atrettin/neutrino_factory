from __future__ import annotations

import json
from pathlib import Path

from ..common_output import version_metadata, write_common_hdf5
from .base import OutputNormalizer


class NeutNormalizer(OutputNormalizer):
    name = "neut"

    def normalize(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        raw = json.loads(Path(raw_output_path).read_text(encoding="utf-8"))
        metadata = version_metadata(self.name, task, execution_mode)
        metadata["translated_config"] = raw.get("translated_config", {})
        return write_common_hdf5(normalized_output_path, metadata, raw.get("events", []))
