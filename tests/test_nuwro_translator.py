from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from neutrino_factory import catalog
from neutrino_factory.cli import build_parser
from neutrino_factory.config import ConfigError, ensure_no_legacy_versions
from neutrino_factory.translators.nuwro import NuWroTranslator, FLUX_NBINS

from .helpers import job, run_config, view_config


def _task() -> dict:
    return {
        "event_count": 10,
        "seed": 42,
        "code_version": "nuwro_25.11",
        "config_version": "sf",
        "generator_version_id": "nuwro_25.11+sf",
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
        return view_config({"flux": flux, "target": {"nucleus": "Ar40"}, "generator": "nuwro",
                            "code_version": "nuwro_25.11", "config_version": "sf"})

    def test_power_law_renders_histogram_beam_energy(self) -> None:
        translated = NuWroTranslator().translate(self._config(), _task())
        params = translated["nuwro_params"]
        self.assertEqual(params["beam_type"], 0)
        # The cascade fills e/post, which the final-state columns are read from.
        self.assertEqual(params["FSI_on"], 1)

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


def _nuwro_job(config_version: str, nucleus: str) -> dict:
    return job(
        generator="nuwro",
        code_version="nuwro_25.11",
        config_version=config_version,
        target={"nucleus": nucleus},
    )


class NuWroConfigVersionTests(unittest.TestCase):
    """``sf`` and ``lfg`` differ only in the QE nuclear model; ``sf`` exists for four nuclei."""

    def _params(self, config_version: str, nucleus: str) -> dict:
        task = dict(_task(), config_version=config_version)
        config = view_config(_nuwro_job(config_version, nucleus))
        return NuWroTranslator().translate(config, task)["nuwro_params"]

    def test_sf_and_lfg_pin_the_qe_model(self) -> None:
        sf = self._params("sf", "C12")
        lfg = self._params("lfg", "C12")
        self.assertEqual((sf["sf_method"], sf["nucleus_target"]), (1, 2))
        self.assertEqual((lfg["sf_method"], lfg["nucleus_target"]), (0, 2))
        # Nothing else differs between the two.
        self.assertEqual(
            {k: v for k, v in sf.items() if k != "sf_method"},
            {k: v for k, v in lfg.items() if k != "sf_method"},
        )

    def test_lfg_accepts_every_nucleus(self) -> None:
        for nucleus in ("H1", "C12", "W184"):
            self.assertEqual(self._params("lfg", nucleus)["sf_method"], 0)

    def test_sf_refuses_a_nucleus_without_spectral_function(self) -> None:
        for nucleus in ("H1", "W184"):
            with self.subTest(nucleus=nucleus):
                with self.assertRaisesRegex(ConfigError, f"cannot predict {nucleus}"):
                    run_config([_nuwro_job("sf", nucleus)])
                # The translator refuses too, should a task bypass validation.
                config = view_config(_nuwro_job("lfg", nucleus))
                with self.assertRaisesRegex(ValueError, "cannot predict"):
                    NuWroTranslator().translate(config, _task())

    def test_sf_accepts_its_four_nuclei(self) -> None:
        for nucleus in ("C12", "O16", "Ar40", "Fe56"):
            self.assertEqual(self._params("sf", nucleus)["sf_method"], 1)

    def test_legacy_default_loads_but_cannot_be_submitted(self) -> None:
        with self.assertLogs("neutrino_factory.config", "WARNING") as logs:
            config = run_config([_nuwro_job("default", "W184")])
        self.assertIn("is legacy", "\n".join(logs.output))
        with self.assertRaisesRegex(ConfigError, "legacy and cannot be submitted"):
            ensure_no_legacy_versions(config)
        ensure_no_legacy_versions(run_config([_nuwro_job("lfg", "W184")]))
        # It is not offered to new runs, but stays known for old ones.
        self.assertEqual(catalog.available_config_versions("nuwro", "nuwro_25.11"), ["sf", "lfg"])
        catalog.ensure_compatible("nuwro", "nuwro_25.11", "default")

    def test_legacy_default_writes_no_qe_model(self) -> None:
        # Legacy tasks must stay bit-identical to the runs they belong to.
        params = self._params("default", "C12")
        self.assertNotIn("sf_method", params)
        self.assertNotIn("nucleus_target", params)

    def test_submit_refuses_legacy_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.yaml"
            path.write_text(
                yaml.safe_dump({"jobs": [_nuwro_job("default", "C12")]}), encoding="utf-8"
            )
            args = build_parser().parse_args(["submit", "--config", str(path), "--dry-run"])
            with patch("neutrino_factory.cli.write_manifest") as write_manifest:
                with self.assertRaisesRegex(ConfigError, "legacy"):
                    args.func(args)
            write_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
