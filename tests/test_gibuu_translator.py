from __future__ import annotations

import unittest

import numpy as np

from neutrino_factory.config import resolve_config
from neutrino_factory.flux import build_flux
from neutrino_factory.translators.gibuu import GiBUUTranslator, FLUX_NBINS


def _task() -> dict:
    return {
        "event_count": 100,
        "seed": 12345,
        "code_version": "release2025",
        "config_version": "default",
        "generator_version_id": "release2025+default",
    }


def _only_jobcard(translated: dict) -> str:
    """The jobcard of a single-current run (one pass)."""
    passes = translated["gibuu_passes"]
    assert len(passes) == 1, passes
    return passes[0]["jobcard"]


class GiBUUTranslatorFluxTests(unittest.TestCase):
    def _config(self, **flux_overrides) -> dict:
        flux = {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": -2.0,
        }
        flux.update(flux_overrides)
        return resolve_config({"flux": flux, "target": {"nucleus": "C12"}})

    def test_power_law_uses_user_flux_jobcard_and_table(self) -> None:
        translated = GiBUUTranslator().translate(self._config(), _task())
        jobcard = _only_jobcard(translated)
        self.assertIn("nuXsectionMode = 16", jobcard)
        self.assertIn("nuExp          = 99", jobcard)
        self.assertIn("FileNameFlux   = './flux.dat'", jobcard)
        # Fixed-energy namelist must not be present in flux mode.
        self.assertNotIn("&nl_SigmaMC", jobcard)

        table = translated["gibuu_flux_table"]
        rows = [ln for ln in table.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(rows), FLUX_NBINS)
        energies = [float(ln.split()[0]) for ln in rows]
        # Equidistant, ascending bin centers within the flux support (GeV).
        self.assertGreater(energies[0], 0.5)
        self.assertLess(energies[-1], 5.0)
        spacings = [b - a for a, b in zip(energies, energies[1:])]
        self.assertTrue(all(abs(s - spacings[0]) < 1e-9 for s in spacings))

    def test_monoenergetic_fallback_when_range_degenerate(self) -> None:
        translated = GiBUUTranslator().translate(
            self._config(emin_gev=2.0, emax_gev=2.0), _task()
        )
        jobcard = _only_jobcard(translated)
        self.assertIsNone(translated["gibuu_flux_table"])
        self.assertIn("nuXsectionMode = 6", jobcard)
        self.assertIn("nuExp          = 0", jobcard)
        self.assertIn("&nl_SigmaMC", jobcard)
        self.assertIn("enu = 2.0000", jobcard)


class GiBUUTranslatorXsecWeightTests(unittest.TestCase):
    def _config(self, **flux_overrides) -> dict:
        flux = {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": 0.0,
        }
        flux.update(flux_overrides)
        return resolve_config({"flux": flux, "target": {"nucleus": "C12"}})

    def test_translator_is_instantiable(self) -> None:
        # compute_xsec_weight is now implemented, so the abstract base no longer
        # blocks instantiation (previously raised TypeError).
        self.assertIsInstance(GiBUUTranslator(), GiBUUTranslator)

    def test_translate_exposes_flux_config_and_num_runs(self) -> None:
        translated = GiBUUTranslator().translate(self._config(), _task())
        self.assertIn("flux_config", translated)
        self.assertEqual(translated["flux_config"]["type"], "power_law")
        self.assertEqual(translated["num_runs"], 1)
        # num_runs in the returned dict must match the jobcard so they can't drift.
        self.assertIn("num_runs_SameEnergy = 1", _only_jobcard(translated))

    def test_xsec_weight_flat_flux_hand_derivation(self) -> None:
        # Flat flux: flux_hat is constant at 1/(emax-emin), so
        # xsec_weight = raw / (num_runs * flux_hat) = raw * (emax - emin).
        config = self._config()
        translated = GiBUUTranslator().translate(config, _task())
        flux = build_flux(config["flux"])
        energies = np.array([1.0, 2.5, 4.0])
        raw = np.array([1.0, 0.8, 1.2])
        weights = GiBUUTranslator().compute_xsec_weight(energies, raw, translated, flux)
        width = 5.0 - 0.5
        np.testing.assert_allclose(weights, raw * width, rtol=1e-6)

    def test_xsec_weight_preserves_negative_interference_weights(self) -> None:
        config = self._config()
        translated = GiBUUTranslator().translate(config, _task())
        flux = build_flux(config["flux"])
        energies = np.array([1.0, 2.5])
        raw = np.array([1.0, -0.3])
        weights = GiBUUTranslator().compute_xsec_weight(energies, raw, translated, flux)
        self.assertLess(weights[1], 0.0)

    def test_xsec_weight_monoenergetic_divides_by_num_runs_only(self) -> None:
        config = self._config(emin_gev=2.0, emax_gev=2.0)
        translated = GiBUUTranslator().translate(config, _task())
        flux = build_flux(config["flux"])
        energies = np.array([2.0, 2.0])
        raw = np.array([1.0, 0.5])
        weights = GiBUUTranslator().compute_xsec_weight(energies, raw, translated, flux)
        # num_runs = 1, no flux division for a single-energy run.
        np.testing.assert_allclose(weights, raw, rtol=1e-6)


if __name__ == "__main__":
    unittest.main()
