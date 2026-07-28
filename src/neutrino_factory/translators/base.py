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

    @abstractmethod
    def xsec_norm_count(
        self, translated_config: dict[str, Any], event_count: int
    ) -> float:
        """Return the normalization denominator ``compute_xsec_weight`` divided by.

        Every generator's ``xsec_weight`` column is an estimate of sigma built
        from *one chunk*, so chunks must be **averaged, not summed**, when they
        are merged — concatenating N chunks otherwise reports N times the cross
        section. ``merge_hdf5_files`` performs that averaging by rescaling each
        input's weights by its share of the total, and this method is how a chunk
        declares the size of its own contribution.

        Concretely, ``compute_xsec_weight`` has the shape
        ``numerator_i / (D * phi_hat(E_i))``; return that ``D``. It is the
        per-event sample count for rejection-sampled/unweighted generators
        (GENIE, NuWro, NEUT: the number of events in the chunk) but the number of
        independent generator *runs* for GiBUU, whose per-event weights already
        sum to sigma within one run. Getting it wrong reintroduces the merge bug,
        so it is abstract rather than defaulting to ``event_count``.
        """
        raise NotImplementedError
