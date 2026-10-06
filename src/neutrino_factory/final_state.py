"""Final-state summary columns for the common output format.

The ``interaction`` label (``qel``/``res``/``mec``/``dis``/``coh``/``other``) is
too coarse to cut on: "CC0pi", "CC1pi+" or "hadronic energy below threshold" are
all invisible at that granularity. Every generator carries a full post-FSI
particle list in its raw output; this module turns that list into a fixed set of
scalar summary columns, by one shared formula, so a pion multiplicity from GENIE
means exactly what one from NuWro means.

Conventions:

* **Final state means post-FSI** — the particles that leave the nucleus. Where a
  generator exposes both a primary (pre-FSI) and a post-FSI list, the normalizer
  passes the post-FSI one.
* **"Hadronic" means non-leptonic**: every final-state particle except the
  charged leptons and neutrinos (``|pdg|`` in 11..16). Photons and kaons are
  therefore included in the hadronic energy sums — a slight abuse of the name,
  chosen so that the sum accounts for all the non-leptonic energy leaving the
  interaction rather than silently dropping species.
* **Nuclear remnants are excluded** (``|pdg| > 1e9``, the ``10LZZZAAAI`` ion
  codes). GENIE's final-state list carries the residual nucleus, whose rest mass
  (~37 GeV for argon) would otherwise dominate ``hadronic_energy_gev``.
* Particle masses are taken from the four-momentum, ``m = sqrt(E^2 - |p|^2)``,
  never from a lookup table: all four generators supply full four-vectors, so a
  table would only add a way to disagree with the generator.

Per-event particle lists are deliberately *not* stored. The common format is
strictly rectangular (one 1-D dataset per column, see ``common_output``); these
summaries are what fits, and they are what cuts are actually made on.
"""

from __future__ import annotations

from typing import Any

import numpy as np


# Placeholder for the multiplicity columns. Counts are non-negative by
# construction, so -1 is unmistakably "not available" rather than "none found".
MISSING_COUNT = -1

# Placeholder for the hadronic energy columns, matching kinematics.MISSING.
MISSING_ENERGY = -1.0

# Placeholder for the leading-particle cosines, matching kinematics.MISSING_SIGNED:
# -1 is a physical value of a cosine.
MISSING_SIGNED = -999.0

# Placeholder for the generator-native interaction code. It cannot be 0: NuWro's
# dyn = 0 is CC quasi-elastic, and it cannot be a small negative number either,
# since NEUT encodes antineutrino channels as negative modes. This is the
# smallest int32, outside every generator's code range.
MISSING_NATIVE_CODE = -(2**31)

PDG_PROTON = 2212
PDG_NEUTRON = 2112
PDG_PI_PLUS = 211
PDG_PI_MINUS = -211
PDG_PI_ZERO = 111

# The counted species, in column order. Signed: pi+ and pi- are distinct
# columns, while a proton column must not absorb antiprotons.
COUNTED_PDG: dict[str, int] = {
    "n_proton": PDG_PROTON,
    "n_neutron": PDG_NEUTRON,
    "n_pi_plus": PDG_PI_PLUS,
    "n_pi_minus": PDG_PI_MINUS,
    "n_pi_zero": PDG_PI_ZERO,
}

PION_PDGS = tuple(COUNTED_PDG[name] for name in ("n_pi_plus", "n_pi_minus", "n_pi_zero"))

# Every meson that is not a pion (kaons, eta, ...), summed into one column, so a
# "single pion, no other mesons" signal definition can be applied exactly.
OTHER_MESONS_FIELD = "n_other_mesons"

COUNT_FIELDS = (*COUNTED_PDG, OTHER_MESONS_FIELD)
ENERGY_FIELDS = ("hadronic_energy_gev", "hadronic_kinetic_energy_gev")

# The highest-kinetic-energy pion of any charge, with its angle to the beam.
# Combined with the n_pi_* counts this selects e.g. the pi+ of a CC1pi+ event.
# The highest-kinetic-energy proton, with its angles to the beam and to the
# outgoing lepton; a proton visibility threshold is a cut on its T. An event
# without such a particle carries the placeholders: there is nothing leading.
LEADING_PION_FIELDS = ("leading_pion_kinetic_energy_gev", "leading_pion_costheta")
LEADING_PROTON_FIELDS = (
    "leading_proton_kinetic_energy_gev",
    "leading_proton_costheta",
    "leading_proton_lepton_costheta",
)

# The columns derived from the final-state particle list, with the placeholder
# each falls back to when the list is unavailable (stub mode, or a file written
# before these columns existed).
FIELD_DEFAULTS: dict[str, Any] = {
    **{name: MISSING_COUNT for name in COUNT_FIELDS},
    **{name: MISSING_ENERGY for name in ENERGY_FIELDS},
    **{
        name: MISSING_ENERGY if name.endswith("_gev") else MISSING_SIGNED
        for name in (*LEADING_PION_FIELDS, *LEADING_PROTON_FIELDS)
    },
}

FINAL_STATE_FIELDS = tuple(FIELD_DEFAULTS)

# The generator's own channel code, carried verbatim. This is the one column of
# the common format that is *not* universal -- its meaning depends on
# ``generator`` (see docs/design_decisions.md) -- and it exists so that the
# exact channel split can be reverse-engineered without the raw files.
NATIVE_CODE_FIELD = "native_interaction_code"

# The column table this module contributes to ``common_output.NUMERIC_FIELD_SPECS``.
NUMERIC_FIELD_SPECS: dict[str, tuple[type, Any]] = {
    **{
        name: (np.int64 if name in COUNT_FIELDS else np.float64, default)
        for name, default in FIELD_DEFAULTS.items()
    },
    NATIVE_CODE_FIELD: (np.int64, MISSING_NATIVE_CODE),
}

# Leptons: charged leptons (11, 13, 15) and neutrinos (12, 14, 16). Excluded
# from the hadronic sums -- the outgoing lepton has its own columns, and a
# neutrino in the final state carries energy no hadronic measure should claim.
MIN_LEPTON_PDG = 11
MAX_LEPTON_PDG = 16

# Nuclear/ion PDG codes are 10LZZZAAAI, i.e. above 1e9.
MIN_NUCLEUS_PDG = 1_000_000_000


def is_meson(pdg: Any) -> np.ndarray:
    """PDG-scheme meson test: digits ``n_q1 = 0`` and ``n_q2, n_q3 != 0``.

    Covers excited states (``9000111``) and the K0_L code 130, and rejects
    baryons (``n_q1 != 0``), leptons, gauge bosons and nuclei.
    """
    a = np.abs(np.asarray(pdg, dtype=np.int64))
    return (
        (a < MIN_NUCLEUS_PDG)
        & ((a // 1000) % 10 == 0)
        & ((a // 100) % 10 != 0)
        & ((a // 10) % 10 != 0)
    )


def missing_final_state(count: int) -> dict[str, np.ndarray]:
    """Return a full block of ``count`` all-placeholder final-state columns."""
    return {
        name: np.full(int(count), default, dtype=np.int64 if name in COUNT_FIELDS else np.float64)
        for name, default in FIELD_DEFAULTS.items()
    }


def flatten_particle_arrays(
    ak: Any,
    pdg: Any,
    energy: Any,
    px: Any,
    py: Any,
    pz: Any,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Turn jagged per-event particle branches into ``summarize_final_state`` inputs.

    Every generator's final-state list arrives as parallel jagged arrays, so all
    four normalizers share this decomposition into flat arrays plus a per-event
    count. ``ak`` is the ``awkward`` module, passed in rather than imported at
    module scope so this file stays importable without it (the same pattern the
    NuWro normalizer uses).
    """
    counts = np.asarray(ak.to_numpy(ak.num(pdg, axis=1)), dtype=np.int64)
    pdg_flat = np.asarray(ak.to_numpy(ak.flatten(pdg, axis=1)), dtype=np.int64)
    energy_flat = np.asarray(ak.to_numpy(ak.flatten(energy, axis=1)), dtype=np.float64)
    momentum_flat = np.column_stack([
        np.asarray(ak.to_numpy(ak.flatten(component, axis=1)), dtype=np.float64)
        for component in (px, py, pz)
    ]).reshape(-1, 3)
    return pdg_flat, energy_flat, momentum_flat, counts


def event_fields(columns: dict[str, np.ndarray], index: int) -> dict[str, Any]:
    """One event's final-state columns as plain Python scalars, ready for the writer."""
    return {
        field: int(columns[field][index]) if field in COUNT_FIELDS else float(columns[field][index])
        for field in FINAL_STATE_FIELDS
    }


def summarize_final_state(
    pdg: Any,
    energy: Any,
    momentum: Any,
    counts: Any,
    beam_momentum: Any,
    lepton_momentum: Any,
) -> dict[str, np.ndarray]:
    """Summarize per-event final-state particle lists into the scalar columns.

    The particle lists come in **flattened**, the natural decomposition of a
    jagged branch (``ak.flatten`` / ``ak.num``), so the whole summary is a pair
    of ``np.bincount`` calls with no Python loop over events:

    * ``pdg`` — flat ``(n_particles,)`` array of PDG codes.
    * ``energy`` — flat ``(n_particles,)`` array of energies in GeV.
    * ``momentum`` — flat ``(n_particles, 3)`` array of ``(px, py, pz)`` in GeV.
    * ``counts`` — ``(n_events,)`` array giving how many particles each event
      contributed, in order; must sum to ``n_particles``.
    * ``beam_momentum`` — ``(n_events, 3)`` incoming-neutrino three-momentum,
      the per-event axis for the ``*_costheta`` columns (as in ``kinematics``).
    * ``lepton_momentum`` — ``(n_events, 3)`` outgoing-lepton three-momentum,
      the axis for ``leading_proton_lepton_costheta``.

    Returns a dict keyed by ``FINAL_STATE_FIELDS``. Events with an empty final
    state get zero counts and zero energies -- that is a measurement, not a
    missing value; use ``missing_final_state`` when the list itself is absent.
    The leading-particle columns are the exception: without such a particle
    they keep their placeholders.
    """
    event_counts = np.asarray(counts, dtype=np.int64).reshape(-1)
    n_events = int(event_counts.size)
    pdg_flat = np.asarray(pdg, dtype=np.int64).reshape(-1)
    energy_flat = np.asarray(energy, dtype=np.float64).reshape(-1)
    momentum_flat = np.asarray(momentum, dtype=np.float64).reshape(-1, 3)
    beam = np.asarray(beam_momentum, dtype=np.float64).reshape(-1, 3)
    lepton = np.asarray(lepton_momentum, dtype=np.float64).reshape(-1, 3)
    if beam.shape[0] != n_events or lepton.shape[0] != n_events:
        raise ValueError(
            f"Got {beam.shape[0]} beam and {lepton.shape[0]} lepton momenta "
            f"for {n_events} events."
        )

    total = int(event_counts.sum())
    if pdg_flat.size != total or energy_flat.size != total or momentum_flat.shape[0] != total:
        raise ValueError(
            "Flattened final-state arrays do not match the per-event counts: "
            f"counts sum to {total}, got {pdg_flat.size} pdg, {energy_flat.size} "
            f"energy and {momentum_flat.shape[0]} momentum entries. Mismatched "
            "arrays would silently attribute particles to the wrong events."
        )

    columns: dict[str, np.ndarray] = {
        name: np.zeros(n_events, dtype=np.int64) for name in COUNT_FIELDS
    }
    columns.update({name: np.zeros(n_events, dtype=np.float64) for name in ENERGY_FIELDS})
    columns.update({
        name: np.full(n_events, FIELD_DEFAULTS[name], dtype=np.float64)
        for name in (*LEADING_PION_FIELDS, *LEADING_PROTON_FIELDS)
    })
    if n_events == 0 or total == 0:
        return columns

    # Which event each flattened particle belongs to.
    event_index = np.repeat(np.arange(n_events, dtype=np.int64), event_counts)

    for name, code in COUNTED_PDG.items():
        columns[name] = np.bincount(
            event_index[pdg_flat == code], minlength=n_events
        ).astype(np.int64)
    columns[OTHER_MESONS_FIELD] = np.bincount(
        event_index[is_meson(pdg_flat) & ~np.isin(pdg_flat, PION_PDGS)], minlength=n_events
    ).astype(np.int64)

    flat = (energy_flat, momentum_flat, event_index)
    _fill_leading(columns, "leading_pion", np.isin(pdg_flat, PION_PDGS), *flat,
                  {"costheta": beam})
    _fill_leading(columns, "leading_proton", pdg_flat == PDG_PROTON, *flat,
                  {"costheta": beam, "lepton_costheta": lepton})

    abs_pdg = np.abs(pdg_flat)
    hadronic = ~(
        ((abs_pdg >= MIN_LEPTON_PDG) & (abs_pdg <= MAX_LEPTON_PDG))
        | (abs_pdg >= MIN_NUCLEUS_PDG)
    )
    if not hadronic.any():
        return columns

    hadron_events = event_index[hadronic]
    hadron_energy = energy_flat[hadronic]
    hadron_p = momentum_flat[hadronic]

    # m^2 can go slightly negative through rounding for a massless or
    # on-shell-by-construction particle; clamping only removes that noise.
    mass = np.sqrt(
        np.maximum(hadron_energy**2 - np.einsum("ij,ij->i", hadron_p, hadron_p), 0.0)
    )

    columns["hadronic_energy_gev"] = np.bincount(
        hadron_events, weights=hadron_energy, minlength=n_events
    )
    columns["hadronic_kinetic_energy_gev"] = np.bincount(
        hadron_events, weights=hadron_energy - mass, minlength=n_events
    )
    return columns


def _fill_leading(
    columns: dict[str, np.ndarray],
    prefix: str,
    selected: np.ndarray,
    energy: np.ndarray,
    momentum: np.ndarray,
    event_index: np.ndarray,
    axes: dict[str, np.ndarray],
) -> None:
    """Write ``<prefix>_kinetic_energy_gev`` and ``<prefix>_<axis name>`` in place.

    The leading particle is the ``selected`` one with the highest kinetic
    energy in each event; ``axes`` maps a column suffix to the per-event
    ``(n_events, 3)`` direction its cosine is taken against.
    """
    if not selected.any():
        return
    events = event_index[selected]
    p = momentum[selected]
    p2 = np.einsum("ij,ij->i", p, p)
    kinetic = energy[selected] - np.sqrt(np.maximum(energy[selected] ** 2 - p2, 0.0))

    # Sort by (event, T) and keep each event's last entry: its highest-T particle.
    order = np.lexsort((kinetic, events))
    last = np.append(events[order][1:] != events[order][:-1], True)
    chosen = order[last]
    chosen_events = events[chosen]
    columns[f"{prefix}_kinetic_energy_gev"][chosen_events] = kinetic[chosen]

    for suffix, directions in axes.items():
        axis = directions[chosen_events]
        norm = np.sqrt(p2[chosen] * np.einsum("ij,ij->i", axis, axis))
        defined = norm > 0.0
        columns[f"{prefix}_{suffix}"][chosen_events[defined]] = (
            np.einsum("ij,ij->i", p[chosen][defined], axis[defined]) / norm[defined]
        )
