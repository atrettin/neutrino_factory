"""Contract every translator must satisfy, checked across all generators.

The per-generator test modules cover each translator's own physics. This one
guards the shared interface, so a newly added generator cannot quietly skip a
piece of it — in particular ``xsec_norm_count``, which merging depends on and
whose absence would silently reintroduce the N-chunks-report-N-times-sigma bug.
"""

from __future__ import annotations

import unittest

from neutrino_factory.particles import PARTICLE_PDG
from neutrino_factory.translators.base import ConfigTranslator
from neutrino_factory.translators.genie import GenieTranslator
from neutrino_factory.translators.gibuu import (
    FLAVOR_ID,
    GiBUUTranslator,
    NUM_RUNS_SAME_ENERGY,
)
from neutrino_factory.translators.neut import NeutTranslator
from neutrino_factory.translators.nuwro import NuWroTranslator

from .helpers import view_config

TRANSLATORS = (GenieTranslator, GiBUUTranslator, NeutTranslator, NuWroTranslator)


class TranslatorContractTests(unittest.TestCase):
    def test_every_translator_is_instantiable(self) -> None:
        # ConfigTranslator's methods are abstract, so a translator that forgets
        # one cannot be constructed at all — a regression that has shipped twice.
        for translator_class in TRANSLATORS:
            with self.subTest(translator=translator_class.__name__):
                self.assertIsInstance(translator_class(), ConfigTranslator)

    def test_every_translator_declares_a_positive_norm_count(self) -> None:
        for translator_class in TRANSLATORS:
            with self.subTest(translator=translator_class.__name__):
                count = translator_class().xsec_norm_count({"num_runs": 1}, 500)
                self.assertGreater(count, 0.0)
                self.assertIsInstance(count, float)

    def test_sampled_generators_normalize_by_event_count(self) -> None:
        # GENIE, NuWro and NEUT are unweighted/rejection-sampled: one event is
        # one sample of the estimator.
        for translator_class in (GenieTranslator, NeutTranslator, NuWroTranslator):
            with self.subTest(translator=translator_class.__name__):
                self.assertEqual(translator_class().xsec_norm_count({}, 750), 750.0)

    def test_gibuu_normalizes_by_run_count_not_event_count(self) -> None:
        """GiBUU is the reason this is not just ``len(events)``.

        Its per-event weights already sum to sigma within one run, so a chunk's
        weight in a merged average is its run multiplicity. Using the event count
        would weight chunks by how many interactions GiBUU happened to produce,
        which itself varies with the cross section.
        """
        count = GiBUUTranslator().xsec_norm_count({"num_runs": NUM_RUNS_SAME_ENERGY}, 9999)
        self.assertEqual(count, float(NUM_RUNS_SAME_ENERGY))
        self.assertNotEqual(count, 9999.0)

    def test_gibuu_norm_count_follows_num_runs(self) -> None:
        self.assertEqual(GiBUUTranslator().xsec_norm_count({"num_runs": 4}, 100), 4.0)
        # Missing or nonsensical values fall back to a single run, matching
        # compute_xsec_weight's own guard, so the two can never disagree.
        self.assertEqual(GiBUUTranslator().xsec_norm_count({}, 100), 1.0)
        self.assertEqual(GiBUUTranslator().xsec_norm_count({"num_runs": 0}, 100), 1.0)


class TranslatorFlavourTests(unittest.TestCase):
    """Every generator must be runnable for every probe the framework accepts."""

    def _task(self, generator: str) -> dict:
        code_version, config_version = {
            "genie": ("R-3_06_00", "G18_10a_02_11a"),
            "gibuu": ("release2025", "default"),
            "neut": ("5.7.0-nuint2024", "default"),
            "nuwro": ("nuwro_25.11", "default"),
        }[generator]
        return {
            "event_count": 10,
            "seed": 42,
            "code_version": code_version,
            "config_version": config_version,
            "generator_version_id": f"{code_version}+{config_version}",
        }

    def _config(self, particle: str) -> dict:
        return view_config(
            {
                "flux": {
                    "type": "power_law",
                    "particle": particle,
                    "emin_gev": 0.5,
                    "emax_gev": 5.0,
                    "gamma": -2.0,
                },
                "target": {"nucleus": "C12"},
            }
        )

    def test_every_translator_accepts_every_flavour(self) -> None:
        for translator_class in TRANSLATORS:
            for particle, pdg in PARTICLE_PDG.items():
                with self.subTest(
                    translator=translator_class.__name__, particle=particle
                ):
                    translator = translator_class()
                    config = self._config(particle)
                    translated = translator.translate(
                        config, self._task(translator.name)
                    )
                    # Each generator takes the probe in its own form: GENIE and
                    # NEUT as a PDG code, NuWro as beam_particle, GiBUU as a
                    # flavour ID plus the sign of process_ID.
                    if translator.name == "nuwro":
                        self.assertEqual(
                            translated["nuwro_params"]["beam_particle"], pdg
                        )
                    elif translator.name == "gibuu":
                        for gibuu_pass in translated["gibuu_passes"]:
                            jobcard = gibuu_pass["jobcard"]
                            self.assertIn(
                                f"flavor_ID      = {FLAVOR_ID[particle]}", jobcard
                            )
                            magnitude = 2 if gibuu_pass["current"] == "cc" else 3
                            sign = -1 if pdg < 0 else 1
                            self.assertIn(
                                f"process_ID     = {sign * magnitude}", jobcard
                            )
                    else:
                        self.assertEqual(translated["probe_pdg"], pdg)

    def test_gibuu_flavour_table_covers_exactly_the_supported_probes(self) -> None:
        # GiBUU keeps its own flavour_ID table because the IDs are its own; it
        # must not drift out of step with the framework's probe table.
        self.assertEqual(set(FLAVOR_ID), set(PARTICLE_PDG))

    def test_every_translator_rejects_an_unknown_flavour(self) -> None:
        # validate_config rejects this first, so a translator only ever sees an
        # unknown name if it is called directly — it must still say which name
        # it could not use rather than raise a bare lookup error.
        for translator_class in TRANSLATORS:
            with self.subTest(translator=translator_class.__name__):
                translator = translator_class()
                config = self._config("numu")
                config["flux"]["particle"] = "nu_mu"
                with self.assertRaises(KeyError) as ctx:
                    translator.translate(config, self._task(translator.name))
                self.assertIn("nu_mu", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
