"""Adapter native-branch behavior under the apptainer runtime.

On the cluster the Slurm task already runs inside the composed nf-base.sif, so
the adapter's native-first branch fires. There it must emit the version-explicit
``nf-run <gen> <cv> <binary> ...`` form so the right payload is selected when
several versions are composed side by side. Under docker/local the command must
stay the bare binary (verified in the per-adapter tests too).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.config import resolve_config
from neutrino_factory.generators.genie import GenieAdapter
from neutrino_factory.generators.gibuu import GiBUUAdapter
from neutrino_factory.generators.neut import NeutAdapter
from neutrino_factory.generators.nuwro import NuWroAdapter
from neutrino_factory.translators.neut import NeutTranslator


class ApptainerDispatchAdapterTests(unittest.TestCase):
    def test_genie_native_branch_dispatches_with_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GenieAdapter(resolve_config({"storage": {"software_root": tmpdir}}))
            translated = {
                "energy_range_gev": [0.5, 10.0],
                "events": 10,
                "probe_pdg": 14,
                "target": "Ar40",
                "seed": 42,
                "config_version": "G18_10a_02_11a",
                "code_version": "R-3_06_00",
                "genie_flux": {"kind": "function", "expr": "x^(-2.0)"},
                "event_generator_list": None,
            }
            env = {"NF_CONTAINER_RUNTIME": "apptainer"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.genie.shutil.which",
                    return_value="/usr/local/bin/gevgen",
                ):
                    command = adapter.build_run_command(translated, Path(tmpdir))

        self.assertEqual(command[:4], ["nf-run", "genie", "R-3_06_00", "gevgen"])

    def test_nuwro_native_branch_dispatches_with_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = NuWroAdapter({})
            translated = {
                "code_version": "nuwro_25.11",
                "nuwro_params": {"number_of_events": 10},
            }
            env = {"NF_CONTAINER_RUNTIME": "apptainer"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.nuwro.shutil.which",
                    return_value="/usr/local/bin/nuwro",
                ):
                    command = adapter.build_run_command(translated, Path(tmpdir))

        self.assertEqual(
            command,
            ["nf-run", "nuwro", "nuwro_25.11", "nuwro", "-o", "events.root", "-i", "params.txt"],
        )

    def _neut_translated(self) -> tuple[dict, dict]:
        config = resolve_config(
            {
                "flux": {
                    "type": "power_law",
                    "particle": "numu",
                    "emin_gev": 0.5,
                    "emax_gev": 5.0,
                    "gamma": -2.0,
                },
                "target": {"nucleus": "C12"},
            }
        )
        translated = NeutTranslator().translate(
            config,
            {
                "event_count": 10,
                "seed": 1,
                "code_version": "5.7.0-nuint2024",
                "config_version": "default",
                "generator_version_id": "5.7.0-nuint2024+default",
            },
        )
        return config, translated

    def test_neut_native_branch_dispatches_with_version(self) -> None:
        config, translated = self._neut_translated()
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = NeutAdapter(config)
            env = {"NF_CONTAINER_RUNTIME": "apptainer"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.neut.shutil.which",
                    return_value="/usr/local/bin/neutroot2",
                ):
                    command = adapter.build_run_command(translated, Path(tmpdir))

        self.assertEqual(
            command,
            [
                "nf-run", "neut", "5.7.0-nuint2024",
                "neutroot2", "neut.card", "events.neut.root",
            ],
        )

    def test_neut_flatten_stage_dispatches_with_version(self) -> None:
        # NEUT's second stage is a second payload binary, so it needs the same
        # version-explicit dispatch as the generation step.
        config, _ = self._neut_translated()
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = NeutAdapter(config)
            env = {"NF_CONTAINER_RUNTIME": "apptainer"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.neut.shutil.which",
                    return_value="/usr/local/bin/nf-neut-flatten",
                ):
                    with patch("neutrino_factory.generators.neut.subprocess.run") as run:
                        adapter._run_flatten(Path(tmpdir), "5.7.0-nuint2024")

        self.assertEqual(
            run.call_args[0][0],
            [
                "nf-run", "neut", "5.7.0-nuint2024",
                "nf-neut-flatten", "events.neut.root", "events.flat.root",
            ],
        )

    def test_gibuu_native_branch_dispatches_inside_shell_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GiBUUAdapter({})
            translated = {
                "code_version": "release2025",
                "gibuu_jobcard": "&input\n/\n",
            }
            env = {"NF_CONTAINER_RUNTIME": "apptainer"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.gibuu.shutil.which",
                    return_value="/usr/local/bin/GiBUU.x",
                ):
                    command = adapter.build_run_command(translated, Path(tmpdir))

        self.assertEqual(
            command, ["bash", "-c", "nf-run gibuu release2025 GiBUU.x < job.job"]
        )

    def test_gibuu_jobcard_buuinput_path_is_runtime_and_version_aware(self) -> None:
        jobcard = "&input\n    path_to_input   = '@NF_GIBUU_INPUT@'\n/\n"
        translated = {"code_version": "release2025", "gibuu_jobcard": jobcard}

        # Apptainer: version-namespaced payload tree.
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GiBUUAdapter({})
            with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "apptainer"}, clear=False):
                with patch(
                    "neutrino_factory.generators.gibuu.shutil.which",
                    return_value="/usr/local/bin/GiBUU.x",
                ):
                    adapter.build_run_command(dict(translated), Path(tmpdir))
            written = (Path(tmpdir) / "job.job").read_text()
        self.assertIn(
            "path_to_input   = '/opt/nf/generators/gibuu/release2025/GiBUU/buuinput'",
            written,
        )
        self.assertNotIn("@NF_GIBUU_INPUT@", written)

        # Docker: flat image layout.
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = GiBUUAdapter({})
            with patch.dict(os.environ, {"NF_CONTAINER_RUNTIME": "docker"}, clear=False):
                with patch(
                    "neutrino_factory.generators.gibuu.shutil.which",
                    return_value="/usr/local/bin/GiBUU.x",
                ):
                    adapter.build_run_command(dict(translated), Path(tmpdir))
            written = (Path(tmpdir) / "job.job").read_text()
        self.assertIn("path_to_input   = '/opt/GiBUU/buuinput'", written)

    def test_docker_runtime_keeps_bare_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            gibuu = GiBUUAdapter({})
            env = {"NF_CONTAINER_RUNTIME": "docker"}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "neutrino_factory.generators.gibuu.shutil.which",
                    return_value="/usr/local/bin/GiBUU.x",
                ):
                    command = gibuu.build_run_command(
                        {"code_version": "release2025", "gibuu_jobcard": "&input\n/\n"},
                        Path(tmpdir),
                    )

        self.assertEqual(command, ["bash", "-c", "GiBUU.x < job.job"])


if __name__ == "__main__":
    unittest.main()
