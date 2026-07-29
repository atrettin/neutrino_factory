from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neutrino_factory.config import resolve_config
from neutrino_factory.generators.genie import ESSENTIAL_OVERLAY_FILENAME, GenieAdapter
from neutrino_factory.translators.genie import GENIE_FLUX_FILE, GENIE_FLUX_HIST


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
            "genie_flux": {
                "kind": "generated_histogram",
                "file": GENIE_FLUX_FILE,
                "name": GENIE_FLUX_HIST,
                "nbins": 64,
                "spacing": "log",
            },
            "flux_config": {
                "type": "power_law",
                "particle": "numu",
                "emin_gev": 0.5,
                "emax_gev": 10.0,
                "gamma": -2.0,
            },
            "event_generator_list": None,
        }
        base.update(overrides)
        return base

    def test_build_command_uses_pdg_probe_and_powerlaw_flux(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            command = adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))

        self.assertEqual(command[command.index("-p") + 1], "14")
        # A power-law flux is handed to gevgen as a ROOT histogram, never as a TF1
        # string: gevgen resamples a TF1 into a coarse, noisy 300-bin histogram.
        self.assertEqual(
            command[command.index("-f") + 1],
            f"{GENIE_FLUX_FILE},{GENIE_FLUX_HIST},WIDTH",
        )
        self.assertNotIn("--event-generator-list", command)

    def test_build_command_writes_log_binned_flux_histogram(self) -> None:
        import uproot

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))
            with uproot.open(Path(tmpdir) / GENIE_FLUX_FILE) as handle:
                contents, edges = handle[GENIE_FLUX_HIST].to_numpy()

        self.assertEqual(len(contents), 64)
        # Endpoints must land exactly on the configured range: gevgen zeroes any
        # bin that is not strictly inside the -e bounds.
        self.assertEqual(edges[0], 0.5)
        self.assertEqual(edges[-1], 10.0)
        # Log spacing -> constant ratio between successive edges.
        ratios = edges[1:] / edges[:-1]
        self.assertTrue(np.allclose(ratios, ratios[0]))
        # Contents are the flux *density* at the bin centers (gevgen's WIDTH flag
        # multiplies by the bin width to get the sampling probability).
        centers = 0.5 * (edges[:-1] + edges[1:])
        self.assertTrue(np.allclose(contents, centers ** -2.0))

    def test_build_command_pads_energy_range_outward(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            command = adapter.build_run_command(self._translated_config("G18_10a_02_11a"), Path(tmpdir))

        lo, hi = (float(v) for v in command[command.index("-e") + 1].split(","))
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 10.0)
        self.assertAlmostEqual(lo, 0.5, places=9)
        self.assertAlmostEqual(hi, 10.0, places=9)

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

    def test_default_log_level_leaves_genie_thresholds_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            command = adapter.build_run_command(
                self._translated_config("G18_10a_02_11a"), Path(tmpdir)
            )
            self.assertNotIn("--message-thresholds", command)
            self.assertFalse((Path(tmpdir) / ESSENTIAL_OVERLAY_FILENAME).exists())

    def test_log_levels_map_to_messenger_presets(self) -> None:
        expected = {
            "quiet": "Messenger_laconic.xml",
            "verbose": "Messenger_rambling.xml",
            "essential": f"Messenger_laconic.xml:{ESSENTIAL_OVERLAY_FILENAME}",
        }
        for log_level, threshold_arg in expected.items():
            with self.subTest(log_level=log_level):
                with tempfile.TemporaryDirectory() as tmpdir:
                    adapter = GenieAdapter(self._base_config(tmpdir))
                    command = adapter.build_run_command(
                        self._translated_config("G18_10a_02_11a", log_level=log_level),
                        Path(tmpdir),
                    )
                self.assertEqual(
                    command[command.index("--message-thresholds") + 1], threshold_arg
                )

    def test_essential_level_writes_overlay_raising_the_ntp_stream(self) -> None:
        # laconic silences every stream down to WARN, which would also drop the
        # "opening/saving the output ROOT file" lines the user asked to keep.
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            adapter.build_run_command(
                self._translated_config("G18_10a_02_11a", log_level="essential"),
                Path(tmpdir),
            )
            overlay = (Path(tmpdir) / ESSENTIAL_OVERLAY_FILENAME).read_text(encoding="utf-8")

        self.assertIn('msgstream="Ntp"', overlay)
        self.assertIn("INFO", overlay)

    def test_thresholds_survive_docker_wrapping_unremapped(self) -> None:
        # The value is bare basenames resolved from the CWD (/work inside the
        # container), so unlike the xsec XML and flux file it must NOT be
        # rewritten to an in-container path.
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(self._base_config(tmpdir))
            env = {"NF_CONTAINER_RUNTIME": "docker"}
            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.generators.genie.shutil.which", return_value=None):
                    with patch.object(GenieAdapter, "container_available", return_value=True):
                        command = adapter.build_run_command(
                            self._translated_config("G18_10a_02_11a", log_level="essential"),
                            Path(tmpdir),
                        )

        self.assertEqual(
            command[command.index("--message-thresholds") + 1],
            f"Messenger_laconic.xml:{ESSENTIAL_OVERLAY_FILENAME}",
        )

    def test_gntpc_reuses_the_log_level_from_the_sidecar(self) -> None:
        # normalize_output only gets the manifest task, which carries no log
        # level; build_run_command's sidecar is the channel between them.
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            adapter = GenieAdapter(self._base_config(tmpdir))
            adapter.build_run_command(
                self._translated_config("G18_10a_02_11a", log_level="quiet"), work_dir
            )
            with patch("neutrino_factory.generators.genie.shutil.which", return_value="/opt/genie/bin/gntpc"):
                with patch("neutrino_factory.generators.genie.subprocess.run") as run:
                    adapter._run_gntpc(work_dir, "R-3_06_00")

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--message-thresholds") + 1], "Messenger_laconic.xml"
        )

    def test_gntpc_falls_back_to_default_without_a_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            adapter = GenieAdapter(self._base_config(tmpdir))
            with patch("neutrino_factory.generators.genie.shutil.which", return_value="/opt/genie/bin/gntpc"):
                with patch("neutrino_factory.generators.genie.subprocess.run") as run:
                    adapter._run_gntpc(work_dir, "R-3_06_00")

        self.assertNotIn("--message-thresholds", run.call_args.args[0])

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