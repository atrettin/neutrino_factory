"""Shared reference scatter used by the per-generator normalizer tests.

Each generator's synthetic tree is filled with the *same* physical event, so the
three normalizer test modules can assert against one set of expected kinematic
values. That the numbers agree is the point: the common format promises the
variables mean the same thing whichever generator produced them.

Reference event, for a beam neutrino of energy E along +z:

    p_nu     = (E,   0,      0,  E)
    p_lepton = (E/2, 0.3 E,  0,  0.4 E)      (massless: |p| = 0.5 E)

which gives, with nu = E - E/2 = E/2:

    Q^2  = |q_vec|^2 - q_E^2 = 0.45 E^2 - 0.25 E^2 = 0.20 E^2
    y    = 0.5
    x    = 0.20 E^2 / (2 M_N * 0.5 E) = 0.2 E / M_N
    p_par = 0.4 E,  p_T = 0.3 E,  cos(theta) = 0.8
"""

from __future__ import annotations

import numpy as np

from neutrino_factory.kinematics import NUCLEON_MASS_GEV


def reference_lepton_p4(energies_gev) -> np.ndarray:
    """Outgoing-lepton four-vectors ``(E, px, py, pz)`` for the reference scatter."""
    energies = np.asarray(energies_gev, dtype=np.float64)
    return np.column_stack([
        0.5 * energies,
        0.3 * energies,
        np.zeros_like(energies),
        0.4 * energies,
    ])


def reference_kinematics(energy_gev: float) -> dict[str, float]:
    """Expected common-format kinematic values for the reference scatter."""
    return {
        "q2_gev2": 0.20 * energy_gev**2,
        "bjorken_x": 0.20 * energy_gev / NUCLEON_MASS_GEV,
        "inelasticity_y": 0.5,
        "lepton_energy_gev": 0.5 * energy_gev,
        "lepton_momentum_gev": 0.5 * energy_gev,
        "lepton_p_parallel_gev": 0.4 * energy_gev,
        "lepton_p_transverse_gev": 0.3 * energy_gev,
        "lepton_costheta": 0.8,
    }
