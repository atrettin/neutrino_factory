from __future__ import annotations

import re
import unittest

import numpy as np

from neutrino_factory.flux import build_flux
from neutrino_factory.config import ConfigError, resolve_config
from neutrino_factory.translators.gibuu import (
    FLUX_NBINS,
    INT32_MAX,
    MAX_QES_OVERSAMPLING,
    GiBUUTranslator,
)

from .helpers import job, run_config, view_config


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
        return view_config({"flux": flux, "target": {"nucleus": "C12"}})

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

    def test_jobcard_enables_every_available_reaction_channel(self) -> None:
        # All of these default to .false. in GiBUU except includeQE, and the
        # output is meant to be inclusive. include2pi in particular was missing
        # and biased the total cross section low.
        jobcard = _only_jobcard(GiBUUTranslator().translate(self._config(), _task()))
        for switch in (
            "includeQE",
            "includeDELTA",
            "includeRES",
            "includeDIS",
            "include1pi",
            "include2pi",
            "include2p2hQE",
        ):
            with self.subTest(switch=switch):
                self.assertRegex(jobcard, rf"{switch}\s*=\s*\.true\.")
        # 2p2h Delta is unpublished: release2025 aborts the run if it is on.
        self.assertRegex(jobcard, r"include2p2hDelta\s*=\s*\.false\.")

    def test_jobcard_runs_fsi_transport(self) -> None:
        # The final-state columns need the post-transport particle list;
        # numTimeSteps = 0 would leave it at the primary vertex.
        jobcard = _only_jobcard(GiBUUTranslator().translate(self._config(), _task()))
        self.assertRegex(jobcard, r"numTimeSteps\s*=\s*150\b")
        self.assertRegex(jobcard, r"delta_T\s*=\s*0\.2\b")

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
        return view_config({"flux": flux, "target": {"nucleus": "C12"}})

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


GIBUU_JOB = {"generator": "gibuu", "code_version": "release2025", "config_version": "default"}
OTHER_CHANNELS = (
    "includeDELTA", "includeRES", "includeDIS", "include1pi", "include2pi", "include2p2hQE",
)


class GiBUUQesOversamplingTests(unittest.TestCase):
    def _config(self, current: str = "inclusive", **section) -> dict:
        job = {
            **GIBUU_JOB,
            "flux": {
                "type": "power_law",
                "particle": "numu",
                "emin_gev": 0.1,
                "emax_gev": 500.0,
                "gamma": -1.0,
            },
            "target": {"nucleus": "C12"},
            "physics": {"mode": "inclusive", "current": current},
        }
        if section:
            job["gibuu"] = section
        return view_config(job)

    def test_no_oversampling_leaves_the_passes_as_they_were(self) -> None:
        unset = GiBUUTranslator().translate(self._config(), _task())
        zero = GiBUUTranslator().translate(self._config(qes_oversampling=0), _task())
        self.assertEqual(unset["gibuu_passes"], zero["gibuu_passes"])
        self.assertEqual(
            [(p["name"], p["current"], p["channels"]) for p in zero["gibuu_passes"]],
            [("cc", "cc", "all"), ("nc", "nc", "all")],
        )
        self.assertEqual(zero["qes_oversampling"], 0)

    def test_qe_passes_follow_the_full_passes(self) -> None:
        translated = GiBUUTranslator().translate(self._config(qes_oversampling=3), _task())
        passes = translated["gibuu_passes"]
        self.assertEqual(
            [(p["name"], p["current"], p["channels"]) for p in passes],
            [
                ("cc", "cc", "all"),
                ("nc", "nc", "all"),
                ("cc_qe01", "cc", "qe"),
                ("cc_qe02", "cc", "qe"),
                ("cc_qe03", "cc", "qe"),
                ("nc_qe01", "nc", "qe"),
                ("nc_qe02", "nc", "qe"),
                ("nc_qe03", "nc", "qe"),
            ],
        )
        self.assertEqual(translated["qes_oversampling"], 3)

    def test_full_pass_jobcards_do_not_depend_on_the_oversampling(self) -> None:
        plain = GiBUUTranslator().translate(self._config(), _task())
        oversampled = GiBUUTranslator().translate(self._config(qes_oversampling=3), _task())
        self.assertEqual(oversampled["gibuu_passes"][:2], plain["gibuu_passes"])

    def test_qe_pass_jobcard_enables_only_qe(self) -> None:
        translated = GiBUUTranslator().translate(self._config(qes_oversampling=1), _task())
        full, qe = translated["gibuu_passes"][0], translated["gibuu_passes"][2]
        self.assertRegex(qe["jobcard"], r"includeQE\s*=\s*\.true\.")
        for switch in OTHER_CHANNELS:
            with self.subTest(switch=switch):
                self.assertRegex(qe["jobcard"], rf"{switch}\s*=\s*\.false\.")
        # Same current, same ensembles: only the channels and the seed differ.
        for pattern in (r"numEnsembles\s*=\s*-?\d+", r"process_ID\s*=\s*-?\d+"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    re.findall(pattern, qe["jobcard"]), re.findall(pattern, full["jobcard"])
                )

    def test_every_pass_has_its_own_int32_seed(self) -> None:
        task = {**_task(), "seed": 2_000_000_000}
        translated = GiBUUTranslator().translate(
            self._config(qes_oversampling=MAX_QES_OVERSAMPLING), task
        )
        seeds = [
            int(re.findall(r"SEED = (\d+)", p["jobcard"])[0])
            for p in translated["gibuu_passes"]
        ]
        self.assertEqual(len(seeds), 2 * (MAX_QES_OVERSAMPLING + 1))
        self.assertEqual(len(set(seeds)), len(seeds))
        self.assertLessEqual(max(seeds), INT32_MAX)

    def test_single_current_run_oversamples_only_that_current(self) -> None:
        translated = GiBUUTranslator().translate(
            self._config("nc", qes_oversampling=2), _task()
        )
        self.assertEqual(
            [p["name"] for p in translated["gibuu_passes"]], ["nc", "nc_qe01", "nc_qe02"]
        )

    def test_translate_rejects_an_invalid_hand_built_section(self) -> None:
        config = self._config()
        config["job"]["gibuu"] = {"qes_oversampling": -1}
        with self.assertRaisesRegex(ValueError, "qes_oversampling"):
            GiBUUTranslator().translate(config, _task())


class GiBUUSectionValidationTests(unittest.TestCase):
    def _job(self, **overrides) -> dict:
        return {
            **GIBUU_JOB,
            "flux": {"type": "power_law", "particle": "numu", "emin_gev": 0.5,
                     "emax_gev": 5.0, "gamma": -1.0},
            "target": {"nucleus": "C12"},
            **overrides,
        }

    def test_valid_section_is_accepted(self) -> None:
        config = run_config([self._job(gibuu={"qes_oversampling": 10})])
        self.assertEqual(config["jobs"][0]["gibuu"], {"qes_oversampling": 10})

    def test_invalid_values_are_rejected(self) -> None:
        for value in (-1, MAX_QES_OVERSAMPLING + 1, 2.5, "10", True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "gibuu.qes_oversampling must be"):
                    run_config([self._job(gibuu={"qes_oversampling": value})])

    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key 'gibuu.oversampling'"):
            run_config([self._job(gibuu={"oversampling": 10})])

    def test_section_is_only_valid_on_a_gibuu_job(self) -> None:
        with self.assertRaisesRegex(ConfigError, "only valid on a gibuu job"):
            run_config([job(gibuu={"qes_oversampling": 10})])

    def test_section_can_be_templated_by_a_macro(self) -> None:
        config = resolve_config(
            {
                "macros": {
                    "gibuu": {
                        "params": {"k": None},
                        "job": self._job(gibuu={"qes_oversampling": "{{k}}"}),
                    }
                },
                "jobs": [{"use": "gibuu", "with": {"k": 7}}],
            }
        )
        self.assertEqual(config["jobs"][0]["gibuu"], {"qes_oversampling": 7})


if __name__ == "__main__":
    unittest.main()
