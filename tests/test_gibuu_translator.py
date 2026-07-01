from __future__ import annotations

import unittest

from neutrino_factory.config import resolve_config
from neutrino_factory.translators.gibuu import GiBUUTranslator, FLUX_NBINS


def _task() -> dict:
    return {
        "event_count": 100,
        "seed": 12345,
        "code_version": "release2025",
        "config_version": "default",
        "generator_version_id": "release2025+default",
    }


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
        jobcard = translated["gibuu_jobcard"]
        self.assertIn("nuXsectionMode = 16", jobcard)
        self.assertIn("nuExp          = 99", jobcard)
        self.assertIn("FileNameFlux   = '/work/flux.dat'", jobcard)
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
        jobcard = translated["gibuu_jobcard"]
        self.assertIsNone(translated["gibuu_flux_table"])
        self.assertIn("nuXsectionMode = 6", jobcard)
        self.assertIn("nuExp          = 0", jobcard)
        self.assertIn("&nl_SigmaMC", jobcard)
        self.assertIn("enu = 2.0000", jobcard)


if __name__ == "__main__":
    unittest.main()
