"""Derived kinematic variables for the common output format.

Every generator writes the incoming-neutrino and outgoing-lepton four-vectors in
its own native tree; this module turns those into the harmonized kinematic
columns. Deriving them here rather than reading each generator's own precomputed
branches (GENIE's ``gst`` carries ``Q2``/``x``/``y``/``cthl``, for instance) is
what makes a Q^2 histogram from GENIE mean exactly the same thing as one from
GiBUU or NuWro.

Conventions:

* All quantities are in the **lab frame**, in GeV / GeV^2 (dimensionless for
  ``bjorken_x``, ``inelasticity_y`` and ``lepton_costheta``).
* The beam axis is taken **per event** from the incoming neutrino three-momentum,
  never assumed to be +z, so ``lepton_p_parallel_gev`` / ``lepton_p_transverse_gev``
  / ``lepton_costheta`` stay correct for off-axis or divergent beams.
* Bjorken-x uses a **fixed** nucleon mass (see ``NUCLEON_MASS_GEV``) rather than
  the per-event struck-nucleon mass, so the variable is defined identically for
  all generators regardless of whether they expose the hit nucleon.

Missing/undefined values use one of two placeholders, because ``-1`` is a
physical value for the two signed quantities:

* ``MISSING`` (-1.0) for the non-negative-definite columns.
* ``MISSING_SIGNED`` (-999.0) for ``lepton_p_parallel_gev`` and ``lepton_costheta``.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np


# Isoscalar nucleon mass, (m_p + m_n) / 2, from the PDG values
# 0.93827208816 and 0.93956542052 GeV.
NUCLEON_MASS_GEV = 0.9389187543

# Placeholder for quantities that cannot take a negative physical value.
MISSING = -1.0
# Placeholder for quantities whose physical range includes -1.
MISSING_SIGNED = -999.0

# The kinematic columns of the common output format, with the placeholder each
# one falls back to. ``common_output`` builds its column table from this.
FIELD_DEFAULTS: dict[str, float] = {
    "q2_gev2": MISSING,
    "bjorken_x": MISSING,
    "inelasticity_y": MISSING,
    "lepton_energy_gev": MISSING,
    "lepton_momentum_gev": MISSING,
    "lepton_p_parallel_gev": MISSING_SIGNED,
    "lepton_p_transverse_gev": MISSING,
    "lepton_costheta": MISSING_SIGNED,
}

KINEMATIC_FIELDS = tuple(FIELD_DEFAULTS)

# Interaction labels for which Bjorken-x is not meaningful. Coherent scattering
# is off the nucleus as a whole, so there is no struck nucleon to define x
# against; every other channel does scatter off a nucleon (x ~ 1 for quasi-elastic
# is a useful and commonly plotted sanity check, so it is not suppressed).
NO_BJORKEN_X_INTERACTIONS = frozenset({"coh"})


def missing_kinematics(count: int) -> dict[str, np.ndarray]:
    """Return a full block of ``count`` all-placeholder kinematic columns."""
    return {
        name: np.full(int(count), default, dtype=np.float64)
        for name, default in FIELD_DEFAULTS.items()
    }


def derive_kinematics(
    nu_p4: Any,
    lepton_p4: Any,
    interactions: Sequence[str] | np.ndarray | None = None,
    valid: Any = None,
) -> dict[str, np.ndarray]:
    """Derive the kinematic columns from neutrino and outgoing-lepton four-vectors.

    ``nu_p4`` and ``lepton_p4`` are ``(n, 4)`` arrays of ``(E, px, py, pz)`` in GeV.
    ``interactions`` is the per-event common-format interaction label (only used to
    blank Bjorken-x for coherent events). ``valid`` is an optional boolean mask
    marking events for which the generator actually supplied an outgoing lepton;
    events outside it get placeholders throughout.

    Returns a dict keyed by ``KINEMATIC_FIELDS``, each a ``float64`` array of
    length ``n``.
    """
    nu = np.asarray(nu_p4, dtype=np.float64).reshape(-1, 4)
    lepton = np.asarray(lepton_p4, dtype=np.float64).reshape(-1, 4)
    if nu.shape != lepton.shape:
        raise ValueError(
            f"Neutrino and lepton four-vectors have different shapes: {nu.shape} vs {lepton.shape}"
        )

    count = nu.shape[0]
    columns = missing_kinematics(count)
    if count == 0:
        return columns

    usable = np.isfinite(nu).all(axis=1) & np.isfinite(lepton).all(axis=1)
    if valid is not None:
        usable &= np.asarray(valid, dtype=bool).reshape(-1)
    index = np.flatnonzero(usable)
    if index.size == 0:
        return columns

    e_nu = nu[index, 0]
    p_nu = nu[index, 1:]
    e_lepton = lepton[index, 0]
    p_lepton = lepton[index, 1:]

    # Four-momentum transfer q = p_nu - p_lepton; Q^2 = -q^2 = |q_vec|^2 - q_E^2.
    # Q^2 is non-negative for this process up to floating-point noise near
    # forward scattering, so the clamp only removes rounding, never physics.
    energy_transfer = e_nu - e_lepton
    q_vec = p_nu - p_lepton
    q2 = np.maximum(np.einsum("ij,ij->i", q_vec, q_vec) - energy_transfer**2, 0.0)
    columns["q2_gev2"][index] = q2

    # Inelasticity. Fermi motion of the struck nucleon can push E_lepton slightly
    # above E_nu, so a small negative y is physical and is passed through.
    positive_beam_energy = e_nu > 0.0
    columns["inelasticity_y"][index[positive_beam_energy]] = (
        energy_transfer[positive_beam_energy] / e_nu[positive_beam_energy]
    )

    # Bjorken-x against a fixed nucleon mass; undefined without a struck nucleon
    # (coherent) or without a positive energy transfer.
    has_x = energy_transfer > 0.0
    if interactions is not None:
        labels = np.asarray(interactions, dtype=object).reshape(-1)[index]
        has_x &= np.array(
            [str(label) not in NO_BJORKEN_X_INTERACTIONS for label in labels], dtype=bool
        )
    columns["bjorken_x"][index[has_x]] = q2[has_x] / (
        2.0 * NUCLEON_MASS_GEV * energy_transfer[has_x]
    )

    columns["lepton_energy_gev"][index] = e_lepton
    p_lepton_mag = np.linalg.norm(p_lepton, axis=1)
    columns["lepton_momentum_gev"][index] = p_lepton_mag

    # Longitudinal/transverse split and scattering angle, both relative to the
    # per-event neutrino direction.
    p_nu_mag = np.linalg.norm(p_nu, axis=1)
    has_axis = p_nu_mag > 0.0
    p_parallel = np.einsum("ij,ij->i", p_lepton[has_axis], p_nu[has_axis]) / p_nu_mag[has_axis]
    columns["lepton_p_parallel_gev"][index[has_axis]] = p_parallel
    columns["lepton_p_transverse_gev"][index[has_axis]] = np.sqrt(
        np.maximum(p_lepton_mag[has_axis] ** 2 - p_parallel**2, 0.0)
    )

    # cos(theta) additionally needs a non-zero lepton momentum to divide by.
    moving = p_lepton_mag[has_axis] > 0.0
    columns["lepton_costheta"][index[has_axis][moving]] = (
        p_parallel[moving] / p_lepton_mag[has_axis][moving]
    )

    return columns
