from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.config import resolve_config
from neutrino_factory.generators.genie import GenieAdapter


class GenieAdapterTests(unittest.TestCase):
    def _base_config(self, software_root: str) -> dict:
        return resolve_config(
            {
                "storage": {
                    "software_root": software_root,
                },
            }
        )

    def _translated_config(self, tune: str, **overrides) -> dict:
        base = {
            "energy_range_gev": [0.5, 10.0],
            "events": 10,
            "probe": "numu",
            "probe_pdg": 14,
            "target": "Ar40",
            "seed": 42,
            "config_version": tune,
            "code_version": "R-3_06_00",
            "genie_flux": {"kind": "function", "expr": "x^(-2.0)"},
            "event_generator_list": None,
        }
        base.update(overrides)
        return base

    def test_build_command_uses_pdg_probe_and_powerlaw_flux(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            command = adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))

        self.assertEqual(command[command.index("-p") + 1], "14")
        self.assertEqual(command[command.index("-f") + 1], "x^(-2.0)")
        self.assertNotIn("--event-generator-list", command)

    def test_build_command_adds_event_generator_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            command = adapter.build_run_command(
                self._translated_config("G18_10a_02_11a", event_generator_list="CCQE"),
                Path(tmpdir),
            )

        self.assertIn("--event-generator-list", command)
        self.assertEqual(command[command.index("--event-generator-list") + 1], "CCQE")

    def test_build_command_histogram_flux_uses_file_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            flux_file = Path(tmpdir) / "flux.root"
            flux_file.write_text("dummy", encoding="utf-8")
            adapter = GenieAdapter(self._base_config(tmpdir))
            with patch.object(GenieAdapter, "container_available", return_value=False):
                command = adapter.build_run_command(
                    self._translated_config(
                        "G18_10a_02_11a",
                        genie_flux={"kind": "histogram", "file": str(flux_file), "name": "numu_flux"},
                    ),
                    Path(tmpdir),
                )

        self.assertEqual(command[command.index("-f") + 1], f"{flux_file},numu_flux")

    def test_build_command_adds_cross_sections_for_exact_tune_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_file = (
                Path(tmpdir)
                / "genie"
                / "genie_xsec"
                / "R-3_06_00"
                / "G18_10a_02_11a"
                / "xsecs.xml"
            )
            xml_file.parent.mkdir(parents=True, exist_ok=True)
            xml_file.write_text("dummy", encoding="utf-8")

            adapter = GenieAdapter(self._base_config(tmpdir))
            with patch.object(GenieAdapter, "container_available", return_value=False):
                command = adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))

        self.assertIn("--cross-sections", command)
        self.assertEqual(command[command.index("--cross-sections") + 1], str(xml_file))

    def test_build_command_matches_compact_tune_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_file = (
                Path(tmpdir)
                / "genie"
                / "genie_xsec"
                / "R-3_06_00"
                / "G1810a0211a"
                / "xsecs.xml"
            )
            xml_file.parent.mkdir(parents=True, exist_ok=True)
            xml_file.write_text("dummy", encoding="utf-8")

            adapter = GenieAdapter(self._base_config(tmpdir))
            with patch.object(GenieAdapter, "container_available", return_value=False):
                command = adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))

        self.assertIn("--cross-sections", command)
        self.assertEqual(command[command.index("--cross-sections") + 1], str(xml_file))

    def test_native_binary_wins_over_available_container(self) -> None:
        # Cluster-critical branch: inside the generator's Apptainer image the
        # binary is on $PATH and must be run directly (no container wrapping).
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            with patch("neutrino_factory.generators.genie.shutil.which", return_value="/opt/genie/bin/gevgen"):
                with patch.object(GenieAdapter, "container_available", return_value=True):
                    command = adapter.build_run_command(
                        self._translated_config("G18_10a_02_11a"), Path(tmpdir)
                    )

        self.assertEqual(command[0], "gevgen")
        self.assertNotIn("docker", command)
        self.assertNotIn("apptainer", command)

    def test_docker_runtime_wraps_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            env = {"NF_CONTAINER_RUNTIME": "docker"}
            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.generators.genie.shutil.which", return_value=None):
                    with patch.object(GenieAdapter, "container_available", return_value=True):
                        command = adapter.build_run_command(
                            self._translated_config("G18_10a_02_11a"), Path(tmpdir)
                        )

        self.assertEqual(command[:3], ["docker", "run", "--platform"])
        self.assertIn("genie:R-3_06_00", command)

    def test_apptainer_runtime_refuses_to_wrap(self) -> None:
        # Apptainer cannot nest: reaching the container branch without a native
        # binary means the task was launched outside its image — fail clearly.
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            env = {"NF_CONTAINER_RUNTIME": "apptainer", "NF_IMAGE_ROOT": tmpdir}
            (Path(tmpdir) / "genie_R-3_06_00.sif").write_text("", encoding="utf-8")
            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.generators.genie.shutil.which", return_value=None):
                    with self.assertRaises(RuntimeError):
                        adapter.build_run_command(
                            self._translated_config("G18_10a_02_11a"), Path(tmpdir)
                        )

    def test_build_command_warns_when_tune_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            with self.assertLogs("neutrino_factory.generators.genie", level="WARNING") as logs:
                command = adapter.build_run_command(
                    self._translated_config("AR23_20i_00_000"),
                    Path(tmpdir),
                )

        self.assertNotIn("--cross-sections", command)
        self.assertTrue(any("precomputed cross sections not found" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()