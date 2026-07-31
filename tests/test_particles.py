from __future__ import annotations

import unittest

from neutrino_factory.particles import PARTICLE_PDG, is_antineutrino, probe_pdg


class ParticleTableTests(unittest.TestCase):
    def test_all_six_probes_are_present_with_pdg_codes(self) -> None:
        self.assertEqual(
            PARTICLE_PDG,
            {
                "nue": 12,
                "nuebar": -12,
                "numu": 14,
                "numubar": -14,
                "nutau": 16,
                "nutaubar": -16,
            },
        )

    def test_antineutrino_codes_are_the_negated_neutrino_codes(self) -> None:
        for particle in ("nue", "numu", "nutau"):
            with self.subTest(particle=particle):
                self.assertEqual(
                    probe_pdg(f"{particle}bar", "test"), -probe_pdg(particle, "test")
                )

    def test_is_antineutrino_follows_the_pdg_sign(self) -> None:
        for particle, expected in (
            ("nue", False),
            ("numu", False),
            ("nutau", False),
            ("nuebar", True),
            ("numubar", True),
            ("nutaubar", True),
        ):
            with self.subTest(particle=particle):
                self.assertIs(is_antineutrino(particle), expected)

    def test_unknown_particle_names_the_context_and_the_known_flavours(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            probe_pdg("nu_mu", "GENIE")
        message = str(ctx.exception)
        self.assertIn("nu_mu", message)
        self.assertIn("GENIE", message)
        self.assertIn("numu", message)


if __name__ == "__main__":
    unittest.main()
