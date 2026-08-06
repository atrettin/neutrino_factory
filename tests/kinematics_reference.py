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

The struck nucleon is taken at rest and on shell, ``p_N = (M_N, 0, 0, 0)``. With
that choice ``(p_nu + p_N - p_l)^2`` reduces algebraically to
``M_N^2 + 2 M_N nu - Q^2``, so the two W columns must come out *equal* -- which
is exactly why the reference uses it: it makes the per-generator tests pin the
two independent code paths against each other as well as against a number.
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


def reference_nucleon_p4(energies_gev) -> np.ndarray:
    """Struck-nucleon four-vectors: at rest and on shell, one per event."""
    energies = np.asarray(energies_gev, dtype=np.float64)
    zeros = np.zeros_like(energies)
    return np.column_stack([np.full_like(energies, NUCLEON_MASS_GEV), zeros, zeros, zeros])


def reference_kinematics(energy_gev: float) -> dict[str, float]:
    """Expected common-format kinematic values for the reference scatter."""
    # nu = E/2, Q^2 = 0.20 E^2. Both W columns coincide for a static on-shell
    # nucleon, so one expression serves both.
    w_gev = np.sqrt(
        NUCLEON_MASS_GEV**2 + NUCLEON_MASS_GEV * energy_gev - 0.20 * energy_gev**2
    )
    return {
        "q2_gev2": 0.20 * energy_gev**2,
        "bjorken_x": 0.20 * energy_gev / NUCLEON_MASS_GEV,
        "w_gev": float(w_gev),
        "w_true_gev": float(w_gev),
        "inelasticity_y": 0.5,
        "lepton_energy_gev": 0.5 * energy_gev,
        "lepton_momentum_gev": 0.5 * energy_gev,
        "lepton_p_parallel_gev": 0.4 * energy_gev,
        "lepton_p_transverse_gev": 0.3 * energy_gev,
        "lepton_costheta": 0.8,
    }
