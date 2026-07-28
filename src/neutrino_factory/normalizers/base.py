from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


def interaction_from_flags(qel, res, dis, coh, mec) -> str:
    """Map a set of mutually-exclusive per-mode booleans to the common label.

    GENIE's ``gst`` and NuWro's ``treeout`` both expose the interaction class as
    parallel boolean branches with the same names, so they share this priority
    chain. GiBUU instead uses an integer ``evType`` code.
    """
    if qel:
        return "qel"
    if res:
        return "res"
    if dis:
        return "dis"
    if coh:
        return "coh"
    if mec:
        return "mec"
    return "other"


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
