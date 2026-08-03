from __future__ import annotations

import unittest

import numpy as np

from tests.final_state_reference import reference_final_state, reference_flat_arrays
from neutrino_factory.final_state import (
    COUNT_FIELDS,
    ENERGY_FIELDS,
    FINAL_STATE_FIELDS,
    MISSING_COUNT,
    MISSING_ENERGY,
    missing_final_state,
    summarize_final_state,
)


def _summarize(particles: list[list[tuple[int, float, float, float, float]]]) -> dict:
    """Summarize a per-event list of ``(pdg, E, px, py, pz)`` tuples."""
    flat = [p for event in particles for p in event]
    counts = np.array([len(event) for event in particles], dtype=np.int64)
    pdg = np.array([p[0] for p in flat], dtype=np.int64)
    energy = np.array([p[1] for p in flat], dtype=np.float64)
    momentum = np.array([p[2:] for p in flat], dtype=np.float64).reshape(-1, 3)
    return summarize_final_state(pdg, energy, momentum, counts)


class MultiplicityTests(unittest.TestCase):
    def test_counts_each_species_separately(self) -> None:
        columns = _summarize([[
            (2212, 1.0, 0.0, 0.0, 0.0),
            (2212, 1.0, 0.0, 0.0, 0.0),
            (2112, 1.0, 0.0, 0.0, 0.0),
            (211, 1.0, 0.0, 0.0, 0.0),
            (-211, 1.0, 0.0, 0.0, 0.0),
            (111, 1.0, 0.0, 0.0, 0.0),
        ]])
        self.assertEqual(columns["n_proton"][0], 2)
        self.assertEqual(columns["n_neutron"][0], 1)
        self.assertEqual(columns["n_pi_plus"][0], 1)
        self.assertEqual(columns["n_pi_minus"][0], 1)
        self.assertEqual(columns["n_pi_zero"][0], 1)

    def test_counting_is_signed(self) -> None:
        # An antiproton is not a proton and a pi- is not a pi+; counting on
        # |pdg| would merge charge states and quietly break a CC1pi+ selection.
        columns = _summarize([[
            (-2212, 1.0, 0.0, 0.0, 0.0),
            (-2112, 1.0, 0.0, 0.0, 0.0),
            (-211, 1.0, 0.0, 0.0, 0.0),
        ]])
        self.assertEqual(columns["n_proton"][0], 0)
        self.assertEqual(columns["n_neutron"][0], 0)
        self.assertEqual(columns["n_pi_plus"][0], 0)
        self.assertEqual(columns["n_pi_minus"][0], 1)

    def test_particles_are_attributed_to_the_right_events(self) -> None:
        columns = _summarize([
            [(2212, 1.0, 0.0, 0.0, 0.0)],
            [],
            [(211, 1.0, 0.0, 0.0, 0.0), (211, 1.0, 0.0, 0.0, 0.0), (2112, 1.0, 0.0, 0.0, 0.0)],
        ])
        self.assertEqual(list(columns["n_proton"]), [1, 0, 0])
        self.assertEqual(list(columns["n_pi_plus"]), [0, 0, 2])
        self.assertEqual(list(columns["n_neutron"]), [0, 0, 1])


class HadronicEnergyTests(unittest.TestCase):
    def test_energy_sums_use_the_four_vector_mass(self) -> None:
        # (E, |p|, m) = (1.0, 0.6, 0.8) and (5.0, 4.0, 3.0): T = 0.2 and 2.0.
        columns = _summarize([[
            (2212, 1.0, 0.0, 0.6, 0.0),
            (211, 5.0, 4.0, 0.0, 0.0),
        ]])
        self.assertAlmostEqual(columns["hadronic_energy_gev"][0], 6.0)
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 2.2)

    def test_massless_particle_has_kinetic_energy_equal_to_its_energy(self) -> None:
        columns = _summarize([[(22, 0.5, 0.0, 0.0, 0.5)]])
        self.assertAlmostEqual(columns["hadronic_energy_gev"][0], 0.5)
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 0.5)

    def test_rounding_below_the_mass_shell_does_not_produce_nan(self) -> None:
        # |p| marginally above E happens through rounding in single-precision
        # generator output; sqrt of a negative m^2 would poison the whole column.
        columns = _summarize([[(22, 1.0, 1.0 + 1e-12, 0.0, 0.0)]])
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 1.0)

    def test_leptons_are_excluded(self) -> None:
        # The outgoing muon has its own columns, and a final-state neutrino
        # carries energy no hadronic measure should claim.
        columns = _summarize([[
            (13, 2.5, 1.5, 0.0, 0.0),
            (14, 1.0, 1.0, 0.0, 0.0),
            (-11, 0.7, 0.7, 0.0, 0.0),
            (2212, 1.0, 0.0, 0.6, 0.0),
        ]])
        self.assertAlmostEqual(columns["hadronic_energy_gev"][0], 1.0)
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 0.2)

    def test_nuclear_remnant_is_excluded(self) -> None:
        # The residual argon's ~37 GeV rest mass would dominate the sum.
        columns = _summarize([[
            (1000180400, 37.5, 0.0, 0.0, 0.3),
            (2212, 1.0, 0.0, 0.6, 0.0),
        ]])
        self.assertAlmostEqual(columns["hadronic_energy_gev"][0], 1.0)
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 0.2)

    def test_photons_and_kaons_count_as_hadronic(self) -> None:
        # "Hadronic" here means non-leptonic: dropping these species would lose
        # energy that genuinely left the interaction.
        columns = _summarize([[
            (22, 0.5, 0.0, 0.0, 0.5),
            (321, 1.0, 0.0, 0.6, 0.0),
        ]])
        self.assertAlmostEqual(columns["hadronic_energy_gev"][0], 1.5)
        self.assertAlmostEqual(columns["hadronic_kinetic_energy_gev"][0], 0.7)


class EmptyAndDegenerateInputTests(unittest.TestCase):
    def test_empty_final_state_is_zero_not_missing(self) -> None:
        # An event with nothing hadronic out is a measurement; only an absent
        # particle list is missing.
        columns = _summarize([[(13, 2.5, 1.5, 0.0, 0.0)]])
        for field in COUNT_FIELDS:
            self.assertEqual(columns[field][0], 0, msg=field)
        for field in ENERGY_FIELDS:
            self.assertEqual(columns[field][0], 0.0, msg=field)

    def test_no_events(self) -> None:
        columns = summarize_final_state(
            np.array([], dtype=np.int64),
            np.array([], dtype=np.float64),
            np.zeros((0, 3), dtype=np.float64),
            np.array([], dtype=np.int64),
        )
        for field in FINAL_STATE_FIELDS:
            self.assertEqual(len(columns[field]), 0, msg=field)

    def test_mismatched_lengths_raise(self) -> None:
        with self.assertRaises(ValueError):
            summarize_final_state(
                np.array([2212, 2112], dtype=np.int64),
                np.array([1.0, 1.0], dtype=np.float64),
                np.zeros((2, 3), dtype=np.float64),
                np.array([3], dtype=np.int64),
            )

    def test_missing_block_is_all_placeholders(self) -> None:
        columns = missing_final_state(4)
        for field in COUNT_FIELDS:
            self.assertTrue(np.all(columns[field] == MISSING_COUNT), msg=field)
        for field in ENERGY_FIELDS:
            self.assertTrue(np.all(columns[field] == MISSING_ENERGY), msg=field)


class ReferenceFinalStateTests(unittest.TestCase):
    """The same list the four normalizer test modules assert against."""

    def test_matches_the_shared_reference(self) -> None:
        pdg, energy, momentum, counts = reference_flat_arrays(3)
        columns = summarize_final_state(pdg, energy, momentum, counts)
        expected = reference_final_state()
        for index in range(3):
            for field in FINAL_STATE_FIELDS:
                self.assertAlmostEqual(
                    float(columns[field][index]), float(expected[field]), places=9, msg=field
                )


if __name__ == "__main__":
    unittest.main()
