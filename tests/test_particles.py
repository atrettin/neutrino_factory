from __future__ import annotations

import unittest

from neutrino_factory.particles import (
    PARTICLE_PDG,
    is_antineutrino,
    nucleus_composition,
    nucleus_pdg,
    nucleus_z_a,
    probe_pdg,
)


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


class NucleusTableTests(unittest.TestCase):
    def test_pdg_codes_match_the_hand_written_values_they_replaced(self) -> None:
        # These are the codes the shipped configs used to spell out by hand.
        for nucleus, pdg in (
            ("H1", 1000010010),
            ("C12", 1000060120),
            ("O16", 1000080160),
            ("Ar40", 1000180400),
            ("Ca40", 1000200400),
            ("Fe56", 1000260560),
        ):
            with self.subTest(nucleus=nucleus):
                self.assertEqual(nucleus_pdg(nucleus), pdg)

    def test_composition_reproduces_the_retired_lookup_table(self) -> None:
        # The five entries the generators' own NUCLEUS_COMPOSITION table held
        # before composition became derived from the name.
        for nucleus, composition in (
            ("Ar40", (18, 22)),
            ("C12", (6, 6)),
            ("O16", (8, 8)),
            ("Fe56", (26, 30)),
            ("Ca40", (20, 20)),
        ):
            with self.subTest(nucleus=nucleus):
                self.assertEqual(nucleus_composition(nucleus), composition)

    def test_nuclei_outside_the_retired_table_now_work(self) -> None:
        self.assertEqual(nucleus_z_a("Xe136"), (54, 136))
        self.assertEqual(nucleus_composition("Pb208"), (82, 126))

    def test_unparsable_name_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            nucleus_pdg("carbon12")
        self.assertIn("carbon12", str(ctx.exception))

    def test_unknown_element_symbol_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            nucleus_pdg("Xx12")
        self.assertIn("Xx", str(ctx.exception))

    def test_mass_number_below_the_proton_number_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            nucleus_pdg("C4")
        self.assertIn("C4", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
