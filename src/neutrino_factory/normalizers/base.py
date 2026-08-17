from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..common_output import (
    RESONANT_PRIMARY_NO,
    RESONANT_PRIMARY_UNKNOWN,
    RESONANT_PRIMARY_YES,
)


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


# The channels the resonant/non-resonant distinction is about at all. Elsewhere
# (quasi-elastic, coherent, 2p2h) there is no pion-production mechanism to
# attribute, and ``resonant_primary`` stays at RESONANT_PRIMARY_UNKNOWN.
RESONANCE_AMBIGUOUS_INTERACTIONS = frozenset({"res", "dis"})


def resonant_primary_from_interaction(interaction: str, resonant: bool) -> int:
    """``resonant_primary`` for a generator whose channels *are* its mechanisms.

    GENIE and GiBUU both attribute the primary hadronic system by construction --
    GENIE through which generator produced the event, GiBUU through its
    ``evType`` -- so the column follows directly from the common label. NuWro
    needs its own flag instead, because its ``res`` channel mixes the two.
    """
    if interaction not in RESONANCE_AMBIGUOUS_INTERACTIONS:
        return RESONANT_PRIMARY_UNKNOWN
    return RESONANT_PRIMARY_YES if resonant else RESONANT_PRIMARY_NO


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
