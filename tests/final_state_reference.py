"""Shared reference final state used by the per-generator normalizer tests.

Like ``kinematics_reference``, every generator's synthetic tree is filled with
the *same* particle list, so all four normalizer test modules assert one set of
expected multiplicities and hadronic energies. A generator-specific bug then
shows up as a disagreement rather than as four independently wrong numbers.

The list deliberately contains the cases the summary has to get right:

* two protons (multiplicity, not a boolean),
* one pi+ and one pi- (signed counting -- a pi- must not land in ``n_pi_plus``),
* a pi0 and a neutron (neutral species),
* a photon, which counts as hadronic under the non-leptonic convention,
* the outgoing muon, which must be excluded,
* the residual argon nucleus, which must be excluded (its rest mass alone would
  otherwise dominate the energy sums).

Energies and momenta are Pythagorean triples so ``m = sqrt(E^2 - |p|^2)`` and
``T = E - m`` are exact in binary floating point. They are chosen for exact
arithmetic, not for physical realism -- the masses do not match the species.
"""

from __future__ import annotations

import numpy as np


# (pdg, E, px, py, pz) in GeV.
REFERENCE_PARTICLES: tuple[tuple[int, float, float, float, float], ...] = (
    (13, 2.5, 1.5, 0.0, 0.0),            # outgoing muon -- excluded (lepton)
    (2212, 1.0, 0.0, 0.6, 0.0),          # m = 0.8, T = 0.2
    (2212, 1.3, 0.0, 0.0, 0.5),          # m = 1.2, T = 0.1
    (2112, 0.5, 0.3, 0.0, 0.0),          # m = 0.4, T = 0.1
    (211, 5.0, 0.0, 4.0, 0.0),           # m = 3.0, T = 2.0
    (-211, 1.3, 0.5, 0.0, 0.0),          # m = 1.2, T = 0.1
    (111, 1.0, 0.6, 0.0, 0.0),           # m = 0.8, T = 0.2
    (22, 0.5, 0.0, 0.0, 0.5),            # m = 0.0, T = 0.5
    (1000180400, 40.0, 0.0, 0.0, 0.3),   # residual argon -- excluded (nucleus)
)

REFERENCE_PARTICLE_COUNT = len(REFERENCE_PARTICLES)


def reference_final_state() -> dict[str, float]:
    """Expected common-format final-state values for ``REFERENCE_PARTICLES``."""
    return {
        "n_proton": 2,
        "n_neutron": 1,
        "n_pi_plus": 1,
        "n_pi_minus": 1,
        "n_pi_zero": 1,
        # 1.0 + 1.3 + 0.5 + 5.0 + 1.3 + 1.0 + 0.5
        "hadronic_energy_gev": 10.6,
        # 0.2 + 0.1 + 0.1 + 2.0 + 0.1 + 0.2 + 0.5
        "hadronic_kinetic_energy_gev": 3.2,
    }


def reference_particle_lists(n_events: int) -> tuple[list[list[int]], list[list[list[float]]]]:
    """``(pdg, p4)`` jagged lists repeating the reference final state per event."""
    pdg = [[int(p[0]) for p in REFERENCE_PARTICLES] for _ in range(n_events)]
    p4 = [[[float(c) for c in p[1:]] for p in REFERENCE_PARTICLES] for _ in range(n_events)]
    return pdg, p4


def reference_awkward_branches(n_events: int, momentum_scale: float = 1.0):
    """``(pdg, E, px, py, pz)`` as jagged awkward arrays, for writing test trees.

    ``momentum_scale`` converts out of GeV for generators whose native tree is in
    other units (NuWro writes MeV).
    """
    import awkward as ak

    pdg_lists, p4_lists = reference_particle_lists(n_events)
    components = [
        ak.Array([[particle[c] * momentum_scale for particle in event] for event in p4_lists])
        for c in range(4)
    ]
    return (ak.Array(pdg_lists), *components)


def reference_flat_arrays(n_events: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(pdg, energy, momentum, counts)`` in the flattened form the summarizer takes."""
    pdg_lists, p4_lists = reference_particle_lists(n_events)
    pdg = np.array([code for event in pdg_lists for code in event], dtype=np.int64)
    p4 = np.array(
        [particle for event in p4_lists for particle in event], dtype=np.float64
    ).reshape(-1, 4)
    counts = np.full(n_events, REFERENCE_PARTICLE_COUNT, dtype=np.int64)
    return pdg, p4[:, 0], p4[:, 1:], counts
