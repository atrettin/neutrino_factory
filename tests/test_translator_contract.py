"""Contract every translator must satisfy, checked across all generators.

The per-generator test modules cover each translator's own physics. This one
guards the shared interface, so a newly added generator cannot quietly skip a
piece of it — in particular ``xsec_norm_count``, which merging depends on and
whose absence would silently reintroduce the N-chunks-report-N-times-sigma bug.
"""

from __future__ import annotations

import unittest

from neutrino_factory.translators.base import ConfigTranslator
from neutrino_factory.translators.genie import GenieTranslator
from neutrino_factory.translators.gibuu import GiBUUTranslator, NUM_RUNS_SAME_ENERGY
from neutrino_factory.translators.neut import NeutTranslator
from neutrino_factory.translators.nuwro import NuWroTranslator

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


if __name__ == "__main__":
    unittest.main()
