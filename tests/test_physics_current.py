"""How ``physics.current`` reaches each generator's native configuration.

The common output's ``is_cc`` column is only trustworthy if the generator was
actually restricted to the requested current, so these tests pin the native
switch each translator emits — the thing a real run depends on.
"""

from __future__ import annotations

import unittest

from neutrino_factory.translators.base import physics_current
from neutrino_factory.translators.genie import GenieTranslator
from neutrino_factory.translators.gibuu import GiBUUTranslator
from neutrino_factory.translators.neut import CRS_SLOT_CURRENTS, NeutTranslator
from neutrino_factory.translators.nuwro import NuWroTranslator

from .helpers import view_config


def _config(current: str, *, particle: str = "numu", nucleus: str = "C12") -> dict:
    return view_config(
        {
            "flux": {
                "type": "power_law",
                "particle": particle,
                "emin_gev": 0.5,
                "emax_gev": 5.0,
                "gamma": -2.0,
            },
            "target": {"nucleus": nucleus},
            "physics": {"mode": "inclusive", "current": current},
        }
    )


def _task(generator: str) -> dict:
    versions = {
        "genie": ("R-3_06_00", "G18_10a_02_11a"),
        "nuwro": ("nuwro_25.11", "default"),
        "neut": ("5.7.0-nuint2024", "default"),
        "gibuu": ("release2025", "default"),
    }
    code_version, config_version = versions[generator]
    return {
        "run_name": "test_run",
        "chunk_id": 0,
        "start_event": 0,
        "event_count": 10,
        "seed": 42,
        "code_version": code_version,
        "config_version": config_version,
        "generator_version_id": f"{code_version}+{config_version}",
    }


class PhysicsCurrentHelperTests(unittest.TestCase):
    def test_case_is_normalized(self) -> None:
        self.assertEqual(physics_current({"physics": {"current": "NC"}}), "nc")

    def test_unknown_current_raises_rather_than_defaulting(self) -> None:
        with self.assertRaises(ValueError):
            physics_current({"physics": {"current": "ccqe"}})


class GenieCurrentTests(unittest.TestCase):
    def test_event_generator_list_follows_current(self) -> None:
        for current, expected in (("cc", "CC"), ("nc", "NC"), ("inclusive", None)):
            with self.subTest(current=current):
                translated = GenieTranslator().translate(_config(current), _task("genie"))
                self.assertEqual(translated["current"], current)
                # None means the option is omitted, leaving gevgen's own default
                # list, which already covers both currents.
                self.assertEqual(translated["event_generator_list"], expected)

    def test_spline_sum_is_restricted_to_the_generated_current(self) -> None:
        # Two splines for the same (probe, target), one per current, each a
        # constant 1.0: summing both would double the reconstructed sigma.
        import tempfile
        from pathlib import Path

        import numpy as np

        xml = """<?xml version="1.0" encoding="ISO-8859-1"?>
<genie_xsec_spline_list>
  <spline name="genie::A/Default/nu:14;tgt:1000060120;N:2112;proc:Weak[CC],QES;" nknots="2">
    <knot> <E> 0.1 </E> <xsec> 1.0 </xsec> </knot>
    <knot> <E> 10.0 </E> <xsec> 1.0 </xsec> </knot>
  </spline>
  <spline name="genie::B/Default/nu:14;tgt:1000060120;N:2112;proc:Weak[NC],QES;" nknots="2">
    <knot> <E> 0.1 </E> <xsec> 1.0 </xsec> </knot>
    <knot> <E> 10.0 </E> <xsec> 1.0 </xsec> </knot>
  </spline>
</genie_xsec_spline_list>
"""
        energies = np.array([1.0, 2.0])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "xsecs.xml"
            path.write_text(xml, encoding="ISO-8859-1")

            both = GenieTranslator._sum_matching_splines(path, 14, 1000060120, energies)
            cc_only = GenieTranslator._sum_matching_splines(
                path, 14, 1000060120, energies, "proc:Weak[CC]"
            )
            nc_only = GenieTranslator._sum_matching_splines(
                path, 14, 1000060120, energies, "proc:Weak[NC]"
            )

        assert both is not None and cc_only is not None and nc_only is not None
        np.testing.assert_allclose(both, [2.0, 2.0])
        np.testing.assert_allclose(cc_only, [1.0, 1.0])
        np.testing.assert_allclose(nc_only, [1.0, 1.0])


class NuWroCurrentTests(unittest.TestCase):
    def _params(self, current: str) -> dict:
        translated = NuWroTranslator().translate(_config(current), _task("nuwro"))
        return translated["nuwro_params"]

    def test_cc_run_enables_only_charged_current_channels(self) -> None:
        params = self._params("cc")
        for channel in ("qel", "res", "dis", "coh", "mec"):
            self.assertEqual(params[f"dyn_{channel}_cc"], 1, channel)
            self.assertEqual(params[f"dyn_{channel}_nc"], 0, channel)
        self.assertEqual(params["dyn_hyp_cc"], 1)

    def test_nc_run_enables_only_neutral_current_channels(self) -> None:
        params = self._params("nc")
        for channel in ("qel", "res", "dis", "coh", "mec"):
            self.assertEqual(params[f"dyn_{channel}_cc"], 0, channel)
            self.assertEqual(params[f"dyn_{channel}_nc"], 1, channel)
        self.assertEqual(params["dyn_hyp_cc"], 0)

    def test_inclusive_run_enables_both(self) -> None:
        params = self._params("inclusive")
        for channel in ("qel", "res", "dis", "coh", "mec"):
            self.assertEqual(params[f"dyn_{channel}_cc"], 1, channel)
            self.assertEqual(params[f"dyn_{channel}_nc"], 1, channel)

    def test_neutrino_electron_scattering_is_off_for_every_current(self) -> None:
        # Its target is an atomic electron, so its cross section is not on the
        # per-nucleon normalization the common output uses.
        for current in ("cc", "nc", "inclusive"):
            with self.subTest(current=current):
                self.assertEqual(self._params(current)["dyn_lep"], 0)


class NeutCurrentTests(unittest.TestCase):
    def _card(self, current: str) -> dict:
        return NeutTranslator().translate(_config(current), _task("neut"))["neut_card"]

    def test_inclusive_uses_neuts_normal_mode(self) -> None:
        card = self._card("inclusive")
        self.assertEqual(card["NEUT-MODE"], 0)
        self.assertNotIn("NEUT-CRS", card)

    def test_single_current_masks_the_other_currents_channels(self) -> None:
        for current in ("cc", "nc"):
            with self.subTest(current=current):
                card = self._card(current)
                self.assertEqual(card["NEUT-MODE"], -1)
                for key, slots in (
                    ("NEUT-CRS", CRS_SLOT_CURRENTS["nu"]),
                    ("NEUT-CRSB", CRS_SLOT_CURRENTS["nubar"]),
                ):
                    factors = card[key].split()
                    self.assertEqual(len(factors), 30, key)
                    enabled = {
                        slot for slot, factor in zip(slots, factors) if factor == "1."
                    }
                    self.assertEqual(enabled, {current}, key)

    def test_every_slot_is_claimed_by_at_most_one_current(self) -> None:
        # A slot enabled by both masks would leak the other current into the run.
        for beam, slots in CRS_SLOT_CURRENTS.items():
            with self.subTest(beam=beam):
                self.assertEqual(len(slots), 30)
                self.assertTrue(set(slots) <= {"cc", "nc", None})


class GiBUUCurrentTests(unittest.TestCase):
    def _passes(self, current: str, particle: str = "numu") -> list[dict]:
        translated = GiBUUTranslator().translate(
            _config(current, particle=particle), _task("gibuu")
        )
        return translated["gibuu_passes"]

    def test_single_current_runs_one_pass_with_that_process_id(self) -> None:
        for current, process_id in (("cc", 2), ("nc", 3)):
            with self.subTest(current=current):
                passes = self._passes(current)
                self.assertEqual([p["current"] for p in passes], [current])
                self.assertIn(f"process_ID     = {process_id}", passes[0]["jobcard"])

    def test_inclusive_runs_one_pass_per_current(self) -> None:
        passes = self._passes("inclusive")
        self.assertEqual([p["current"] for p in passes], ["cc", "nc"])
        self.assertIn("process_ID     = 2", passes[0]["jobcard"])
        self.assertIn("process_ID     = 3", passes[1]["jobcard"])

    def test_inclusive_passes_do_not_share_a_seed(self) -> None:
        # Identical seeds would draw the identical random sequence in both runs.
        passes = self._passes("inclusive")
        seeds = [
            line for jobcard in (p["jobcard"] for p in passes)
            for line in jobcard.splitlines() if "SEED" in line
        ]
        self.assertEqual(len(seeds), 2)
        self.assertNotEqual(seeds[0], seeds[1])

    def test_antineutrino_negates_every_pass(self) -> None:
        # For every antineutrino flavour: the sign comes from the probe's PDG
        # code, so it must not depend on how the flavour happens to be spelled.
        for particle in ("nuebar", "numubar", "nutaubar"):
            with self.subTest(particle=particle):
                passes = self._passes("inclusive", particle=particle)
                self.assertIn("process_ID     = -2", passes[0]["jobcard"])
                self.assertIn("process_ID     = -3", passes[1]["jobcard"])

    def test_neutrino_flavours_keep_a_positive_process_id(self) -> None:
        for particle, flavor_id in (("nue", 1), ("numu", 2), ("nutau", 3)):
            with self.subTest(particle=particle):
                passes = self._passes("inclusive", particle=particle)
                self.assertIn("process_ID     = 2", passes[0]["jobcard"])
                self.assertIn(f"flavor_ID      = {flavor_id}", passes[0]["jobcard"])


if __name__ == "__main__":
    unittest.main()
