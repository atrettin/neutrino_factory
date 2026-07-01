from __future__ import annotations

import unittest

from neutrino_factory.config import resolve_config
from neutrino_factory.translators.nuwro import NuWroTranslator, FLUX_NBINS


def _task() -> dict:
    return {
        "event_count": 10,
        "seed": 42,
        "code_version": "nuwro_25.11",
        "config_version": "default",
        "generator_version_id": "nuwro_25.11+default",
    }


class NuWroTranslatorFluxTests(unittest.TestCase):
    def _config(self, **flux_overrides) -> dict:
        flux = {
            "type": "power_law",
            "particle": "numu",
            "emin_gev": 0.5,
            "emax_gev": 5.0,
            "gamma": -2.0,
        }
        flux.update(flux_overrides)
        return resolve_config({"flux": flux, "target": {"nucleus": "Ar40"}})

    def test_power_law_renders_histogram_beam_energy(self) -> None:
        translated = NuWroTranslator().translate(self._config(), _task())
        params = translated["nuwro_params"]
        self.assertEqual(params["beam_type"], 0)

        beam_energy = params["beam_energy"]
        self.assertTrue(beam_energy.startswith("500.0 5000.0 "))
        weights = [float(x) for x in beam_energy.split()[2:]]
        self.assertEqual(len(weights), FLUX_NBINS)
        # Falling E^-2 spectrum -> strictly decreasing bin weights.
        self.assertTrue(all(b < a for a, b in zip(weights, weights[1:])))

    def test_monoenergetic_when_range_degenerate(self) -> None:
        translated = NuWroTranslator().translate(
            self._config(emin_gev=2.0, emax_gev=2.0), _task()
        )
        beam_energy = translated["nuwro_params"]["beam_energy"]
        # Single value in MeV, no histogram bins.
        self.assertEqual(beam_energy, "2000.0")


if __name__ == "__main__":
    unittest.main()
