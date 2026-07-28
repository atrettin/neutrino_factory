from __future__ import annotations

import unittest

import numpy as np

from neutrino_factory.kinematics import (
    FIELD_DEFAULTS,
    KINEMATIC_FIELDS,
    MISSING,
    MISSING_SIGNED,
    NUCLEON_MASS_GEV,
    derive_kinematics,
    missing_kinematics,
)


class MissingKinematicsTests(unittest.TestCase):
    def test_returns_all_fields_at_their_placeholder(self) -> None:
        columns = missing_kinematics(4)
        self.assertEqual(set(columns), set(KINEMATIC_FIELDS))
        for field in KINEMATIC_FIELDS:
            self.assertEqual(len(columns[field]), 4)
            np.testing.assert_array_equal(columns[field], FIELD_DEFAULTS[field])

    def test_signed_quantities_use_a_distinct_placeholder(self) -> None:
        # -1 is a physical value for cos(theta) and for the parallel momentum, so
        # those two must not share the -1 placeholder with the rest.
        self.assertEqual(FIELD_DEFAULTS["lepton_costheta"], MISSING_SIGNED)
        self.assertEqual(FIELD_DEFAULTS["lepton_p_parallel_gev"], MISSING_SIGNED)
        self.assertEqual(FIELD_DEFAULTS["q2_gev2"], MISSING)
        self.assertNotEqual(MISSING, MISSING_SIGNED)


class DeriveKinematicsTests(unittest.TestCase):
    def test_reference_scatter_along_z(self) -> None:
        # E_nu = 10 GeV along +z; lepton at E = 5, p = (3, 0, 4) (massless).
        # nu = 5, q_vec = (-3, 0, 6) -> |q|^2 = 45, Q^2 = 45 - 25 = 20.
        columns = derive_kinematics([[10.0, 0.0, 0.0, 10.0]], [[5.0, 3.0, 0.0, 4.0]])
        self.assertAlmostEqual(columns["q2_gev2"][0], 20.0)
        self.assertAlmostEqual(columns["inelasticity_y"][0], 0.5)
        self.assertAlmostEqual(
            columns["bjorken_x"][0], 20.0 / (2.0 * NUCLEON_MASS_GEV * 5.0)
        )
        self.assertAlmostEqual(columns["lepton_energy_gev"][0], 5.0)
        self.assertAlmostEqual(columns["lepton_momentum_gev"][0], 5.0)
        self.assertAlmostEqual(columns["lepton_p_parallel_gev"][0], 4.0)
        self.assertAlmostEqual(columns["lepton_p_transverse_gev"][0], 3.0)
        self.assertAlmostEqual(columns["lepton_costheta"][0], 0.8)

    def test_off_axis_beam_gives_the_same_answer_as_the_rotated_z_case(self) -> None:
        """The beam axis is taken per event, never assumed to be +z."""
        along_z = derive_kinematics([[10.0, 0.0, 0.0, 10.0]], [[5.0, 3.0, 0.0, 4.0]])
        # Same event rotated so the beam runs along +x and the lepton's
        # transverse component along -z.
        off_axis = derive_kinematics([[10.0, 10.0, 0.0, 0.0]], [[5.0, 4.0, 0.0, -3.0]])
        for field in KINEMATIC_FIELDS:
            self.assertAlmostEqual(off_axis[field][0], along_z[field][0], msg=field)

    def test_backward_scatter_keeps_negative_values_distinct_from_placeholders(self) -> None:
        columns = derive_kinematics([[10.0, 0.0, 0.0, 10.0]], [[5.0, 0.0, 0.0, -5.0]])
        self.assertAlmostEqual(columns["lepton_costheta"][0], -1.0)
        self.assertAlmostEqual(columns["lepton_p_parallel_gev"][0], -5.0)
        self.assertAlmostEqual(columns["lepton_p_transverse_gev"][0], 0.0)
        # -1 here is the *physical* cos(theta), not the missing marker.
        self.assertNotEqual(columns["lepton_costheta"][0], MISSING_SIGNED)

    def test_quasi_elastic_like_kinematics_give_bjorken_x_near_one(self) -> None:
        # Elastic scattering off a nucleon at rest: nu = Q^2 / (2 M) exactly,
        # so x = 1. Pick Q^2 = 0.5 GeV^2 -> nu = 0.5 / (2 M).
        m = NUCLEON_MASS_GEV
        q2 = 0.5
        energy_transfer = q2 / (2.0 * m)
        e_nu = 3.0
        e_lepton = e_nu - energy_transfer
        # Solve for the scattering angle that produces this Q^2 with a massless
        # outgoing lepton: Q^2 = 2 E_nu E_l (1 - cos(theta)).
        cos_theta = 1.0 - q2 / (2.0 * e_nu * e_lepton)
        sin_theta = np.sqrt(1.0 - cos_theta**2)
        columns = derive_kinematics(
            [[e_nu, 0.0, 0.0, e_nu]],
            [[e_lepton, e_lepton * sin_theta, 0.0, e_lepton * cos_theta]],
        )
        self.assertAlmostEqual(columns["q2_gev2"][0], q2, places=9)
        self.assertAlmostEqual(columns["bjorken_x"][0], 1.0, places=9)

    def test_coherent_events_get_no_bjorken_x(self) -> None:
        nu = [[10.0, 0.0, 0.0, 10.0]] * 2
        lepton = [[5.0, 3.0, 0.0, 4.0]] * 2
        columns = derive_kinematics(nu, lepton, ["dis", "coh"])
        self.assertGreater(columns["bjorken_x"][0], 0.0)
        self.assertEqual(columns["bjorken_x"][1], MISSING)
        # Only x is suppressed; everything else stays physical.
        self.assertAlmostEqual(columns["q2_gev2"][1], 20.0)
        self.assertAlmostEqual(columns["inelasticity_y"][1], 0.5)
        self.assertAlmostEqual(columns["lepton_costheta"][1], 0.8)

    def test_non_positive_energy_transfer_blanks_bjorken_x_only(self) -> None:
        # Fermi motion can push the outgoing lepton above the beam energy; y then
        # goes slightly negative (kept) but x is undefined (blanked).
        columns = derive_kinematics([[5.0, 0.0, 0.0, 5.0]], [[5.2, 1.0, 0.0, 5.1]])
        self.assertEqual(columns["bjorken_x"][0], MISSING)
        self.assertLess(columns["inelasticity_y"][0], 0.0)
        self.assertGreaterEqual(columns["q2_gev2"][0], 0.0)

    def test_zero_momentum_lepton_blanks_only_the_angle(self) -> None:
        columns = derive_kinematics([[10.0, 0.0, 0.0, 10.0]], [[1.0, 0.0, 0.0, 0.0]])
        self.assertEqual(columns["lepton_costheta"][0], MISSING_SIGNED)
        # p_parallel is well-defined (and zero) even with no lepton momentum.
        self.assertAlmostEqual(columns["lepton_p_parallel_gev"][0], 0.0)
        self.assertAlmostEqual(columns["lepton_momentum_gev"][0], 0.0)
        self.assertAlmostEqual(columns["lepton_energy_gev"][0], 1.0)

    def test_zero_momentum_beam_blanks_the_directional_variables(self) -> None:
        columns = derive_kinematics([[0.0, 0.0, 0.0, 0.0]], [[1.0, 1.0, 0.0, 0.0]])
        self.assertEqual(columns["lepton_costheta"][0], MISSING_SIGNED)
        self.assertEqual(columns["lepton_p_parallel_gev"][0], MISSING_SIGNED)
        self.assertEqual(columns["lepton_p_transverse_gev"][0], MISSING)
        self.assertEqual(columns["inelasticity_y"][0], MISSING)
        # Q^2 and the lepton's own scalars remain well-defined.
        self.assertAlmostEqual(columns["lepton_energy_gev"][0], 1.0)
        self.assertAlmostEqual(columns["q2_gev2"][0], 0.0)

    def test_valid_mask_blanks_whole_events(self) -> None:
        nu = [[10.0, 0.0, 0.0, 10.0]] * 2
        lepton = [[5.0, 3.0, 0.0, 4.0]] * 2
        columns = derive_kinematics(nu, lepton, valid=[True, False])
        for field in KINEMATIC_FIELDS:
            self.assertNotEqual(columns[field][0], FIELD_DEFAULTS[field], msg=field)
            self.assertEqual(columns[field][1], FIELD_DEFAULTS[field], msg=field)

    def test_non_finite_four_vectors_are_blanked(self) -> None:
        columns = derive_kinematics(
            [[10.0, 0.0, 0.0, 10.0]], [[np.nan, 3.0, 0.0, 4.0]]
        )
        for field in KINEMATIC_FIELDS:
            self.assertEqual(columns[field][0], FIELD_DEFAULTS[field], msg=field)

    def test_forward_scatter_does_not_produce_negative_q2(self) -> None:
        # Exactly forward, equal energies: Q^2 is zero up to rounding and must
        # never come back negative.
        columns = derive_kinematics([[7.0, 0.0, 0.0, 7.0]], [[7.0, 0.0, 0.0, 7.0]])
        self.assertGreaterEqual(columns["q2_gev2"][0], 0.0)

    def test_random_batch_stays_within_physical_bounds(self) -> None:
        rng = np.random.default_rng(20260727)
        count = 2000
        e_nu = rng.uniform(0.5, 20.0, count)
        cos_theta = rng.uniform(-1.0, 1.0, count)
        phi = rng.uniform(0.0, 2.0 * np.pi, count)
        e_lepton = e_nu * rng.uniform(0.05, 1.0, count)
        sin_theta = np.sqrt(1.0 - cos_theta**2)
        nu_p4 = np.column_stack([e_nu, np.zeros(count), np.zeros(count), e_nu])
        lepton_p4 = np.column_stack([
            e_lepton,
            e_lepton * sin_theta * np.cos(phi),
            e_lepton * sin_theta * np.sin(phi),
            e_lepton * cos_theta,
        ])

        columns = derive_kinematics(nu_p4, lepton_p4)

        self.assertTrue(np.all(columns["q2_gev2"] >= 0.0))
        self.assertTrue(np.all(columns["inelasticity_y"] > 0.0))
        self.assertTrue(np.all(columns["inelasticity_y"] <= 1.0))
        self.assertTrue(np.all(columns["bjorken_x"] > 0.0))
        self.assertTrue(np.all(np.abs(columns["lepton_costheta"]) <= 1.0 + 1e-12))
        self.assertTrue(np.all(columns["lepton_p_transverse_gev"] >= 0.0))
        np.testing.assert_allclose(
            columns["lepton_p_parallel_gev"] ** 2 + columns["lepton_p_transverse_gev"] ** 2,
            columns["lepton_momentum_gev"] ** 2,
            rtol=1e-9,
            atol=1e-9,
        )

    def test_empty_input(self) -> None:
        columns = derive_kinematics(np.zeros((0, 4)), np.zeros((0, 4)))
        for field in KINEMATIC_FIELDS:
            self.assertEqual(len(columns[field]), 0)

    def test_mismatched_shapes_raise(self) -> None:
        with self.assertRaises(ValueError):
            derive_kinematics(np.zeros((2, 4)), np.zeros((3, 4)))


if __name__ == "__main__":
    unittest.main()
