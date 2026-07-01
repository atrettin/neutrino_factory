from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.config import ConfigError, resolve_config


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
        with self.assertRaises(ConfigError):
            resolve_config(
                {
                    "generators": {
                        "genie": {
                            "versions": [
                                {
                                    "enabled": True,
                                    "code_version": "R-3_06_00",
                                    "config_version": "not_a_real_tune",
                                }
                            ]
                        }
                    }
                }
            )

    def test_invalid_config_raises(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config({"run": {"events": 0}, "generators": {}})

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
