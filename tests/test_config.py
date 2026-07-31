from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.config import (
    LOG_LEVELS,
    PHYSICS_CURRENTS,
    ConfigError,
    resolve_config,
)

from .helpers import job, run_config


def _write_root_histogram(path: Path, name: str, edges, contents) -> None:
    import uproot

    with uproot.recreate(path) as handle:
        handle[name] = (np.asarray(contents, dtype=np.float64), np.asarray(edges, dtype=np.float64))


class ConfigTests(unittest.TestCase):
    def test_env_expansion_and_enabled_generators(self) -> None:
        with patch.dict(os.environ, {"NF_OUTPUT_ROOT": "/tmp/nf-output"}, clear=False):
            config = run_config(
                [
                    job(),
                    job(
                        generator="nuwro",
                        code_version="nuwro_25.11",
                        config_version="default",
                    ),
                ]
            )

        self.assertEqual(config["storage"]["output_root"], "/tmp/nf-output")
        self.assertEqual(config["enabled_generators"], ["genie", "nuwro"])
        first = config["jobs"][0]
        self.assertEqual(first["code_version"], "R-3_06_00")
        self.assertEqual(first["config_version"], "G18_10a_02_11a")
        self.assertEqual(first["label"], "genie_R-3_06_00_G18_10a_02_11a_numu_Ar40_cc")

    def test_missing_code_or_config_version_raises(self) -> None:
        broken = job()
        del broken["config_version"]
        with self.assertRaises(ConfigError):
            run_config([broken])

    def test_unknown_code_version_raises(self) -> None:
        with self.assertRaises(ConfigError):
            run_config([job(code_version="R-9_99_99")])

    def test_unknown_generator_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            run_config([job(generator="geni")])
        self.assertIn("geni", str(ctx.exception))

    def test_incompatible_config_version_raises(self) -> None:
        # GENIE validation is availability-only (tunes are not enumerated in
        # advance), so an incompatible config_version is checked against a
        # generator that declares a static config-version set: NuWro's "default".
        with self.assertRaises(ConfigError):
            run_config(
                [
                    job(
                        generator="nuwro",
                        code_version="nuwro_25.11",
                        config_version="not_a_real_config",
                    )
                ]
            )

    def test_error_message_names_the_offending_job(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            run_config([job(), job(generator="nuwro", code_version="bogus")])
        self.assertIn("jobs[1]", str(ctx.exception))

    def _genie_config(self, software_root: str, stub_mode: bool, tune: str) -> dict:
        return {
            "run": {"stub_mode": stub_mode},
            "storage": {"software_root": software_root},
            "jobs": [job(config_version=tune)],
        }

    @staticmethod
    def _stage_xsecs(software_root: Path, tune_dir: str) -> None:
        dest = software_root / "genie" / "genie_xsec" / "R-3_06_00" / tune_dir / "xsecs.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("<?xml version='1.0'?>", encoding="utf-8")

    def test_non_stub_requires_staged_genie_spline(self) -> None:
        # Real (non-stub) run: an unstaged tune must fail validation so a valid
        # config is guaranteed to actually run.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                resolve_config(
                    self._genie_config(tmp, stub_mode=False, tune="G18_10a_02_11a")
                )

    def test_non_stub_accepts_staged_genie_spline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._stage_xsecs(Path(tmp), "G1810a0211a")
            config = resolve_config(
                self._genie_config(tmp, stub_mode=False, tune="G18_10a_02_11a")
            )
            self.assertEqual(config["enabled_generators"], ["genie"])

    def test_stub_mode_skips_genie_availability(self) -> None:
        # Same unstaged tune passes under stub mode (relaxed validation).
        with tempfile.TemporaryDirectory() as tmp:
            config = resolve_config(
                self._genie_config(tmp, stub_mode=True, tune="G18_10a_02_11a")
            )
            self.assertEqual(config["enabled_generators"], ["genie"])

    def test_config_without_jobs_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            resolve_config({"jobs": []})
        self.assertIn("At least one job", str(ctx.exception))

    def test_zero_events_raises(self) -> None:
        with self.assertRaises(ConfigError):
            run_config([job(events=0)])

    def test_more_chunks_than_events_raises(self) -> None:
        # Splitting 4 events into 10 chunks would silently drop the 6 empty
        # ones, producing fewer tasks than the config asks for.
        with self.assertRaises(ConfigError) as ctx:
            run_config([job(events=4, chunks=10)])
        self.assertIn("exceeds events", str(ctx.exception))

    def test_log_level_defaults_to_generator_stock_logging(self) -> None:
        config = run_config()
        self.assertEqual(config["run"]["log_level"], "default")

    def test_known_log_levels_are_accepted(self) -> None:
        for log_level in LOG_LEVELS:
            with self.subTest(log_level=log_level):
                config = run_config(run={"log_level": log_level})
                self.assertEqual(config["run"]["log_level"], log_level)

    def test_unknown_log_level_raises(self) -> None:
        with self.assertRaises(ConfigError):
            run_config(run={"log_level": "bogus"})

    def test_per_job_log_level_is_validated(self) -> None:
        with self.assertRaises(ConfigError):
            run_config([job(log_level="bogus")])

    def test_current_defaults_to_cc(self) -> None:
        config = resolve_config({"jobs": [{"generator": "genie",
                                           "code_version": "R-3_06_00",
                                           "config_version": "G18_10a_02_11a"}]})
        self.assertEqual(config["jobs"][0]["physics"]["current"], "cc")

    def test_known_currents_are_accepted(self) -> None:
        for current in PHYSICS_CURRENTS:
            with self.subTest(current=current):
                config = run_config(
                    [job(physics={"mode": "inclusive", "current": current})]
                )
                self.assertEqual(config["jobs"][0]["physics"]["current"], current)

    def test_unknown_current_raises(self) -> None:
        # Silently falling back to "cc" would generate different physics than
        # the config asks for.
        with self.assertRaises(ConfigError):
            run_config([job(physics={"mode": "inclusive", "current": "ccqe"})])

    def test_target_pdg_is_derived_from_the_nucleus(self) -> None:
        config = run_config([job(target={"nucleus": "C12"})])
        self.assertEqual(config["jobs"][0]["target"]["pdg"], 1000060120)

    def test_explicit_target_pdg_is_kept(self) -> None:
        config = run_config([job(target={"nucleus": "C12", "pdg": 999})])
        self.assertEqual(config["jobs"][0]["target"]["pdg"], 999)

    def test_unparsable_nucleus_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            run_config([job(target={"nucleus": "carbon"})])
        self.assertIn("carbon", str(ctx.exception))

    def test_histogram_flux_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            flux_path = Path(tmpdir) / "flux.root"
            _write_root_histogram(flux_path, "numu_flux", [0.5, 1.0, 2.0, 4.0], [10.0, 4.0, 1.0])
            config = run_config(
                [
                    job(
                        flux={
                            "type": "histogram",
                            "particle": "numu",
                            "histogram_file": str(flux_path),
                            "histogram_name": "numu_flux",
                        }
                    )
                ]
            )
        flux = config["jobs"][0]["flux"]
        self.assertEqual(flux["type"], "histogram")
        # The power-law defaults must not be merged under a histogram flux: the
        # result would describe two different fluxes at once.
        self.assertNotIn("gamma", flux)

    def test_every_supported_flavour_validates(self) -> None:
        for particle in ("nue", "nuebar", "numu", "numubar", "nutau", "nutaubar"):
            with self.subTest(particle=particle):
                config = run_config([job(flux={"type": "power_law", "particle": particle})])
                self.assertEqual(config["jobs"][0]["flux"]["particle"], particle)

    def test_unknown_flux_particle_raises(self) -> None:
        # Caught at config time rather than as a KeyError inside a translator
        # once tasks are already running.
        with self.assertRaises(ConfigError) as ctx:
            run_config([job(flux={"type": "power_law", "particle": "nu_mu"})])
        self.assertIn("nu_mu", str(ctx.exception))

    def test_histogram_flux_missing_file_raises(self) -> None:
        with self.assertRaises(ConfigError):
            run_config(
                [
                    job(
                        flux={
                            "type": "histogram",
                            "particle": "numu",
                            "histogram_file": "/nonexistent/flux.root",
                            "histogram_name": "numu_flux",
                        }
                    )
                ]
            )


if __name__ == "__main__":
    unittest.main()
