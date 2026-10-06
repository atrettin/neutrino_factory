from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutrino_factory.cli import build_parser
from neutrino_factory.config import load_config
from neutrino_factory.local import run_task_from_manifest
from neutrino_factory.slurm import (
    build_task_manifest,
    chunk_ranges,
    format_slurm_array_spec,
    identify_missing_chunks,
    render_sbatch_script,
)


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

            # Jobs are heterogeneous: the task count is the sum over jobs of
            # each job's own chunk count, never a product.
            expected_tasks = sum(
                len(chunk_ranges(int(job["events"]), int(job["chunks"])))
                for job in config["jobs"]
            )
            self.assertEqual(len(manifest["tasks"]), expected_tasks)
            self.assertEqual(manifest["manifest_version"], 4)
            self.assertEqual(len(manifest["jobs"]), len(config["jobs"]))
            for task in manifest["tasks"]:
                self.assertIn("job_index", task)
                self.assertEqual(
                    task["job_label"], config["jobs"][task["job_index"]]["label"]
                )
            seeds = [task["seed"] for task in manifest["tasks"]]
            self.assertEqual(len(set(seeds)), len(seeds))
            self.assertIn(f"#SBATCH --array=0-{expected_tasks - 1}", script)
        self.assertIn("jobs/run_task.sh", script)
        # The repo root is embedded absolutely (Slurm executes a spool copy, so
        # the script cannot locate the repo via BASH_SOURCE) and .env is sourced.
        self.assertIn(str(repo_root), script)
        self.assertIn('source "' + str(repo_root / ".env") + '"', script)

    def test_apptainer_runtime_uses_unified_nf_base_image(self) -> None:
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
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

            self.assertIn("NF_BASE_SIF=", script)
            self.assertIn("nf-base.sif", script)
            self.assertIn("apptainer exec", script)
            self.assertNotIn("TASK_SIFS", script)
            # The apptainer pathway must not pin the render-time interpreter:
            # the task uses the unified image's own python3.
            self.assertNotIn("export PYTHON=", script)

    def test_rendered_script_is_valid_shell_for_every_runtime(self) -> None:
        """Parse the rendered sbatch with `bash -n`, per runtime.

        The substring assertions above all passed while the apptainer branch
        emitted an unterminated quote — its launcher ends in `"${SLURM_ARRAY_TASK_ID}"`
        and the final quote had merged into the f-string's `\"\"\"` terminator. Slurm
        only reported `unexpected EOF while looking for matching '"'` at run time,
        after submission. Syntax-check the whole script instead of grepping it.
        """
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "configs" / "examples" / "power_law_numu_Ar.yaml"

        for runtime in ("apptainer", "docker", "none"):
            with self.subTest(runtime=runtime):
                with tempfile.TemporaryDirectory() as tmpdir:
                    env = {
                        "NF_OUTPUT_ROOT": f"{tmpdir}/output",
                        "NF_WORK_ROOT": f"{tmpdir}/work",
                        "NF_CONTAINER_RUNTIME": runtime,
                        "NF_IMAGE_ROOT": f"{tmpdir}/images",
                    }
                    with patch.dict(os.environ, env, clear=False):
                        config = load_config(config_path)
                        script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

                    script_path = Path(tmpdir) / "job.sbatch"
                    script_path.write_text(script, encoding="utf-8")
                    result = subprocess.run(
                        ["bash", "-n", str(script_path)],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        f"rendered sbatch is not valid shell for runtime "
                        f"{runtime!r}: {result.stderr}\n{script}",
                    )

    def test_apptainer_launcher_line_is_fully_quoted(self) -> None:
        # Guards the specific regression: the task index argument must be a
        # closed "${SLURM_ARRAY_TASK_ID}", not a dangling open quote.
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
                script = render_sbatch_script(config, f"{tmpdir}/manifest.json")

        launcher = next(
            line for line in script.splitlines() if line.startswith("apptainer exec")
        )
        self.assertTrue(launcher.endswith('"${SLURM_ARRAY_TASK_ID}"'), launcher)
        self.assertEqual(launcher.count('"') % 2, 0, launcher)

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


class RetryTests(unittest.TestCase):
    def test_format_slurm_array_spec_with_consecutive_ranges(self) -> None:
        """Test that consecutive indices are grouped into ranges."""
        self.assertEqual(format_slurm_array_spec([0, 1, 2, 5, 7, 8, 9]), "0-2,5,7-9")

    def test_format_slurm_array_spec_with_single_index(self) -> None:
        """Test a single index."""
        self.assertEqual(format_slurm_array_spec([5]), "5")

    def test_format_slurm_array_spec_with_sparse_indices(self) -> None:
        """Test non-consecutive indices."""
        self.assertEqual(format_slurm_array_spec([0, 2, 4, 6]), "0,2,4,6")

    def test_format_slurm_array_spec_with_empty_list(self) -> None:
        """Test empty list returns empty string."""
        self.assertEqual(format_slurm_array_spec([]), "")

    def test_format_slurm_array_spec_with_unsorted_input(self) -> None:
        """Test that unsorted input is handled correctly."""
        self.assertEqual(format_slurm_array_spec([9, 0, 5, 1, 2]), "0-2,5,9")

    def test_format_slurm_array_spec_with_full_range(self) -> None:
        """Test a complete consecutive range."""
        self.assertEqual(format_slurm_array_spec([0, 1, 2, 3, 4]), "0-4")

    def test_identify_missing_chunks_with_all_present(self) -> None:
        """Test when all chunk files exist."""
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

                # Create all expected chunk files
                for task in manifest["tasks"]:
                    from neutrino_factory.layout import chunk_output_path
                    chunk_path = chunk_output_path(config, task)
                    chunk_path.parent.mkdir(parents=True, exist_ok=True)
                    chunk_path.write_text("dummy", encoding="utf-8")

                missing = identify_missing_chunks(config, manifest)
                self.assertEqual(missing, [])

    def test_identify_missing_chunks_with_some_missing(self) -> None:
        """Test when some chunk files are missing."""
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

                # Create only even-indexed chunk files
                for task in manifest["tasks"]:
                    if task["task_index"] % 2 == 0:
                        from neutrino_factory.layout import chunk_output_path
                        chunk_path = chunk_output_path(config, task)
                        chunk_path.parent.mkdir(parents=True, exist_ok=True)
                        chunk_path.write_text("dummy", encoding="utf-8")

                missing = identify_missing_chunks(config, manifest)
                expected_missing = [t["task_index"] for t in manifest["tasks"] if t["task_index"] % 2 == 1]
                self.assertEqual(missing, expected_missing)

    def test_identify_missing_chunks_with_all_missing(self) -> None:
        """Test when all chunk files are missing."""
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

                missing = identify_missing_chunks(config, manifest)
                expected_missing = [t["task_index"] for t in manifest["tasks"]]
                self.assertEqual(missing, expected_missing)


if __name__ == "__main__":
    unittest.main()
