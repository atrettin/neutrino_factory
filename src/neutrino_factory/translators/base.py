from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from ..flux import Flux


class ConfigTranslator(ABC):
    name = "base"

    @abstractmethod
    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def compute_xsec_weight(
        self,
        energies_gev: np.ndarray,
        raw_weights: np.ndarray,
        translated_config: dict[str, Any],
        flux: Flux,
    ) -> np.ndarray:
        """Return the physically normalized per-event ``xsec_weight``.

        Units: 1e-38 cm^2 per target nucleon. When events are histogrammed by
        energy and weighted by the returned array, ``sum(weight) / bin_width``
        must converge to the average differential cross section in that bin.
        Every generator's raw output encodes cross-section information
        differently (e.g. rejection-sampled vs. phase-space-weighted), so this
        knowledge is generator-specific and lives on the translator, not in a
        shared default.
        """
        raise NotImplementedError
