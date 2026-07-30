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


def _write_root_histogram(path: Path, name: str, edges, contents) -> None:
    import uproot

    with uproot.recreate(path) as handle:
        handle[name] = (np.asarray(contents, dtype=np.float64), np.asarray(edges, dtype=np.float64))


class ConfigTests(unittest.TestCase):
    def test_env_expansion_and_enabled_generators(self) -> None:
        with patch.dict(os.environ, {"NF_OUTPUT_ROOT": "/tmp/nf-output"}, clear=False):
            config = resolve_config(
                {
                    "generators": {
                        "genie": {
                            "versions": [
                                {
                                    "enabled": True,
                                    "code_version": "R-3_06_00",
                                    "config_version": "G18_10a_02_11a",
                                }
                            ]
                        },
                        "neut": {
                            "versions": [
                                {"enabled": False, "code_version": "5.x", "config_version": "default"}
                            ]
                        },
                    }
                }
            )

        self.assertEqual(config["storage"]["output_root"], "/tmp/nf-output")
        self.assertEqual(config["enabled_generators"], ["genie"])
        instance = config["enabled_generator_instances"][0]
        self.assertEqual(instance["code_version"], "R-3_06_00")
        self.assertEqual(instance["config_version"], "G18_10a_02_11a")
        self.assertEqual(instance["version_id"], "R-3_06_00+G18_10a_02_11a")
        self.assertEqual(instance["image"], "genie:R-3_06_00")

    def test_missing_code_or_config_version_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "generators": {
                        "genie": {
                            "versions": [
                                {
                                    "enabled": True,
                                    "code_version": "R-3_06_00",
                                }
                            ]
                        }
                    }
                }
            )

    def test_unknown_code_version_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "generators": {
                        "genie": {
                            "versions": [
                                {
                                    "enabled": True,
                                    "code_version": "R-9_99_99",
                                    "config_version": "G18_10a_02_11a",
                                }
                            ]
                        }
                    }
                }
            )

    def test_incompatible_config_version_raises(self) -> None:
        # GENIE validation is availability-only (tunes are not enumerated in
        # advance), so an incompatible config_version is checked against a
        # generator that declares a static config-version set: NuWro's "default".
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "generators": {
                        "nuwro": {
                            "versions": [
                                {
                                    "enabled": True,
                                    "code_version": "nuwro_25.11",
                                    "config_version": "not_a_real_config",
                                }
                            ]
                        }
                    }
                }
            )

    def _genie_config(self, software_root: str, stub_mode: bool, tune: str) -> dict:
        return {
            "run": {"stub_mode": stub_mode},
            "storage": {"software_root": software_root},
            "generators": {
                "genie": {
                    "versions": [
                        {
                            "enabled": True,
                            "code_version": "R-3_06_00",
                            "config_version": tune,
                        }
                    ]
                }
            },
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

    def test_invalid_config_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config({"run": {"events": 0}, "generators": {}})

    def test_log_level_defaults_to_generator_stock_logging(self) -> None:
        config = resolve_config({"generators": self._genie_generators()})
        self.assertEqual(config["run"]["log_level"], "default")

    def test_known_log_levels_are_accepted(self) -> None:
        for log_level in LOG_LEVELS:
            with self.subTest(log_level=log_level):
                config = resolve_config(
                    {
                        "run": {"log_level": log_level},
                        "generators": self._genie_generators(),
                    }
                )
                self.assertEqual(config["run"]["log_level"], log_level)

    def test_unknown_log_level_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(
                {"run": {"log_level": "bogus"}, "generators": self._genie_generators()}
            )

    def test_current_defaults_to_cc(self) -> None:
        config = resolve_config({"generators": self._genie_generators()})
        self.assertEqual(config["physics"]["current"], "cc")

    def test_known_currents_are_accepted(self) -> None:
        for current in PHYSICS_CURRENTS:
            with self.subTest(current=current):
                config = resolve_config(
                    {
                        "physics": {"current": current},
                        "generators": self._genie_generators(),
                    }
                )
                self.assertEqual(config["physics"]["current"], current)

    def test_unknown_current_raises(self) -> None:
        # Silently falling back to "cc" would generate different physics than
        # the config asks for.
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "physics": {"current": "ccqe"},
                    "generators": self._genie_generators(),
                }
            )

    def _genie_generators(self) -> dict:
        return {
            "genie": {
                "versions": [
                    {"enabled": True, "code_version": "R-3_06_00", "config_version": "G18_10a_02_11a"}
                ]
            }
        }

    def test_histogram_flux_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            flux_path = Path(tmpdir) / "flux.root"
            _write_root_histogram(flux_path, "numu_flux", [0.5, 1.0, 2.0, 4.0], [10.0, 4.0, 1.0])
            config = resolve_config(
                {
                    "flux": {
                        "type": "histogram",
                        "particle": "numu",
                        "histogram_file": str(flux_path),
                        "histogram_name": "numu_flux",
                    },
                    "generators": self._genie_generators(),
                }
            )
        self.assertEqual(config["flux"]["type"], "histogram")

    def test_every_supported_flavour_validates(self) -> None:
        for particle in ("nue", "nuebar", "numu", "numubar", "nutau", "nutaubar"):
            with self.subTest(particle=particle):
                config = resolve_config(
                    {
                        "flux": {"type": "power_law", "particle": particle},
                        "generators": self._genie_generators(),
                    }
                )
                self.assertEqual(config["flux"]["particle"], particle)

    def test_unknown_flux_particle_raises(self) -> None:
        # Caught at config time rather than as a KeyError inside a translator
        # once tasks are already running.
        with self.assertRaises(ConfigError) as ctx:
            resolve_config(
                {
                    "flux": {"type": "power_law", "particle": "nu_mu"},
                    "generators": self._genie_generators(),
                }
            )
        self.assertIn("nu_mu", str(ctx.exception))

    def test_histogram_flux_missing_file_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "flux": {
                        "type": "histogram",
                        "particle": "numu",
                        "histogram_file": "/nonexistent/flux.root",
                        "histogram_name": "numu_flux",
                    },
                    "generators": self._genie_generators(),
                }
            )


if __name__ == "__main__":
    unittest.main()
