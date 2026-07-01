from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class OutputNormalizer(ABC):
    name = "base"

    @abstractmethod
    def normalize(
        self,
        raw_output_path: str | Path,
        normalized_output_path: str | Path,
        task: dict,
        execution_mode: str,
    ) -> str:
        raise NotImplementedError
