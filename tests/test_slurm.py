from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.cli import build_parser
from neutrino_factory.config import load_config
from neutrino_factory.local import run_task_from_manifest
from neutrino_factory.slurm import build_task_manifest, render_sbatch_script


class SlurmPlanningTests(unittest.TestCase):
    def test_manifest_and_sbatch_rendering(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                manifest = build_task_manifest(config)
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

            instance_count = len(config["enabled_generator_instances"])
            expected_tasks = instance_count * int(config["splitting"]["chunks"])
            self.assertEqual(manifest["task_count"], expected_tasks)
            self.assertIn(f"#SBATCH --array=0-{expected_tasks - 1}", script)
        self.assertIn("jobs/run_task.sh", script)
        # The repo root is embedded absolutely (Slurm executes a spool copy, so
        # the script cannot locate the repo via BASH_SOURCE) and .env is sourced.
        self.assertIn(str(repo_root), script)
        self.assertIn('source "' + str(repo_root / ".env") + '"', script)

    def test_apptainer_runtime_dispatches_tasks_into_sifs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_CONTAINER_RUNTIME": "apptainer",
                "NF_IMAGE_ROOT": f"{tmpdir}/images",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                manifest = build_task_manifest(config)
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

            self.assertIn("TASK_SIFS=(", script)
            self.assertIn("apptainer exec", script)
            # One SIF entry per task, resolved under NF_IMAGE_ROOT.
            for task in manifest["tasks"]:
                self.assertIn(f"{tmpdir}/images/", script)
                self.assertIn(task["image"].replace(":", "_") + ".sif", script)
            # The apptainer pathway must not pin the render-time interpreter:
            # the task uses the generator image's own python3.
            self.assertNotIn("export PYTHON=", script)

    def test_docker_runtime_keeps_direct_invocation(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
                "NF_CONTAINER_RUNTIME": "docker",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(config_path)
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

        self.assertNotIn("TASK_SIFS", script)
        self.assertNotIn("apptainer", script)
        self.assertIn("export PYTHON=", script)


class SubmitCliTests(unittest.TestCase):
    def test_submit_uses_executor_from_config_when_flag_is_omitted(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "gibuu_numu_C_slurm.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
            }
            args = build_parser().parse_args(["submit", "--config", str(config_path), "--dry-run"])

            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.cli.write_manifest", return_value=f"{tmpdir}/manifest.json") as write_manifest:
                    with patch("neutrino_factory.cli.write_sbatch_script", return_value=f"{tmpdir}/job.sbatch") as write_sbatch_script:
                        with patch("pathlib.Path.read_text", return_value="#SBATCH --partition=alma\n"):
                            exit_code = args.func(args)

        self.assertEqual(exit_code, 0)
        submitted_config = write_manifest.call_args.args[0]
        self.assertEqual(submitted_config["run"]["executor"], "slurm")
        write_sbatch_script.assert_called_once()

    def test_submit_uses_local_executor_from_config_when_flag_is_omitted(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                "NF_WORK_ROOT": f"{tmpdir}/work",
            }
            args = build_parser().parse_args(["submit", "--config", str(config_path), "--dry-run"])

            with patch.dict(os.environ, env, clear=False):
                with patch("neutrino_factory.cli.write_manifest", return_value=f"{tmpdir}/manifest.json") as write_manifest:
                    with patch("neutrino_factory.cli.write_sbatch_script") as write_sbatch_script:
                        exit_code = args.func(args)

        self.assertEqual(exit_code, 0)
        submitted_config = write_manifest.call_args.args[0]
        self.assertEqual(submitted_config["run"]["executor"], "local")
        write_sbatch_script.assert_not_called()


class RunTaskCliTests(unittest.TestCase):
    def test_run_task_uses_manifest_executor_when_flag_is_omitted(self) -> None:
        config = {
            "run": {"executor": "slurm"},
            "storage": {
                "work_root": "/tmp/work",
                "output_root": "/tmp/output",
            },
        }
        manifest = {
            "executor": "local",
            "tasks": [
                {
                    "task_index": 0,
                    "generator_name": "nuwro",
                    "generator_version_token": "nuwro_25.11_default",
                    "chunk_id": 0,
                    "run_name": "test_run",
                }
            ],
        }

        with patch("neutrino_factory.local.run_task", return_value="/tmp/output.h5") as run_task:
            result = run_task_from_manifest(config, manifest, task_index=0, execution_mode=None)

        self.assertEqual(result, "/tmp/output.h5")
        self.assertEqual(run_task.call_args.kwargs["execution_mode"], "local")


if __name__ == "__main__":
    unittest.main()
