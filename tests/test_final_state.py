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
    MISSING_SIGNED,
    is_meson,
    missing_final_state,
    summarize_final_state,
)


def _summarize(particles: list[list[tuple[int, float, float, float, float]]]) -> dict:
    """Summarize ``(pdg, E, px, py, pz)`` tuples, beam along +z, lepton along +x."""
    flat = [p for event in particles for p in event]
    counts = np.array([len(event) for event in particles], dtype=np.int64)
    pdg = np.array([p[0] for p in flat], dtype=np.int64)
    energy = np.array([p[1] for p in flat], dtype=np.float64)
    momentum = np.array([p[2:] for p in flat], dtype=np.float64).reshape(-1, 3)
    beam = np.tile([0.0, 0.0, 1.0], (len(particles), 1))
    lepton = np.tile([1.0, 0.0, 0.0], (len(particles), 1))
    return summarize_final_state(pdg, energy, momentum, counts, beam, lepton)


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


class OtherMesonTests(unittest.TestCase):
    def test_meson_classification(self) -> None:
        mesons = [211, -211, 111, 321, -321, 311, 130, 310, 221, 223, 331, 411, 9000111]
        others = [2212, 2112, 3122, 3222, 22, 11, 13, 14, 2103, 1000180400, 0]
        self.assertTrue(np.all(is_meson(mesons)))
        self.assertFalse(np.any(is_meson(others)))

    def test_counts_non_pion_mesons_only(self) -> None:
        columns = _summarize([
            [(321, 1.0, 0.0, 0.0, 0.0), (-311, 1.0, 0.0, 0.0, 0.0), (221, 1.0, 0.0, 0.0, 0.0),
             (211, 1.0, 0.0, 0.0, 0.0), (111, 1.0, 0.0, 0.0, 0.0), (3122, 1.0, 0.0, 0.0, 0.0)],
            [(211, 1.0, 0.0, 0.0, 0.0)],
        ])
        self.assertEqual(list(columns["n_other_mesons"]), [3, 0])


class LeadingPionTests(unittest.TestCase):
    def test_charged_and_neutral_pions_lead_separately(self) -> None:
        # pi- (T = 0.2) beats pi+ (T = 0.1) among the charged pions; the pi0
        # (T = 0.1) leads its own pair although it is less energetic. The more
        # energetic proton and kaon are not pions.
        columns = _summarize([[
            (211, 1.3, 0.5, 0.0, 0.0),
            (-211, 1.0, 0.0, 0.0, -0.6),
            (111, 1.3, 0.0, 0.5, 0.0),
            (2212, 5.0, 4.0, 0.0, 0.0),
            (321, 5.0, 4.0, 0.0, 0.0),
        ]])
        self.assertAlmostEqual(columns["leading_pi_charged_kinetic_energy_gev"][0], 0.2)
        self.assertAlmostEqual(columns["leading_pi_charged_costheta"][0], -1.0)
        self.assertAlmostEqual(columns["leading_pi_zero_kinetic_energy_gev"][0], 0.1)
        self.assertAlmostEqual(columns["leading_pi_zero_costheta"][0], 0.0)

    def test_a_pi0_does_not_fill_the_charged_columns_or_vice_versa(self) -> None:
        columns = _summarize([[(111, 1.0, 0.0, 0.0, 0.6)], [(211, 1.0, 0.0, 0.0, 0.6)]])
        self.assertEqual(
            [round(float(t), 9) for t in columns["leading_pi_zero_kinetic_energy_gev"]],
            [0.2, MISSING_ENERGY],
        )
        self.assertEqual(
            [round(float(t), 9) for t in columns["leading_pi_charged_kinetic_energy_gev"]],
            [MISSING_ENERGY, 0.2],
        )

    def test_angle_is_measured_against_each_events_beam(self) -> None:
        pdg = np.array([211, 111], dtype=np.int64)
        energy = np.array([5.0, 5.0])
        momentum = np.array([[0.0, 0.0, 4.0], [0.0, 0.0, 4.0]])
        beam = np.array([[0.0, 0.0, 2.0], [0.0, 3.0, 4.0]])
        columns = summarize_final_state(pdg, energy, momentum, np.array([1, 1]), beam, beam)
        self.assertAlmostEqual(columns["leading_pi_charged_costheta"][0], 1.0)
        self.assertAlmostEqual(columns["leading_pi_zero_costheta"][1], 0.8)

    def test_pion_at_rest_has_no_angle(self) -> None:
        columns = _summarize([[(111, 0.135, 0.0, 0.0, 0.0)]])
        self.assertAlmostEqual(columns["leading_pi_zero_kinetic_energy_gev"][0], 0.0)
        self.assertEqual(columns["leading_pi_zero_costheta"][0], MISSING_SIGNED)

    def test_leading_pion_is_attributed_to_the_right_events(self) -> None:
        columns = _summarize([
            [(211, 1.3, 0.0, 0.0, 0.5)],
            [(2212, 1.0, 0.0, 0.0, 0.0)],
            [(-211, 1.0, 0.0, 0.0, -0.6), (211, 5.0, 0.0, 0.0, 4.0)],
        ])
        self.assertEqual(
            [round(float(t), 9) for t in columns["leading_pi_charged_kinetic_energy_gev"]],
            [0.1, MISSING_ENERGY, 2.0],
        )
        self.assertEqual(
            [round(float(c), 9) for c in columns["leading_pi_charged_costheta"]],
            [1.0, MISSING_SIGNED, 1.0],
        )

    def test_beam_count_must_match_events(self) -> None:
        with self.assertRaises(ValueError):
            summarize_final_state(
                np.array([211]), np.array([1.0]), np.zeros((1, 3)), np.array([1]),
                np.zeros((2, 3)), np.zeros((1, 3)),
            )
        with self.assertRaises(ValueError):
            summarize_final_state(
                np.array([211]), np.array([1.0]), np.zeros((1, 3)), np.array([1]),
                np.zeros((1, 3)), np.zeros((2, 3)),
            )


class LeadingProtonTests(unittest.TestCase):
    def test_picks_highest_kinetic_energy_proton_and_measures_both_angles(self) -> None:
        # The T = 0.2 proton leads over the T = 0.1 one; the more energetic
        # neutron, antiproton and pion are not protons. Its direction
        # (0.6, 0, 0.8)/1 gives cos 0.8 to the +z beam and 0.6 to the +x lepton.
        columns = _summarize([[
            (2212, 1.3, 0.0, 0.5, 0.0),
            (2212, 1.0, 0.36, 0.0, 0.48),
            (2112, 5.0, 4.0, 0.0, 0.0),
            (-2212, 5.0, 4.0, 0.0, 0.0),
            (211, 5.0, 4.0, 0.0, 0.0),
        ]])
        self.assertAlmostEqual(columns["leading_proton_kinetic_energy_gev"][0], 0.2)
        self.assertAlmostEqual(columns["leading_proton_costheta"][0], 0.8)
        self.assertAlmostEqual(columns["leading_proton_lepton_costheta"][0], 0.6)

    def test_lepton_angle_is_measured_against_each_events_lepton(self) -> None:
        pdg = np.array([2212, 2212], dtype=np.int64)
        energy = np.array([1.0, 1.0])
        momentum = np.array([[0.0, 0.0, 0.6], [0.0, 0.0, 0.6]])
        beam = np.tile([0.0, 0.0, 1.0], (2, 1))
        lepton = np.array([[0.0, 0.0, -2.0], [0.0, 0.0, 0.0]])
        columns = summarize_final_state(pdg, energy, momentum, np.array([1, 1]), beam, lepton)
        self.assertAlmostEqual(columns["leading_proton_lepton_costheta"][0], -1.0)
        # No lepton momentum (e.g. a NEUT event without a lepton): no angle.
        self.assertEqual(columns["leading_proton_lepton_costheta"][1], MISSING_SIGNED)
        self.assertAlmostEqual(columns["leading_proton_costheta"][1], 1.0)


class EmptyAndDegenerateInputTests(unittest.TestCase):
    def test_empty_final_state_is_zero_not_missing(self) -> None:
        # An event with nothing hadronic out is a measurement; only an absent
        # particle list is missing.
        columns = _summarize([[(13, 2.5, 1.5, 0.0, 0.0)]])
        for field in COUNT_FIELDS:
            self.assertEqual(columns[field][0], 0, msg=field)
        for field in ENERGY_FIELDS:
            self.assertEqual(columns[field][0], 0.0, msg=field)
        # ...except that there is no leading pion or proton to describe.
        for prefix in ("leading_pi_charged", "leading_pi_zero", "leading_proton"):
            self.assertEqual(columns[f"{prefix}_kinetic_energy_gev"][0], MISSING_ENERGY)
            self.assertEqual(columns[f"{prefix}_costheta"][0], MISSING_SIGNED)
        self.assertEqual(columns["leading_proton_lepton_costheta"][0], MISSING_SIGNED)

    def test_no_events(self) -> None:
        columns = summarize_final_state(
            np.array([], dtype=np.int64),
            np.array([], dtype=np.float64),
            np.zeros((0, 3), dtype=np.float64),
            np.array([], dtype=np.int64),
            np.zeros((0, 3), dtype=np.float64),
            np.zeros((0, 3), dtype=np.float64),
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
                np.zeros((1, 3), dtype=np.float64),
                np.zeros((1, 3), dtype=np.float64),
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
        columns = summarize_final_state(*reference_flat_arrays(3))
        expected = reference_final_state()
        for index in range(3):
            for field in FINAL_STATE_FIELDS:
                self.assertAlmostEqual(
                    float(columns[field][index]), float(expected[field]), places=9, msg=field
                )


if __name__ == "__main__":
    unittest.main()
